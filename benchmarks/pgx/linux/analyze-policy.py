#!/usr/bin/env python3
"""Does a branch need ANALYZE after it grows, if autovacuum is off?

A template with fresh statistics: orders(200k rows) with a skewed status
column ('done' 97%, 'new' 1%, ...) and an index on (status). A branch then
grows orders by 0/10/25/50/100% with rows that are all status='new' (the
agent-workload shape: new rows concentrate where the template was rare).
For each growth level, with autovacuum off:

  stale     the template's statistics as copied
  analyzed  after an explicit ANALYZE orders

record, for three queries, the planner's row estimate vs the actual rows
(EXPLAIN ANALYZE), the plan shape (top node and scan types), and the
execution time (median of 5). Output: <out>/analyze-policy.json.
"""

import argparse
import json
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pgxbench as pb  # noqa: E402

QUERIES = {
    "status_new_count": "SELECT count(*) FROM orders WHERE status = 'new'",
    "status_new_join": "SELECT a.region, count(*) FROM orders o JOIN accounts a ON a.id = o.account_id "
                       "WHERE o.status = 'new' GROUP BY a.region",
    "recent_by_account": "SELECT * FROM orders WHERE status = 'new' AND account_id < 500 ORDER BY id DESC LIMIT 50",
}

SCHEMA = """
CREATE TABLE accounts(id int PRIMARY KEY, region int NOT NULL);
INSERT INTO accounts SELECT g, g % 20 FROM generate_series(1, 20000) g;
CREATE TABLE orders(id bigint PRIMARY KEY, account_id int NOT NULL, status text NOT NULL, amount int NOT NULL);
INSERT INTO orders SELECT g, (g % 20000) + 1,
  CASE WHEN g % 100 = 0 THEN 'new' WHEN g % 100 = 1 THEN 'paid' WHEN g % 100 = 2 THEN 'shipped' ELSE 'done' END, g % 997
  FROM generate_series(1, 200000) g;
CREATE INDEX orders_status ON orders(status);
CREATE INDEX orders_account ON orders(account_id);
"""
# VACUUM cannot run inside the implicit transaction of a multi-statement
# query string; it is sent on its own.
VACUUM_SQL = "VACUUM ANALYZE"


def explain(c, sql):
    rows = c.query("EXPLAIN (ANALYZE, FORMAT TEXT) " + sql)
    text = "\n".join(r[0] for r in rows)
    first = text.splitlines()[0]
    est = re.search(r"rows=(\d+)", first)
    act = re.search(r"actual time=[\d.]+\.\.[\d.]+ rows=(\d+)", first)
    scans = sorted(set(re.findall(r"(Seq Scan|Index Scan|Index Only Scan|Bitmap Heap Scan|Bitmap Index Scan) on (\w+)", text)))
    joins = sorted(set(re.findall(r"(Hash Join|Nested Loop|Merge Join)", text)))
    # The status = 'new' filter node's estimate vs actual (where misestimation starts).
    leaf = None
    for line in text.splitlines():
        if "status = 'new'" in line or "orders_status" in line:
            m1 = re.search(r"rows=(\d+)", line)
            m2 = re.search(r"actual time=[\d.]+\.\.[\d.]+ rows=(\d+)", line)
            if m1 and m2:
                leaf = (int(m1.group(1)), int(m2.group(1)))
                break
    return {"top": first.split("  (")[0].strip(), "estimated_rows": int(est.group(1)) if est else None,
            "actual_rows": int(act.group(1)) if act else None, "filter_node_est_actual": leaf,
            "scans": [" on ".join(s) for s in scans], "joins": joins}


def timed(c, sql, n=5):
    import time
    xs = []
    for _ in range(n):
        t = time.perf_counter()
        c.query(sql)
        xs.append(time.perf_counter() - t)
    return round(statistics.median(xs) * 1e3, 2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--conf", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--growth", default="0,10,25,50,100")
    ap.add_argument("--port", type=int, default=54700)
    args = ap.parse_args()
    ws = pb.Workspace(pb.Engine("pgrust", args.binary, ["-c", "autovacuum=off", "-c", "max_connections=20"], args.conf))
    srv = pb.Server(ws, args.port).launch()
    connect = lambda db: pb.PgConn(ws.sockdir, args.port, database=db, timeout=600)  # noqa: E731
    doc = {"benchmark": "analyze-policy", "git_commit": pb.git("rev-parse", "HEAD"), "levels": []}
    try:
        srv.wait_select1()
        admin = connect("postgres")
        admin.query("CREATE DATABASE tpl")
        c = connect("tpl")
        c.query(SCHEMA)
        c.query(VACUUM_SQL)
        c.close()
        for i, g in enumerate(int(x) for x in args.growth.split(",")):
            db = "branch_%d" % g
            admin.query("CREATE DATABASE %s TEMPLATE tpl" % db)
            c = connect(db)
            if g:
                n = 200000 * g // 100
                c.query("INSERT INTO orders SELECT 1000000 + s, (s % 20000) + 1, 'new', s % 997 FROM generate_series(1, %d) s" % n)
            row = {"growth_percent": g, "stale": {}, "analyzed": {}}
            for state in ("stale", "analyzed"):
                if state == "analyzed":
                    c.query("ANALYZE orders")
                for name, sql in QUERIES.items():
                    e = explain(c, sql)
                    e["median_ms"] = timed(c, sql)
                    row[state][name] = e
            doc["levels"].append(row)
            for name in QUERIES:
                s, a = row["stale"][name], row["analyzed"][name]
                print("growth %3d%% %-18s stale: est %s act %s %6.2f ms %s | analyzed: est %s %6.2f ms %s" % (
                    g, name, s["filter_node_est_actual"], s["actual_rows"], s["median_ms"], s["scans"],
                    a["filter_node_est_actual"], a["median_ms"], a["scans"]), flush=True)
            c.close()
            admin.query("DROP DATABASE %s" % db)
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "analyze-policy.json"), "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
    finally:
        srv.stop()
        ws.cleanup()


if __name__ == "__main__":
    main()
