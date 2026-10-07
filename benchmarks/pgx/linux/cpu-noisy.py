#!/usr/bin/env python3
"""CPU noisy neighbour: how a quiet database's latency changes as other
databases in the same runtime saturate the host's cores.

One runtime, PGX profile. Database B (quiet) runs SELECT 1 + a small indexed
query in a loop the whole time. At each level N (default 0,1,2,4,6) there are
N noisy clients, each in its own database, running a CPU-bound query back to
back. Per level: B's latency p50/p95/p99/max, B's errors, a brand-new
connection's latency, host CPU utilisation (from /proc/stat), runnable load,
noisy query throughput.

Output: <out>/cpu-noisy.json.
"""

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pgxbench as pb  # noqa: E402
import ephemeral as eph  # noqa: E402

NOISY_SQL = "SELECT sum((g::bigint * g) % 7) FROM generate_series(1, 20000000) g"
B_QUERY = "SELECT count(*) FROM t1 WHERE account_id < 50"


def cpu_times():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + v[4]
    return sum(v), idle


def ms(values):
    if not values:
        return None
    return {k: (round(v * 1e3, 2) if k != "n" else v) for k, v in pb.summarize(values).items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--conf", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--guc", action="append", default=[])
    ap.add_argument("--levels", default="0,1,2,4,6")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--port", type=int, default=54480)
    ap.add_argument("--same-db", action="store_true",
                    help="all noisy clients in ONE database (one agent with many connections)")
    ap.add_argument("--conn-limit", type=int, default=-1,
                    help="ALTER DATABASE <noisy> CONNECTION LIMIT n (with --same-db); refused clients are counted")
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    gucs = ["max_connections=50", "statement_timeout=0"] + args.guc
    server_args = []
    for g in gucs:
        server_args += ["-c", g]
    ws = pb.Workspace(pb.Engine("pgrust", args.binary, server_args, args.conf))
    srv = pb.Server(ws, args.port).launch()
    connect = lambda db: pb.PgConn(ws.sockdir, args.port, database=db, timeout=600)  # noqa: E731
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "cpu-noisy%s.json" % (("-" + args.label) if args.label else ""))
    doc = {"benchmark": "cpu-noisy", "git_commit": pb.git("rev-parse", "HEAD"), "settings": gucs,
           "same_db": args.same_db, "conn_limit": args.conn_limit, "label": args.label,
           "cpus": os.cpu_count(), "noisy_sql": NOISY_SQL, "levels": []}
    levels = [int(x) for x in args.levels.split(",")]
    try:
        srv.wait_select1()
        admin = connect("postgres")
        admin.query("CREATE DATABASE quiet")
        for i in range(1 if args.same_db else max(levels)):
            admin.query("CREATE DATABASE noisy%d" % i)
        if args.same_db and args.conn_limit >= 0:
            admin.query("ALTER DATABASE noisy0 CONNECTION LIMIT %d" % args.conn_limit)
        admin.close()
        c = connect("quiet")
        c.query(eph.template_sql(10, 200))
        c.query("VACUUM ANALYZE")
        c.close()
        for n in levels:
            stop = [False]
            b_lat, b_err, nc_lat, nc_err, noisy_done = [], [], [], [], [0]

            def quiet():
                c = connect("quiet")
                while not stop[0]:
                    t0 = time.perf_counter()
                    try:
                        c.query("SELECT 1")
                        c.query(B_QUERY)
                        b_lat.append(time.perf_counter() - t0)
                    except Exception as e:  # noqa: BLE001
                        b_err.append(str(e)[:160])
                    time.sleep(0.005)
                c.close()

            def newconn():
                while not stop[0]:
                    t0 = time.perf_counter()
                    try:
                        c = connect("quiet")
                        c.query("SELECT 1")
                        c.close()
                        nc_lat.append(time.perf_counter() - t0)
                    except Exception as e:  # noqa: BLE001
                        nc_err.append(str(e)[:160])
                    time.sleep(0.25)

            noisy_err = []

            refused = []

            def noisy(i):
                try:
                    try:
                        c = connect("noisy0" if args.same_db else "noisy%d" % i)
                    except Exception as e:  # noqa: BLE001 - connection limit
                        refused.append(str(e)[:120])
                        return
                    while not stop[0]:
                        c.query(NOISY_SQL)
                        noisy_done[0] += 1
                    c.close()
                except Exception as e:  # noqa: BLE001 - a dead noisy client invalidates the level
                    noisy_err.append(str(e)[:160])
            ths = [threading.Thread(target=noisy, args=(i,), daemon=True) for i in range(n)]
            for t in ths:
                t.start()
            time.sleep(3)  # let the noisy clients reach steady state
            tot0, idle0 = cpu_times()
            s0 = pb.memory_sample(srv.proc.pid)
            q = [threading.Thread(target=quiet, daemon=True), threading.Thread(target=newconn, daemon=True)]
            for t in q:
                t.start()
            time.sleep(args.seconds)
            tot1, idle1 = cpu_times()
            s1 = pb.memory_sample(srv.proc.pid)
            load = os.getloadavg()[0]
            stop[0] = True
            for t in q + ths:
                t.join(timeout=120)
            row = {"noisy_clients": n, "quiet_latency_ms": ms(b_lat), "quiet_errors": len(b_err),
                   "quiet_error_examples": b_err[:3], "new_connection_ms": ms(nc_lat), "new_connection_errors": len(nc_err),
                   "host_cpu_busy_percent": round(100.0 * (1 - (idle1 - idle0) / max(tot1 - tot0, 1)), 1),
                   "server_cpu_percent_of_one_core": round(((s1["cpu_user_ns_sum"] + s1["cpu_system_ns_sum"])
                                                            - (s0["cpu_user_ns_sum"] + s0["cpu_system_ns_sum"]))
                                                           / (args.seconds * 1e9) * 100, 1),
                   "load1": round(load, 2), "noisy_queries_completed": noisy_done[0], "noisy_errors": noisy_err[:3], "noisy_refused": len(refused),
                   "pss_mb": round((s1.get(pb.MEM_KEY) or 0) / 1048576.0, 1)}
            doc["levels"].append(row)
            with open(path, "w") as f:
                json.dump(doc, f, indent=2)
                f.write("\n")
            lq = row["quiet_latency_ms"] or {}
            print("noisy=%d refused=%d host cpu=%5.1f%% quiet p50=%s p95=%s p99=%s max=%s ms errors=%d newconn p50=%s" % (
                n, len(refused), row["host_cpu_busy_percent"], lq.get("p50"), lq.get("p95"), lq.get("p99"), lq.get("max"),
                len(b_err), (row["new_connection_ms"] or {}).get("p50")), flush=True)
            time.sleep(5)
        print("wrote", path)
    finally:
        srv.stop()
        ws.cleanup()


if __name__ == "__main__":
    main()
