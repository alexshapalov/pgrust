#!/usr/bin/env python3
"""Churn benchmark: does memory plateau or keep growing as ephemeral databases
are created, used and dropped over and over?

One server. Each cycle mints N databases from a sealed template, runs a small
workload in each, disconnects, and waits for the janitor to drop them all.
After every cycle the server's footprint, RSS, thread count, open file
descriptors, database count and data directory size are recorded.

Output: <out>/churn.json, with one row per cycle (the series to plot is
cycle -> footprint_bytes) and a finer-grained memory trace of the first cycle.
"""

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pgxbench as pb  # noqa: E402
import ephemeral as eph  # noqa: E402

M = 1048576.0
WORKLOAD = ("INSERT INTO accounts(email) VALUES ('churn@example.com');"
            "UPDATE t0 SET name = name || '!' WHERE id <= 20;"
            "SELECT count(*) FROM t1 JOIN accounts a ON a.id = t1.account_id;"
            "CREATE TABLE scratch(id int PRIMARY KEY, v text); INSERT INTO scratch VALUES (1, 'x')")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--conf", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--guc", action="append", default=[])
    ap.add_argument("--cycles", type=int, default=100)
    ap.add_argument("--databases", type=int, default=100, help="databases minted per cycle")
    ap.add_argument("--grace", type=int, default=2)
    ap.add_argument("--tables", type=int, default=50)
    ap.add_argument("--rows", type=int, default=200)
    ap.add_argument("--port", type=int, default=54390)
    args = ap.parse_args()

    gucs = ["pgrust.ephemeral_db_prefix=" + eph.PREFIX, "pgrust.ephemeral_db_mint_roles=postgres",
            "pgrust.ephemeral_db_grace=%d" % args.grace] + args.guc
    server_args = []
    for g in gucs:
        server_args += ["-c", g]
    ws = pb.Workspace(pb.Engine("pgrust", args.binary, server_args, args.conf))
    srv = pb.Server(ws, args.port).launch()
    conn = lambda db: pb.PgConn(ws.sockdir, args.port, database=db, timeout=300)  # noqa: E731
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "churn.json")
    doc = {"benchmark": "churn", "git_commit": pb.git("rev-parse", "HEAD"), "binary": args.binary,
           "conf": args.conf, "settings": gucs, "databases_per_cycle": args.databases,
           "template": {"tables": args.tables, "rows_per_table": args.rows}, "workload": WORKLOAD, "cycles": []}

    def sample(admin):
        m = pb.memory_sample(srv.proc.pid)
        dbs = int(admin.query("SELECT count(*) FROM pg_database WHERE datname LIKE 'tdb\\_%'")[0][0])
        du = subprocess.run(["du", "-sk", srv.datadir], capture_output=True, text=True).stdout
        return {"footprint_bytes": m[pb.MEM_KEY], "rss_bytes": m["rss_bytes_sum"], "threads": m.get("thread_count"),
                "fds": pb.fd_count(srv.proc.pid), "ephemeral_databases": dbs, "datadir_bytes": int(du.split()[0]) * 1024}

    try:
        srv.wait_select1()
        admin = conn("postgres")
        admin.query("CREATE DATABASE %s" % eph.TEMPLATE)
        c = conn(eph.TEMPLATE)
        c.query(eph.template_sql(args.tables, args.rows))
        c.query("VACUUM ANALYZE")
        c.close()
        admin.query("SELECT pgrust_seal_template('%s')" % eph.TEMPLATE)
        time.sleep(3)
        doc["before"] = sample(admin)
        print("before: %.1f MB" % (doc["before"]["footprint_bytes"] / M), flush=True)

        for cycle in range(1, args.cycles + 1):
            t0 = time.perf_counter()
            trace = {}
            conns = []
            for i in range(args.databases):
                c = conn("%s%s__c%d_%d" % (eph.PREFIX, eph.TEMPLATE, cycle, i))
                c.query("SELECT 1")
                conns.append(c)
                # Stay under max_connections: the workload runs right away
                # when connections would otherwise pile up.
                if len(conns) >= 8:
                    for x in conns:
                        x.query(WORKLOAD)
                        x.close()
                    conns = []
            if cycle == 1:
                trace["after_create"] = sample(admin)
            for x in conns:
                x.query(WORKLOAD)
                x.close()
            t_created = time.perf_counter()
            if cycle == 1:
                trace["after_workload_and_disconnect"] = sample(admin)
            deadline = time.perf_counter() + args.grace + 300
            while time.perf_counter() < deadline:
                if admin.query("SELECT count(*) FROM pg_database WHERE datname LIKE 'tdb\\_%'")[0][0] == "0":
                    break
                time.sleep(0.5)
            t_reaped = time.perf_counter()
            time.sleep(1)
            row = sample(admin)
            row.update({"cycle": cycle, "create_and_use_s": round(t_created - t0, 2),
                        "reap_wait_s": round(t_reaped - t_created, 2)})
            if trace:
                trace["after_drop"] = dict(row)
                doc["first_cycle_trace"] = trace
            doc["cycles"].append(row)
            print("cycle %3d  footprint=%.1f MB rss=%.1f MB threads=%s fds=%s dbs=%d disk=%.0f MB  (%.0fs + %.0fs)" % (
                cycle, row["footprint_bytes"] / M, row["rss_bytes"] / M, row["threads"], row["fds"],
                row["ephemeral_databases"], row["datadir_bytes"] / M, row["create_and_use_s"], row["reap_wait_s"]),
                flush=True)
            with open(path, "w") as f:
                json.dump(doc, f, indent=2)
                f.write("\n")
        fp = [r["footprint_bytes"] for r in doc["cycles"]]
        if len(fp) >= 20:
            tail = fp[len(fp) // 2:]
            doc["summary"] = {
                "footprint_before_bytes": doc["before"]["footprint_bytes"],
                "footprint_after_cycle_1_bytes": fp[0], "footprint_after_last_cycle_bytes": fp[-1],
                "growth_per_cycle_second_half_bytes": (tail[-1] - tail[0]) / (len(tail) - 1),
                "growth_per_database_second_half_bytes": (tail[-1] - tail[0]) / (len(tail) - 1) / args.databases,
                "databases_created_total": args.databases * len(fp)}
        doc["server_log_tail"] = open(srv.log_path, errors="replace").read()[-1200:]
        with open(path, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        print("wrote", path)
    finally:
        import shutil
        shutil.copy(srv.log_path, os.path.join(args.out, "server.log"))
        srv.stop()
        ws.cleanup()


if __name__ == "__main__":
    main()
