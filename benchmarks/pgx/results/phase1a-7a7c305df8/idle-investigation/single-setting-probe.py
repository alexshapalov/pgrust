import sys, time, os, json
sys.path.insert(0, '.')
import pgxbench as pb
variants = [
 ("baseline", []),
 ("shared_buffers=16MB", ["shared_buffers=16MB"]),
 ("shared_buffers=1MB", ["shared_buffers=1MB"]),
 ("max_connections=20", ["max_connections=20"]),
 ("max_worker_processes=2,par=0", ["max_worker_processes=2","max_parallel_workers=0","max_parallel_workers_per_gather=0"]),
 ("pgrust.runtime=off", ["pgrust.runtime=off"]),
 ("jit=off", ["jit=off"]),
 ("autovacuum=off", ["autovacuum=off"]),
 ("memory_watchdog=off", ["pgrust.memory_watchdog=off"]),
 ("autoprewarm=off", ["pg_prewarm.autoprewarm=off"]),
 ("shared_catalog_cache=off", ["shared_catalog_cache=off"]),
 ("pg_stat_statements.max=100", ["pg_stat_statements.max=100"]),
 ("wal_buffers=64kB", ["wal_buffers=64kB"]),
 ("max_logical_replication_workers=0", ["max_logical_replication_workers=0"]),
 ("wal_level=minimal,senders=0", ["wal_level=minimal","max_wal_senders=0","max_replication_slots=0"]),
 ("max_locks_per_transaction=10", ["max_locks_per_transaction=10"]),
]
out = []
for name, gucs in variants:
    args = []
    for g in gucs: args += ["-c", g]
    eng = pb.Engine("pgrust", os.path.join(pb.REPO, "target/release/postgres"), args)
    ws = pb.Workspace(eng)
    srv = pb.Server(ws, 54341).launch()
    try:
        # before any connection: wait for socket, then settle
        sock = os.path.join(ws.sockdir, ".s.PGSQL.54341")
        t0 = time.time()
        while not os.path.exists(sock) and time.time() - t0 < 30:
            if srv.proc.poll() is not None: raise RuntimeError("exited")
            time.sleep(0.01)
        time.sleep(3)
        pre = pb.memory_sample(srv.proc.pid)
        srv.wait_select1()
        time.sleep(5)
        post = pb.memory_sample(srv.proc.pid)
        row = dict(name=name, pre_fp=pre[pb.MEM_KEY]/1048576, pre_threads=pre["thread_count"],
                   fp=post[pb.MEM_KEY]/1048576, threads=post["thread_count"])
    except Exception as e:
        row = dict(name=name, error=str(e), log=open(srv.log_path, errors="replace").read()[-400:])
    finally:
        srv.stop(); ws.cleanup()
    out.append(row); print(row, flush=True)
json.dump(out, open("/tmp/pgxidle/probe.json", "w"), indent=1)
