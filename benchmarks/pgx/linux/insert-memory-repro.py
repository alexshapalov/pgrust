#!/usr/bin/env python3
"""Does INSERT ... SELECT (or a whole-table UPDATE) hold memory per row?

PostgreSQL resets per-tuple memory for every row, so peak memory of a bulk
INSERT ... SELECT should not grow with the row count (generate_series
materializes its output in work_mem and spills beyond it). For each variant
and row count, one fresh server with no memory limit, work_mem = 64MB;
records peak PSS while the statement runs. Output: <out>/insert-memory.json
"""

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pgxbench as pb  # noqa: E402

M = 1048576.0
VARIANTS = {
    "ints": "INSERT INTO t(a) SELECT g FROM generate_series(1, %d) g",
    "md5": "INSERT INTO t(a, s) SELECT g, md5(g::text) FROM generate_series(1, %d) g",
    "jsonb": "INSERT INTO t(a, j) SELECT g, jsonb_build_object('n', g) FROM generate_series(1, %d) g",
    "md5+jsonb": "INSERT INTO t(a, s, j) SELECT g, md5(g::text), jsonb_build_object('n', g, 's', md5(g::text)) FROM generate_series(1, %d) g",
    "select-only": "SELECT count(*) FROM (SELECT md5(g::text), jsonb_build_object('n', g, 's', md5(g::text)) FROM generate_series(1, %d) g) x",
    # (setup, measured statement): a source table is filled first
    "insert-from-table": ("CREATE TABLE src AS SELECT g AS a, md5(g::text) AS s, "
                          "jsonb_build_object('n', g, 's', md5(g::text)) AS j FROM generate_series(1, %d) g",
                          "INSERT INTO t SELECT * FROM src"),
    # the table is filled first, then every row is rewritten
    "update": ("INSERT INTO t(a) SELECT g FROM generate_series(1, %d) g",
               "UPDATE t SET s = md5(a::text), j = jsonb_build_object('n', a, 's', md5(a::text))"),
}


def run(args, engine, sql, port, setup=None):
    ws = pb.Workspace(engine)
    srv = pb.Server(ws, port).launch()
    try:
        srv.wait_select1()
        c = pb.PgConn(ws.sockdir, port, timeout=1800)
        c.query("CREATE TABLE t(a int, s text, j jsonb)")
        c.query("SET work_mem = '64MB'")
        if setup:
            c.query(setup)
        pid = srv.proc.pid
        base = pb.memory_sample(pid).get(pb.MEM_KEY, 0)
        peak = [base]
        stop = [False]

        def watch():
            while not stop[0]:
                peak[0] = max(peak[0], pb.memory_sample(pid).get(pb.MEM_KEY, 0) or 0)
                time.sleep(0.1)
        w = threading.Thread(target=watch, daemon=True)
        w.start()
        t0 = time.perf_counter()
        c.query(sql)
        dt = time.perf_counter() - t0
        stop[0] = True
        w.join()
        c.close()
        return {"base_mb": round(base / M, 1), "peak_mb": round(peak[0] / M, 1),
                "growth_mb": round((peak[0] - base) / M, 1), "seconds": round(dt, 2)}
    finally:
        srv.stop()
        ws.cleanup()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--conf", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--rows", default="1000000,2000000,3000000")
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--engines", default="pgx,postgres")
    ap.add_argument("--port", type=int, default=54900)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    engines = {"pgx": lambda: pb.Engine("pgrust", args.binary, ["-c", "max_connections=20"], args.conf),
               "postgres": lambda: pb.Engine("postgres", os.path.join(pb.pg18_prefix(), "bin", "postgres"),
                                             ["-c", "max_connections=20"])}
    doc = {"benchmark": "insert-memory", "git_commit": pb.git("rev-parse", "HEAD"), "rows": []}
    for e in args.engines.split(","):
        for v in args.variants.split(","):
            for n in (int(x) for x in args.rows.split(",")):
                q = VARIANTS[v]
                r = (run(args, engines[e](), q[1], args.port, setup=q[0] % n) if isinstance(q, tuple)
                     else run(args, engines[e](), q % n, args.port))
                r.update(engine=e, variant=v, n=n)
                doc["rows"].append(r)
                print("%-9s %-12s %8d rows  peak growth %7.1f MB  (%.1fs)" % (e, v, n, r["growth_mb"], r["seconds"]),
                      flush=True)
                with open(os.path.join(args.out, "insert-memory.json"), "w") as f:
                    json.dump(doc, f, indent=2)


if __name__ == "__main__":
    main()
