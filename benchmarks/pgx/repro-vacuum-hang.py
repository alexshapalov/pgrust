#!/usr/bin/env python3
"""Reproducer for the intermittent whole-server hang in VACUUM ANALYZE.

Each iteration: fresh data directory, start the server, build a 50-table
schema in one database, run a database-wide VACUUM ANALYZE with a timeout.
When the statement does not return, the script measures the blast radius
(which other connections and databases still answer), collects diagnostics
and kills the server.

  repro-vacuum-hang.py --out DIR                 # loop until the first hang
  repro-vacuum-hang.py --out DIR --tries 100 --max-hangs 0   # measure the rate
  repro-vacuum-hang.py --out DIR --binary target-sym/release/postgres

Output: DIR/repro.json (every iteration) and, per hang, DIR/hang-<n>/ with
sample.txt (thread backtraces), server.log, report.json.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pgxbench as pb  # noqa: E402
import ephemeral as eph  # noqa: E402

PROBE_TIMEOUT_S = 5
TIMEOUTS = (socket.timeout, TimeoutError)


def probe(fn):
    """Run one blast-radius probe; never raises."""
    t0 = time.perf_counter()
    try:
        fn()
        return {"answered": True, "ms": round((time.perf_counter() - t0) * 1e3, 1)}
    except TIMEOUTS:
        return {"answered": False, "error": "no answer in %d s" % PROBE_TIMEOUT_S}
    except Exception as e:  # noqa: BLE001 - the error text is the result
        return {"answered": False, "error": str(e)[:200]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--tries", type=int, default=200)
    ap.add_argument("--max-hangs", type=int, default=1, help="stop after this many hangs (0 = never)")
    ap.add_argument("--timeout", type=float, default=30.0, help="seconds before VACUUM ANALYZE counts as hung")
    ap.add_argument("--tables", type=int, default=50)
    ap.add_argument("--rows", type=int, default=200)
    ap.add_argument("--statement", default="VACUUM ANALYZE")
    ap.add_argument("--guc", action="append", default=[], help="extra server setting name=value (repeatable)")
    ap.add_argument("--no-ephemeral", action="store_true", help="do not enable the ephemeral-database janitor")
    ap.add_argument("--port", type=int, default=54370)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    gucs = list(args.guc)
    if not args.no_ephemeral:
        gucs += ["pgrust.ephemeral_db_prefix=tdb_", "pgrust.ephemeral_db_mint_roles=postgres",
                 "pgrust.ephemeral_db_grace=3600"]
    server_args = []
    for g in gucs:
        server_args += ["-c", g]
    engine = pb.Engine("pgrust", args.binary, server_args)
    doc = {"binary": engine.binary, "settings": gucs, "statement": args.statement,
           "schema": {"tables": args.tables, "rows_per_table": args.rows},
           "timeout_s": args.timeout, "iterations": [], "hangs": 0}
    ephemeral_db = "tdb_tpl_small__probe"

    for it in range(1, args.tries + 1):
        ws = pb.Workspace(engine)
        srv = pb.Server(ws, args.port).launch()
        row = {"iteration": it, "stage": "start"}
        conn = lambda db, t=args.timeout: pb.PgConn(ws.sockdir, args.port, database=db, timeout=t)  # noqa: E731
        try:
            srv.wait_select1()
            admin = conn("postgres")
            if not args.no_ephemeral:
                row["stage"] = "ephemeral setup"
                admin.query("CREATE DATABASE tpl_small")
                c = conn("tpl_small")
                c.query("CREATE TABLE small(x int); INSERT INTO small VALUES (1)")
                c.close()
                admin.query("SELECT pgrust_seal_template('tpl_small')")
                conn(ephemeral_db).close()
            row["stage"] = "create database"
            admin.query("CREATE DATABASE tpl_app")
            work = conn("tpl_app")
            row["stage"] = "schema and inserts"
            work.query(eph.template_sql(args.tables, args.rows))
            bystander_same = conn("tpl_app")
            bystander_same.query("SELECT 1")
            bystander_other = conn("postgres")
            bystander_other.query("SELECT 1")
            row["stage"] = args.statement
            t0 = time.perf_counter()
            work.query(args.statement)
            row.update(stage="ok", statement_s=round(time.perf_counter() - t0, 3))
        except TIMEOUTS:
            doc["hangs"] += 1
            row["hung_in"] = row["stage"]
            hang_dir = os.path.join(args.out, "hang-%d" % it)
            os.makedirs(hang_dir, exist_ok=True)
            pid = srv.proc.pid
            report = {"iteration": it, "hung_in": row["stage"], "pid": pid}
            if row["stage"] == args.statement:
                for c in (bystander_same, bystander_other):
                    c.sock.settimeout(PROBE_TIMEOUT_S)
                short = lambda db: pb.PgConn(ws.sockdir, args.port, database=db, timeout=PROBE_TIMEOUT_S)  # noqa: E731
                report["blast_radius"] = {
                    "existing connection, same database": probe(lambda: bystander_same.query("SELECT 1")),
                    "existing connection, other database": probe(lambda: bystander_other.query("SELECT 1")),
                    "new connection, same database": probe(lambda: short("tpl_app").query("SELECT 1")),
                    "new connection, other database": probe(lambda: short("postgres").query("SELECT 1")),
                }
                if not args.no_ephemeral:
                    report["blast_radius"]["new connection, existing ephemeral database"] = probe(
                        lambda: short(ephemeral_db).query("SELECT 1"))
                    report["blast_radius"]["mint a new ephemeral database"] = probe(
                        lambda: short("tdb_tpl_small__during_hang").query("SELECT 1"))
                for name, sql in (
                        ("activity", "SELECT pid, datname, backend_type, state, wait_event_type, wait_event, "
                                     "left(query, 120) FROM pg_stat_activity ORDER BY pid"),
                        ("ungranted_locks", "SELECT pid, locktype, relation::regclass, mode FROM pg_locks WHERE NOT granted")):
                    try:
                        report[name] = bystander_other.query(sql)
                    except Exception as e:  # noqa: BLE001
                        report[name] = "unavailable: " + str(e)[:120]
            s0 = pb.memory_sample(pid)
            subprocess.run(["sample", str(pid), "3", "-file", os.path.join(hang_dir, "sample.txt")],
                           capture_output=True)
            s1 = pb.memory_sample(pid)
            report.update({
                "threads": s1.get("thread_count"),
                "footprint_bytes": s1.get(pb.MEM_KEY), "rss_bytes": s1.get("rss_bytes_sum"),
                "cpu_ns_during_3s_sample": (s1["cpu_user_ns_sum"] + s1["cpu_system_ns_sum"]
                                            - s0["cpu_user_ns_sum"] - s0["cpu_system_ns_sum"])})
            shutil.copy(srv.log_path, os.path.join(hang_dir, "server.log"))
            with open(os.path.join(hang_dir, "report.json"), "w") as f:
                json.dump(report, f, indent=2)
                f.write("\n")
            row["report"] = os.path.relpath(hang_dir, args.out)
            print("iteration %d: HANG in %s; diagnostics in %s" % (it, row["stage"], hang_dir), flush=True)
            for k, v in report.get("blast_radius", {}).items():
                print("    %-45s %s" % (k, "answered in %s ms" % v["ms"] if v["answered"] else v["error"]), flush=True)
            srv.proc.kill()
        except Exception as e:  # noqa: BLE001 - recorded per iteration
            row["error"] = str(e)[:300]
        finally:
            srv.stop()
            ws.cleanup()
        doc["iterations"].append(row)
        if it % 10 == 0 or "hung_in" in row:
            print("iteration %d  hangs so far: %d" % (it, doc["hangs"]), flush=True)
        with open(os.path.join(args.out, "repro.json"), "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        if args.max_hangs and doc["hangs"] >= args.max_hangs:
            break
    print("tries %d, hangs %d" % (len(doc["iterations"]), doc["hangs"]))


if __name__ == "__main__":
    main()
