#!/usr/bin/env python3
"""Linux-native memory accounting for an idle PgRust server.

For each configuration (default settings, then the PGX profile, then the
profile's settings added one at a time) the server is started, one
connection runs SELECT 1 and disconnects, and after a settle period the
script records:

  - /proc/<pid>/status: VmRSS, RssAnon, RssFile, RssShmem, VmSwap, Threads
  - /proc/<pid>/smaps_rollup: Pss and its anon / file / shmem split,
    Private_Dirty, AnonHugePages
  - /proc/<pid>/smaps grouped by kind of mapping: the server binary, other
    files, thread stacks, [heap], and anonymous mappings bucketed by size
  - cgroup v2 memory.current when the process is in a bounded cgroup
  - the transparent-huge-page mode of the host

Output: <out>/memory-breakdown.json and a table on stdout.
"""

import argparse
import collections
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pgxbench as pb  # noqa: E402

M = 1048576.0
PROFILE_STEPS = [
    ("max_parallel_workers=0", ["max_parallel_workers=0", "max_parallel_workers_per_gather=0"]),
    ("pgrust.runtime=off", ["pgrust.runtime=off"]),
    ("shared_buffers=16MB", ["shared_buffers=16MB"]),
    ("wal_buffers=64kB", ["wal_buffers=64kB"]),
    ("max_connections=20", ["max_connections=20"]),
    ("replication off", ["wal_level=minimal", "max_wal_senders=0", "max_replication_slots=0",
                         "max_logical_replication_workers=0"]),
]


def kb(text, key):
    m = re.search(r"^%s:\s+(\d+) kB" % re.escape(key), text, re.M)
    return int(m.group(1)) * 1024 if m else 0


def smaps_groups(pid, binary):
    """PSS by kind of mapping."""
    groups = collections.Counter()
    counts = collections.Counter()
    name, size = None, 0
    with open("/proc/%d/smaps" % pid) as f:
        for line in f:
            head = re.match(r"^([0-9a-f]+)-([0-9a-f]+) (\S+) \S+ \S+ \S+\s*(.*)$", line)
            if head:
                size = int(head.group(2), 16) - int(head.group(1), 16)
                path = head.group(4).strip()
                if path == binary or path.endswith("/postgres"):
                    name = "server binary (text+data)"
                elif path == "[heap]":
                    name = "[heap] (brk)"
                elif path.startswith("[stack") or path == "[stack]":
                    name = "main thread stack"
                elif path.startswith("/"):
                    name = "other files and libraries"
                elif path.startswith("["):
                    name = path
                elif size >= 64 * 1048576:
                    name = "anonymous, mapping >= 64 MB"
                elif size >= 1048576:
                    name = "anonymous, mapping 1-64 MB"
                else:
                    name = "anonymous, mapping < 1 MB"
                counts[name] += 1
                continue
            m = re.match(r"^Pss:\s+(\d+) kB", line)
            if m and name:
                groups[name] += int(m.group(1)) * 1024
    return {k: {"pss_bytes": v, "mappings": counts[k]} for k, v in groups.most_common()}


def measure(engine, port, settle, workdir):
    ws = pb.Workspace(engine, workdir)
    srv = pb.Server(ws, port).launch()
    try:
        srv.wait_select1()
        time.sleep(settle)
        pid = srv.proc.pid
        status = open("/proc/%d/status" % pid).read()
        rollup = open("/proc/%d/smaps_rollup" % pid).read()
        out = {
            "threads": int(re.search(r"^Threads:\s+(\d+)", status, re.M).group(1)),
            "rss_bytes": kb(status, "VmRSS"), "rss_anon_bytes": kb(status, "RssAnon"),
            "rss_file_bytes": kb(status, "RssFile"), "rss_shmem_bytes": kb(status, "RssShmem"),
            "swap_bytes": kb(status, "VmSwap"), "virtual_bytes": kb(status, "VmSize"),
            "pss_bytes": kb(rollup, "Pss"), "pss_anon_bytes": kb(rollup, "Pss_Anon"),
            "pss_file_bytes": kb(rollup, "Pss_File"), "pss_shmem_bytes": kb(rollup, "Pss_Shmem"),
            "private_dirty_bytes": kb(rollup, "Private_Dirty"),
            "anon_huge_pages_bytes": kb(rollup, "AnonHugePages"),
            "by_mapping": smaps_groups(pid, engine.binary),
        }
        try:
            cg = open("/proc/%d/cgroup" % pid).read().strip().split("::")[-1]
            with open("/sys/fs/cgroup%s/memory.current" % cg) as f:
                out["cgroup_memory_current_bytes"] = int(f.read())
        except (OSError, ValueError):
            pass
        return out
    finally:
        srv.stop()
        ws.cleanup()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--settle", type=float, default=15.0)
    ap.add_argument("--port", type=int, default=54450)
    ap.add_argument("--workdir")
    ap.add_argument("--with-postgres", action="store_true", help="also measure stock PostgreSQL 18")
    args = ap.parse_args()
    if sys.platform != "linux":
        sys.exit("Linux only (reads /proc); on macOS use pgxbench.py idle")

    def thp(name):
        try:
            return re.search(r"\[(\w+)\]", open("/sys/kernel/mm/transparent_hugepage/" + name).read()).group(1)
        except (OSError, AttributeError):
            return None
    doc = {"benchmark": "memory-breakdown", "git_commit": pb.git("rev-parse", "HEAD"), "settle_s": args.settle,
           "transparent_hugepage": {"enabled": thp("enabled"), "defrag": thp("defrag")},
           "page_size": os.sysconf("SC_PAGE_SIZE"), "rows": []}

    def run(label, engine):
        r = measure(engine, args.port, args.settle, args.workdir)
        r["label"] = label
        doc["rows"].append(r)
        print("%-34s pss=%6.1f  anon=%6.1f file=%5.1f shmem=%5.1f  rss=%6.1f  thp=%5.1f  threads=%d" % (
            label, r["pss_bytes"] / M, r["pss_anon_bytes"] / M, r["pss_file_bytes"] / M, r["pss_shmem_bytes"] / M,
            r["rss_bytes"] / M, r["anon_huge_pages_bytes"] / M, r["threads"]), flush=True)
        with open(os.path.join(args.out, "memory-breakdown.json"), "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        return r

    os.makedirs(args.out, exist_ok=True)
    print("MB; transparent huge pages: %s" % doc["transparent_hugepage"])
    if args.with_postgres:
        run("PostgreSQL 18 (stock)", pb.Engine("postgres", os.path.join(pb.pg18_prefix(), "bin", "postgres")))
    base = run("PgRust defaults", pb.Engine("pgrust", args.binary))
    acc = []
    for label, gucs in PROFILE_STEPS:
        acc += gucs
        server_args = []
        for g in acc:
            server_args += ["-c", g]
        run("+ " + label, pb.Engine("pgrust", args.binary, server_args))
    prof = run("PGX profile (configs/pgx-ephemeral.conf)",
               pb.Engine("pgrust", args.binary, [], os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf")))
    for label, r in (("PgRust defaults", base), ("PGX profile", prof)):
        print("\n%s, PSS by mapping kind:" % label)
        for k, v in r["by_mapping"].items():
            if v["pss_bytes"] >= 0.5 * M:
                print("  %7.1f MB  %4d mappings  %s" % (v["pss_bytes"] / M, v["mappings"], k))


if __name__ == "__main__":
    main()
