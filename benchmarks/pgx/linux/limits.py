#!/usr/bin/env python3
"""Memory-limit enforcement on Linux, and the cgroup backstop behind it.

--mode limits (default): one fresh server per case, PGX profile.
  none      one session runs a ~1.2 GB array_agg; no limit set
  session   same query, pgrust.session_memory_limit
  database  two sessions of one database run ~420 MB each at once,
            pgrust.database_memory_limit; a session of another database runs
            the same query at the same time and must succeed
  runtime   four sessions in four databases run ~840 MB each at once,
            pgrust.runtime_memory_limit
  For each: who failed, with which error and hint, time to fail, whether the
  failed session then answers SELECT 1, whether a bystander session and a
  brand-new connection still work, server alive, PANIC in the log, peak PSS.

--mode cgroup: run inside a cgroup with a hard memory.max (the caller starts
  it with systemd-run -p MemoryMax=...). Two cases:
  limited   pgrust.runtime_memory_limit set below the cgroup: the 1.2 GB query
            must fail with the out-of-memory error and the cgroup must record
            no OOM kill
  unlimited no PgRust limit: the cgroup's OOM killer is the only boundary;
            record what is killed and that the host stays healthy

Output: <out>/limits.json or <out>/cgroup.json.
"""

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pgxbench as pb  # noqa: E402

M = 1048576.0


def big_query(rows):
    return "SELECT array_length(array_agg(g), 1) FROM generate_series(1, %d) g" % rows


def mem_available_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024.0
    return None


def own_cgroup():
    with open("/proc/self/cgroup") as f:
        path = f.read().strip().split("::", 1)[-1]
    return "/sys/fs/cgroup" + path


def cgroup_events(cg):
    try:
        with open(os.path.join(cg, "memory.events")) as f:
            return dict((k, int(v)) for k, v in (l.split() for l in f))
    except OSError:
        return {}


def cgroup_read(cg, name):
    try:
        with open(os.path.join(cg, name)) as f:
            return f.read().strip()
    except OSError:
        return None


class Peak(threading.Thread):
    def __init__(self, pid):
        super().__init__(daemon=True)
        self.pid, self.stop_flag, self.peak = pid, False, 0

    def run(self):
        while not self.stop_flag:
            s = pb.memory_sample(self.pid)
            self.peak = max(self.peak, s.get(pb.MEM_KEY, 0) or 0)
            time.sleep(0.2)


def run_session(connect, db, rows, out):
    t0 = time.perf_counter()
    rec = {"database": db, "rows": rows}
    try:
        c = connect(db)
        c.query("SET work_mem = '8GB'")
        try:
            r = c.query(big_query(rows))
            rec.update(ok=True, result=str(r)[:40])
        except Exception as e:  # noqa: BLE001 - the error is the result
            rec.update(ok=False, error=str(e)[:400])
        rec["seconds"] = round(time.perf_counter() - t0, 2)
        try:
            rec["select1_after"] = c.query("SELECT 1") == [["1"]]
        except Exception as e:  # noqa: BLE001
            rec["select1_after"] = False
            rec["select1_error"] = str(e)[:200]
        c.close()
    except Exception as e:  # noqa: BLE001
        rec.update(ok=False, fatal=str(e)[:300], seconds=round(time.perf_counter() - t0, 2))
    out.append(rec)


def start_server(args, gucs, port):
    server_args = []
    for g in gucs:
        server_args += ["-c", g]
    ws = pb.Workspace(pb.Engine("pgrust", args.binary, server_args, args.conf))
    srv = pb.Server(ws, port).launch()
    srv.wait_select1()
    return ws, srv


def run_case(args, name, gucs, sessions, port):
    """sessions: list of (database, rows). Bystander db 'bystander' runs SELECT 1 throughout."""
    ws, srv = start_server(args, gucs, port)
    connect = lambda db: pb.PgConn(ws.sockdir, port, database=db, timeout=600)  # noqa: E731
    admin = connect("postgres")
    for db in sorted({d for d, _ in sessions} | {"bystander"}):
        admin.query("CREATE DATABASE %s" % db)
    admin.close()
    pid = srv.proc.pid
    base = pb.memory_sample(pid).get(pb.MEM_KEY, 0)
    by_lat, by_err = [], []
    stop = [False]

    def bystander():
        try:
            c = connect("bystander")
            while not stop[0]:
                t0 = time.perf_counter()
                try:
                    c.query("SELECT 1")
                    by_lat.append(time.perf_counter() - t0)
                except Exception as e:  # noqa: BLE001
                    by_err.append(str(e)[:160])
                time.sleep(0.05)
            c.close()
        except Exception as e:  # noqa: BLE001
            by_err.append("connect: " + str(e)[:160])

    peak = Peak(pid)
    peak.start()
    bt = threading.Thread(target=bystander, daemon=True)
    bt.start()
    results = []
    threads = [threading.Thread(target=run_session, args=(connect, d, r, results)) for d, r in sessions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop[0] = True
    bt.join(timeout=10)
    try:
        c = connect("bystander")
        new_conn_ok = c.query("SELECT 1") == [["1"]]
        c.close()
    except Exception:  # noqa: BLE001
        new_conn_ok = False
    peak.stop_flag = True
    peak.join()
    alive = srv.proc.poll() is None
    with open(srv.log_path, "rb") as f:
        log = f.read().decode("utf-8", "replace")
    row = {"case": name, "settings": gucs, "sessions": sorted(results, key=lambda r: r["database"]),
           "base_pss_mb": round(base / M, 1), "peak_pss_mb": round(peak.peak / M, 1),
           "bystander": {"queries": len(by_lat), "errors": len(by_err), "error_examples": by_err[:3],
                         "max_ms": round(max(by_lat) * 1e3, 1) if by_lat else None},
           "new_connection_after": new_conn_ok, "server_alive": alive,
           "panic_in_log": "PANIC" in log, "limit_hints_in_log": log.count("reached pgrust.")}
    srv.stop()
    ws.cleanup()
    return row


def mode_limits(args):
    prof = ["max_connections=50"]
    cases = [
        ("none", prof, [("solo", 30_000_000)]),
        ("session", prof + ["pgrust.session_memory_limit=%d" % args.session_mb], [("solo", 30_000_000)]),
        ("database", prof + ["pgrust.database_memory_limit=%d" % args.database_mb],
         [("shared", 10_000_000), ("shared", 10_000_000), ("other", 10_000_000)]),
        ("runtime", prof + ["pgrust.runtime_memory_limit=%d" % args.runtime_mb],
         [("r1", 20_000_000), ("r2", 20_000_000), ("r3", 20_000_000), ("r4", 20_000_000)]),
    ]
    doc = {"benchmark": "memory-limits", "git_commit": pb.git("rev-parse", "HEAD"), "conf": args.conf, "cases": []}
    for i, (name, gucs, sessions) in enumerate(cases):
        if (mem_available_mb() or 0) < args.min_available_mb:
            doc["cases"].append({"case": name, "skipped": "MemAvailable below %d MB" % args.min_available_mb})
            continue
        row = run_case(args, name, gucs, sessions, args.port + i)
        doc["cases"].append(row)
        print("%-9s peak=%6.0f MB  %s  bystander errors=%d alive=%s panic=%s" % (
            name, row["peak_pss_mb"],
            ", ".join("%s:%s%s" % (s["database"], "ok" if s.get("ok") else "FAIL",
                                   "" if s.get("ok") else "(%ss)" % s.get("seconds")) for s in row["sessions"]),
            row["bystander"]["errors"], row["server_alive"], row["panic_in_log"]), flush=True)
        time.sleep(3)
    return doc, "limits.json"


def mode_cgroup(args):
    cg = own_cgroup()
    doc = {"benchmark": "cgroup-backstop", "git_commit": pb.git("rev-parse", "HEAD"), "cgroup": cg,
           "memory.max": cgroup_read(cg, "memory.max"), "memory.swap.max": cgroup_read(cg, "memory.swap.max"),
           "cases": []}
    if doc["memory.max"] in (None, "max"):
        sys.exit("not inside a cgroup with memory.max set; start with systemd-run -p MemoryMax=...")
    for i, (name, gucs) in enumerate([
            ("limited", ["max_connections=50", "pgrust.runtime_memory_limit=%d" % args.cgroup_runtime_mb]),
            ("unlimited", ["max_connections=50"])]):
        ev0 = cgroup_events(cg)
        host0 = mem_available_mb()
        ws, srv = start_server(args, gucs, args.port + 10 + i)
        connect = lambda db: pb.PgConn(ws.sockdir, args.port + 10 + i, database=db, timeout=600)  # noqa: E731
        peak = Peak(srv.proc.pid)
        peak.start()
        results = []
        run_session(connect, "postgres", 30_000_000, results)
        time.sleep(1)
        peak.stop_flag = True
        peak.join()
        rc = srv.proc.poll()
        ev1 = cgroup_events(cg)
        with open(srv.log_path, "rb") as f:
            log = f.read().decode("utf-8", "replace")
        row = {"case": name, "settings": gucs, "session": results[0], "peak_pss_mb": round(peak.peak / M, 1),
               "server_alive": rc is None, "server_exit": rc,
               "oom_kill_delta": ev1.get("oom_kill", 0) - ev0.get("oom_kill", 0),
               "memory_max_events_delta": ev1.get("max", 0) - ev0.get("max", 0),
               "host_mem_available_mb_before": round(host0 or 0), "host_mem_available_mb_after": round(mem_available_mb() or 0),
               "cgroup_memory_peak": cgroup_read(cg, "memory.peak"),
               "panic_in_log": "PANIC" in log, "log_tail": log[-600:]}
        doc["cases"].append(row)
        print("%-9s session ok=%s alive=%s exit=%s oom_kills=%d peak=%.0f MB host avail %s->%s MB" % (
            name, results[0].get("ok"), row["server_alive"], rc, row["oom_kill_delta"], row["peak_pss_mb"],
            row["host_mem_available_mb_before"], row["host_mem_available_mb_after"]), flush=True)
        try:
            srv.stop()
        except Exception:  # noqa: BLE001
            pass
        ws.cleanup()
        time.sleep(3)
    return doc, "cgroup.json"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=["limits", "cgroup"], default="limits")
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--conf", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--session-mb", type=int, default=256)
    ap.add_argument("--database-mb", type=int, default=512)
    ap.add_argument("--runtime-mb", type=int, default=2048)
    ap.add_argument("--cgroup-runtime-mb", type=int, default=700)
    ap.add_argument("--min-available-mb", type=int, default=3500)
    ap.add_argument("--port", type=int, default=54440)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    doc, fname = (mode_limits if args.mode == "limits" else mode_cgroup)(args)
    path = os.path.join(args.out, fname)
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    print("wrote", path)


if __name__ == "__main__":
    main()
