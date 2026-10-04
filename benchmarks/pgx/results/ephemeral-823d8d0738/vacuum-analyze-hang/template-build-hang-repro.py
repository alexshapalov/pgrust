"""Repro: building the benchmark template (50 tables, indexes, inserts in one
multi-statement query, then VACUUM ANALYZE) intermittently hangs the server."""
import sys, os, time, socket, subprocess, argparse
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, ".")
import pgxbench as pb, ephemeral as eph
ap = argparse.ArgumentParser(); ap.add_argument("--tries", type=int, default=10); ap.add_argument("--guc", action="append", default=[]); ap.add_argument("--engine", default="pgrust")
a = ap.parse_args()
sa = []
for g in a.guc: sa += ["-c", g]
binary = os.path.join(pb.REPO, "target/release/postgres") if a.engine == "pgrust" else os.path.join(pb.pg18_prefix(), "bin/postgres")
hangs, where = 0, []
for i in range(a.tries):
    ws = pb.Workspace(pb.Engine(a.engine, binary, sa)); srv = pb.Server(ws, 54361).launch()
    stage = "start"
    try:
        srv.wait_select1(); adm = srv.connect(); adm.sock.settimeout(30)
        stage = "create database"; adm.query("CREATE DATABASE tpl_app")
        c = pb.PgConn(ws.sockdir, 54361, database="tpl_app"); c.sock.settimeout(30)
        stage = "schema+inserts"; c.query(eph.template_sql(50, 200))
        stage = "vacuum analyze"; c.query("VACUUM ANALYZE")
        stage = "ok"
    except (socket.timeout, TimeoutError):
        hangs += 1; where.append(stage)
        subprocess.run(["sample", str(srv.proc.pid), "1", "-file", "/tmp/eph-evidence/hang-%s-%d.txt" % (a.engine, i)], capture_output=True)
        srv.proc.kill()
    finally:
        srv.stop(); ws.cleanup()
print(a.engine, a.guc, "tries", a.tries, "hangs", hangs, where, flush=True)
