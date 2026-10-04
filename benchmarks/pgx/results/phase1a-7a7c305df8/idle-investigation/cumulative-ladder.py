import sys, time, os, json, subprocess, re, collections
sys.path.insert(0, '.')
import pgxbench as pb
steps = [
 ("baseline (defaults)", []),
 ("+ max_parallel_workers=0, per_gather=0", ["max_parallel_workers=0", "max_parallel_workers_per_gather=0"]),
 ("+ pgrust.runtime=off", ["pgrust.runtime=off"]),
 ("+ shared_buffers=16MB", ["shared_buffers=16MB"]),
 ("+ wal_buffers=64kB", ["wal_buffers=64kB"]),
 ("+ max_connections=20", ["max_connections=20"]),
 ("+ shared_buffers=1MB", ["shared_buffers=1MB"]),
 ("+ max_connections=10", ["max_connections=10", "superuser_reserved_connections=1", "reserved_connections=0"]),
 ("+ max_locks_per_transaction=10 (min)", ["max_locks_per_transaction=10"]),
 ("+ replication off (wal_level=minimal, senders/slots/logical workers=0)", ["wal_level=minimal", "max_wal_senders=0", "max_replication_slots=0", "max_logical_replication_workers=0"]),
 ("+ max_worker_processes=1, autovacuum_worker_slots=1", ["max_worker_processes=1", "autovacuum_worker_slots=1", "autovacuum_max_workers=1"]),
 ("+ autovacuum=off", ["autovacuum=off"]),
 ("+ pgrust.memory_watchdog=off", ["pgrust.memory_watchdog=off"]),
 ("+ shared_catalog_cache=off", ["shared_catalog_cache=off"]),
 ("+ jit=off", ["jit=off"]),
 ("+ shared_buffers=128kB (min)", ["shared_buffers=128kB"]),
]
acc, out = [], []
for name, gucs in steps:
    replaced = {g.split("=")[0] for g in gucs}
    acc = [g for g in acc if g.split("=")[0] not in replaced] + gucs
    args = []
    for g in acc: args += ["-c", g]
    ws = pb.Workspace(pb.Engine("pgrust", os.path.join(pb.REPO, "target/release/postgres"), args))
    srv = pb.Server(ws, 54341).launch()
    try:
        t = srv.wait_select1(); time.sleep(8)
        m = pb.memory_sample(srv.proc.pid)
        fp = subprocess.run(["footprint", str(srv.proc.pid)], capture_output=True, text=True).stdout
        cats = {}
        for l in fp.splitlines():
            mm = re.match(r"\s*(\d+) (B|KB|MB)\s+\S+ \S+\s+\S+ \S+\s+\d+\s+(.*)$", l) or re.match(r"\s*(\d+) (B|KB|MB)\s+\d+ \S+\s+\d+ \S+\s+\d+\s+(.*)$", l)
            if mm and mm.group(3) != "TOTAL":
                v = int(mm.group(1)) * {"B": 1, "KB": 1024, "MB": 1048576}[mm.group(2)]
                if v >= 500*1024: cats[mm.group(3).strip()] = round(v/1048576, 1)
        smp = subprocess.run(["sample", str(srv.proc.pid), "1"], capture_output=True, text=True).stdout
        names = re.findall(r"^\s+\d+ Thread_\d+:? +(.*)$", smp, re.M)
        tc = collections.Counter(re.sub(r"[:\d]+$", "", re.sub(r"^pg:", "", n))[:22] for n in names)
        row = dict(step=name, footprint_mb=round(m[pb.MEM_KEY]/1048576, 1), rss_mb=round(m["rss_bytes_sum"]/1048576, 1), threads=m["thread_count"], startup_ms=round(t["select1_s"]*1e3, 1), categories=cats, thread_names=dict(tc), settings=list(acc))
    except Exception as e:
        row = dict(step=name, error=str(e)[:200], log=open(srv.log_path, errors="replace").read()[-500:])
    finally:
        srv.stop(); ws.cleanup()
    out.append(row)
    print({k: v for k, v in row.items() if k not in ("settings", "thread_names")}, flush=True)
    if "error" in row: acc = [g for g in acc if g not in gucs]
print(out[-1].get("thread_names")); print(out[-1].get("settings"))
json.dump(out, open("/tmp/pgxidle/ladder.json", "w"), indent=1)
