import sys, time, os, re, subprocess, collections
sys.path.insert(0, '.')
import pgxbench as pb
for name, gucs in [("mwp=2 only", ["max_worker_processes=2"]), ("max_parallel_workers=0 only", ["max_parallel_workers=0"]), ("mpwpg=0 only", ["max_parallel_workers_per_gather=0"]), ("runtime=off", ["pgrust.runtime=off"]), ("max_connections=20", ["max_connections=20"])]:
    args = []
    for g in gucs: args += ["-c", g]
    ws = pb.Workspace(pb.Engine("pgrust", os.path.join(pb.REPO, "target/release/postgres"), args))
    srv = pb.Server(ws, 54341).launch()
    try:
        srv.wait_select1(); time.sleep(4)
        m = pb.memory_sample(srv.proc.pid)
        out = subprocess.run(["sample", str(srv.proc.pid), "1"], capture_output=True, text=True).stdout
        names = re.findall(r"^\s+\d+ Thread_\d+:? +(.*)$", out, re.M)
        c = collections.Counter(re.sub(r"\d+", "", n.split(":")[1] if n.startswith("pg:") else n)[:30] for n in names)
        print(name, "fp=%.1f" % (m[pb.MEM_KEY]/1048576), "threads", m["thread_count"], dict(c), flush=True)
    finally:
        srv.stop(); ws.cleanup()
