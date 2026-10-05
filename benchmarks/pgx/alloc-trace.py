#!/usr/bin/env python3
"""Which allocations survive database create/use/drop cycles?

Needs a build with the debug allocation tracker (debug assertions on) and
symbols:

  CARGO_PROFILE_RELEASE_STRIP=false CARGO_PROFILE_RELEASE_DEBUG=line-tables-only \\
  CARGO_PROFILE_RELEASE_DEBUG_ASSERTIONS=true CARGO_PROFILE_RELEASE_LTO=off \\
  CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 \\
    cargo build --release --locked --bin postgres --target-dir target-track

  alloc-trace.py [databases per cycle, default 50]

Runs 3 warm-up cycles, asks the server for a dump of live allocations
(SIGWINCH), runs 6 more cycles, dumps again, and prints the growth between
the two dumps summed by call site, in bytes and blocks per database.
Symbolication uses macOS `atos`. Output also in /tmp/leaktrace.json.
"""
import sys, time, os, re, signal, subprocess, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pgxbench as pb, ephemeral as eph, churn
BIN = os.path.join(pb.REPO, "target-track/release/postgres")
os.environ["PGRUST_ALLOC_TRACK"] = "all,dump"
gucs = ["pgrust.ephemeral_db_prefix=tdb_", "pgrust.ephemeral_db_mint_roles=postgres", "pgrust.ephemeral_db_grace=2"]
args = []
for g in gucs: args += ["-c", g]
ws = pb.Workspace(pb.Engine("pgrust", BIN, args, os.path.join(pb.REPO, "configs/pgx-ephemeral.conf")))
srv = pb.Server(ws, 54440).launch()
conn = lambda db: pb.PgConn(ws.sockdir, 54440, database=db, timeout=600)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 50
def cycle(tag):
    for i in range(N):
        c = conn("tdb_tpl_app__%s_%d" % (tag, i)); c.query(churn.WORKLOAD); c.close()
    while admin.query("SELECT count(*) FROM pg_database WHERE datname LIKE 'tdb\\_%'")[0][0] != "0": time.sleep(0.5)
    time.sleep(1)
def dump():
    os.kill(srv.proc.pid, signal.SIGWINCH)
    time.sleep(0.5); admin.query("SELECT 1"); time.sleep(3)
    log = open(srv.log_path, errors="replace").read()
    last = log.rfind("ALLOC-TRACK dump (mid-run)")
    head = re.search(r"ALLOC-TRACK dump \(mid-run\): (\d+) live tracked blocks, (\d+) bytes, slide=0x([0-9a-f]+)", log[last:])
    rows = re.findall(r"ALLOC-TRACK leak: thread=(.*?) n=(\d+) bytes=(\d+) bt=(.*)", log[last:])
    return int(head.group(1)), int(head.group(2)), int(head.group(3), 16), {(t, bt): (int(n), int(b)) for t, n, b, bt in rows}
try:
    srv.wait_select1(timeout=300)
    admin = conn("postgres")
    admin.query("CREATE DATABASE tpl_app"); c = conn("tpl_app"); c.query(eph.template_sql(50, 200)); c.query("VACUUM ANALYZE"); c.close()
    admin.query("SELECT pgrust_seal_template('tpl_app')")
    for k in range(3): cycle("w%d" % k); print("warm cycle", k, pb.memory_sample(srv.proc.pid)[pb.MEM_KEY]/1048576, flush=True)
    b1, by1, slide, d1 = dump(); print("dump1 blocks", b1, "bytes", by1, flush=True)
    K = 6
    for k in range(K): cycle("m%d" % k); print("cycle", k, pb.memory_sample(srv.proc.pid)[pb.MEM_KEY]/1048576, flush=True)
    b2, by2, slide, d2 = dump(); print("dump2 blocks", b2, "bytes", by2, "delta per db: blocks %.1f bytes %.0f" % ((b2-b1)/(K*N), (by2-by1)/(K*N)), flush=True)
    growth = []
    for key, (n2, bytes2) in d2.items():
        n1, bytes1 = d1.get(key, (0, 0))
        if bytes2 - bytes1 > 0: growth.append((bytes2 - bytes1, n2 - n1, key, key in d1))
    growth.sort(reverse=True)
    out = []
    alladdr = sorted({x for _, _, (t, bt), _ in growth for x in bt.split()})
    symout = subprocess.run(["atos", "-o", BIN, "-s", hex(slide)] + alladdr, capture_output=True, text=True).stdout.splitlines()
    symmap = dict(zip(alladdr, [re.sub(r"::h[0-9a-f]{16}.*$", "", re.sub(r" \(in postgres\).*$", "", x)) for x in symout]))
    skip = ("alloc_track", "alloc::", "hashbrown", "_RNv", "core::", "std::", "__rust", "_$LT$")
    import collections
    cat_b, cat_n = collections.Counter(), collections.Counter()
    for gb, gn, (t, bt), was in growth:
        frames = [symmap.get(x, x) for x in bt.split()]
        key = [f for f in frames if not f.startswith(skip)]
        site = key[0] if key else "?"
        if site.startswith("mcx::") and len(key) > 1 and key[1].startswith("mcx::"): site = key[0] + " < " + key[1]
        cat_b[site] += gb; cat_n[site] += gn
        out.append({"bytes_per_db": gb/(K*N), "blocks_per_db": gn/(K*N), "thread": t, "stack": frames})
    listed = sum(cat_b.values())
    print("growth visible in the top-80 rows: %.0f B/db of %.0f B/db total" % (listed/(K*N), (by2-by1)/(K*N)))
    for site, bts in cat_b.most_common(14):
        print("%8.0f B/db %6.1f blk/db  %s" % (bts/(K*N), cat_n[site]/(K*N), site))
    json.dump({"dbs": K*N, "blocks1": b1, "bytes1": by1, "blocks2": b2, "bytes2": by2, "growth": out}, open("/tmp/leaktrace.json", "w"), indent=1)
finally:
    import shutil; shutil.copy(srv.log_path, "/tmp/leaktrace-server.log")
    srv.stop(); ws.cleanup()
