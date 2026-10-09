#!/usr/bin/env python3
"""Regression: a large non-HOT UPDATE on a packed, multi-index table must not
hold memory per modified row.

Found by the PGRun beta on 2026-10-09 (synthetic project, 100k-row UPDATE of a
600k-row restored template: SQLSTATE 53200 at pgrust.session_memory_limit =
128MB; PostgreSQL 18 ran the same statement in 145 ms at ~zero growth).
Cause: _bt_bottomupdel_pass / _bt_simpledel_pass / heap_index_delete_tuples
allocated their per-page scratch arrays in the executor's query context,
which is a bump context in PGX (frees are no-ops until the statement ends),
so every bottom-up deletion pass leaked ~20 KB. Fixed by allocating that
scratch as plain Vec (tableam_vocab::TM_IndexDeleteOp).

Shape that triggers it: rows packed by VACUUM FREEZE (a sealed template),
three btree indexes so updates are non-HOT, and enough rows that index pages
fill and bottom-up deletion runs.

  python3 benchmarks/pgx/compatibility/update_packed_table_memory.py [--rows 300000] [--limit 64MB]

Exit 0 = PASS (the UPDATE completed under the session limit), 1 = FAIL.
"""
import argparse, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pgxbench as pb  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=300000)
    ap.add_argument("--limit", default="64MB", help="pgrust.session_memory_limit for the run")
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target/release/postgres"))
    args = ap.parse_args()
    # The limit is applied AFTER loading (ALTER SYSTEM + reload): the load path
    # has its own case, insert_under_limit_errors.py; this one isolates the UPDATE.
    engine = pb.Engine("pgrust", args.binary, ["-c", "max_connections=20"],
                       os.path.join(pb.REPO, "configs/pgx-ephemeral.conf"))
    ws = pb.Workspace(engine)
    srv = pb.Server(ws, 54952).launch()
    def step(label, sql):
        print("..", label, flush=True)
        return c.query(sql)
    try:
        srv.wait_select1(timeout=300)
        c = pb.PgConn(ws.sockdir, 54952, timeout=3600)
        step("parent", "CREATE TABLE parent(id bigint PRIMARY KEY); INSERT INTO parent SELECT generate_series(1, 2000)")
        step("events", """CREATE TABLE events(id bigserial PRIMARY KEY, account_id bigint NOT NULL REFERENCES parent(id),
                   name varchar NOT NULL, properties jsonb NOT NULL DEFAULT '{}', occurred_at timestamptz NOT NULL DEFAULT now());
                   CREATE INDEX ON events(account_id, occurred_at DESC); CREATE INDEX ON events(name)""")
        step("insert", "INSERT INTO events(account_id, name, properties, occurred_at) SELECT 1 + g %% 2000, 'n' || (g %% 5), "
                "jsonb_build_object('g', g), now() - (g %% 100000) * interval '1 minute' FROM generate_series(1, %d) g" % args.rows)
        step("vacuum freeze", "VACUUM FREEZE events")   # packed pages, like a sealed template
        step("analyze", "ANALYZE events")
        step("limit", "ALTER SYSTEM SET pgrust.session_memory_limit = '%s'" % args.limit)   # one statement: no transaction block
        step("reload", "SELECT pg_reload_conf()")
        time.sleep(1.0)
        c = pb.PgConn(ws.sockdir, 54952, timeout=3600)   # a fresh session picks the limit up for sure
        print(".. effective limit:", c.query("SHOW pgrust.session_memory_limit"))
        n = args.rows // 2
        t0 = time.time()
        try:
            c.query("SET enable_seqscan = off; SET enable_bitmapscan = off; UPDATE events SET name = 'x' WHERE id <= %d" % n)
        except Exception as e:  # pgxbench raises on an ErrorResponse
            print("FAIL: UPDATE of %d rows failed under session limit %s after %.1fs: %s" % (n, args.limit, time.time() - t0, str(e)[:200]))
            return 1
        got = c.query("SELECT count(*) FROM events WHERE name = 'x'")
        print("PASS: UPDATE of %d rows completed in %.1fs under session limit %s (rows now 'x': %s)" % (n, time.time() - t0, args.limit, got))
        return 0
    except Exception:
        try:
            print("server log tail:\n" + open(srv.log_path, errors="replace").read()[-1500:])
        except OSError:
            pass
        raise
    finally:
        srv.stop()
        ws.cleanup()


if __name__ == "__main__":
    sys.exit(main())
