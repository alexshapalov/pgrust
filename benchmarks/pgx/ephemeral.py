#!/usr/bin/env python3
"""Benchmark PgRust's built-in ephemeral databases (janitor / mint-on-connect).

One server, many disposable databases cloned from a sealed template: connect
to `<prefix><template>__<token>` and the database is created on the spot;
after `pgrust.ephemeral_db_grace` seconds without connections it is dropped.

Scenarios (each writes a section of <out>/ephemeral.json):

  density   mint N databases one after another; latency of every mint and
            server memory / data-directory size at checkpoints of N
  idle      CPU, wakeups and disk writes with all N databases existing, no clients
  active    memory with connections open to K distinct databases
  reconnect connect + query against an existing, idle ephemeral database
  isolation writes in one clone are invisible to another and to the template
  burst     K clients connect to K new names at once
  pool      mint latency with a warm pool of pre-made spares
  reap      time for the janitor to drop idle databases, memory and disk after
  createdb  reference: plain CREATE DATABASE ... TEMPLATE + first connection
            (also runs against stock PostgreSQL with --engine postgres)
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pgxbench as pb  # noqa: E402

PREFIX = "tdb_"
TEMPLATE = "tpl_app"
M = 1048576.0
STATEMENT_TIMEOUT_S = 90


class ServerHang(Exception):
    """The server stopped answering. Seen intermittently in the database-wide
    VACUUM ANALYZE that finishes the template build; the scenario is retried
    on a fresh server and the hang is counted in the results."""


def template_sql(tables, rows):
    """A small application-shaped schema: FK, unique and secondary indexes, JSONB."""
    out = ["CREATE TABLE accounts(id bigserial PRIMARY KEY, email text UNIQUE NOT NULL, "
           "created_at timestamptz DEFAULT now());",
           "INSERT INTO accounts(email) SELECT 'user' || g || '@example.com' FROM generate_series(1, %d) g;" % rows]
    for i in range(tables - 1):
        out.append("CREATE TABLE t%d(id bigserial PRIMARY KEY, account_id bigint REFERENCES accounts(id), "
                   "name text NOT NULL, payload jsonb, created_at timestamptz DEFAULT now());" % i)
        out.append("CREATE INDEX t%d_account ON t%d(account_id);" % (i, i))
        out.append("CREATE INDEX t%d_name ON t%d(name);" % (i, i))
        out.append("INSERT INTO t%d(account_id, name, payload) SELECT g, 'row ' || g, "
                   "jsonb_build_object('n', g, 'tag', 't%d') FROM generate_series(1, %d) g;" % (i, i, rows))
    return "\n".join(out)


class Bench:
    def __init__(self, args, extra_gucs=(), ephemeral=True):
        self.args = args
        self.hung = False
        server_args = []
        gucs = list(extra_gucs) + list(args.guc)
        if ephemeral:
            gucs += ["pgrust.ephemeral_db_prefix=" + PREFIX, "pgrust.ephemeral_db_mint_roles=postgres"]
        for g in gucs:
            server_args += ["-c", g]
        binary = args.binary or (os.path.join(pb.REPO, "target", "release", "postgres") if args.engine == "pgrust"
                                 else os.path.join(pb.pg18_prefix(), "bin", "postgres"))
        self.engine = pb.Engine(args.engine, binary, server_args, args.conf)
        self.ws = pb.Workspace(self.engine)
        self.srv = pb.Server(self.ws, args.port).launch()
        self.srv.wait_select1()
        self.admin = self.conn("postgres")

    def conn(self, db):
        c = pb.PgConn(self.ws.sockdir, self.args.port, database=db)
        # No statement here should take anywhere near this long; a timeout
        # means the server is wedged (see ServerHang).
        c.sock.settimeout(STATEMENT_TIMEOUT_S)
        return c

    def make_template(self, seal=True):
        t0 = time.perf_counter()
        self.admin.query("CREATE DATABASE %s" % TEMPLATE)
        c = self.conn(TEMPLATE)
        c.query(template_sql(self.args.tables, self.args.rows))
        c.query("VACUUM ANALYZE")
        size = int(c.query("SELECT pg_database_size(current_database())")[0][0])
        c.close()
        t1 = time.perf_counter()
        if seal:
            self.admin.query("SELECT pgrust_seal_template('%s')" % TEMPLATE)
        return {"tables": self.args.tables, "rows_per_table": self.args.rows, "size_bytes": size,
                "build_s": t1 - t0, "seal_s": time.perf_counter() - t1}

    def mint(self, token):
        """Connect to a new ephemeral name and run one query. Returns (seconds, conn)."""
        t0 = time.perf_counter()
        c = self.conn("%s%s__%s" % (PREFIX, TEMPLATE, token))
        rows = c.query("SELECT count(*) FROM accounts")
        dt = time.perf_counter() - t0
        assert rows == [[str(self.args.rows)]], rows
        return dt, c

    def mem(self):
        s = pb.memory_sample(self.srv.proc.pid)
        return {"footprint_bytes": s[pb.MEM_KEY], "rss_bytes": s["rss_bytes_sum"],
                "threads": s.get("thread_count"), "raw": s}

    def datadir_bytes(self):
        out = subprocess.run(["du", "-sk", self.srv.datadir], capture_output=True, text=True).stdout
        return int(out.split()[0]) * 1024

    def ephemeral_dbs(self):
        return [r[0] for r in self.admin.query(
            "SELECT datname FROM pg_database WHERE datname LIKE 'tdb\\_%' ORDER BY 1")]

    def close(self):
        try:
            self.admin.close()
        except OSError:
            pass
        if self.hung:
            pb.stack_dump(self.srv.proc.pid, os.path.join(self.args.out, "hang-sample-%d.txt" % int(time.time())))
            self.srv.proc.kill()
        self.srv.stop()
        self.ws.cleanup()


def ms(values):
    return {k: (v * 1e3 if k != "n" else v) for k, v in pb.summarize(values).items()}


def scenario_main(args):
    """density, idle, active, reconnect, isolation, burst: one long-lived server."""
    b = Bench(args, ["pgrust.ephemeral_db_grace=3600"])
    res = {}
    try:
        res["template"] = b.make_template()
        time.sleep(3)
        base_mem, base_disk = b.mem(), b.datadir_bytes()
        checkpoints = [int(x) for x in args.counts.split(",")]
        curve = [{"databases": 0, "footprint_bytes": base_mem["footprint_bytes"], "rss_bytes": base_mem["rss_bytes"],
                  "threads": base_mem["threads"], "datadir_bytes": base_disk}]
        latencies = []
        for n in range(1, max(checkpoints) + 1):
            dt, c = b.mint("d%d" % n)
            c.close()
            latencies.append(dt)
            if n in checkpoints:
                time.sleep(args.settle)
                m = b.mem()
                curve.append({"databases": n, "footprint_bytes": m["footprint_bytes"], "rss_bytes": m["rss_bytes"],
                              "threads": m["threads"], "datadir_bytes": b.datadir_bytes()})
                print("density n=%-4d footprint=%.1f MB  datadir=%.0f MB  last mint=%.1f ms" % (
                    n, m["footprint_bytes"] / M, curve[-1]["datadir_bytes"] / M, dt * 1e3), flush=True)
        n_max = max(checkpoints)
        last = curve[-1]
        res["density"] = {
            "definition": "sequential mint-on-connect of N databases (connect to a new name, SELECT count(*), "
                          "disconnect); memory and data directory sampled with no clients connected",
            "mint_latency_ms": ms(latencies),
            "mint_latency_first_30_ms": ms(latencies[:30]),
            "mint_latency_last_30_ms": ms(latencies[-30:]),
            "curve": curve,
            "per_database_footprint_bytes": (last["footprint_bytes"] - curve[0]["footprint_bytes"]) / n_max,
            "per_database_disk_bytes": (last["datadir_bytes"] - curve[0]["datadir_bytes"]) / n_max,
            "mint_latencies_s": latencies}

        # idle: all databases exist, no clients except the harness's admin session.
        b.admin.close()
        time.sleep(10)
        s0 = pb.memory_sample(b.srv.proc.pid)
        t0 = time.perf_counter()
        time.sleep(args.idle_window)
        s1 = pb.memory_sample(b.srv.proc.pid)
        win = time.perf_counter() - t0
        cpu = s1["cpu_user_ns_sum"] + s1["cpu_system_ns_sum"] - s0["cpu_user_ns_sum"] - s0["cpu_system_ns_sum"]
        res["idle"] = {"databases": n_max, "window_s": win, "footprint_bytes": s1[pb.MEM_KEY],
                       "cpu_percent_of_one_core": cpu / (win * 1e9) * 100,
                       "disk_written_bytes": s1.get("disk_written_bytes_sum", 0) - s0.get("disk_written_bytes_sum", 0),
                       "idle_wakeups": s1.get("idle_wakeups_sum", 0) - s0.get("idle_wakeups_sum", 0)}
        print("idle with %d dbs: footprint=%.1f MB cpu=%.3f%%" % (
            n_max, s1[pb.MEM_KEY] / M, res["idle"]["cpu_percent_of_one_core"]), flush=True)
        b.admin = b.conn("postgres")

        # reconnect: existing idle databases, never connected since their mint.
        lat = []
        for n in range(1, min(30, n_max) + 1):
            t0 = time.perf_counter()
            c = b.conn("%s%s__d%d" % (PREFIX, TEMPLATE, n))
            c.query("SELECT count(*) FROM accounts")
            lat.append(time.perf_counter() - t0)
            c.close()
        res["reconnect"] = {"definition": "connect to an existing idle ephemeral database and run one query",
                            "latency_ms": ms(lat)}

        # active: K distinct databases each with one open connection that has touched a table.
        before = b.mem()
        conns = []
        for n in range(1, args.active + 1):
            c = b.conn("%s%s__d%d" % (PREFIX, TEMPLATE, n))
            c.query("SELECT count(*) FROM t0 WHERE account_id < 10")
            conns.append(c)
        time.sleep(args.settle)
        during = b.mem()
        for c in conns:
            c.close()
        res["active"] = {"databases_existing": n_max, "databases_active": args.active,
                         "footprint_before_bytes": before["footprint_bytes"],
                         "footprint_active_bytes": during["footprint_bytes"],
                         "per_active_database_bytes":
                             (during["footprint_bytes"] - before["footprint_bytes"]) / args.active,
                         "threads_active": during["threads"]}
        print("active %d of %d: footprint=%.1f MB" % (args.active, n_max, during["footprint_bytes"] / M), flush=True)

        # isolation.
        a, c2 = b.conn("%s%s__d1" % (PREFIX, TEMPLATE)), b.conn("%s%s__d2" % (PREFIX, TEMPLATE))
        a.query("INSERT INTO accounts(email) VALUES ('only-in-d1'); CREATE TABLE only_d1(x int)")
        in_a = a.query("SELECT count(*) FROM accounts")[0][0]
        in_b = c2.query("SELECT count(*) FROM accounts")[0][0]
        b_sees_table = c2.query("SELECT count(*) FROM pg_class WHERE relname = 'only_d1'")[0][0]
        a.close()
        c2.close()
        _, fresh = b.mint("fresh_after_write")
        in_fresh = fresh.query("SELECT count(*) FROM accounts")[0][0]
        fresh.close()
        res["isolation"] = {"rows_in_written_db": int(in_a), "rows_in_sibling_db": int(in_b),
                            "sibling_sees_new_table": b_sees_table != "0", "rows_in_fresh_mint": int(in_fresh),
                            "isolated": int(in_a) == args.rows + 1 and int(in_b) == args.rows
                            and b_sees_table == "0" and int(in_fresh) == args.rows}

        # burst: K clients ask for K new databases at the same moment.
        lat, errs = [None] * args.burst, []

        def one(i):
            try:
                dt, c = b.mint("burst%d" % i)
                c.close()
                lat[i] = dt
            except Exception as e:  # noqa: BLE001 - recorded, not swallowed
                errs.append(str(e)[:200])
        threads = [threading.Thread(target=one, args=(i,)) for i in range(args.burst)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.perf_counter() - t0
        ok = [x for x in lat if x is not None]
        res["burst"] = {"clients": args.burst, "succeeded": len(ok), "errors": errs, "wall_s": wall,
                        "latency_ms": ms(ok) if ok else None}
        print("burst %d: wall=%.0f ms ok=%d" % (args.burst, wall * 1e3, len(ok)), flush=True)
        res["server_log_tail"] = open(b.srv.log_path, errors="replace").read()[-1500:]
    except (socket.timeout, TimeoutError) as e:
        b.hung = True
        raise ServerHang(str(e))
    finally:
        b.close()
    return res


def scenario_pool(args):
    b = Bench(args, ["pgrust.ephemeral_db_grace=3600", "pgrust.ephemeral_db_pool_size=%d" % args.pool])
    try:
        b.make_template()
        first, c = b.mint("first")  # served cold; registers the template as pooled
        c.close()
        deadline = time.time() + 60
        spares = 0
        while time.time() < deadline:
            spares = len([d for d in b.ephemeral_dbs() if d.startswith(PREFIX + "spare_")])
            if spares >= args.pool:
                break
            time.sleep(0.2)
        warm = []
        for i in range(args.pool):
            dt, c = b.mint("warm%d" % i)
            c.close()
            warm.append(dt)
        m = b.mem()
        return {"pool_size": args.pool, "spares_ready_before_test": spares, "first_cold_mint_ms": first * 1e3,
                "warm_mint_latency_ms": ms(warm), "warm_mint_latencies_s": warm,
                "footprint_bytes": m["footprint_bytes"]}
    except (socket.timeout, TimeoutError) as e:
        b.hung = True
        raise ServerHang(str(e))
    finally:
        b.close()


def scenario_reap(args):
    b = Bench(args, ["pgrust.ephemeral_db_grace=%d" % args.grace])
    try:
        b.make_template()
        time.sleep(2)
        m0, d0 = b.mem(), b.datadir_bytes()
        for i in range(args.reap_count):
            _, c = b.mint("r%d" % i)
            c.close()
        t_idle = time.perf_counter()
        m1, d1 = b.mem(), b.datadir_bytes()
        gone = None
        while time.perf_counter() - t_idle < args.grace + 120:
            if not b.ephemeral_dbs():
                gone = time.perf_counter() - t_idle
                break
            time.sleep(0.25)
        time.sleep(3)
        m2, d2 = b.mem(), b.datadir_bytes()
        return {"databases": args.reap_count, "grace_s": args.grace, "all_dropped_after_s": gone,
                "footprint_before_bytes": m0["footprint_bytes"], "footprint_with_dbs_bytes": m1["footprint_bytes"],
                "footprint_after_reap_bytes": m2["footprint_bytes"],
                "datadir_before_bytes": d0, "datadir_with_dbs_bytes": d1, "datadir_after_reap_bytes": d2}
    except (socket.timeout, TimeoutError) as e:
        b.hung = True
        raise ServerHang(str(e))
    finally:
        b.close()


def scenario_createdb(args):
    """Reference: what the same clone costs through plain SQL, on either engine."""
    b = Bench(args, ephemeral=False)
    try:
        tpl = b.make_template(seal=False)
        b.admin.query("ALTER DATABASE %s WITH IS_TEMPLATE true ALLOW_CONNECTIONS false" % TEMPLATE)
        out = {"template": tpl}
        for strategy in ("file_copy", "wal_log"):
            lat = []
            for i in range(args.createdb_count):
                name = "plain_%s_%d" % (strategy, i)
                t0 = time.perf_counter()
                b.admin.query("CREATE DATABASE %s TEMPLATE %s STRATEGY %s" % (name, TEMPLATE, strategy))
                c = b.conn(name)
                c.query("SELECT count(*) FROM accounts")
                lat.append(time.perf_counter() - t0)
                c.close()
            out[strategy + "_create_and_connect_ms"] = ms(lat)
            print("createdb %s p50=%.1f ms" % (strategy, out[strategy + "_create_and_connect_ms"]["p50"]), flush=True)
        return out
    except (socket.timeout, TimeoutError) as e:
        b.hung = True
        raise ServerHang(str(e))
    finally:
        b.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenarios", nargs="*", default=["main", "pool", "reap", "createdb"])
    ap.add_argument("--engine", choices=["pgrust", "postgres"], default="pgrust")
    ap.add_argument("--binary")
    ap.add_argument("--conf", help="file appended to postgresql.conf")
    ap.add_argument("--guc", action="append", default=[], help="extra server setting name=value (repeatable)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=54360)
    ap.add_argument("--tables", type=int, default=50)
    ap.add_argument("--rows", type=int, default=200)
    ap.add_argument("--counts", default="1,10,30,100,300", help="database counts at which to sample memory")
    ap.add_argument("--settle", type=float, default=3.0)
    ap.add_argument("--idle-window", type=float, default=20.0)
    ap.add_argument("--active", type=int, default=10)
    ap.add_argument("--burst", type=int, default=12)
    ap.add_argument("--pool", type=int, default=8)
    ap.add_argument("--grace", type=int, default=5)
    ap.add_argument("--reap-count", type=int, default=30)
    ap.add_argument("--createdb-count", type=int, default=10)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "ephemeral.json")
    doc = json.load(open(path)) if os.path.exists(path) else {}
    doc.update({"benchmark": "ephemeral", "engine": args.engine, "conf": args.conf, "extra_gucs": args.guc,
                "git_commit": pb.git("rev-parse", "HEAD"), "load_average": os.getloadavg(),
                "template_shape": {"tables": args.tables, "rows_per_table": args.rows}})
    fns = {"main": scenario_main, "pool": scenario_pool, "reap": scenario_reap, "createdb": scenario_createdb}
    for name in args.scenarios:
        print("== " + name, flush=True)
        for attempt in range(4):
            try:
                result = fns[name](args)
                break
            except ServerHang:
                doc.setdefault("server_hangs", []).append({"scenario": name, "attempt": attempt + 1})
                print("server hang in %s (attempt %d); retrying on a fresh server" % (name, attempt + 1), flush=True)
        else:
            sys.exit("scenario %s hung four times in a row" % name)
        if name == "main":
            doc.update(result)
        else:
            doc[name] = result
        with open(path, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
    print("wrote", path)


if __name__ == "__main__":
    main()
