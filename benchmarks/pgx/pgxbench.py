#!/usr/bin/env python3
"""PGX benchmark harness (LLM.md sections 8-11, tasks 3-5).

Measures the cost of a short-lived database server: startup latency, idle
memory, and per-connection memory. Standard library only; talks the Postgres
wire protocol directly over a Unix socket so client start-up cost (psql,
drivers) is not part of any number.

Subcommands:
  env       record commit / toolchain / machine / binary facts
  startup   process launch -> first successful SELECT 1
  idle      startup, connect, SELECT 1, disconnect, wait, measure memory + CPU
  conns     memory with N idle connections held open (default 0,1,10,100)
  all       env + startup + idle + conns

Every run writes raw samples as JSON under --out (one file per benchmark).
"""

import argparse
import ctypes
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


# --------------------------------------------------------------------------
# Minimal Postgres wire client (protocol 3.0, trust auth, simple query).
# --------------------------------------------------------------------------

class ServerNotReady(Exception):
    pass


class PgConn:
    def __init__(self, sockdir, port, user="postgres", database="postgres", timeout=None):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)  # None = block forever; socket.timeout on expiry
        try:
            self.sock.connect(os.path.join(sockdir, ".s.PGSQL.%d" % port))
            params = b"user\0" + user.encode() + b"\0database\0" + database.encode() + b"\0\0"
            self.sock.sendall(struct.pack("!ii", 8 + len(params), 196608) + params)
            self._until_ready()
        except BaseException:
            self.sock.close()
            raise

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ServerNotReady("connection closed by server")
            buf += chunk
        return buf

    def _read_msg(self):
        head = self._recv_exact(5)
        (length,) = struct.unpack("!i", head[1:])
        return head[:1], self._recv_exact(length - 4)

    def _until_ready(self):
        rows = []
        while True:
            kind, body = self._read_msg()
            if kind == b"E":
                raise ServerNotReady(body.replace(b"\0", b" ").decode(errors="replace"))
            if kind == b"R" and struct.unpack("!i", body[:4])[0] != 0:
                raise RuntimeError("server requested authentication; initdb with trust auth")
            if kind == b"D":
                (ncols,) = struct.unpack("!h", body[:2])
                off, row = 2, []
                for _ in range(ncols):
                    (clen,) = struct.unpack("!i", body[off:off + 4])
                    off += 4
                    if clen < 0:
                        row.append(None)
                    else:
                        row.append(body[off:off + clen].decode())
                        off += clen
                rows.append(row)
            if kind == b"Z":
                return rows

    def query(self, sql):
        payload = sql.encode() + b"\0"
        self.sock.sendall(b"Q" + struct.pack("!i", 4 + len(payload)) + payload)
        return self._until_ready()

    def close(self):
        try:
            self.sock.sendall(b"X" + struct.pack("!i", 4))
        except OSError:
            pass
        self.sock.close()


# --------------------------------------------------------------------------
# Process memory / CPU via libproc (macOS) or /proc (Linux).
# --------------------------------------------------------------------------

class _RusageInfoV2(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
    ]


class _MachTimebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


_libproc = None
_abs_to_ns = 1.0
if sys.platform == "darwin":
    _libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    _tb = _MachTimebase()
    ctypes.CDLL("/usr/lib/libSystem.dylib").mach_timebase_info(ctypes.byref(_tb))
    _abs_to_ns = _tb.numer / _tb.denom


def process_tree(root_pid):
    """root pid plus all descendants (stock Postgres forks; pgrust uses threads)."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout
    children = {}
    for line in out.splitlines():
        pid, ppid = (int(x) for x in line.split())
        children.setdefault(ppid, []).append(pid)
    tree, stack = [], [root_pid]
    while stack:
        pid = stack.pop()
        tree.append(pid)
        stack.extend(children.get(pid, []))
    return tree


def _proc_sample(pid):
    if sys.platform == "darwin":
        info = _RusageInfoV2()
        if _libproc.proc_pid_rusage(pid, 2, ctypes.byref(info)) != 0:
            return None
        return {
            "pid": pid,
            "rss_bytes": info.ri_resident_size,
            "phys_footprint_bytes": info.ri_phys_footprint,
            "cpu_user_ns": int(info.ri_user_time * _abs_to_ns),
            "cpu_system_ns": int(info.ri_system_time * _abs_to_ns),
            "idle_wakeups": info.ri_pkg_idle_wkups,
            "disk_read_bytes": info.ri_diskio_bytesread,
            "disk_written_bytes": info.ri_diskio_byteswritten,
        }
    try:
        with open("/proc/%d/status" % pid) as f:
            status = dict(l.split(":", 1) for l in f if ":" in l)
        with open("/proc/%d/stat" % pid) as f:
            stat = f.read().rsplit(")", 1)[1].split()
        with open("/proc/%d/smaps_rollup" % pid) as f:
            pss = sum(int(l.split()[1]) for l in f if l.startswith("Pss:"))
    except OSError:
        return None
    tick_ns = 1_000_000_000 // os.sysconf("SC_CLK_TCK")
    return {
        "pid": pid,
        "rss_bytes": int(status["VmRSS"].split()[0]) * 1024,
        "pss_bytes": pss * 1024,
        "cpu_user_ns": int(stat[11]) * tick_ns,
        "cpu_system_ns": int(stat[12]) * tick_ns,
    }


def thread_count(pid):
    out = subprocess.run(["ps", "-M", "-p", str(pid)], capture_output=True, text=True).stdout
    return max(len(out.splitlines()) - 1, 0)


def memory_sample(root_pid):
    """Per-process samples for the server's process tree plus sums.

    rss_bytes summed across a multi-process server double-counts shared
    memory; phys_footprint_bytes (macOS) / pss_bytes (Linux) does not, so the
    *_sum of those is the number to compare between engines.
    """
    procs = [s for s in (_proc_sample(p) for p in process_tree(root_pid)) if s]
    sample = {"process_count": len(procs), "processes": procs}
    if sys.platform == "darwin":
        sample["thread_count"] = sum(thread_count(p["pid"]) for p in procs)
    for key in ("rss_bytes", "phys_footprint_bytes", "pss_bytes", "cpu_user_ns",
                "cpu_system_ns", "idle_wakeups", "disk_read_bytes", "disk_written_bytes"):
        if procs and key in procs[0]:
            sample[key + "_sum"] = sum(p[key] for p in procs)
    return sample


# --------------------------------------------------------------------------
# Engines.
# --------------------------------------------------------------------------

def pg18_prefix():
    for cand in (os.environ.get("PG18_PREFIX"), "/opt/homebrew/opt/postgresql@18",
                 "/usr/local/opt/postgresql@18", "/usr/lib/postgresql/18"):
        if cand and os.path.exists(os.path.join(cand, "bin", "initdb")):
            return cand
    sys.exit("PostgreSQL 18 tools not found; set PG18_PREFIX")


def pg18_sharedir():
    return subprocess.run([os.path.join(pg18_prefix(), "bin", "pg_config"), "--sharedir"],
                          capture_output=True, text=True, check=True).stdout.strip()


class Engine:
    """How to launch one server variant. Launch settings are part of the result."""

    def __init__(self, name, binary, extra_args=(), conf_file=None):
        self.name = name
        self.binary = os.path.abspath(binary)
        self.extra_args = list(extra_args)
        self.conf_file = conf_file
        self.env = {}
        self.stack_limit = None
        if name == "pgrust":
            # Launch line from the pgrust README quickstart, unchanged.
            share = pg18_sharedir()
            self.env = {"PGRUST_PGSHAREDIR": share,
                        "PGRUST_TZDIR": os.path.join(share, "timezone"),
                        "RUST_MIN_STACK": "33554432"}
            self.engine_args = ["-c", "io_method=sync", "-c", "max_stack_depth=60000"]
            self.stack_limit = 65520 * 1024
        else:
            self.engine_args = []

    def argv(self, datadir, sockdir, port):
        return ([self.binary, "-D", datadir, "-k", sockdir, "-p", str(port),
                 "-c", "listen_addresses="] + self.engine_args + self.extra_args)

    def describe(self):
        return {"name": self.name, "binary": self.binary, "env": self.env,
                "args": self.argv("$DATADIR", "$SOCKDIR", "$PORT")[1:],
                "stack_limit_bytes": self.stack_limit, "conf_file": self.conf_file}


class Workspace:
    """Scratch area holding an initdb template and per-run copies of it."""

    def __init__(self, engine, workdir=None):
        self.engine = engine
        self.root = tempfile.mkdtemp(prefix="pgxbench-", dir=workdir or "/tmp")
        self.template = os.path.join(self.root, "template")
        self.sockdir = self.root
        subprocess.run([os.path.join(pg18_prefix(), "bin", "initdb"), "-D", self.template,
                        "--no-locale", "--encoding", "UTF8", "-U", "postgres", "-A", "trust"],
                       check=True, capture_output=True)
        if engine.conf_file:
            with open(engine.conf_file) as src, \
                    open(os.path.join(self.template, "postgresql.conf"), "a") as dst:
                dst.write("\n# --- %s ---\n%s" % (engine.conf_file, src.read()))
        self.counter = 0

    def fresh_datadir(self):
        self.counter += 1
        path = os.path.join(self.root, "data-%d" % self.counter)
        # -c: APFS clone where available, plain copy elsewhere.
        flags = "-cR" if sys.platform == "darwin" else "-R"
        subprocess.run(["cp", flags, self.template, path], check=True)
        return path

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class Server:
    def __init__(self, ws, port):
        self.ws, self.port = ws, port
        self.datadir = ws.fresh_datadir()
        self.log_path = self.datadir + ".log"
        self.proc = None

    def launch(self):
        eng = self.ws.engine
        env = dict(os.environ, **eng.env)

        argv = eng.argv(self.datadir, self.ws.sockdir, self.port)
        if eng.stack_limit:
            # `ulimit -s` then exec, as in the README; the pid stays the server's.
            # (setrlimit from Python fails with EINVAL on macOS, so use the shell.)
            argv = ["/bin/sh", "-c", 'ulimit -s %d; exec "$0" "$@"' % (eng.stack_limit // 1024)] + argv
        self.log = open(self.log_path, "wb")
        self.t_launch = time.perf_counter()
        self.proc = subprocess.Popen(argv, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        return self

    def wait_select1(self, timeout=120.0):
        """Poll until SELECT 1 succeeds. Returns seconds since launch for each milestone."""
        sock_path = os.path.join(self.ws.sockdir, ".s.PGSQL.%d" % self.port)
        t_sock = t_conn = None
        deadline = self.t_launch + timeout
        while time.perf_counter() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited with %s; see %s" % (self.proc.returncode, self.log_path))
            if not os.path.exists(sock_path):
                time.sleep(0.0005)
                continue
            if t_sock is None:
                t_sock = time.perf_counter()
            try:
                conn = PgConn(self.ws.sockdir, self.port)
            except (OSError, ServerNotReady):
                time.sleep(0.0005)
                continue
            t_conn = time.perf_counter()
            rows = conn.query("SELECT 1")
            t_done = time.perf_counter()
            conn.close()
            if rows != [["1"]]:
                raise RuntimeError("SELECT 1 returned %r" % (rows,))
            return {"socket_visible_s": t_sock - self.t_launch,
                    "connected_s": t_conn - self.t_launch,
                    "select1_s": t_done - self.t_launch}
        raise RuntimeError("timed out waiting for SELECT 1; see %s" % self.log_path)

    def connect(self):
        return PgConn(self.ws.sockdir, self.port)

    def stop(self):
        if self.proc is None:
            return None
        t0 = time.perf_counter()
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)  # fast shutdown
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log.close()
        elapsed = time.perf_counter() - t0
        shutil.rmtree(self.datadir, ignore_errors=True)
        return elapsed


# --------------------------------------------------------------------------
# Statistics and output.
# --------------------------------------------------------------------------

def percentile(values, p):
    """Linear-interpolated percentile (same definition as numpy's default)."""
    xs = sorted(values)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(values):
    return {"n": len(values), "min": min(values), "p50": percentile(values, 50),
            "p95": percentile(values, 95), "p99": percentile(values, 99),
            "max": max(values), "mean": sum(values) / len(values)}


def git(*args):
    return subprocess.run(["git", "-C", REPO] + list(args), capture_output=True, text=True).stdout.strip()


def tool_version(*argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True).stdout.strip().splitlines()[0]
    except (OSError, IndexError):
        return None


def sysctl(name):
    return tool_version("sysctl", "-n", name)


def collect_env(engine):
    st = os.stat(engine.binary)
    sha = hashlib.sha256()
    with open(engine.binary, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
    env = {
        "recorded_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "git_commit": git("rev-parse", "HEAD"),
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty_tracked_files": bool(git("status", "--porcelain", "--untracked-files=no")),
        "engine": engine.describe(),
        "binary_size_bytes": st.st_size,
        "binary_sha256": sha.hexdigest(),
        "server_version": tool_version(engine.binary, "--version"),
        "initdb_version": tool_version(os.path.join(pg18_prefix(), "bin", "initdb"), "--version"),
        "rustc": tool_version("rustc", "--version"),
        "cargo": tool_version("cargo", "--version"),
        "build_command": "cargo build --release --locked --bin postgres" if engine.name == "pgrust" else None,
        "python": platform.python_version(),
        "os": platform.platform(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
    }
    if sys.platform == "darwin":
        env.update({
            "cpu_model": sysctl("machdep.cpu.brand_string"),
            "ram_bytes": int(sysctl("hw.memsize")),
            "macos": tool_version("sw_vers", "-productVersion"),
            "filesystem": tool_version("sh", "-c", "mount | grep ' on / ' | head -1"),
        })
    return env


def system_cpu_idle_percent():
    """How busy the rest of the machine is; timings from a busy host are noisy."""
    if sys.platform != "darwin":
        return None
    out = subprocess.run(["top", "-l", "2", "-n", "0", "-s", "1"], capture_output=True, text=True).stdout
    idle = re.findall(r"CPU usage:.* ([0-9.]+)% idle", out)
    return float(idle[-1]) if idle else None


def write_result(out_dir, name, engine, payload):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name + ".json")
    doc = {"benchmark": name, "engine": engine.name, "git_commit": git("rev-parse", "HEAD"),
           "load_average_at_end": os.getloadavg(),
           "system_cpu_idle_percent_at_end": system_cpu_idle_percent(),
           "recorded_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    doc.update(payload)
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    print("wrote", path)


def mb(n):
    return "%.1f MB" % (n / 1048576.0)


MEM_KEY = "phys_footprint_bytes_sum" if sys.platform == "darwin" else "pss_bytes_sum"


# --------------------------------------------------------------------------
# Benchmarks.
# --------------------------------------------------------------------------

def bench_startup(ws, args):
    """Task 3: process launch -> SELECT 1, on a pristine copy of an initdb'd cluster."""
    samples = []
    for i in range(args.warmup + args.iterations):
        srv = Server(ws, args.port).launch()
        try:
            timing = srv.wait_select1()
        finally:
            timing_stop = srv.stop()
        if i < args.warmup:
            continue
        timing["shutdown_s"] = timing_stop
        samples.append(timing)
        print("startup %3d/%d  select1=%.1f ms" % (len(samples), args.iterations, timing["select1_s"] * 1e3))
    summary = {k + "_ms": {s: v * 1e3 if s != "n" else v
                           for s, v in summarize([x[k] for x in samples]).items()}
               for k in ("socket_visible_s", "connected_s", "select1_s", "shutdown_s")}
    write_result(args.out, "startup", ws.engine, {
        "definition": "wall time from fork/exec of the server to completion of the first successful "
                      "SELECT 1 over a Unix socket; fresh copy of an initdb template per iteration",
        "iterations": args.iterations, "warmup_discarded": args.warmup,
        "summary": summary, "samples": samples})
    return summary


def bench_idle(ws, args):
    """Task 4: startup, connect, SELECT 1, disconnect, wait, measure."""
    samples = []
    for i in range(args.idle_iterations):
        srv = Server(ws, args.port).launch()
        try:
            srv.wait_select1()
            after_query = memory_sample(srv.proc.pid)
            time.sleep(args.idle_wait)
            start = memory_sample(srv.proc.pid)
            t0 = time.perf_counter()
            time.sleep(args.idle_window)
            end = memory_sample(srv.proc.pid)
            window = time.perf_counter() - t0
        finally:
            srv.stop()
        cpu_ns = (end["cpu_user_ns_sum"] + end["cpu_system_ns_sum"]
                  - start["cpu_user_ns_sum"] - start["cpu_system_ns_sum"])
        sample = {"after_select1": after_query, "idle": end, "idle_window_s": window,
                  "idle_cpu_ns": cpu_ns, "idle_cpu_percent_of_one_core": cpu_ns / (window * 1e9) * 100}
        for key in ("idle_wakeups_sum", "disk_written_bytes_sum", "disk_read_bytes_sum"):
            if key in end:
                sample["idle_window_" + key.replace("_sum", "")] = end[key] - start[key]
        samples.append(sample)
        print("idle %2d/%d  rss=%s  %s=%s  cpu=%.3f%%" % (
            i + 1, args.idle_iterations, mb(end["rss_bytes_sum"]), MEM_KEY, mb(end[MEM_KEY]),
            sample["idle_cpu_percent_of_one_core"]))
    summary = {
        "idle_rss_bytes_sum": summarize([s["idle"]["rss_bytes_sum"] for s in samples]),
        "idle_" + MEM_KEY: summarize([s["idle"][MEM_KEY] for s in samples]),
        "idle_cpu_percent_of_one_core": summarize([s["idle_cpu_percent_of_one_core"] for s in samples]),
        "process_count": samples[0]["idle"]["process_count"],
        "thread_count": samples[0]["idle"].get("thread_count"),
    }
    write_result(args.out, "idle", ws.engine, {
        "definition": "server started, one connection runs SELECT 1 and disconnects, wait "
                      "idle_wait_s, then memory is sampled after a further idle_window_s during "
                      "which CPU time, wakeups and disk IO deltas are taken; no clients connected",
        "idle_wait_s": args.idle_wait, "idle_window_s": args.idle_window,
        "iterations": args.idle_iterations, "summary": summary, "samples": samples})
    return summary


def bench_conns(ws, args):
    """Task 5: memory with N idle connections (each has run SELECT 1)."""
    counts = [int(x) for x in args.conn_counts.split(",")]
    samples = []
    for rep in range(args.conn_iterations):
        for n in counts:
            srv = Server(ws, args.port).launch()
            conns = []
            try:
                srv.wait_select1()
                for _ in range(n):
                    c = srv.connect()
                    c.query("SELECT 1")
                    conns.append(c)
                time.sleep(args.conn_settle)
                mem = memory_sample(srv.proc.pid)
            finally:
                for c in conns:
                    c.close()
                srv.stop()
            mem.update({"connections": n, "repetition": rep})
            samples.append(mem)
            print("conns rep %d  n=%-4d rss=%s  %s=%s  procs=%d threads=%s" % (
                rep + 1, n, mb(mem["rss_bytes_sum"]), MEM_KEY, mb(mem[MEM_KEY]),
                mem["process_count"], mem.get("thread_count")))
    summary = {}
    for n in counts:
        rows = [s for s in samples if s["connections"] == n]
        summary[str(n)] = {"rss_bytes_sum_median": percentile([r["rss_bytes_sum"] for r in rows], 50),
                           MEM_KEY + "_median": percentile([r[MEM_KEY] for r in rows], 50),
                           "process_count": rows[0]["process_count"],
                           "thread_count": rows[0].get("thread_count")}
    base = summary[str(counts[0])][MEM_KEY + "_median"]
    for n in counts[1:]:
        summary[str(n)]["per_connection_bytes"] = (summary[str(n)][MEM_KEY + "_median"] - base) / (n - counts[0])
    write_result(args.out, "conns", ws.engine, {
        "definition": "fresh server per measurement; N connections opened over a Unix socket, each "
                      "runs SELECT 1 and stays idle; memory sampled after settle_s. "
                      "per_connection_bytes = (median at N - median at lowest N) / delta N using " + MEM_KEY,
        "settle_s": args.conn_settle, "iterations": args.conn_iterations,
        "summary": summary, "samples": samples})
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["env", "startup", "idle", "conns", "all"])
    ap.add_argument("--engine", choices=["pgrust", "postgres"], default="pgrust",
                    help="launch convention: pgrust (README quickstart flags) or stock postgres")
    ap.add_argument("--binary", help="server binary (default: target/release/postgres, or PG18 postgres)")
    ap.add_argument("--label", help="result directory name under results/ (default: <engine>-<short sha>)")
    ap.add_argument("--out", help="result directory (overrides --label)")
    ap.add_argument("--conf", help="file appended to postgresql.conf in the template cluster")
    ap.add_argument("--server-arg", action="append", default=[], help="extra server argument (repeatable)")
    ap.add_argument("--port", type=int, default=54329)
    ap.add_argument("--workdir", help="directory for scratch clusters (default /tmp)")
    ap.add_argument("--iterations", type=int, default=30, help="startup iterations (>= 30)")
    ap.add_argument("--warmup", type=int, default=2, help="startup iterations discarded first")
    ap.add_argument("--idle-iterations", type=int, default=5)
    ap.add_argument("--idle-wait", type=float, default=10.0)
    ap.add_argument("--idle-window", type=float, default=20.0)
    ap.add_argument("--conn-counts", default="0,1,10,100")
    ap.add_argument("--conn-iterations", type=int, default=3)
    ap.add_argument("--conn-settle", type=float, default=5.0)
    args = ap.parse_args()

    if args.binary is None:
        args.binary = (os.path.join(REPO, "target", "release", "postgres") if args.engine == "pgrust"
                       else os.path.join(pg18_prefix(), "bin", "postgres"))
    if not os.path.exists(args.binary):
        sys.exit("server binary not found: " + args.binary)
    engine = Engine(args.engine, args.binary, args.server_arg, args.conf)
    if args.out is None:
        label = args.label or "%s-%s" % (args.engine, git("rev-parse", "--short=10", "HEAD"))
        args.out = os.path.join(HERE, "results", label)

    env = collect_env(engine)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "env.json"), "w") as f:
        json.dump(env, f, indent=2)
        f.write("\n")
    print("wrote", os.path.join(args.out, "env.json"))
    if args.command == "env":
        return

    ws = Workspace(engine, args.workdir)
    try:
        if args.command in ("startup", "all"):
            bench_startup(ws, args)
        if args.command in ("idle", "all"):
            bench_idle(ws, args)
        if args.command in ("conns", "all"):
            bench_conns(ws, args)
    finally:
        ws.cleanup()


if __name__ == "__main__":
    main()
