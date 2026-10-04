#!/usr/bin/env python3
"""Noisy-neighbour and failure-containment tests for a shared runtime.

Two ephemeral databases in one server. Database B runs a light workload the
whole time and is the measuring stick; database A misbehaves. For every
scenario the script records B's latency (p50 / p95 / p99 / max), B's errors,
whether brand-new connections to B still work, the server's peak memory, and
what happened to A.

Scenarios:
  baseline        A idle
  sort            A: large sort / aggregation
  long_txn        A: open transaction with a heavy write, then holds it
  memory          A: tries to use far more memory than the watchdog limit
  temp_spill      A: sort forced to spill to temporary files
  pathological    A: huge cross join, stopped by statement_timeout
  error           A: statements that fail, in a loop
  abort           A: transactions rolled back, in a loop
  cancel          A: long query cancelled with pg_cancel_backend
  terminate       A: session killed with pg_terminate_backend

Output: <out>/noisy-neighbor.json.
"""

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pgxbench as pb  # noqa: E402
import ephemeral as eph  # noqa: E402

M = 1048576.0
DB_A = eph.PREFIX + eph.TEMPLATE + "__noisy"
DB_B = eph.PREFIX + eph.TEMPLATE + "__quiet"
B_QUERY = "SELECT count(*) FROM t1 WHERE account_id < 50"


class Probe(threading.Thread):
    """Database B's workload: alternating SELECT 1 and a small indexed query."""

    def __init__(self, connect):
        super().__init__(daemon=True)
        self.connect, self.stop_flag = connect, False
        self.select1, self.query, self.errors = [], [], []

    def run(self):
        c = self.connect(DB_B)
        while not self.stop_flag:
            try:
                t0 = time.perf_counter()
                c.query("SELECT 1")
                t1 = time.perf_counter()
                c.query(B_QUERY)
                t2 = time.perf_counter()
                self.select1.append(t1 - t0)
                self.query.append(t2 - t1)
            except Exception as e:  # noqa: BLE001 - B's errors are the result
                self.errors.append(str(e)[:160])
                try:
                    c = self.connect(DB_B)
                except Exception as e2:  # noqa: BLE001
                    self.errors.append("reconnect: " + str(e2)[:160])
                    time.sleep(0.2)
            time.sleep(0.002)
        try:
            c.close()
        except OSError:
            pass


class NewConnProbe(threading.Thread):
    """Can a brand-new session still reach database B?"""

    def __init__(self, connect):
        super().__init__(daemon=True)
        self.connect, self.stop_flag, self.lat, self.errors = connect, False, [], []

    def run(self):
        while not self.stop_flag:
            t0 = time.perf_counter()
            try:
                c = self.connect(DB_B)
                c.query("SELECT 1")
                c.close()
                self.lat.append(time.perf_counter() - t0)
            except Exception as e:  # noqa: BLE001
                self.errors.append(str(e)[:160])
            time.sleep(0.25)


class MemPeak(threading.Thread):
    def __init__(self, pid):
        super().__init__(daemon=True)
        self.pid, self.stop_flag, self.peak, self.peak_threads = pid, False, 0, 0

    def run(self):
        while not self.stop_flag:
            s = pb.memory_sample(self.pid)
            self.peak = max(self.peak, s.get(pb.MEM_KEY, 0))
            self.peak_threads = max(self.peak_threads, s.get("thread_count") or 0)
            time.sleep(0.5)


def ms(values):
    if not values:
        return None
    return {k: (round(v * 1e3, 2) if k != "n" else v) for k, v in pb.summarize(values).items()}


def run_a(connect, steps):
    """Run A's statements; returns what happened, never raises."""
    t0 = time.perf_counter()
    out = {"statements": []}
    try:
        a = connect(DB_A)
        for sql in steps:
            if callable(sql):
                sql(a)
                continue
            s0 = time.perf_counter()
            try:
                rows = a.query(sql)
                out["statements"].append({"sql": sql[:90], "ok": True, "s": round(time.perf_counter() - s0, 2),
                                          "result": str(rows[:1])[:80]})
            except Exception as e:  # noqa: BLE001 - A failing is expected
                out["statements"].append({"sql": sql[:90], "ok": False, "s": round(time.perf_counter() - s0, 2),
                                          "error": str(e)[:240]})
                a = connect(DB_A)
        a.close()
    except Exception as e:  # noqa: BLE001
        out["fatal"] = str(e)[:240]
    out["total_s"] = round(time.perf_counter() - t0, 2)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--binary", default=os.path.join(pb.REPO, "target", "release", "postgres"))
    ap.add_argument("--conf", default=os.path.join(pb.REPO, "configs", "pgx-ephemeral.conf"))
    ap.add_argument("--no-conf", action="store_true", help="default PgRust settings (runtime and parallel query on)")
    ap.add_argument("--guc", action="append", default=[])
    ap.add_argument("--label", default="profile")
    ap.add_argument("--watchdog-limit-mb", type=int, default=1024)
    ap.add_argument("--window", type=float, default=15.0, help="seconds of statement_timeout for A's long queries")
    ap.add_argument("--scenarios", default="")
    ap.add_argument("--port", type=int, default=54400)
    args = ap.parse_args()

    gucs = ["pgrust.ephemeral_db_prefix=" + eph.PREFIX, "pgrust.ephemeral_db_mint_roles=postgres",
            "pgrust.ephemeral_db_grace=3600", "pgrust.memory_watchdog_limit=%d" % args.watchdog_limit_mb] + args.guc
    server_args = []
    for g in gucs:
        server_args += ["-c", g]
    conf = None if args.no_conf else args.conf
    ws = pb.Workspace(pb.Engine("pgrust", args.binary, server_args, conf))
    srv = pb.Server(ws, args.port).launch()
    connect = lambda db: pb.PgConn(ws.sockdir, args.port, database=db, timeout=120)  # noqa: E731
    timeout_ms = int(args.window * 1000)
    T = "SET statement_timeout = %d" % timeout_ms

    def admin_signal(fn_name, delay):
        """After `delay` seconds, call pg_cancel_backend / pg_terminate_backend on A's session."""
        def go(_a):
            def fire():
                time.sleep(delay)
                adm = connect("postgres")
                adm.query("SELECT %s(pid) FROM pg_stat_activity WHERE datname = '%s' AND state = 'active'"
                          % (fn_name, DB_A))
                adm.close()
            threading.Thread(target=fire, daemon=True).start()
        return go

    big_sort = ("SELECT count(*) FROM (SELECT g, md5(g::text) m FROM generate_series(1, 4000000) g ORDER BY 2) s")
    scenarios = [
        ("baseline", [lambda a: time.sleep(10)]),
        ("sort", [T, big_sort, "SELECT sum(length(m)) FROM (SELECT md5(g::text) m FROM generate_series(1, 4000000) g "
                              "GROUP BY 1) s"]),
        ("long_txn", ["BEGIN", "INSERT INTO t0(account_id, name, payload) SELECT 1, md5(g::text), "
                               "jsonb_build_object('n', g) FROM generate_series(1, 400000) g",
                      "LOCK TABLE t2 IN ACCESS EXCLUSIVE MODE", lambda a: time.sleep(8), "ROLLBACK"]),
        ("memory", [T, "SET work_mem = '8GB'",
                    "SELECT count(*) FROM (SELECT array_agg(md5(g::text) || md5((g+1)::text)) FROM "
                    "generate_series(1, 30000000) g) s"]),
        ("temp_spill", [T, "SET work_mem = '64kB'", big_sort]),
        ("pathological", [T, "SELECT count(*) FROM generate_series(1, 100000) a, generate_series(1, 100000) b "
                             "WHERE a + b < 0"]),
        ("error", ["SELECT 1/0", "SELECT * FROM no_such_table", "SELECT 'x'::int",
                   "INSERT INTO accounts(id, email) VALUES (1, 'dup')"] * 50),
        ("abort", ["BEGIN", "UPDATE t3 SET name = name || 'x'", "ROLLBACK"] * 30),
        ("cancel", [admin_signal("pg_cancel_backend", 3), big_sort]),
        ("terminate", [admin_signal("pg_terminate_backend", 3), big_sort]),
    ]
    wanted = [s for s in args.scenarios.split(",") if s]
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "noisy-neighbor.json")
    doc = json.load(open(path)) if os.path.exists(path) else {"benchmark": "noisy-neighbor", "runs": {}}
    run = {"git_commit": pb.git("rev-parse", "HEAD"), "conf": conf, "settings": gucs, "b_query": B_QUERY,
           "scenarios": {}}
    try:
        srv.wait_select1()
        admin = connect("postgres")
        admin.query("CREATE DATABASE %s" % eph.TEMPLATE)
        c = connect(eph.TEMPLATE)
        c.query(eph.template_sql(50, 2000))
        c.query("VACUUM ANALYZE")
        c.close()
        admin.query("SELECT pgrust_seal_template('%s')" % eph.TEMPLATE)
        for db in (DB_A, DB_B):
            connect(db).close()
        for name, steps in scenarios:
            if wanted and name not in wanted:
                continue
            probe, newc, mem = Probe(connect), NewConnProbe(connect), MemPeak(srv.proc.pid)
            for t in (probe, newc, mem):
                t.start()
            time.sleep(1)
            a = run_a(connect, steps)
            time.sleep(1)
            for t in (probe, newc, mem):
                t.stop_flag = True
            probe.join(130)
            newc.join(130)
            alive = srv.proc.poll() is None
            res = {"a": a, "b_select1_ms": ms(probe.select1), "b_query_ms": ms(probe.query),
                   "b_errors": len(probe.errors), "b_error_examples": probe.errors[:3],
                   "b_new_connection_ms": ms(newc.lat), "b_new_connection_errors": newc.errors[:3],
                   "b_longest_gap_ms": round(max(probe.query + probe.select1, default=0) * 1e3, 1),
                   "server_alive": alive, "peak_footprint_bytes": mem.peak, "peak_threads": mem.peak_threads}
            run["scenarios"][name] = res
            q = res["b_query_ms"] or {}
            print("%-13s B query p50=%s p99=%s max=%s ms  errors=%d  new-conn p50=%s  peak=%.0f MB  A: %.1fs %s" % (
                name, q.get("p50"), q.get("p99"), q.get("max"), res["b_errors"],
                (res["b_new_connection_ms"] or {}).get("p50"), mem.peak / M, a["total_s"],
                "; ".join(s.get("error", "ok")[:60] for s in a["statements"] if not s["ok"])[:150] or "ok"),
                flush=True)
            if not alive:
                run["server_died_in"] = name
                break
            doc["runs"][args.label] = run
            with open(path, "w") as f:
                json.dump(doc, f, indent=2)
                f.write("\n")
        run["server_log_tail"] = open(srv.log_path, errors="replace").read()[-3000:]
        doc["runs"][args.label] = run
        with open(path, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        print("wrote", path)
    finally:
        srv.stop()
        ws.cleanup()


if __name__ == "__main__":
    main()
