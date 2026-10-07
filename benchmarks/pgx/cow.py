#!/usr/bin/env python3
"""Copy-on-write cloning benchmark for ephemeral databases.

Compares `file_copy_method = copy` (full file copy, the default) with
`file_copy_method = clone` (filesystem reflink: clonefile on APFS,
copy_file_range on Linux filesystems that support block cloning) for
templates of several sizes.

Per template size and method:
  clone      mint latency (connect to a new name -> SELECT 1), first real
             query, logical size, physical bytes added per clone
  writes     physical growth after inserting ~1 / 10 / 100 MB into one clone,
             and after rewriting one existing table
  delete     DROP DATABASE latency and physical bytes returned

Physical bytes are measured as the change in used space on the volume
holding the data directory (after CHECKPOINT and sync), minus the change in
pg_wal. On a shared, busy volume that is accurate to a few MB, not to the byte.
"""

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pgxbench as pb  # noqa: E402
import ephemeral as eph  # noqa: E402

M = 1048576.0
ROW_BYTES = 300  # measured: heap row + two index entries in the template schema


def zfs_dataset(path):
    """The ZFS dataset holding `path`, or None when it is not on ZFS."""
    if sys.platform == "darwin":
        return None
    out = subprocess.run(["df", "--output=fstype,source", path], capture_output=True, text=True).stdout.splitlines()
    if len(out) >= 2 and out[1].split()[0] == "zfs":
        return out[1].split()[1]
    return None


def zpool_props(pool, *props):
    out = subprocess.run(["zpool", "get", "-Hp", "-o", "value", ",".join(props), pool],
                         capture_output=True, text=True).stdout.split()
    return [int(x) if x.isdigit() else None for x in out]


def zfs_quiesce(pool, timeout=120.0):
    """Commit the open transaction group and wait until deferred frees are done.

    Without this, space freed by earlier deletes (recycled WAL, dropped
    databases) is returned to the pool at an unpredictable moment, and a
    before/after difference can come out negative. `zpool sync` needs root;
    without it this falls back to waiting for `freeing` to reach zero.
    """
    deadline = time.time() + timeout
    while True:
        subprocess.run(["sudo", "-n", "zpool", "sync", pool], capture_output=True)
        freeing = zpool_props(pool, "freeing")[0]
        if not freeing or time.time() > deadline:
            break
        time.sleep(1)
    subprocess.run(["sudo", "-n", "zpool", "sync", pool], capture_output=True)


def bclone(path):
    """(bcloneused, bclonesaved) of the pool holding `path`, or (None, None)."""
    ds = zfs_dataset(path)
    if not ds:
        return None, None
    v = zpool_props(ds.split("/")[0], "bcloneused", "bclonesaved")
    return (v + [None, None])[:2]


def volume_used(path):
    """Bytes in use where `path` lives: the ZFS pool's allocation, else the volume's."""
    ds = zfs_dataset(path)
    if ds:
        zfs_quiesce(ds.split("/")[0])
        # Pool-level ALLOC sees block cloning; per-dataset `used` charges a
        # cloned block to every dataset that references it.
        out = subprocess.run(["zpool", "list", "-Hp", "-o", "allocated", ds.split("/")[0]],
                             capture_output=True, text=True).stdout.strip()
        if out.isdigit():
            return int(out)
    st = os.statvfs(path)
    return (st.f_blocks - st.f_bfree) * st.f_frsize


def du(path):
    out = subprocess.run(["du", "-sk", path], capture_output=True, text=True).stdout
    return int(out.split()[0]) * 1024


class Run:
    def __init__(self, args, method):
        gucs = ["pgrust.ephemeral_db_prefix=" + eph.PREFIX, "pgrust.ephemeral_db_mint_roles=postgres",
                "pgrust.ephemeral_db_grace=3600", "file_copy_method=" + method, "max_wal_size=8GB"]
        server_args = []
        for g in gucs:
            server_args += ["-c", g]
        self.args = args
        engine = pb.Engine("pgrust", args.binary, server_args, args.conf)
        self.ws = pb.Workspace(engine, args.workdir)
        self.srv = pb.Server(self.ws, args.port).launch()
        self.srv.wait_select1()
        self.admin = self.conn("postgres")

    def conn(self, db):
        return pb.PgConn(self.ws.sockdir, self.args.port, database=db, timeout=1800)

    def settle(self):
        """Flush everything so volume usage reflects the databases, not dirty buffers."""
        self.admin.query("CHECKPOINT")
        os.sync()
        # ZFS accounts space when a transaction group commits (every ~5 s).
        time.sleep(max(self.args.settle, 7.0) if zfs_dataset(self.srv.datadir) else self.args.settle)

    def physical(self):
        """Used bytes on the volume, excluding WAL."""
        self.settle()
        return volume_used(self.srv.datadir) - du(os.path.join(self.srv.datadir, "pg_wal"))

    def close(self):
        try:
            self.admin.close()
        except OSError:
            pass
        self.srv.stop()
        self.ws.cleanup()


def one(args, method, target_mb):
    r = Run(args, method)
    name = lambda t: "%s%s__%s" % (eph.PREFIX, eph.TEMPLATE, t)  # noqa: E731
    try:
        rows = max(200, int((target_mb - 8) * M / args.tables / ROW_BYTES))
        t0 = time.perf_counter()
        r.admin.query("CREATE DATABASE %s" % eph.TEMPLATE)
        c = r.conn(eph.TEMPLATE)
        c.query(eph.template_sql(args.tables, rows))
        c.query("VACUUM ANALYZE")
        size = int(c.query("SELECT pg_database_size(current_database())")[0][0])
        c.close()
        build_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        r.admin.query("SELECT pgrust_seal_template('%s')" % eph.TEMPLATE)
        seal_s = time.perf_counter() - t0
        out = {"method": method, "target_mb": target_mb,
               "template": {"tables": args.tables, "rows_per_table": rows, "logical_bytes": size,
                            "build_s": build_s, "seal_s": seal_s}}

        # clone
        before = r.physical()
        bc_before = bclone(r.srv.datadir)
        clones = []
        for i in range(args.clones):
            t0 = time.perf_counter()
            c = r.conn(name("c%d" % i))
            c.query("SELECT 1")
            t1 = time.perf_counter()
            c.query("SELECT count(*) FROM accounts")
            t2 = time.perf_counter()
            c.close()
            clones.append({"mint_to_select1_s": t1 - t0, "first_real_query_s": t2 - t1})
        after = r.physical()
        bc_after = bclone(r.srv.datadir)
        out["clone"] = {"count": args.clones, "samples": clones,
                        "bclone_used_delta_bytes": (bc_after[0] - bc_before[0]) if bc_before[0] is not None else None,
                        "bclone_saved_per_clone_bytes": ((bc_after[1] - bc_before[1]) / args.clones
                                                         if bc_before[1] is not None else None),
                        "mint_to_select1_ms_median": pb.percentile([x["mint_to_select1_s"] for x in clones], 50) * 1e3,
                        "first_real_query_ms_median": pb.percentile([x["first_real_query_s"] for x in clones], 50) * 1e3,
                        "logical_bytes_per_clone": size,
                        "physical_bytes_per_clone": (after - before) / args.clones}
        bs = out["clone"]["bclone_saved_per_clone_bytes"]
        print("%-5s %5d MB  mint p50=%.0f ms  physical/clone=%.1f MB  logical=%.0f MB  block-cloned/clone=%s MB" % (
            method, target_mb, out["clone"]["mint_to_select1_ms_median"],
            out["clone"]["physical_bytes_per_clone"] / M, size / M,
            "%.1f" % (bs / M) if bs is not None else "n/a"), flush=True)

        # writes into clone c0
        w = r.conn(name("c0"))
        writes = []
        base = r.physical()
        dbsize = lambda: int(w.query("SELECT pg_database_size(current_database())")[0][0])  # noqa: E731
        logical_base = dbsize()
        written = 0
        for mb in [int(x) for x in args.write_mb.split(",")]:
            n = int(mb * M / ROW_BYTES)
            t0 = time.perf_counter()
            w.query("INSERT INTO t0(account_id, name, payload) SELECT 1, md5(g::text) || md5((g + 1)::text), "
                    "jsonb_build_object('n', g, 'h', md5((g * 7)::text)) FROM generate_series(1, %d) g" % n)
            dt = time.perf_counter() - t0
            written += mb
            now = r.physical()
            writes.append({"inserted_mb_nominal": mb, "cumulative_nominal_mb": written, "insert_s": dt,
                           "cumulative_logical_growth_bytes": dbsize() - logical_base,
                           "cumulative_physical_growth_bytes": now - base})
            print("   +%d MB nominal inserted: logical growth %.1f MB, physical growth %.1f MB" % (
                mb, writes[-1]["cumulative_logical_growth_bytes"] / M, (now - base) / M), flush=True)
        out["writes_insert"] = writes
        rel = int(w.query("SELECT pg_total_relation_size('t1')")[0][0])
        base, logical_base = r.physical(), dbsize()
        w.query("UPDATE t1 SET name = name || 'x'")
        out["write_update_one_table"] = {"table_total_bytes_before": rel,
                                         "logical_growth_bytes": dbsize() - logical_base,
                                         "physical_growth_bytes": r.physical() - base}

        # Agent-shaped writes in a fresh clone each, so every figure starts
        # from an untouched branch: scattered single-row updates across all
        # tables, an index build, and a migration that rewrites a table.
        def measure(tag, sqls):
            c = r.conn(name(tag))
            c.query("SELECT 1")
            b0, l0 = r.physical(), int(c.query("SELECT pg_database_size(current_database())")[0][0])
            t0 = time.perf_counter()
            for q in sqls:
                c.query(q)
            dt = time.perf_counter() - t0
            c.query("CHECKPOINT")
            res = {"seconds": round(dt, 3),
                   "logical_growth_bytes": int(c.query("SELECT pg_database_size(current_database())")[0][0]) - l0,
                   "physical_growth_bytes": r.physical() - b0}
            c.close()
            print("   %-22s logical %+7.1f MB  physical %+7.1f MB  (%.2fs)" % (
                tag, res["logical_growth_bytes"] / M, res["physical_growth_bytes"] / M, dt), flush=True)
            return res
        out["agent_writes"] = {
            "scattered_updates_1000": measure("scatter", [
                "UPDATE t%d SET name = name || '!' WHERE id = %d" % (i % (args.tables - 1), (i * 7919) % rows + 1)
                for i in range(1000)]),
            "create_index": measure("index", ["CREATE INDEX t2_payload_n ON t2 ((payload->>'n'))"]),
            "migration_rewrite": measure("migrate", [
                "ALTER TABLE t3 ADD COLUMN status text NOT NULL DEFAULT 'new'",
                "ALTER TABLE t3 ADD COLUMN r float8 NOT NULL DEFAULT random()",  # volatile default: full rewrite
                "CREATE INDEX t3_status ON t3(status)"]),
        }
        w.close()

        # delete
        base = r.physical()
        lat = []
        for i in range(1, args.clones):
            t0 = time.perf_counter()
            r.admin.query("DROP DATABASE %s" % name("c%d" % i))
            lat.append(time.perf_counter() - t0)
        out["delete"] = {"count": len(lat), "drop_ms_median": pb.percentile(lat, 50) * 1e3 if lat else None,
                         "physical_bytes_returned_per_clone": (base - r.physical()) / len(lat) if lat else None}
        return out
    finally:
        r.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--conf", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--workdir", help="directory for the data directory (default /tmp); its filesystem is what is tested")
    ap.add_argument("--sizes", default="15,100,1000", help="template sizes in MB")
    ap.add_argument("--methods", default="copy,clone")
    ap.add_argument("--clones", type=int, default=5)
    ap.add_argument("--write-mb", default="1,10,100")
    ap.add_argument("--tables", type=int, default=50)
    ap.add_argument("--settle", type=float, default=2.0)
    ap.add_argument("--port", type=int, default=54380)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "cow.json")
    where = args.workdir or os.environ.get("PGXBENCH_WORKDIR") or "/tmp"
    fs = subprocess.run(["sh", "-c", "df -h %s | tail -1; mount | grep -E ' on (/System/Volumes/Data|%s) ' | head -2"
                         % (where, where)], capture_output=True, text=True).stdout.strip()
    doc = {"benchmark": "cow", "git_commit": pb.git("rev-parse", "HEAD"), "conf": args.conf,
           "platform": sys.platform, "filesystem": fs, "zfs_dataset": zfs_dataset(where), "runs": []}
    for size in [int(x) for x in args.sizes.split(",")]:
        for method in args.methods.split(","):
            doc["runs"].append(one(args, method, size))
            with open(path, "w") as f:
                json.dump(doc, f, indent=2)
                f.write("\n")
    print("wrote", path)


if __name__ == "__main__":
    main()
