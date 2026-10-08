# Memory limits and the cgroup backstop on Linux

Settings (`memory-limits.md` has the mechanism): `pgrust.session_memory_limit`,
`pgrust.database_memory_limit`, `pgrust.runtime_memory_limit` (MB, 0 = off,
reloadable). A refused allocation fails the statement with PostgreSQL's
ordinary out-of-memory error (SQLSTATE 53200) and a hint naming the limit;
the session stays connected. Host: `linux-host.md`. Harness:
`benchmarks/pgx/linux/limits.py`.

## The three limits (passes 3 and 4, builds `999a78baff` / `83fbf8d06e`)

Query: `array_agg` over N generated integers with `work_mem = 8GB`; a
bystander database runs `SELECT 1` every 50 ms throughout; a brand-new
connection is opened afterwards.

| Case | Limit | Sessions | Outcome | Peak PSS |
|---|---|---|---|---|
| none | — | 1 × 30 M elements (~1.8 GB) | completes in ~7.9 s | 1,800–1,838 MB |
| session | 256 MB | 1 × 30 M | refused in 0.8 s | 332–350 MB |
| database | 512 MB | 2 × 5 M in one database + 1 × 5 M in another | one of the pair refused; the other completes; the other database's session completes | 790–812 MB |
| runtime | 2,048 MB | 4 × 20 M in four databases | three refused, one completes | 2,012–2,044 MB |

In every case and both passes:

- the refused statements returned SQLSTATE 53200 with the hint
  `The session reached pgrust.<…>_memory_limit`;
- the refused session then answered `SELECT 1` (it stays usable);
- the bystander database had 0 errors (max latency 0.6–5.2 ms);
- a new connection after the case succeeded;
- the server stayed up and its log had no PANIC.

Before `dde6c13de4` one of the runtime-case refusals came back as `XX000`
with a Rust debug dump (a sort's buffer growth `.expect()`ed the
allocator result). Fixed; every refusal since is 53200.

## cgroup backstop

Each server is started in its own transient systemd scope with
`MemoryMax=1G`, `MemorySwapMax=0`; the harness stays outside it, so an OOM
kill can only reach the server. Query: the 30 M-element `array_agg`.

| Case | Query | Server | cgroup OOM kills | cgroup peak | Host memory |
|---|---|---|---|---|---|
| `pgrust.runtime_memory_limit = 700` | refused, 53200 | alive | 0 | 676 MB | unaffected |
| no PgRust limit | connection lost | killed by the kernel (SIGKILL) | 1 | 1,024 MB | unaffected |

Kernel log for the second case:
`Memory cgroup out of memory: Killed process … (postgres) … oom_memcg=/system.slice/pgx-cgroup-unlimited-….scope`.

- The internal limits keep a runaway query from reaching the cgroup.
- Without them the kernel kills the **whole runtime** — every database in
  it loses its sessions. The host itself is never at risk.
- PgRust reads `memory.max` at start (`pool-qos memory governor bar`).
- What a control plane sees: the runtime process exits with SIGKILL, its
  socket refuses connections, `memory.events` in the scope records
  `oom_kill`. Restart = start the runtime again on the same data directory
  (crash recovery replays WAL; ephemeral databases are swept by the
  janitor at start). The PGX profile runs `fsync = off`: a killed process
  loses nothing the OS already has, but after a host crash nothing in the
  runtime is guaranteed intact — templates included, since with fsync off
  even a checkpoint does not reach the disk. Branches are disposable;
  templates must be restorable from durable golden storage (the planned
  object-storage copy) and re-verified after a host crash.

## Recommended production setting

```
cgroup memory.max                 = runtime's memory budget
pgrust.runtime_memory_limit       = 70–80% of memory.max
pgrust.database_memory_limit      = 2–4 × the session limit
pgrust.session_memory_limit       = a few hundred MB
```

The internal limits govern memory-context memory (query and cache memory),
not thread stacks, allocator overhead or the binary, which is why the
runtime limit must sit well below the cgroup.

## Memory-heavy workloads under a session limit

`limits.py --mode workloads`: sort, hash join, hash aggregate, materialized
CTE, JSON aggregation, `array_agg`, COPY out/in, large INSERT…SELECT,
CREATE INDEX, a rewriting ALTER TABLE and a spilling sort, each under
`pgrust.session_memory_limit = 256` with `work_mem = 2GB`. Results: pass 5
(WORKLOADS_PENDING).
