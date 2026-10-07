#!/usr/bin/env python3
"""Memory-limit enforcement on Linux, and the cgroup backstop behind it.

--mode limits (default): one fresh server per case, PGX profile.
  none      one session runs a ~1.2 GB array_agg; no limit set
  session   same query, pgrust.session_memory_limit
  database  two sessions of one database run ~330 MB each at once (Linux:
            ~65 bytes per array_agg element),
            pgrust.database_memory_limit; a session of another database runs
            the same query at the same time and must succeed
  runtime   four sessions in four databases run ~840 MB each at once,
            pgrust.runtime_memory_limit
  For each: who failed, with which error and hint, time to fail, whether the
  failed session then answers SELECT 1, whether a bystander session and a
  brand-new connection still work, server alive, PANIC in the log, peak PSS.

--mode workloads: one server with pgrust.session_memory_limit (default 256)
  and work_mem/maintenance_work_mem set high, so memory-hungry operations
  try to stay in memory. Each workload runs in database "heavy" (a 3M-row
  table) while a bystander database runs SELECT 1 throughout. Per workload:
  ok or the error's SQLSTATE and hint, time, peak PSS, whether the same
  session answers SELECT 1 afterwards, server alive. No workload may
  produce XX000 (internal error) or a PANIC.

--mode cgroup: each server runs in its own transient systemd scope with a
  hard memory.max (--cgroup-mb) and no swap; the harness stays outside it, so
  an OOM kill can only hit the server. Needs passwordless sudo. Two cases:
  limited   pgrust.runtime_memory_limit set below the cgroup: the 1.2 GB query
            must fail with the out-of-memory error and the cgroup must record
            no OOM kill
  unlimited no PgRust limit: the cgroup's OOM killer is the only boundary;
            record what is killed and that the host stays healthy

Output: <out>/limits.json or <out>/cgroup.json.
"""

import argparse
import subprocess
import json
import re
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
         [("shared", 5_000_000), ("shared", 5_000_000), ("other", 5_000_000)]),
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


HEAVY_SETUP = (
    "CREATE TABLE big(id int, k int, t text, j jsonb);"
    "INSERT INTO big SELECT g, g % 100000, md5(g::text), jsonb_build_object('n', g, 's', md5(g::text)) "
    "FROM generate_series(1, 3000000) g;"
    "CREATE TABLE big2 AS SELECT id, k, t FROM big;"
    "ANALYZE big; ANALYZE big2;"
)

WORKLOADS = [
    ("sort", "SELECT count(*) FROM (SELECT t FROM big ORDER BY t) s"),
    ("hash_join", "SELECT count(*) FROM big a JOIN big2 b ON a.t = b.t"),
    ("hash_aggregate", "SELECT count(*) FROM (SELECT t, count(*) FROM big GROUP BY t) s"),
    ("cte", "WITH x AS MATERIALIZED (SELECT t, j FROM big) SELECT count(*) FROM x a JOIN x b USING (t)"),
    ("json_aggregation", "SELECT length(json_agg(j)::text) FROM big"),
    ("array_agg", "SELECT array_length(array_agg(t), 1) FROM big"),
    ("copy_out", "COPY (SELECT * FROM big) TO '/dev/null'"),
    ("copy_in", "CREATE TEMP TABLE ci(id int, k int, t text, j jsonb); COPY ci FROM '%COPYFILE%'; DROP TABLE ci"),
    ("large_insert", "CREATE TABLE ins AS SELECT * FROM big WHERE false; INSERT INTO ins SELECT * FROM big; DROP TABLE ins"),
    ("create_index", "CREATE INDEX big_t ON big(t); DROP INDEX big_t"),
    ("migration_rewrite", "ALTER TABLE big2 ALTER COLUMN k TYPE bigint; ALTER TABLE big2 ALTER COLUMN k TYPE int"),
    ("temp_spill", "SET work_mem = '256kB'; SELECT count(*) FROM (SELECT t FROM big ORDER BY t) s; RESET work_mem"),
]


def mode_workloads(args):
    gucs = ["max_connections=20", "pgrust.session_memory_limit=%d" % args.session_mb,
            "work_mem=2GB", "maintenance_work_mem=2GB", "temp_buffers=64MB"]
    ws, srv = start_server(args, gucs, args.port + 20)
    connect = lambda db: pb.PgConn(ws.sockdir, args.port + 20, database=db, timeout=1800)  # noqa: E731
    pid = srv.proc.pid
    doc = {"benchmark": "memory-limit-workloads", "git_commit": pb.git("rev-parse", "HEAD"), "settings": gucs,
           "workloads": []}
    copy_file = os.path.join(ws.root, "big.copy")
    try:
        admin = connect("postgres")
        admin.query("CREATE DATABASE heavy")
        admin.query("CREATE DATABASE bystander")
        h = connect("heavy")
        h.query(HEAVY_SETUP)
        h.query("COPY big TO '%s'" % copy_file)
        h.close()
        stop = [False]
        by_err, by_lat = [], []

        def bystander():
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
        bt = threading.Thread(target=bystander, daemon=True)
        bt.start()
        for name, sql in WORKLOADS:
            sql = sql.replace("%COPYFILE%", copy_file)
            peak = Peak(pid)
            peak.start()
            c = connect("heavy")
            t0 = time.perf_counter()
            rec = {"workload": name}
            try:
                c.query(sql)
                rec["ok"] = True
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                rec["ok"] = False
                rec["sqlstate"] = (re.search(r"C([0-9A-Z]{5})", msg) or [None, None])[1]
                rec["error"] = msg[:300]
                rec["hint"] = (re.findall(r"reached pgrust\.\w+", msg) or [None])[0]
            rec["seconds"] = round(time.perf_counter() - t0, 2)
            try:
                rec["select1_after"] = c.query("SELECT 1") == [["1"]]
            except Exception as e:  # noqa: BLE001
                rec["select1_after"] = False
                rec["select1_error"] = str(e)[:200]
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
            peak.stop_flag = True
            peak.join()
            rec["peak_pss_mb"] = round(peak.peak / M, 1)
            rec["server_alive"] = srv.proc.poll() is None
            doc["workloads"].append(rec)
            print("%-18s %-5s %-6s %6.2fs peak=%6.0f MB select1_after=%s %s" % (
                name, "ok" if rec["ok"] else "FAIL", rec.get("sqlstate") or "", rec["seconds"], rec["peak_pss_mb"],
                rec["select1_after"], rec.get("hint") or ""), flush=True)
            if not rec["server_alive"]:
                break
        stop[0] = True
        bt.join(timeout=10)
        with open(srv.log_path, "rb") as f:
            log = f.read().decode("utf-8", "replace")
        doc["bystander"] = {"queries": len(by_lat), "errors": len(by_err), "error_examples": by_err[:3],
                            "max_ms": round(max(by_lat) * 1e3, 1) if by_lat else None}
        doc["panic_in_log"] = "PANIC" in log
        doc["internal_errors"] = sum(1 for w in doc["workloads"] if w.get("sqlstate") == "XX000")
        print("bystander errors=%d max=%s ms  internal_errors=%d panic=%s" % (
            len(by_err), doc["bystander"]["max_ms"], doc["internal_errors"], doc["panic_in_log"]), flush=True)
    finally:
        srv.stop()
        ws.cleanup()
    return doc, "workloads.json"


class ScopedServer(pb.Server):
    """A server started in its own transient systemd scope with a hard
    memory.max, so the cgroup OOM killer can only ever hit the server, never
    this harness. Needs passwordless sudo."""

    def __init__(self, ws, port, unit, memory_max_mb):
        super().__init__(ws, port)
        self.unit, self.memory_max_mb = unit, memory_max_mb
        self.cg = "/sys/fs/cgroup/system.slice/%s.scope" % unit

    def launch(self):
        eng = self.ws.engine
        env = dict(os.environ, **eng.env)
        argv = eng.argv(self.datadir, self.ws.sockdir, self.port)
        if eng.stack_limit:
            argv = ["/bin/sh", "-c", 'ulimit -s %d; exec "$0" "$@"' % (eng.stack_limit // 1024)] + argv
        # sudo on Ubuntu 26.04 (sudo-rs) ignores -E: hand the server's
        # environment to systemd-run explicitly.
        setenv = []
        for k in sorted(set(eng.env) | {"PATH", "HOME"}):
            if k in env:
                setenv.append("--setenv=%s=%s" % (k, env[k]))
        argv = ["sudo", "-n", "systemd-run", "--scope", "--quiet", "--unit=" + self.unit,
                "-p", "MemoryMax=%dM" % self.memory_max_mb, "-p", "MemorySwapMax=0",
                "--uid=%d" % os.getuid(), "--gid=%d" % os.getgid()] + setenv + ["--"] + argv
        self.log = open(self.log_path, "wb")
        self.t_launch = time.perf_counter()
        self.proc = subprocess.Popen(argv, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        return self


class CgroupWatch(threading.Thread):
    """Keep the last memory.events / memory.peak seen: the scope (and its
    files) disappears as soon as the killed server exits."""

    def __init__(self, cg):
        super().__init__(daemon=True)
        self.cg, self.stop_flag, self.events, self.peak = cg, False, {}, None

    def run(self):
        while not self.stop_flag:
            ev = cgroup_events(self.cg)
            if ev:
                self.events = ev
            pk = cgroup_read(self.cg, "memory.peak")
            if pk and pk.isdigit():
                self.peak = int(pk)
            time.sleep(0.1)


def mode_cgroup(args):
    doc = {"benchmark": "cgroup-backstop", "git_commit": pb.git("rev-parse", "HEAD"),
           "memory_max_mb": args.cgroup_mb, "cases": []}
    path = os.path.join(args.out, "cgroup.json")
    for i, (name, gucs) in enumerate([
            ("limited", ["max_connections=50", "pgrust.runtime_memory_limit=%d" % args.cgroup_runtime_mb]),
            ("unlimited", ["max_connections=50"])]):
        server_args = []
        for g in gucs:
            server_args += ["-c", g]
        ws = pb.Workspace(pb.Engine("pgrust", args.binary, server_args, args.conf))
        unit = "pgx-cgroup-%s-%d" % (name, int(time.time()))
        port = args.port + 10 + i
        srv = ScopedServer(ws, port, unit, args.cgroup_mb).launch()
        host0 = mem_available_mb()
        srv.wait_select1()
        watch = CgroupWatch(srv.cg)
        watch.start()
        connect = lambda db: pb.PgConn(ws.sockdir, port, database=db, timeout=600)  # noqa: E731
        results = []
        run_session(connect, "postgres", 30_000_000, results)
        time.sleep(1.5)
        watch.stop_flag = True
        watch.join()
        rc = srv.proc.poll()
        try:
            c = connect("postgres")
            after_ok = c.query("SELECT 1") == [["1"]]
            c.close()
        except Exception:  # noqa: BLE001
            after_ok = False
        kern = subprocess.run(["sudo", "-n", "journalctl", "-k", "--no-pager", "--since", "-2min"],
                              capture_output=True, text=True).stdout
        killed = [l for l in kern.splitlines() if unit in l or "Killed process" in l]
        with open(srv.log_path, "rb") as f:
            log = f.read().decode("utf-8", "replace")
        row = {"case": name, "settings": gucs, "session": results[0],
               "server_alive_after": rc is None and after_ok, "server_exit": rc,
               "oom_kill_events": watch.events.get("oom_kill", 0), "memory_max_hits": watch.events.get("max", 0),
               "cgroup_memory_peak_mb": round(watch.peak / M, 1) if watch.peak else None,
               "host_mem_available_mb_before": round(host0), "host_mem_available_mb_after": round(mem_available_mb()),
               "kernel_log": killed[-3:], "panic_in_log": "PANIC" in log, "log_tail": log[-400:]}
        doc["cases"].append(row)
        with open(path, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        print("%-9s session ok=%s server alive=%s exit=%s oom_kills=%s cgroup peak=%s MB host avail %s->%s MB" % (
            name, results[0].get("ok"), row["server_alive_after"], rc, row["oom_kill_events"],
            row["cgroup_memory_peak_mb"], row["host_mem_available_mb_before"], row["host_mem_available_mb_after"]),
            flush=True)
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
    ap.add_argument("--mode", choices=["limits", "cgroup", "workloads"], default="limits")
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--conf", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--session-mb", type=int, default=256)
    ap.add_argument("--database-mb", type=int, default=512)
    ap.add_argument("--runtime-mb", type=int, default=2048)
    ap.add_argument("--cgroup-runtime-mb", type=int, default=700)
    ap.add_argument("--cgroup-mb", type=int, default=1024, help="memory.max of the server's scope in --mode cgroup")
    ap.add_argument("--min-available-mb", type=int, default=3500)
    ap.add_argument("--port", type=int, default=54440)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    doc, fname = {"limits": mode_limits, "cgroup": mode_cgroup, "workloads": mode_workloads}[args.mode](args)
    path = os.path.join(args.out, fname)
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    print("wrote", path)


if __name__ == "__main__":
    main()
