#!/usr/bin/env python3
"""Where does a database lifecycle's memory go, stage by stage?

One runtime, PGX profile, a sealed template. Each batch takes N databases
through the lifecycle with explicit commands (no janitor timing), sampling
the server's PSS after each stage:

  create      CREATE DATABASE d_i TEMPLATE tpl_app        (admin session)
  connect     one new session per database, SELECT 1      (held open)
  workload    churn.py's workload in each session
  disconnect  close every session
  drop        DROP DATABASE d_i                           (admin session)

Batches repeat (--batches); warm-up batches are run first and not counted.
The per-lifecycle retained figure is the slope of the post-drop PSS over the
measured batches, divided by N. A second variant (--connections-only) skips
create/drop and only opens and closes sessions to an existing database, to
separate per-connection retention from per-database retention.

Output: <out>/lifecycle.json.
"""

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pgxbench as pb  # noqa: E402
import ephemeral as eph  # noqa: E402
import churn  # noqa: E402

M = 1048576.0


def slope(ys):
    xs = list(range(len(ys)))
    if len(ys) < 2:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--conf", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--guc", action="append", default=[])
    ap.add_argument("--n", type=int, default=100, help="databases (or connections) per batch")
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--batches", type=int, default=10)
    ap.add_argument("--settle", type=float, default=3.0)
    ap.add_argument("--connections-only", action="store_true")
    ap.add_argument("--label", default="")
    ap.add_argument("--port", type=int, default=54500)
    args = ap.parse_args()
    gucs = ["max_connections=%d" % (args.n + 20)] + args.guc
    server_args = []
    for g in gucs:
        server_args += ["-c", g]
    ws = pb.Workspace(pb.Engine("pgrust", args.binary, server_args, args.conf))
    srv = pb.Server(ws, args.port).launch()
    connect = lambda db: pb.PgConn(ws.sockdir, args.port, database=db, timeout=600)  # noqa: E731
    os.makedirs(args.out, exist_ok=True)
    name = "lifecycle%s%s.json" % ("-connections" if args.connections_only else "", ("-" + args.label) if args.label else "")
    path = os.path.join(args.out, name)
    doc = {"benchmark": "lifecycle", "git_commit": pb.git("rev-parse", "HEAD"), "settings": gucs, "conf": args.conf,
           "n": args.n, "connections_only": args.connections_only, "label": args.label, "batches": []}
    pid = None

    def pss():
        time.sleep(args.settle)
        s = pb.memory_sample(pid)
        return s[pb.MEM_KEY], s.get("thread_count")

    try:
        srv.wait_select1()
        pid = srv.proc.pid
        admin = connect("postgres")
        admin.query("CREATE DATABASE tpl_app")
        c = connect("tpl_app")
        c.query(eph.template_sql(50, 200))
        c.query("VACUUM ANALYZE")
        c.close()
        if args.connections_only:
            admin.query("CREATE DATABASE target TEMPLATE tpl_app")
        start = pss()[0]
        doc["start_pss_bytes"] = start
        for b in range(args.warmup + args.batches):
            row = {"batch": b, "warmup": b < args.warmup, "stage_pss_bytes": {}}
            row["stage_pss_bytes"]["before"] = pss()[0]
            dbs = ["target"] * args.n if args.connections_only else ["d%d_%d" % (b, i) for i in range(args.n)]
            if not args.connections_only:
                for d in dbs:
                    admin.query("CREATE DATABASE %s TEMPLATE tpl_app" % d)
                row["stage_pss_bytes"]["create"] = pss()[0]
            sessions = []
            for d in dbs:
                s = connect(d)
                s.query("SELECT 1")
                sessions.append(s)
            row["stage_pss_bytes"]["connect"], row["threads_connected"] = pss()
            for i, s in enumerate(sessions):
                if args.connections_only:
                    s.query("SELECT count(*) FROM t1 JOIN accounts a ON a.id = t1.account_id")
                else:
                    s.query(churn.WORKLOAD)
            row["stage_pss_bytes"]["workload"] = pss()[0]
            for s in sessions:
                s.close()
            row["stage_pss_bytes"]["disconnect"], row["threads_after"] = pss()
            if not args.connections_only:
                for d in dbs:
                    admin.query("DROP DATABASE %s" % d)
                row["stage_pss_bytes"]["drop"] = pss()[0]
            doc["batches"].append(row)
            last = list(row["stage_pss_bytes"].values())[-1]
            print("batch %2d%s  %s" % (b, " (warm-up)" if row["warmup"] else "",
                  "  ".join("%s=%.1f" % (k, v / M) for k, v in row["stage_pss_bytes"].items())), flush=True)
            with open(path, "w") as f:
                json.dump(doc, f, indent=2)
        measured = [list(r["stage_pss_bytes"].values())[-1] for r in doc["batches"] if not r["warmup"]]
        sl = slope(measured)
        doc["retained_bytes_per_batch"] = sl
        doc["retained_bytes_per_lifecycle"] = sl / args.n if sl is not None else None
        # Mean stage-to-stage deltas over measured batches, per database.
        stages = list(doc["batches"][-1]["stage_pss_bytes"].keys())
        deltas = {}
        for a, bname in zip(stages, stages[1:]):
            deltas[bname] = statistics.mean((r["stage_pss_bytes"][bname] - r["stage_pss_bytes"][a]) / args.n
                                            for r in doc["batches"] if not r["warmup"])
        doc["stage_delta_bytes_per_db"] = deltas
        with open(path, "w") as f:
            json.dump(doc, f, indent=2)
        print("retained per lifecycle: %.0f bytes  (per batch of %d: %.1f KB)" % (
            doc["retained_bytes_per_lifecycle"], args.n, sl / 1024), flush=True)
        print("stage deltas per db:", {k: round(v) for k, v in deltas.items()})
        print("wrote", path)
    finally:
        srv.stop()
        ws.cleanup()


if __name__ == "__main__":
    main()
