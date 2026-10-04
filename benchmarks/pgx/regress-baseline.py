#!/usr/bin/env python3
"""Regression baseline for PGX (LLM.md task 2).

Runs PostgreSQL's own regression suite (vendored, unmodified, under
crates/postgres-18.6-reference/src/test/regress) with the stock PostgreSQL 18
`pg_regress` in --use-existing mode against a server built from this tree.

This fork does not contain upstream pgrust's driver (scripts/pg-regress-fast.sh)
or its comparator, which honours the `-- pgrust:rowsort` annotations in
regress/overlay/sql. So two verdicts are recorded per test:

  byte_exact       stock pg_regress says "ok" (stricter than the upstream gate)
  order_only_diff  not byte-exact, but the output is the expected output with
                   lines in a different order (what rowsort would forgive;
                   an approximation of the upstream gate, not the gate itself)

Output: <out>/regress.json plus pg_regress's own regression.diffs/.out.
"""

import argparse
import collections
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pgxbench as pb  # noqa: E402

REGRESS_SRC = os.path.join(pb.REPO, "crates", "postgres-18.6-reference", "src", "test", "regress")
RESULT_RE = re.compile(r"^(not ok|ok)\s+\d+\s+[-+]\s+(\S+)\s+(\d+) ms")
FAILED_RE = re.compile(r"^(not ok|ok)\s+\d+\s+[-+]\s+(\S+)")


def order_only(test, outdir):
    """True if results/<test>.out equals some expected variant up to line order."""
    try:
        with open(os.path.join(outdir, "results", test + ".out"), errors="replace") as f:
            got = collections.Counter(f.read().splitlines())
    except OSError:
        return False
    variants = [os.path.join(REGRESS_SRC, "expected", test + ".out")]
    variants += glob.glob(os.path.join(REGRESS_SRC, "expected", test + "_[0-9].out"))
    for path in variants:
        try:
            with open(path, errors="replace") as f:
                if collections.Counter(f.read().splitlines()) == got:
                    return True
        except OSError:
            pass
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", choices=["pgrust", "postgres"], default="pgrust")
    ap.add_argument("--binary")
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=54330)
    ap.add_argument("--schedule", default=os.path.join(REGRESS_SRC, "parallel_schedule"))
    ap.add_argument("--server-arg", action="append", default=[],
                    help="extra server argument (repeatable); makes the run a diagnostic, not a baseline")
    ap.add_argument("--max-connections", type=int, default=None,
                    help="pg_regress --max-connections (limit tests run in parallel)")
    args = ap.parse_args()

    prefix = pb.pg18_prefix()
    binary = args.binary or (os.path.join(pb.REPO, "target", "release", "postgres")
                             if args.engine == "pgrust" else os.path.join(prefix, "bin", "postgres"))
    engine = pb.Engine(args.engine, binary, args.server_arg)
    pg_regress = os.path.join(prefix, "lib", "postgresql", "pgxs", "src", "test", "regress", "pg_regress")
    pkglibdir = subprocess.run([os.path.join(prefix, "bin", "pg_config"), "--pkglibdir"],
                               capture_output=True, text=True, check=True).stdout.strip()

    out = os.path.abspath(args.out)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    ws = pb.Workspace(engine)
    srv = pb.Server(ws, args.port).launch()
    crashed = False
    regress_lib_found = False
    try:
        srv.wait_select1()
        # --use-existing skips pg_regress's create_database(); do what it does.
        conn = srv.connect()
        conn.query('CREATE DATABASE "regression" TEMPLATE=template0')
        conn.query('ALTER DATABASE "regression" SET lc_messages TO \'C\';'
                   'ALTER DATABASE "regression" SET lc_monetary TO \'C\';'
                   'ALTER DATABASE "regression" SET lc_numeric TO \'C\';'
                   'ALTER DATABASE "regression" SET lc_time TO \'C\';'
                   'ALTER DATABASE "regression" SET bytea_output TO \'hex\';'
                   'ALTER DATABASE "regression" SET timezone_abbreviations TO \'Default\';')
        # pgrust serves regress.c in-process, keyed on the pkglibdir it derives
        # from its own executable path; probe for the directory it accepts.
        suffix = ".dylib" if sys.platform == "darwin" else ".so"
        exe_dir = os.path.dirname(engine.binary)
        candidates = [pkglibdir, os.path.normpath(os.path.join(exe_dir, "..", "lib")),
                      os.path.normpath(os.path.join(exe_dir, "..", "lib", "postgresql"))]
        rows = conn.query("SELECT setting FROM pg_config WHERE name = 'PKGLIBDIR'")
        if rows and rows[0][0]:
            candidates.insert(0, rows[0][0])
        for cand in candidates:
            try:
                conn.query("CREATE FUNCTION pgx_probe(oid, oid) RETURNS bool AS '%s/regress%s', "
                           "'binary_coercible' LANGUAGE C STRICT" % (cand, suffix))
            except pb.ServerNotReady:
                conn.close()
                conn = srv.connect()
                continue
            conn.query("DROP FUNCTION pgx_probe(oid, oid)")
            pkglibdir = cand
            regress_lib_found = True
            break
        conn.close()
        cmd = [pg_regress, "--use-existing", "--host", ws.sockdir, "--port", str(args.port),
               "--user", "postgres", "--bindir", os.path.join(prefix, "bin"),
               "--inputdir", REGRESS_SRC, "--expecteddir", REGRESS_SRC, "--outputdir", out,
               "--dlpath", pkglibdir, "--schedule", args.schedule]
        if args.max_connections:
            cmd += ["--max-connections", str(args.max_connections)]
        t0 = time.time()
        with open(os.path.join(out, "pg_regress.log"), "w") as log:
            rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
        elapsed = time.time() - t0
        crashed = srv.proc.poll() is not None
    finally:
        shutil.copy(srv.log_path, os.path.join(out, "server.log"))
        srv.stop()
        ws.cleanup()

    tests = []
    with open(os.path.join(out, "pg_regress.log")) as f:
        for line in f:
            m = RESULT_RE.match(line) or FAILED_RE.match(line)
            if not m:
                continue
            name, ok = m.group(2), m.group(1) == "ok"
            tests.append({"test": name, "byte_exact": ok,
                          "order_only_diff": (not ok) and order_only(name, out),
                          "ms": int(m.group(3)) if m.re is RESULT_RE else None})
    scheduled = []
    with open(args.schedule) as f:
        for line in f:
            if line.startswith("test:"):
                scheduled += line.split(":", 1)[1].split()
    ran = {t["test"] for t in tests}
    summary = {
        "scheduled": len(scheduled),
        "reported": len(tests),
        "byte_exact_pass": sum(t["byte_exact"] for t in tests),
        "order_only_diff": sum(t["order_only_diff"] for t in tests),
        "real_diff": sum(not t["byte_exact"] and not t["order_only_diff"] for t in tests),
        "not_reported": sorted(set(scheduled) - ran),
        "pg_regress_exit_code": rc,
        "server_died_during_run": crashed,
        "elapsed_s": round(elapsed, 1),
    }
    doc = {"benchmark": "regress", "engine": engine.describe(), "git_commit": pb.git("rev-parse", "HEAD"),
           "corpus": os.path.relpath(REGRESS_SRC, pb.REPO),
           "schedule": os.path.relpath(args.schedule, pb.REPO),
           "pg_regress": pg_regress, "dlpath": pkglibdir, "regress_library_found": regress_lib_found, "summary": summary, "tests": tests}
    with open(os.path.join(out, "regress.json"), "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
