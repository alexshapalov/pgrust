# PGX on Linux / ZFS: results from the 4 vCPU / 8 GB benchmark host

Host: OVH VPS `vps-d2a3c460`, 4 vCPU (x86_64), 7.6 GiB RAM, no swap, kernel
7.0 (Ubuntu 26.04), $8.50/month. Root shrunk to 25 GiB ext4; the remaining
48.9 GiB partition holds a real ZFS pool `tank` (ZFS 2.4.1, ashift 12,
compression off, atime off, ARC capped at 1 GiB, `zfs_bclone_enabled=1`).
No loop devices. THP system setting `madvise`.

Raw data: `benchmarks/pgx/results/vps-d2a3c460-9727706fe7/` (first pass) and
`vps-d2a3c460-1781903578/` (second pass, with the fixes below). Server logs
over 1 MB were left out. Every run was inside a 6 GB, no-swap cgroup with a
5 s host monitor (`host-monitor.csv`): swap was never used, available memory
never went below 4.4 GB, the bench cgroup peaked at 2.2 GB.

This host answers whether PGX works correctly and efficiently on Linux/ZFS.
It does not establish production capacity; the load generator runs on the
same four vCPUs as the server.

Numbers below are from the second pass (fixed build) unless marked "pass 1".

## Answers

| # | Question | Answer |
|---|---|---|
| 1 | Base memory, native Linux | PGX profile idle: **45.8 MB PSS** (16.8 MB anonymous + 29 MB binary pages, shared between runtimes). Stock PostgreSQL 18: 16.6 MB. |
| 2 | Incremental memory per idle DB | **0.58 MB** (86 MB → 669 MB from 0 to 1000 databases) |
| 3 | 1000 idle DBs in 8 GB? | Yes: **669 MB** for the runtime, 5+ GB still available |
| 4 | Memory plateau after 10,000 lifecycles? | **No.** Linear, ~1.1 MB per 100-database cycle (~11 KB per lifecycle): 85 → 262 MB. Same as macOS after its fixes. |
| 5 | Earlier leak fixes hold on Linux? | Yes: 11 KB/lifecycle vs 34 KB before them |
| 6 | Session/database/runtime limits | All three refuse correctly with SQLSTATE 53200 and a hint; session survives; bystanders unaffected; no PANIC |
| 7 | cgroup backstop | Works. With `runtime_memory_limit` below `memory.max`, the query fails gracefully. With no PgRust limit, the kernel OOM-kills the **whole runtime**; the host is unaffected. |
| 8 | Real COW on ZFS? | Yes (`file_copy_method=clone` → block cloning) |
| 9 | Clone latency | 71 / 74 / 95 / 113 ms for 19 / 74 / 339 / 674 MB templates |
| 10 | Physical bytes per fresh branch | **0.4–2.0 MB** regardless of template size |
| 11 | Storage after writes | ~1.2× logical for bulk inserts; small writes cost more (1 MB → 2.5 MB) because of 128 KB records |
| 12 | Idle CPU at 100 / 300 / 1000 DBs | 0.9 / 3.2 / 6.5 % of one core (autovacuum on); 0.10 / 0.12 / 0.23 % (autovacuum off) |
| 13 | Autovacuum effect | The autovacuum launcher is almost all idle CPU at density |
| 14 | 5 / 20 / 50 / 100 active | p99 1.8 / 2.3 / 12.9 / 43 ms, no errors; throughput saturates at ~50 active |
| 15 | 4 cores saturated by neighbours | Quiet DB p50 unchanged (0.6–0.9 ms), p99 1.1 → 6–8 ms, no errors |
| 16 | 1×1000 vs smaller runtimes | 1×1000 is cheapest (649 MB); each extra runtime adds ~35–55 MB base and idle CPU, no latency gain |
| 17 | Warm mint | **7 ms** p50 (pool of 8), 4–13 ms single requests |
| 18 | Cold mint | **70–75 ms** steady state; first mint after start 155–165 ms |
| 19 | request → usable DB → SELECT 1 | Same as mint: the connection to the new name mints it. Reconnect to an existing idle DB: 5.6 ms |
| 20 | Ready for PGRun integration? | Architecture yes; see blockers |

## Bugs found and fixed

| Commit | Fix |
|---|---|
| `dde6c13de4` | `tuplesort`/`tuplestore` `grow_memtuples` called `.expect()` on the allocator result, so a memory-limit refusal became a panic reported as XX000 with a Rust debug dump. Now returns the ordinary out-of-memory error and restores its accounting. Verified: all refusals are 53200. |
| `680fab7c8a` | mimalloc's `allow_thp` default marks allocator regions `MADV_HUGEPAGE`, so even on a `madvise` host the PGX runtime faulted in 2 MB pages. Now disabled at process start on Linux (`PGRUST_ALLOW_THP=1` keeps them). Idle PGX profile 83 → 46 MB; PgRust defaults 193 → 81 MB; 100-active footprint 1265 → 844 MB; churn slope 1.75 → 1.1 MB/cycle. |

Harness fixes: `cow.py` read pool allocation during deferred ZFS frees and
reported negative clone costs (`33035c540d`); `cpu-noisy.py` integer overflow
(`9727706fe7`); limits database case sized for Linux (`e31419b3ff`); cgroup
mode moved the server into its own scope (`1781903578`, `e90bc05091`).

Not fixed: the remaining ~11 KB per database lifecycle. It is mostly the
per-thread `session_root` shells that are deliberately never freed so that
`&'static` handles cannot dangle (see `memory-reclamation.md`). Freeing them
changes a use-after-free invariant in a process shared by every database;
that is not a contained change.

## Detail

### Memory, idle server (PSS)

| | THP default (`madvise`) | THP off for the process |
|---|---|---|
| PostgreSQL 18 | 16.6 MB | 16.3 MB |
| PgRust defaults, pass 1 | 193.4 MB (108 MB huge pages) | 78.6 MB |
| PgRust defaults, fixed | 81.2 MB | — |
| PGX profile, pass 1 | 82.7 MB | 45.4 MB |
| PGX profile, fixed | **45.8 MB** | — |

Startup to `SELECT 1` (pass 1, PgRust defaults): 79.8 ms p50; PostgreSQL 18:
55.3 ms.

### Density (one runtime, PGX profile, autovacuum on)

| DBs | Footprint | Idle CPU (one core) | FDs |
|---|---|---|---|
| 0 | 86 MB | 0.08 % | — |
| 100 | 145 MB | 0.92 % | 43 |
| 300 | 255 MB | 3.24 % | 40 |
| 500 | 371 MB | 5.70 % | 40 |
| 1000 | 669 MB | 6.46 % | 37 |

Autovacuum off: 87 / 137 / 235 / 341 / 602 MB, idle CPU ≤ 0.23 %.
Threads stay at 7–8 at every count.

### Active databases (1000 existing)

| Active | p50 | p99 | Server CPU | Footprint |
|---|---|---|---|---|
| 5 | 0.66 ms | 1.79 ms | 31 % | 679 MB |
| 20 | 0.64 ms | 2.31 ms | 98 % | 705 MB |
| 50 | 1.60 ms | 12.9 ms | 208 % | 756 MB |
| 100 | 13.0 ms | 43.0 ms | 214 % | 844 MB |

Each "query" is an indexed count plus a one-row update, then 10 ms sleep.
Throughput tops out at ~3,500 of these per second at 50 active (pass 1:
74,278 in 20 s at 50, 66,121 at 100). The Python load generator shares the
four vCPUs, so this is a host ceiling, not a runtime one.

### CPU noisy neighbour (pass 1)

| Noisy clients | Host CPU | Quiet p50 | p95 | p99 | New connection p50 |
|---|---|---|---|---|---|
| 0 | 4 % | 0.58 ms | 0.87 | 1.14 | 3.8 ms |
| 1 | 31 % | 0.56 | 0.83 | 2.06 | 3.8 |
| 2 | 56 % | 0.53 | 0.88 | 2.95 | 3.9 |
| 4 | 99 % | 0.59 | 3.41 | 6.22 | 7.7 |
| 6 | 100 % | 0.90 | 4.89 | 8.33 | 8.1 |

Failure containment (`noisy-neighbor.py`, pass 1): in all ten scenarios
(sort, long transaction, memory, temp spill, pathological, error, abort,
cancel, terminate) the quiet database's p99 stayed ≤ 1.3 ms with no errors.
With a 256 MB session limit the memory scenario was refused at 400 MB peak
instead of reaching 1.46 GB.

### Churn (100 databases × 100 cycles = 10,000 lifecycles)

| Cycle | 1 | 10 | 30 | 50 | 70 | 100 |
|---|---|---|---|---|---|---|
| Fixed build | 127 | 160 | 180 | 203 | 225 | 262 MB |
| Pass 1 | 167 | 231 | 266 | 299 | 343 | 391 MB |

Start: 85 MB (fixed). Slope by quarter, fixed build: 1.56 / 1.07 / 1.04 /
1.27 MB per cycle. Threads and FDs flat; data directory back to 73 MB after
every cycle.

### Memory limits (fixed build)

| Case | Outcome | Peak |
|---|---|---|
| none, one 30M-element `array_agg` | completes | 1,811 MB |
| session 256 MB | refused in 0.85 s | 376 MB |
| database 512 MB: two sessions in one DB + one in another, ~330 MB each | one of the pair refused; the other DB completes | 809 MB |
| runtime 2,048 MB: four sessions ~1.3 GB each | three refused, one completes | 2,050 MB |

Every refused session answered `SELECT 1` afterwards; the bystander and new
connections never failed.

cgroup backstop (server alone in a scope, `memory.max` 1 GiB, no swap):

| Case | Query | Server | OOM kills | cgroup peak |
|---|---|---|---|---|
| `runtime_memory_limit=700` | refused (53200) | alive | 0 | 674 MB |
| no PgRust limit | lost | killed (SIGKILL) | 1 | 1,024 MB |

PgRust also reads `memory.max` at start (`pool-qos memory governor bar`).

### ZFS copy-on-write (fixed measurement)

| Template (logical) | Clone mint p50 | Copy mint p50 | Physical per clone | Physical per copy | Block-cloned per clone |
|---|---|---|---|---|---|
| 19 MB | 71 ms | 87 ms | 0.38 MB | 20 MB | 19.7 MB |
| 74 MB | 74 ms | 171 ms | 1.94 MB | 89 MB | 86.7 MB |
| 339 MB | 95 ms | 465 ms | 2.01 MB | 350 MB | 348 MB |
| 674 MB | 113 ms | 933 ms | 1.98 MB | 680 MB | 678 MB |

The harness targets were 15 / 100 / 500 / 1000 MB; the schema builds to the
logical sizes shown. Writes into one clone (cumulative physical / logical):
+1 MB → 1.5–2.5 / 1.1 MB; +10 MB → 20–30 / 12.3 MB; +100 MB → 132–148 /
123 MB. `DROP DATABASE` of a clone: 33–69 ms, returning its 0.4–2 MB.

### Runtime topology (fixed build, 1000 databases total)

| Layout | Base, each runtime | Idle total | Idle CPU | Mint p50 / p99 | 20 active p99 | 50 active p99 |
|---|---|---|---|---|---|---|
| 1 × 1000 | 54 MB | 649 MB | 5.2 % | 72 / 101 ms | 2.33 ms | 15.7 ms |
| 2 × 500 | 42–43 MB | 724 MB | 10.2 % | 81 / 121 ms | 2.48 ms | 13.8 ms |
| 4 × 250 | 34–35 MB | 821 MB | 9.4 % | 102 / 209 ms | 2.45 ms | 13.4 ms |

### Warm pool and mint (pass 1)

| Pool | Burst 1 | Burst 10 p50 / p95 | Burst 50 p50 | Burst 100 p50 |
|---|---|---|---|---|
| 0 | 77 ms | 238 / 247 ms | 996 ms | 1,546 ms |
| 8 | 5 ms | 64 / 132 ms | 885 ms | 1,184 ms |
| 32 | 13 ms | 19 / 24 ms | 225 ms | 939 ms |

Bursts of 50–100 simultaneous new connections are limited by four vCPUs
accepting and authenticating 100 sessions, with or without spares. One
anomaly: pool 4, burst 1 took 116 ms (a miss with spares ready); single
sample, not investigated. `CREATE DATABASE … STRATEGY file_copy` + connect:
77 ms p50 (PostgreSQL 18: 312 ms).

### Regression suite

219 of 231 byte-exact, 12 plan-only differences: unchanged by both fixes and
identical to macOS.

## Recommended topology

- **One PGX runtime per host** (1 × N). Smaller runtimes buy failure
  isolation at ~35–55 MB and several percent of a core each, with no latency
  benefit at these loads. Use 2 runtimes only if halving the blast radius of
  an OOM kill or crash is worth ~75 MB.
- **Run each runtime in its own cgroup** with `memory.max`, and set
  `pgrust.runtime_memory_limit` to 70–80 % of it,
  `pgrust.session_memory_limit` to a few hundred MB, and
  `pgrust.database_memory_limit` to 2–4× the session limit. Without the
  runtime limit, one query can get every database in the runtime OOM-killed.
- **Restart runtimes on a schedule or after a mint count** until the
  per-thread shell leak is fixed: 11 KB per lifecycle is ~1 GB per 90,000
  databases created and dropped.
- **Tune autovacuum for density**: at 1000 databases the launcher costs ~6 %
  of a core while every database is idle. A longer `autovacuum_naptime` in
  the PGX profile is the obvious lever; not measured here.
- **ZFS with block cloning, compression as a separate decision** (off here
  so physical numbers are exact).

## Unit economics (estimate; this VPS, not production hardware)

Server: $8.50 / month = $0.0116 / hour (730 h). Price: $0.012 per
branch-hour + $0.28 per GB-month.

| Branches existing on average | Branch revenue / month | Server cost | Server margin |
|---|---|---|---|
| 1 | $8.76 | $8.50 | 3 % |
| 20 | $175 | $8.50 | 95 % |
| 100 | $876 | $8.50 | 99 % |
| 1,000 (idle-heavy) | $8,760 | $8.50 | ~100 % |

- Break-even is one branch existing continuously.
- **Memory capacity**: 1,000 idle branches use 0.67 GB; RAM is not the
  constraint at this density.
- **CPU capacity**: ~20 simultaneously busy branches stay under 2.5 ms p99,
  ~50 under 16 ms; beyond that latency rises with no added throughput.
  Billing is per branch-hour regardless of activity, so a host full of busy
  branches is the case to cap. Even 4 branches saturating all four cores pay
  $0.048/h against $0.0116/h.
- **Storage**: a fresh branch costs ~2 MB physical regardless of logical
  size; growth tracks writes (~1.2× for bulk, more for scattered small
  writes). The pool is 47 GB; $0.28/GB-month on all of it is $13/month.
  If storage is billed on logical size, cloned branches are almost pure
  margin; if on physical, it is about cost-plus.
- Not included: control plane, backups/object storage, egress, the template
  (golden) copy itself, support, and spare capacity. Repeat on production
  hardware before setting prices.

## Remaining blockers before PGRun integration

1. Residual ~11 KB per database lifecycle (session-root shells): needs an
   owner decision (pool shells, or free them from the reaping thread), or a
   restart policy.
2. Production config for limits + cgroup (above) must be part of the
   runtime launcher; without it an OOM takes the whole runtime.
3. PGRun's branch path: the earlier PgRust benchmark measured ~3.9 s of SSH
   executor dispatch per branch, against 7–113 ms mint here. Dispatch has to
   be persistent (ControlMaster or one batched call) for PGX speed to reach
   users.
4. Integration surface not built: branch → (runtime, database name) mapping
   in the control plane, gateway routing to a database inside a shared
   runtime, per-branch credentials, backup/restore of a single database.
5. Capacity numbers (maximum active branches, QPS, branch-creation
   concurrency) need a larger host with the load generator on a separate
   machine.
6. Autovacuum idle cost at density; warm-pool single-request miss seen once.
