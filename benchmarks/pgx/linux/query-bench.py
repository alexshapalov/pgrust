#!/usr/bin/env python3
"""Query performance: PostgreSQL 18 vs PgRust defaults vs the PGX profile.

One server per engine/config, same schema and data:
  accounts(100k), orders(500k, FK -> accounts, index on account_id),
  items(20 per order bucket via product_id), products(1k), events(jsonb, 100k)

Per operation: warm-up, then --iterations sequential executions from one
client; latency min/p50/p95/p99 (client-measured, includes the Python
client's own overhead, identical across engines). Then throughput: point
SELECTs from --clients concurrent clients for --seconds. Server CPU time and
peak PSS are recorded per engine.

Operations: point_select, indexed_lookup, insert, update, delete,
simple_join, multi_join, aggregation, sort, jsonb, transaction,
copy_10k (server-side COPY FROM a CSV file), create_index (fresh index on
orders, dropped after each run), migration (ALTER TABLE add column with a
default, add index, add FK, rename column, then undo).

Output: <out>/query-bench.json.
"""

import argparse
import json
import os
import random
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pgxbench as pb  # noqa: E402

M = 1048576.0

SCHEMA = """
CREATE TABLE accounts(id bigint PRIMARY KEY, email text NOT NULL, region int NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO accounts SELECT g, 'user' || g || '@example.com', g % 50, now() - (g || ' minutes')::interval FROM generate_series(1, 100000) g;
CREATE INDEX accounts_region ON accounts(region);
CREATE TABLE products(id int PRIMARY KEY, name text NOT NULL, price numeric(10,2) NOT NULL);
INSERT INTO products SELECT g, 'product ' || g, (g % 500) + 0.99 FROM generate_series(1, 1000) g;
CREATE TABLE orders(id bigint PRIMARY KEY, account_id bigint NOT NULL REFERENCES accounts(id), product_id int NOT NULL REFERENCES products(id), qty int NOT NULL, status text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO orders SELECT g, (g % 100000) + 1, (g % 1000) + 1, (g % 7) + 1, CASE g % 4 WHEN 0 THEN 'new' WHEN 1 THEN 'paid' WHEN 2 THEN 'shipped' ELSE 'done' END, now() - (g || ' seconds')::interval FROM generate_series(1, 500000) g;
CREATE INDEX orders_account ON orders(account_id);
CREATE TABLE events(id bigint PRIMARY KEY, account_id bigint NOT NULL, payload jsonb NOT NULL);
INSERT INTO events SELECT g, (g % 100000) + 1, jsonb_build_object('type', CASE g % 3 WHEN 0 THEN 'click' WHEN 1 THEN 'view' ELSE 'buy' END, 'amount', g % 1000, 'tags', jsonb_build_array('a' || (g % 10), 'b' || (g % 7))) FROM generate_series(1, 100000) g;
CREATE TABLE copy_target(id bigint, account_id bigint, note text);
CREATE TABLE scratch(id bigserial PRIMARY KEY, account_id bigint, note text);
"""
# VACUUM cannot run inside the implicit transaction of a multi-statement
# query string; it is sent on its own.
VACUUM_SQL = "VACUUM ANALYZE"


def ops(copy_file):
    r = random.Random(42)
    n = [0]

    def nid():
        n[0] += 1
        return n[0]
    return [
        ("point_select", lambda: "SELECT email FROM accounts WHERE id = %d" % r.randint(1, 100000)),
        ("indexed_lookup", lambda: "SELECT id, qty, status FROM orders WHERE account_id = %d" % r.randint(1, 100000)),
        ("insert", lambda: "INSERT INTO scratch(account_id, note) VALUES (%d, 'n')" % r.randint(1, 100000)),
        ("update", lambda: "UPDATE accounts SET region = region WHERE id = %d" % r.randint(1, 100000)),
        ("delete", lambda: "INSERT INTO scratch(id, account_id, note) VALUES (%d, 1, 'd'); DELETE FROM scratch WHERE id = %d"
         % (10_000_000 + nid(), 10_000_000 + n[0])),
        ("simple_join", lambda: "SELECT a.email, o.qty FROM accounts a JOIN orders o ON o.account_id = a.id WHERE a.id = %d"
         % r.randint(1, 100000)),
        ("multi_join", lambda: "SELECT p.name, sum(o.qty * p.price) FROM accounts a JOIN orders o ON o.account_id = a.id "
         "JOIN products p ON p.id = o.product_id WHERE a.region = %d AND a.id < 2000 GROUP BY p.name ORDER BY 2 DESC LIMIT 5"
         % r.randint(0, 49)),
        ("aggregation", lambda: "SELECT status, count(*), sum(qty) FROM orders GROUP BY status"),
        ("sort", lambda: "SELECT id FROM (SELECT id, created_at FROM orders ORDER BY created_at DESC, id OFFSET 100000 LIMIT 10) s"),
        ("jsonb", lambda: "SELECT payload->>'type', sum((payload->>'amount')::int) FROM events WHERE payload @> '{\"type\": \"buy\"}' GROUP BY 1"),
        ("transaction", lambda: "BEGIN; UPDATE accounts SET region = region WHERE id = %d; INSERT INTO scratch(account_id, note) VALUES (%d, 't'); "
         "SELECT count(*) FROM orders WHERE account_id = %d; COMMIT" % (r.randint(1, 100000), r.randint(1, 100000), r.randint(1, 100000))),
        ("copy_10k", lambda: "TRUNCATE copy_target; COPY copy_target FROM '%s' WITH (FORMAT csv)" % copy_file),
        ("create_index", lambda: "CREATE INDEX qb_idx ON orders(product_id, created_at); DROP INDEX qb_idx"),
        ("migration", lambda: "ALTER TABLE products ADD COLUMN sku text DEFAULT 'none' NOT NULL; CREATE INDEX products_sku ON products(sku); "
         "ALTER TABLE scratch ADD CONSTRAINT scratch_acct FOREIGN KEY (account_id) REFERENCES accounts(id) NOT VALID; "
         "ALTER TABLE products RENAME COLUMN sku TO sku2; ALTER TABLE scratch DROP CONSTRAINT scratch_acct; "
         "DROP INDEX products_sku; ALTER TABLE products DROP COLUMN sku2"),
    ]


ITER = {"aggregation": 30, "sort": 30, "jsonb": 30, "multi_join": 100, "copy_10k": 20, "create_index": 10, "migration": 20}


def cpu_ns(s):
    return s["cpu_user_ns_sum"] + s["cpu_system_ns_sum"]


def run_engine(label, engine, args, copy_file, port):
    ws = pb.Workspace(engine)
    srv = pb.Server(ws, port).launch()
    out = {"label": label, "engine": engine.describe(), "ops": {}}
    try:
        srv.wait_select1(timeout=300)
        pid = srv.proc.pid
        c = pb.PgConn(ws.sockdir, port, timeout=1800)
        t0 = time.perf_counter()
        c.query(SCHEMA)
        c.query(VACUUM_SQL)
        out["load_s"] = round(time.perf_counter() - t0, 2)
        peak = [pb.memory_sample(pid).get(pb.MEM_KEY, 0)]
        stop = [False]

        def watch():
            while not stop[0]:
                peak[0] = max(peak[0], pb.memory_sample(pid).get(pb.MEM_KEY, 0) or 0)
                time.sleep(0.5)
        w = threading.Thread(target=watch, daemon=True)
        w.start()
        for name, mk in ops(copy_file):
            iters = ITER.get(name, args.iterations)
            for _ in range(max(3, iters // 10)):
                c.query(mk())
            lat = []
            s0 = pb.memory_sample(pid)
            for _ in range(iters):
                q = mk()
                t = time.perf_counter()
                c.query(q)
                lat.append(time.perf_counter() - t)
            s1 = pb.memory_sample(pid)
            out["ops"][name] = {"iterations": iters,
                                "latency_ms": {k: round(v * 1e3, 3) for k, v in pb.summarize(lat).items() if k != "n"},
                                "server_cpu_ms_per_op": round((cpu_ns(s1) - cpu_ns(s0)) / 1e6 / iters, 3)}
            print("%-14s %-15s p50=%8.3f p95=%8.3f p99=%8.3f ms  cpu/op=%.3f ms" % (
                label, name, out["ops"][name]["latency_ms"]["p50"], out["ops"][name]["latency_ms"]["p95"],
                out["ops"][name]["latency_ms"]["p99"], out["ops"][name]["server_cpu_ms_per_op"]), flush=True)
        # throughput: point selects from N clients
        counts = [0] * args.clients
        end = time.perf_counter() + args.seconds

        def client(i):
            cc = pb.PgConn(ws.sockdir, port, timeout=300)
            rr = random.Random(i)
            while time.perf_counter() < end:
                cc.query("SELECT email FROM accounts WHERE id = %d" % rr.randint(1, 100000))
                counts[i] += 1
            cc.close()
        s0 = pb.memory_sample(pid)
        t0 = time.perf_counter()
        ths = [threading.Thread(target=client, args=(i,)) for i in range(args.clients)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        wall = time.perf_counter() - t0
        s1 = pb.memory_sample(pid)
        out["throughput"] = {"clients": args.clients, "qps": round(sum(counts) / wall, 1),
                             "server_cpu_cores": round((cpu_ns(s1) - cpu_ns(s0)) / 1e9 / wall, 2)}
        stop[0] = True
        w.join()
        out["peak_pss_mb"] = round(peak[0] / M, 1)
        out["idle_pss_mb_after"] = round((pb.memory_sample(pid).get(pb.MEM_KEY) or 0) / M, 1)
        print("%-14s throughput %d clients: %.0f qps, server %.2f cores, peak PSS %.0f MB" % (
            label, args.clients, out["throughput"]["qps"], out["throughput"]["server_cpu_cores"], out["peak_pss_mb"]), flush=True)
        c.close()
    finally:
        srv.stop()
        ws.cleanup()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--profile", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--iterations", type=int, default=500)
    ap.add_argument("--clients", type=int, default=4)
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--engines", default="postgres,postgres-nodurable,pgrust,pgx")
    ap.add_argument("--port", type=int, default=54600)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    copy_dir = tempfile.mkdtemp(prefix="qb-copy-")
    copy_file = os.path.join(copy_dir, "rows.csv")
    with open(copy_file, "w") as f:
        for i in range(10000):
            f.write("%d,%d,note %d\n" % (i, i % 100000 + 1, i))
    os.chmod(copy_dir, 0o755)
    os.chmod(copy_file, 0o644)
    common = ["-c", "max_connections=40"]
    engines = {
        "postgres": ("postgres18", lambda: pb.Engine("postgres", os.path.join(pb.pg18_prefix(), "bin", "postgres"), common)),
        # PostgreSQL with the PGX profile's durability settings: the fair
        # reference for write latency (the profile runs fsync off).
        "postgres-nodurable": ("postgres18-nodur", lambda: pb.Engine(
            "postgres", os.path.join(pb.pg18_prefix(), "bin", "postgres"),
            common + ["-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"])),
        "pgrust": ("pgrust-default", lambda: pb.Engine("pgrust", args.binary, common)),
        "pgx": ("pgx-profile", lambda: pb.Engine("pgrust", args.binary, common, args.profile)),
    }
    doc = {"benchmark": "query-bench", "git_commit": pb.git("rev-parse", "HEAD"), "iterations": args.iterations,
           "clients": args.clients, "runs": []}
    for i, key in enumerate(args.engines.split(",")):
        label, mk = engines[key]
        doc["runs"].append(run_engine(label, mk(), args, copy_file, args.port + i))
        with open(os.path.join(args.out, "query-bench.json"), "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
    print("wrote", os.path.join(args.out, "query-bench.json"))


if __name__ == "__main__":
    main()
