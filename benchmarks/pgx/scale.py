#!/usr/bin/env python3
"""Scale test: one runtime, up to 1,000 ephemeral databases.

At each database count (default 0, 100, 300, 1000), with no clients connected:
  - footprint, RSS, threads, file descriptors, data directory size
  - idle CPU as a time series (one figure per 10 s interval), wakeups, disk writes
  - which server threads are on-CPU, from a `sample` of the process

At the largest count, with K databases active (default 20 and 100) and the
rest idle: per-query latency in the active databases, errors, footprint, CPU.

Output: <out>/scale.json.
"""

import argparse
import collections
import json
import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pgxbench as pb  # noqa: E402
import ephemeral as eph  # noqa: E402

M = 1048576.0
ACTIVE_SQL = ("SELECT count(*) FROM t1 WHERE account_id < 50;"
              "UPDATE t2 SET name = name WHERE id = 1")


def cpu_ns(s):
    return s["cpu_user_ns_sum"] + s["cpu_system_ns_sum"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--conf", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--guc", action="append", default=[])
    ap.add_argument("--counts", default="0,100,300,1000")
    ap.add_argument("--idle-seconds", type=int, default=60, help="idle observation window at each count")
    ap.add_argument("--long-idle-seconds", type=int, default=300, help="window at the largest count")
    ap.add_argument("--active", default="20,100")
    ap.add_argument("--active-seconds", type=float, default=20.0)
    ap.add_argument("--tables", type=int, default=50)
    ap.add_argument("--rows", type=int, default=200)
    ap.add_argument("--port", type=int, default=54410)
    args = ap.parse_args()

    gucs = ["pgrust.ephemeral_db_prefix=" + eph.PREFIX, "pgrust.ephemeral_db_mint_roles=postgres",
            "pgrust.ephemeral_db_grace=86400", "max_connections=130"] + args.guc
    server_args = []
    for g in gucs:
        server_args += ["-c", g]
    ws = pb.Workspace(pb.Engine("pgrust", args.binary, server_args, args.conf))
    srv = pb.Server(ws, args.port).launch()
    conn = lambda db: pb.PgConn(ws.sockdir, args.port, database=db, timeout=300)  # noqa: E731
    name = lambda i: "%s%s__s%d" % (eph.PREFIX, eph.TEMPLATE, i)  # noqa: E731
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "scale.json")
    doc = {"benchmark": "scale", "git_commit": pb.git("rev-parse", "HEAD"), "conf": args.conf, "settings": gucs,
           "template": {"tables": args.tables, "rows_per_table": args.rows}, "idle": [], "active": []}

    def save():
        with open(path, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")

    try:
        srv.wait_select1()
        admin = conn("postgres")
        admin.query("CREATE DATABASE %s" % eph.TEMPLATE)
        c = conn(eph.TEMPLATE)
        c.query(eph.template_sql(args.tables, args.rows))
        c.query("VACUUM ANALYZE")
        c.close()
        admin.query("SELECT pgrust_seal_template('%s')" % eph.TEMPLATE)
        admin.close()
        pid = srv.proc.pid
        counts = [int(x) for x in args.counts.split(",")]
        made = 0
        for n in counts:
            lat = []
            while made < n:
                made += 1
                t0 = time.perf_counter()
                c = conn(name(made))
                c.query("SELECT 1")
                lat.append(time.perf_counter() - t0)
                c.close()
            time.sleep(15)  # let prewarm and checkpoints finish before calling it idle
            window = args.long_idle_seconds if n == counts[-1] else args.idle_seconds
            series = []
            s_first = prev = pb.memory_sample(pid)
            t_prev = time.perf_counter()
            for _ in range(window // 10):
                time.sleep(10)
                cur = pb.memory_sample(pid)
                now = time.perf_counter()
                series.append(round((cpu_ns(cur) - cpu_ns(prev)) / ((now - t_prev) * 1e9) * 100, 4))
                prev, t_prev = cur, now
            du = subprocess.run(["du", "-sk", srv.datadir], capture_output=True, text=True).stdout
            fds = pb.fd_count(pid)
            row = {"databases": n, "mint_ms": ({k: round(v * 1e3, 1) for k, v in pb.summarize(lat).items()}
                                               if lat else None),
                   "footprint_bytes": prev[pb.MEM_KEY], "rss_bytes": prev["rss_bytes_sum"],
                   "threads": prev.get("thread_count"), "fds": fds,
                   "datadir_apparent_bytes": int(du.split()[0]) * 1024,
                   "idle_window_s": window, "idle_cpu_percent_per_10s": series,
                   "idle_cpu_percent_mean": round(sum(series) / len(series), 4),
                   "idle_cpu_percent_last_minute": round(sum(series[-6:]) / len(series[-6:]), 4),
                   "idle_disk_written_bytes": prev.get("disk_written_bytes_sum", 0) - s_first.get("disk_written_bytes_sum", 0),
                   "idle_wakeups": prev.get("idle_wakeups_sum", 0) - s_first.get("idle_wakeups_sum", 0),
                   "on_cpu": pb.busy_threads(pid, 10)}
            doc["idle"].append(row)
            save()
            print("n=%-5d footprint=%.0f MB threads=%s fds=%s  idle cpu mean=%.3f%% last-minute=%.3f%%  busy=%s" % (
                n, row["footprint_bytes"] / M, row["threads"], fds, row["idle_cpu_percent_mean"],
                row["idle_cpu_percent_last_minute"], row["on_cpu"]["busy_share_by_thread"]), flush=True)

        total = counts[-1]
        for k in [int(x) for x in args.active.split(",") if x]:
            if k > total:
                continue
            lats, errors = [], []
            stop = time.perf_counter() + args.active_seconds

            def worker(i):
                try:
                    c = conn(name(i))
                    while time.perf_counter() < stop:
                        t0 = time.perf_counter()
                        c.query(ACTIVE_SQL)
                        lats.append(time.perf_counter() - t0)
                        time.sleep(0.01)
                    c.close()
                except Exception as e:  # noqa: BLE001 - recorded
                    errors.append(str(e)[:160])
            s0 = pb.memory_sample(pid)
            t0 = time.perf_counter()
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, k + 1)]
            for t in threads:
                t.start()
            time.sleep(args.active_seconds / 2)
            mid = pb.memory_sample(pid)
            for t in threads:
                t.join()
            s1 = pb.memory_sample(pid)
            row = {"databases": total, "active": k, "queries": len(lats), "errors": len(errors),
                   "error_examples": errors[:3],
                   "latency_ms": {a: round(v * 1e3, 2) for a, v in pb.summarize(lats).items()} if lats else None,
                   "footprint_bytes": mid[pb.MEM_KEY], "threads": mid.get("thread_count"),
                   "cpu_percent_of_one_core": round((cpu_ns(s1) - cpu_ns(s0)) / ((time.perf_counter() - t0) * 1e9) * 100, 1)}
            doc["active"].append(row)
            save()
            print("active %d of %d: p50=%s p99=%s ms errors=%d footprint=%.0f MB cpu=%.0f%%" % (
                k, total, row["latency_ms"] and row["latency_ms"]["p50"], row["latency_ms"] and row["latency_ms"]["p99"],
                len(errors), row["footprint_bytes"] / M, row["cpu_percent_of_one_core"]), flush=True)
            time.sleep(5)
        print("wrote", path)
    finally:
        srv.stop()
        ws.cleanup()


if __name__ == "__main__":
    main()
