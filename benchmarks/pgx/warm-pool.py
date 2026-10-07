#!/usr/bin/env python3
"""Warm-pool behaviour of ephemeral databases.

For each pool size: wait until the janitor has that many spares ready, then
fire bursts of simultaneous mint-on-connect requests (default 1, 10, 50, 100)
and record per-request latency, the burst's drain time (first request sent to
last SELECT 1 answered), the true pool hit rate (a request was served by a
spare if its database's OID existed before the burst started), how many
requests were slower than --miss-ms, and how long the janitor takes to refill
the pool.

Output: <out>/warm-pool.json.
"""

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pgxbench as pb  # noqa: E402
import ephemeral as eph  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--conf", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--guc", action="append", default=[])
    ap.add_argument("--pool-sizes", default="0,1,4,8,32")
    ap.add_argument("--bursts", default="1,10,50,100")
    ap.add_argument("--miss-ms", type=float, default=30.0)
    ap.add_argument("--refill-timeout", type=float, default=120.0)
    ap.add_argument("--port", type=int, default=54420)
    ap.add_argument("--max-connections", type=int, default=0, help="default: largest burst + 30")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "warm-pool.json")
    doc = {"benchmark": "warm-pool", "git_commit": pb.git("rev-parse", "HEAD"), "conf": args.conf,
           "miss_threshold_ms": args.miss_ms, "pools": []}

    for pool in [int(x) for x in args.pool_sizes.split(",")]:
        gucs = ["pgrust.ephemeral_db_prefix=" + eph.PREFIX, "pgrust.ephemeral_db_mint_roles=postgres",
                "pgrust.ephemeral_db_grace=86400", "pgrust.ephemeral_db_pool_size=%d" % pool,
                "max_connections=%d" % (args.max_connections or max(130, max(int(x) for x in args.bursts.split(",")) + 30))] + args.guc
        server_args = []
        for g in gucs:
            server_args += ["-c", g]
        ws = pb.Workspace(pb.Engine("pgrust", args.binary, server_args, args.conf))
        srv = pb.Server(ws, args.port).launch()
        conn = lambda db: pb.PgConn(ws.sockdir, args.port, database=db, timeout=300)  # noqa: E731
        try:
            srv.wait_select1()
            admin = conn("postgres")
            admin.query("CREATE DATABASE %s" % eph.TEMPLATE)
            c = conn(eph.TEMPLATE)
            c.query(eph.template_sql(50, 200))
            c.query("VACUUM ANALYZE")
            c.close()
            admin.query("SELECT pgrust_seal_template('%s')" % eph.TEMPLATE)
            conn("%s%s__first" % (eph.PREFIX, eph.TEMPLATE)).close()  # registers the template for pooling

            def spares():
                return int(admin.query("SELECT count(*) FROM pg_database WHERE datname LIKE 'tdb\\_spare\\_%'")[0][0])

            def wait_full():
                t0 = time.perf_counter()
                while time.perf_counter() - t0 < args.refill_timeout:
                    if spares() >= pool:
                        return time.perf_counter() - t0
                    time.sleep(0.05)
                return None

            first_fill = wait_full()
            entry = {"pool_size": pool, "initial_fill_s": first_fill, "bursts": []}
            seq = 0
            for burst in [int(x) for x in args.bursts.split(",")]:
                ready = spares()
                max_oid = int(admin.query("SELECT max(oid::int8) FROM pg_database")[0][0])
                hit = [None] * burst
                lat = [None] * burst
                errors = []
                gate = threading.Event()

                def one(i, base):
                    gate.wait()
                    t0 = time.perf_counter()
                    try:
                        c = conn("%s%s__b%d" % (eph.PREFIX, eph.TEMPLATE, base + i))
                        c.query("SELECT 1")
                        lat[i] = time.perf_counter() - t0
                        hit[i] = int(c.query("SELECT oid::int8 FROM pg_database WHERE datname = current_database()")[0][0]) <= max_oid
                        c.close()
                    except Exception as e:  # noqa: BLE001 - recorded
                        errors.append(str(e)[:160])
                threads = [threading.Thread(target=one, args=(i, seq)) for i in range(burst)]
                seq += burst
                for t in threads:
                    t.start()
                t0 = time.perf_counter()
                gate.set()
                for t in threads:
                    t.join()
                wall = time.perf_counter() - t0
                refill = wait_full()
                ok = [x for x in lat if x is not None]
                row = {"burst": burst, "spares_ready_before": ready, "errors": len(errors), "error_examples": errors[:2],
                       "wall_ms": round(wall * 1e3, 1),
                       "latency_ms": {k: round(v * 1e3, 1) for k, v in pb.summarize(ok).items()} if ok else None,
                       "pool_hits": sum(1 for h in hit if h), "pool_misses_true": sum(1 for h in hit if h is False),
                       "pool_hit_rate": round(sum(1 for h in hit if h) / burst, 3),
                       "slower_than_miss_ms": sum(1 for x in ok if x * 1e3 > args.miss_ms),
                       "refill_s": None if refill is None else round(refill, 2),
                       "latencies_ms_sorted": sorted(round(x * 1e3, 1) for x in ok)}
                entry["bursts"].append(row)
                l = row["latency_ms"] or {}
                print("pool=%-3d burst=%-4d ready=%-3d p50=%s p95=%s p99=%s ms  drain=%s ms  hits=%d/%d  errors=%d  refill=%s s" % (
                    pool, burst, ready, l.get("p50"), l.get("p95"), l.get("p99"), row["wall_ms"], row["pool_hits"], burst, len(errors),
                    row["refill_s"]), flush=True)
            doc["pools"].append(entry)
            with open(path, "w") as f:
                json.dump(doc, f, indent=2)
                f.write("\n")
        finally:
            srv.stop()
            ws.cleanup()
    print("wrote", path)


if __name__ == "__main__":
    main()
