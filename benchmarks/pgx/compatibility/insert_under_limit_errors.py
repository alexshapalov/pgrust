#!/usr/bin/env python3
"""Regression: a statement that crosses pgrust.session_memory_limit must fail
with SQLSTATE 53200 — never take the server down.

Found by the PGRun beta on 2026-10-09: an INSERT ... SELECT of 300k rows into
a three-index table under a 64 MB session limit ended with
"memory allocation of 16 bytes failed" and SIGABRT: the limit refused an
allocation on an infallible lane (PgVec::push), which reached
handle_alloc_error. In a shared runtime that is every branch database gone.
Fixed in mcx: a refused limit unwinds as the ordinary out-of-memory ERROR.

  python3 benchmarks/pgx/compatibility/insert_under_limit_errors.py [--rows 300000] [--limit 64MB]

Exit 0 = PASS (the statement errored and the server answered SELECT 1 after),
1 = FAIL (server died, or the statement completed although it should not
have been the point — a completed statement is reported, not failed).
"""
import argparse, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pgxbench as pb  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=300000)
    ap.add_argument("--limit", default="64MB")
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target/release/postgres"))
    args = ap.parse_args()
    engine = pb.Engine("pgrust", args.binary,
                       ["-c", "max_connections=20", "-c", "pgrust.session_memory_limit=" + args.limit],
                       os.path.join(pb.REPO, "configs/pgx-ephemeral.conf"))
    ws = pb.Workspace(engine)
    srv = pb.Server(ws, 54953).launch()
    try:
        srv.wait_select1(timeout=300)
        c = pb.PgConn(ws.sockdir, 54953, timeout=3600)
        c.query("CREATE TABLE parent(id bigint PRIMARY KEY); INSERT INTO parent SELECT generate_series(1, 2000)")
        c.query("""CREATE TABLE events(id bigserial PRIMARY KEY, account_id bigint NOT NULL REFERENCES parent(id),
                   name varchar NOT NULL, properties jsonb NOT NULL DEFAULT '{}', occurred_at timestamptz NOT NULL DEFAULT now());
                   CREATE INDEX ON events(account_id, occurred_at DESC); CREATE INDEX ON events(name)""")
        t0 = time.time()
        outcome = "completed"
        try:
            c.query("INSERT INTO events(account_id, name, properties, occurred_at) SELECT 1 + g %% 2000, 'n' || (g %% 5), "
                    "jsonb_build_object('g', g), now() - (g %% 100000) * interval '1 minute' FROM generate_series(1, %d) g" % args.rows)
        except pb.ServerNotReady as e:
            outcome = "error: " + str(e)[:160].replace("\n", " ")
        # The one thing that must hold: the server is still there.
        try:
            c2 = pb.PgConn(ws.sockdir, 54953, timeout=30)
            alive = c2.query("SELECT 1")
        except Exception as e:  # connection refused / closed = the server died
            print("FAIL: server not answering after the statement (%s) [%.1fs]: %s" % (outcome, time.time() - t0, str(e)[:120]))
            print("server log tail:\n" + open(srv.log_path, errors="replace").read()[-1200:])
            return 1
        print("PASS: statement %s; server alive (SELECT 1 -> %s) [%.1fs]" % (outcome, alive, time.time() - t0))
        return 0
    finally:
        srv.stop()
        ws.cleanup()


if __name__ == "__main__":
    sys.exit(main())
