#!/usr/bin/env python3
"""Runtime topology: one runtime with N databases vs several smaller runtimes.

For each layout (default 1x1000, 2x500, 4x250), with the same total number of
ephemeral databases spread evenly over R runtimes:
  - mint latency (each runtime minted by its own client thread, in parallel)
  - idle: total PSS / RSS / threads / FDs over all runtimes, idle CPU over a
    window, and the per-runtime base (each runtime before any database)
  - active: K databases active (spread round-robin over the runtimes), latency
    p50/p95/p99, errors, CPU, PSS

A layout is skipped if MemAvailable falls below --min-available-mb before it
starts, and stopped if it falls below that while minting.

Output: <out>/multiruntime.json.
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

M = 1048576.0
ACTIVE_SQL = ("SELECT count(*) FROM t1 WHERE account_id < 50;"
              "UPDATE t2 SET name = name WHERE id = 1")


def mem_available_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024.0
    return 0


def cpu_ns(s):
    return s["cpu_user_ns_sum"] + s["cpu_system_ns_sum"]


def total(samples, key):
    return sum((s.get(key) or 0) for s in samples)


def name(i):
    return "%s%s__m%d" % (eph.PREFIX, eph.TEMPLATE, i)


def run_layout(args, runtimes, per_runtime, port0):
    gucs = ["pgrust.ephemeral_db_prefix=" + eph.PREFIX, "pgrust.ephemeral_db_mint_roles=postgres",
            "pgrust.ephemeral_db_grace=86400", "max_connections=130"] + args.guc
    server_args = []
    for g in gucs:
        server_args += ["-c", g]
    rts = []
    try:
        for r in range(runtimes):
            ws = pb.Workspace(pb.Engine("pgrust", args.binary, server_args, args.conf))
            srv = pb.Server(ws, port0 + r).launch()
            srv.wait_select1()
            rts.append((ws, srv))
        time.sleep(5)
        base = [pb.memory_sample(srv.proc.pid) for _, srv in rts]
        for ws, srv in rts:
            c = pb.PgConn(ws.sockdir, srv.port, timeout=300)
            c.query("CREATE DATABASE %s" % eph.TEMPLATE)
            c.close()
            c = pb.PgConn(ws.sockdir, srv.port, database=eph.TEMPLATE, timeout=300)
            c.query(eph.template_sql(args.tables, args.rows))
            c.query("VACUUM ANALYZE")
            c.close()
            c = pb.PgConn(ws.sockdir, srv.port, timeout=300)
            c.query("SELECT pgrust_seal_template('%s')" % eph.TEMPLATE)
            c.close()
        lat, errors, aborted = [], [], [False]

        def mint(ws, srv):
            for i in range(1, per_runtime + 1):
                if aborted[0]:
                    return
                if i % 50 == 0 and mem_available_mb() < args.min_available_mb:
                    aborted[0] = True
                    return
                t0 = time.perf_counter()
                try:
                    c = pb.PgConn(ws.sockdir, srv.port, database=name(i), timeout=300)
                    c.query("SELECT 1")
                    lat.append(time.perf_counter() - t0)
                    c.close()
                except Exception as e:  # noqa: BLE001
                    errors.append(str(e)[:160])
        t_mint = time.perf_counter()
        threads = [threading.Thread(target=mint, args=rt) for rt in rts]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        mint_wall = time.perf_counter() - t_mint
        time.sleep(20)
        s0 = [pb.memory_sample(srv.proc.pid) for _, srv in rts]
        t0 = time.perf_counter()
        time.sleep(args.idle_seconds)
        s1 = [pb.memory_sample(srv.proc.pid) for _, srv in rts]
        idle_cpu = sum(cpu_ns(b) - cpu_ns(a) for a, b in zip(s0, s1)) / ((time.perf_counter() - t0) * 1e9) * 100
        row = {"layout": "%dx%d" % (runtimes, per_runtime), "runtimes": runtimes, "databases_per_runtime": per_runtime,
               "aborted_on_memory": aborted[0], "mint_errors": len(errors), "mint_error_examples": errors[:3],
               "mint_ms": {k: round(v * 1e3, 1) for k, v in pb.summarize(lat).items()} if lat else None,
               "mint_wall_s": round(mint_wall, 1),
               "base_pss_mb_total": round(total(base, pb.MEM_KEY) / M, 1),
               "base_pss_mb_per_runtime": [round((s.get(pb.MEM_KEY) or 0) / M, 1) for s in base],
               "idle_pss_mb_total": round(total(s1, pb.MEM_KEY) / M, 1),
               "idle_rss_mb_total": round(total(s1, "rss_bytes_sum") / M, 1),
               "idle_pss_mb_per_runtime": [round((s.get(pb.MEM_KEY) or 0) / M, 1) for s in s1],
               "threads_total": total(s1, "thread_count"),
               "fds_total": sum(pb.fd_count(srv.proc.pid) for _, srv in rts),
               "idle_cpu_percent_of_one_core": round(idle_cpu, 3),
               "host_mem_available_mb": round(mem_available_mb()),
               "active": []}
        for k in [int(x) for x in args.active.split(",") if x]:
            lats, errs = [], []
            stop = time.perf_counter() + args.active_seconds

            def worker(j):
                ws, srv = rts[j % runtimes]
                try:
                    c = pb.PgConn(ws.sockdir, srv.port, database=name(j // runtimes + 1), timeout=300)
                    while time.perf_counter() < stop:
                        q0 = time.perf_counter()
                        c.query(ACTIVE_SQL)
                        lats.append(time.perf_counter() - q0)
                        time.sleep(0.01)
                    c.close()
                except Exception as e:  # noqa: BLE001
                    errs.append(str(e)[:160])
            a0 = [pb.memory_sample(srv.proc.pid) for _, srv in rts]
            ta = time.perf_counter()
            ths = [threading.Thread(target=worker, args=(j,)) for j in range(k)]
            for t in ths:
                t.start()
            time.sleep(args.active_seconds / 2)
            mid = [pb.memory_sample(srv.proc.pid) for _, srv in rts]
            for t in ths:
                t.join()
            a1 = [pb.memory_sample(srv.proc.pid) for _, srv in rts]
            row["active"].append({
                "active": k, "queries": len(lats), "errors": len(errs), "error_examples": errs[:3],
                "latency_ms": {a: round(v * 1e3, 2) for a, v in pb.summarize(lats).items()} if lats else None,
                "pss_mb_total": round(total(mid, pb.MEM_KEY) / M, 1),
                "cpu_percent_of_one_core": round(sum(cpu_ns(b) - cpu_ns(a) for a, b in zip(a0, a1))
                                                 / ((time.perf_counter() - ta) * 1e9) * 100, 1)})
            time.sleep(3)
        return row
    finally:
        for ws, srv in rts:
            try:
                srv.stop()
            except Exception:  # noqa: BLE001
                pass
            ws.cleanup()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--conf", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--guc", action="append", default=[])
    ap.add_argument("--layouts", default="1x1000,2x500,4x250")
    ap.add_argument("--idle-seconds", type=int, default=60)
    ap.add_argument("--active", default="20,50")
    ap.add_argument("--active-seconds", type=float, default=20.0)
    ap.add_argument("--tables", type=int, default=50)
    ap.add_argument("--rows", type=int, default=200)
    ap.add_argument("--min-available-mb", type=int, default=2000)
    ap.add_argument("--port", type=int, default=54460)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "multiruntime.json")
    doc = {"benchmark": "multiruntime", "git_commit": pb.git("rev-parse", "HEAD"), "conf": args.conf,
           "template": {"tables": args.tables, "rows_per_table": args.rows}, "layouts": []}
    for i, lay in enumerate(args.layouts.split(",")):
        r, n = (int(x) for x in lay.split("x"))
        if mem_available_mb() < args.min_available_mb + 500:
            doc["layouts"].append({"layout": lay, "skipped": "MemAvailable %.0f MB" % mem_available_mb()})
            continue
        row = run_layout(args, r, n, args.port + 10 * i)
        doc["layouts"].append(row)
        with open(path, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        print("%-7s base=%s MB idle pss=%.0f MB threads=%d idle cpu=%.3f%% mint p50=%s p99=%s %s" % (
            lay, row["base_pss_mb_total"], row["idle_pss_mb_total"], row["threads_total"],
            row["idle_cpu_percent_of_one_core"], row["mint_ms"] and row["mint_ms"]["p50"],
            row["mint_ms"] and row["mint_ms"]["p99"],
            " ".join("active%d p50=%s p99=%s err=%d" % (a["active"], a["latency_ms"] and a["latency_ms"]["p50"],
                                                       a["latency_ms"] and a["latency_ms"]["p99"], a["errors"])
                     for a in row["active"])), flush=True)
        time.sleep(10)
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    print("wrote", path)


if __name__ == "__main__":
    main()
