#!/usr/bin/env python3
"""Which call sites hold memory while ONE statement runs? (tracker build)

Starts the tracker build (target-track, PGRUST_ALLOC_TRACK=1,dump: backend
threads only), runs the setup SQL, then runs the statement on a second
thread; after --after seconds sends SIGWINCH for a mid-run dump of live
allocations, and prints the largest call sites (symbolized with addr2line).

  stmt-alloc-trace.py "<setup sql>" "<statement>" [--after 4]
"""
import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pgxbench as pb  # noqa: E402

BIN = os.path.join(pb.REPO, "target-track/release/postgres")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("setup")
    ap.add_argument("statement")
    ap.add_argument("--after", type=float, default=4.0)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    os.environ["PGRUST_ALLOC_TRACK"] = "1,dump"
    ws = pb.Workspace(pb.Engine("pgrust", BIN, ["-c", "max_connections=20"],
                                os.path.join(pb.REPO, "configs/pgx-ephemeral.conf")))
    srv = pb.Server(ws, 54950).launch()
    try:
        srv.wait_select1(timeout=300)
        c = pb.PgConn(ws.sockdir, 54950, timeout=3600)
        c.query(args.setup)
        base = None
        for line in open("/proc/%d/maps" % srv.proc.pid):
            f = line.split()
            if len(f) >= 6 and os.path.realpath(f[5]) == os.path.realpath(BIN) and int(f[2], 16) == 0:
                base = int(f[0].split("-")[0], 16)
                break
        err = []
        t = threading.Thread(target=lambda: err.append(c.query(args.statement)), daemon=True)
        t.start()
        time.sleep(args.after)
        os.kill(srv.proc.pid, signal.SIGWINCH)
        t.join()
        time.sleep(1)
        log = open(srv.log_path, errors="replace").read()
        last = log.rfind("ALLOC-TRACK dump (mid-run)")
        head = log[last:last + 200].splitlines()[0]
        rows = re.findall(r"ALLOC-TRACK leak: thread=(.*?) n=(\d+) bytes=(\d+) bt=(.*)", log[last:])
        print(head)
        rows = sorted(((int(b), int(n), bt) for _, n, b, bt in rows), reverse=True)[:args.top]
        addrs = sorted({a for _, _, bt in rows for a in bt.split()})
        rel = [hex(max(int(a, 16) - (base or 0), 0)) for a in addrs]
        out = subprocess.run(["addr2line", "-f", "-C", "-e", BIN] + rel, capture_output=True, text=True).stdout.splitlines()
        sym = dict(zip(addrs, [re.sub(r"::h[0-9a-f]{16}$", "", n) for n in out[0::2]]))
        skip = ("alloc_track", "__rust", "alloc::", "core::", "std::", "hashbrown", "<alloc::", "<core::", "<std::",
                "mimalloc", "allocator_api2", "??")
        for b, n, bt in rows:
            fr = [sym.get(a, a) for a in bt.split()]
            fr = [f for f in fr if not any(k in f for k in skip)]
            print("%9.1f MB %7d blk  %s" % (b / 1048576, n, " < ".join(fr[:9])))
    finally:
        srv.stop()
        ws.cleanup()


if __name__ == "__main__":
    main()
