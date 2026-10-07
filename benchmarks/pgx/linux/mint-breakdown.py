#!/usr/bin/env python3
"""Where does a mint's time go? Cold and warm, one request at a time.

Server started with PGRUST_MINT_TIMING=1, PGX profile, a sealed template.

  cold   pool size 0: N sequential mint-on-connect requests, each a fresh
         name; per request the client measures connect -> SELECT 1, and the
         server log gives the janitor's stages (pre-checkpoint, directory
         clone, post-checkpoint, the rest of the transaction) and the
         connecting backend's wait.
  warm   pool size --pool: N sequential requests with --gap seconds between
         them (the pool refills); a spare handout has no clone stage.
  reconnect  N connections to one existing database (the floor: connection
         startup + SELECT 1 with no mint).

Derived per request: other = client total - backend wait, i.e. connection
setup, authentication and the new database's first-session initialization.
Output: <out>/mint-breakdown.json.
"""

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pgxbench as pb  # noqa: E402
import ephemeral as eph  # noqa: E402


def ms(xs):
    return {k: round(v, 2) for k, v in pb.summarize(xs).items() if k != "n"} if xs else None


def run(args, pool, mode):
    os.environ["PGRUST_MINT_TIMING"] = "1"
    gucs = ["pgrust.ephemeral_db_prefix=" + eph.PREFIX, "pgrust.ephemeral_db_mint_roles=postgres",
            "pgrust.ephemeral_db_grace=86400", "pgrust.ephemeral_db_pool_size=%d" % pool, "max_connections=40"]
    sargs = []
    for g in gucs:
        sargs += ["-c", g]
    ws = pb.Workspace(pb.Engine("pgrust", args.binary, sargs, args.conf))
    srv = pb.Server(ws, args.port).launch()
    conn = lambda db: pb.PgConn(ws.sockdir, args.port, database=db, timeout=300)  # noqa: E731
    out = {"mode": mode, "pool": pool, "requests": []}
    try:
        srv.wait_select1()
        admin = conn("postgres")
        admin.query("CREATE DATABASE %s" % eph.TEMPLATE)
        c = conn(eph.TEMPLATE)
        c.query(eph.template_sql(args.tables, args.rows))
        c.query("VACUUM ANALYZE")
        c.close()
        admin.query("SELECT pgrust_seal_template('%s')" % eph.TEMPLATE)
        conn("%s%s__first" % (eph.PREFIX, eph.TEMPLATE)).close()
        time.sleep(3 if pool else 1)
        names = []
        for i in range(args.n + 3):
            name = "%s%s__%s%d" % (eph.PREFIX, eph.TEMPLATE, mode[0], i)
            if mode == "reconnect":
                name = "%s%s__first" % (eph.PREFIX, eph.TEMPLATE)
            t0 = time.perf_counter()
            s = conn(name)
            s.query("SELECT 1")
            total = (time.perf_counter() - t0) * 1e3
            s.close()
            if i >= 3:  # warm-up
                names.append((name, total))
            time.sleep(args.gap if mode == "warm" else 0.2)
        time.sleep(1)
        log = open(srv.log_path, errors="replace").read()
        mints = {m.group(1): {k: int(v) for k, v in re.findall(r"(\w+)_us=(\d+)", m.group(0))}
                 for m in re.finditer(r'pgrust mint timing: db="([^"]+)"[^\n]*', log)}
        waits = {m.group(2): int(m.group(1)) for m in re.finditer(r'backend waited (\d+) us for "([^"]+)"', log)}
        for name, total in names:
            r = {"client_total_ms": round(total, 2)}
            if name in waits:
                r["backend_wait_ms"] = round(waits[name] / 1e3, 2)
                r["connect_and_init_ms"] = round(total - waits[name] / 1e3, 2)
            if mode == "reconnect":
                out["requests"].append(r)
                continue
            if name in mints:
                r.update({"mint_" + k + "_ms": round(v / 1e3, 2) for k, v in mints[name].items()})
            out["requests"].append(r)
        keys = sorted({k for r in out["requests"] for k in r})
        out["summary"] = {k: ms([r[k] for r in out["requests"] if k in r]) for k in keys}
    finally:
        srv.stop()
        ws.cleanup()
    s = out["summary"]
    print("%-9s pool=%-2d " % (mode, pool) + "  ".join(
        "%s p50=%s" % (k.replace("_ms", ""), v["p50"]) for k, v in s.items() if v), flush=True)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--conf", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--pool", type=int, default=8)
    ap.add_argument("--gap", type=float, default=1.0)
    ap.add_argument("--tables", type=int, default=50)
    ap.add_argument("--rows", type=int, default=200)
    ap.add_argument("--port", type=int, default=54720)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    doc = {"benchmark": "mint-breakdown", "git_commit": pb.git("rev-parse", "HEAD"),
           "template": {"tables": args.tables, "rows": args.rows}, "runs": []}
    for pool, mode in ((0, "cold"), (args.pool, "warm"), (0, "reconnect")):
        doc["runs"].append(run(args, pool, mode))
        with open(os.path.join(args.out, "mint-breakdown.json"), "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")


if __name__ == "__main__":
    main()
