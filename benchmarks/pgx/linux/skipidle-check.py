#!/usr/bin/env python3
"""Does autovacuum still reach the databases that change when idle ones are skipped?

One runtime, PGX profile, pgrust.autovacuum_skip_idle_databases = on and a
short naptime. Creates --idle databases that are never written and --busy
databases that each get a table with dead tuples above the vacuum threshold.
Then waits for every busy table to show last_autovacuum and last_autoanalyze.
Round 2 writes the same busy tables again after their first visit, and
waits for a second autovacuum: a visited database must be visited again
once it changes. Exit status 0 = every check passed. Output:
<out>/skipidle-check-<on|off>.json
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pgxbench as pb  # noqa: E402

STATS = ("SELECT coalesce(autovacuum_count, 0), coalesce(autoanalyze_count, 0), n_dead_tup "
         "FROM pg_stat_user_tables WHERE relname = 't'")
CHURN = "INSERT INTO t SELECT g, md5(g::text) FROM generate_series(1, 2000) g; DELETE FROM t WHERE id % 10 <> 0"


def counts(ws, port, db):
    c = pb.PgConn(ws.sockdir, port, database=db, timeout=30)
    try:
        r = c.query(STATS)
    finally:
        c.close()
    return tuple(int(x) for x in r[0]) if r else (0, 0, 0)


def wait_for(ws, port, dbs, need_vac, need_an, timeout):
    t0 = time.time()
    while True:
        state = {db: counts(ws, port, db) for db in dbs}
        done = [db for db, (v, a, _) in state.items() if v >= need_vac[db] and a >= need_an[db]]
        if len(done) == len(dbs) or time.time() - t0 > timeout:
            return round(time.time() - t0, 1), state, done
        time.sleep(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--conf", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--idle", type=int, default=50)
    ap.add_argument("--busy", type=int, default=5)
    ap.add_argument("--naptime", default="5s")
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--port", type=int, default=54970)
    ap.add_argument("--skip-idle", default="on", help="off = control run with the stock launcher")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    gucs = ["pgrust.autovacuum_skip_idle_databases=%s" % args.skip_idle, "autovacuum_naptime=%s" % args.naptime,
            "autovacuum=on", "max_connections=40"]
    eng = pb.Engine("pgrust", args.binary, sum((["-c", g] for g in gucs), []), args.conf)
    ws = pb.Workspace(eng)
    srv = pb.Server(ws, args.port).launch()
    doc = {"benchmark": "skipidle-check", "git_commit": pb.git("rev-parse", "HEAD"), "settings": gucs,
           "idle": args.idle, "busy": args.busy, "checks": {}}
    ok = True
    try:
        srv.wait_select1(timeout=300)
        c = pb.PgConn(ws.sockdir, args.port, timeout=600)
        for i in range(args.idle):
            c.query("CREATE DATABASE idle_%d" % i)
        busy = ["busy_%d" % i for i in range(args.busy)]
        for db in busy:
            c.query("CREATE DATABASE %s" % db)
            b = pb.PgConn(ws.sockdir, args.port, database=db, timeout=60)
            b.query("CREATE TABLE t(id int PRIMARY KEY, v text)")
            b.query(CHURN)
            b.close()
        c.close()

        need = {db: 1 for db in busy}
        secs, state, done = wait_for(ws, args.port, busy, need, need, args.timeout)
        r1 = {"seconds": secs, "visited": len(done), "of": len(busy), "state": state}
        doc["checks"]["first_visit"] = r1
        ok &= len(done) == len(busy)
        print("round 1: %d/%d busy databases autovacuumed + autoanalyzed in %.0f s" % (len(done), len(busy), secs),
              flush=True)

        base = {db: state[db] for db in busy}
        for db in busy:
            b = pb.PgConn(ws.sockdir, args.port, database=db, timeout=60)
            b.query("DELETE FROM t; " + CHURN.replace("1, 2000", "2001, 4000"))
            b.close()
        need_v = {db: base[db][0] + 1 for db in busy}
        need_a = {db: base[db][1] + 1 for db in busy}
        secs, state, done = wait_for(ws, args.port, busy, need_v, need_a, args.timeout)
        r2 = {"seconds": secs, "visited": len(done), "of": len(busy), "state": state}
        doc["checks"]["revisit_after_write"] = r2
        ok &= len(done) == len(busy)
        print("round 2: %d/%d busy databases autovacuumed again after new writes in %.0f s"
              % (len(done), len(busy), secs), flush=True)
    finally:
        srv.stop()
        ws.cleanup()
    doc["ok"] = bool(ok)
    with open(os.path.join(args.out, "skipidle-check-%s.json" % args.skip_idle), "w") as f:
        json.dump(doc, f, indent=2)
    print("OK" if ok else "FAILED", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
