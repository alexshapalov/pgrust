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
CREATE INDEX, a rewriting ALTER TABLE and a spilling sort (`work_mem =
256kB`), each under `pgrust.session_memory_limit = 256` with `work_mem =
2GB` and `maintenance_work_mem = 2GB`, over a 3M-row table (`big`: int,
int, md5 text, jsonb; ~600 MB). A bystander database runs `SELECT 1` every
50 ms throughout. Pass 7, build `67269453b2` (re-run on `d48f587da0`)
(`vps-d2a3c460-d48f587da0/limits/workloads.json`):

| Workload | Outcome | Peak PSS |
|---|---|---|
| sort (`ORDER BY` md5) | refused, 53200, 1.5 s | 277 MB |
| hash join | refused, 53200, 2.1 s | 311 MB |
| hash aggregate | refused, 53200, 2.3 s | 316 MB |
| materialized CTE self-join | refused, 53200, 1.3 s | 299 MB |
| `json_agg` | refused, 53200, 2.1 s | 263 MB |
| `array_agg` | refused, 53200, 1.2 s | 292 MB |
| COPY out | ok, 3.4 s | 75 MB |
| COPY in (3M rows into a temp table) | ok, 6.4 s | 141 MB |
| `INSERT INTO ins SELECT * FROM big` | ok, 4.9 s | 76 MB |
| CREATE INDEX | ok, 5.3 s | 308 MB |
| rewriting `ALTER TABLE … TYPE` | ok, 6.7 s | 76 MB |
| sort with `work_mem = 256kB` (spills) | ok, 4.3 s | 78 MB |

- Every refusal is SQLSTATE 53200 with the session-limit hint; every
  session answered `SELECT 1` afterwards; the bystander had 0 errors (max
  10.4 ms); no internal errors, no PANIC.
- The refused six are the operations `work_mem = 2GB` lets keep their whole
  input in memory — the limit is doing its job. With a `work_mem` below the
  limit they spill, as the last row shows.
- Peak PSS is the whole runtime (~70 MB idle base, shared buffers, the
  binary), so it exceeds the 256 MB session figure without a breach.
  CREATE INDEX's sort is bounded by `maintenance_work_mem`'s tuplesort
  accounting and finished under the limit.
- **Bugs this found** (all DML paths that kept one tuple copy per row in
  the statement's arena until the statement ended):
  - `ea6d055cb2` INSERT … SELECT through a projection (2M rows +290 MB;
    3M rows refused);
  - `67269453b2` INSERT … SELECT without projection (`SELECT *` from a
    table): `large_insert` above, refused at 321 MB on the build before;
  - `daf827c0ff` UPDATE / MERGE UPDATE / ON CONFLICT DO UPDATE: a 3M-row
    whole-table UPDATE grew 400 MB.
  - `d48f587da0` corrects the two above: their first version also
    materialized *virtual* slots into per-row memory, but a virtual slot
    reuses its materialize buffer across rows, so multi-row `VALUES` into a
    partitioned table stored later rows' text columns as empty strings
    (caught by the regression suite: 14 tests changed results). Virtual
    slots keep the statement context (one reused buffer, no growth); the
    row context is node-owned and reset only between rows. Regression back
    to 219/231 with no semantic differences.
  
  `insert-memory-repro.py` (pass 7): peak growth at 1M / 2M / 3M rows

  | Statement | PGX before | PGX after | PostgreSQL 18 |
  |---|---|---|---|
  | INSERT … SELECT md5, jsonb FROM generate_series | 59 / ~290 / refused | 63 / 85 / 89 MB | 165 / 144 / 146 MB |
  | INSERT INTO t SELECT * FROM src | (refused in workloads) | 13 / 13 / 13 MB | 111 / 111 / 112 MB |
  | UPDATE t SET s = md5(…), j = jsonb… (every row) | 98 / 261 / 400 MB | 1 / 3 / 11 MB | 93 / 59 / 24 MB |

  The generate_series variants hold the function's output in `work_mem`
  (64 MB) on both engines; PostgreSQL's figures also include filling its
  shared buffers, so compare the trend with row count, not the absolute
  value.
