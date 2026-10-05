# One runtime, many databases: what holds and what does not

Results of the experiments set out after the first ephemeral-database
evaluation. Same machine as `docs/pgx/baseline.md` (Apple M1 Pro, 10 cores,
16 GB, macOS, APFS). Settings: `configs/pgx-ephemeral.conf` unless stated.
Engine: commit `a4babda0bf` (the hang fix) or later.

Two items have their own reports: `vacuum-analyze-hang.md` and `cow-cloning.md`.

## Update

Since this report was written: the shared catalog cache is purged on DROP
DATABASE, two leaks were fixed, and enforced memory limits were added. See
`memory-reclamation.md` and `memory-limits.md`. Questions 3 and 4 below now
read: memory grows by about 11 KB per database lifecycle instead of 34 KB
plus a 650 MB cache; and a per-session, per-database or per-runtime memory
limit can be enforced. The Linux port is described in `linux.md`.

## Answers to the seven questions

| # | Question | Answer | Basis |
|---|---|---|---|
| 1 | Can the global VACUUM hang be fixed? | Yes. Fixed. | 12 hangs in 40 runs before, 0 in 100 after |
| 2 | Can copy-on-write remove the full-copy cost? | Yes, by configuration, on a filesystem with block cloning | 0.2 MB and 85–91 ms per clone from 19 MB to 674 MB |
| 3 | Does memory plateau under churn? | **No.** Two effects: a cache that plateaus near 650 MB, and steady growth of about 33 KB per database created that did not stop in 10,000 databases | `churn.py`, 100 cycles |
| 4 | Can one noisy database be contained? | For CPU, IO, locks, errors, cancel and kill: yes in these tests. For memory: **no limit was enforced** | `noisy-neighbor.py`, 10 scenarios, two configurations |
| 5 | Does idle CPU stay low at 1,000 databases? | Only with autovacuum off (0.14% of a core). With it on, 4.1% and growing with database count | `scale.py` |
| 6 | Do realistic templates clone quickly? | Yes with `clone`: 104 ms for 58 MB, 133 ms for 461 MB, 85 ms for 674 MB | `ephemeral.py`, `cow.py` |
| 7 | Can one runtime hold hundreds or thousands of databases? | 1,000 idle databases ran in 659 MB with 9 threads and no errors. Not shown safe for long-lived churn (question 3) | `scale.py` |

Recommendation: stay on the existing PgRust ephemeral architecture. Two
subsystems need engineering before it can run unattended: memory retained
per dropped database, and per-database memory limits. Neither calls for a
new shared runtime.

## Churn and retained memory

100 cycles; each mints 100 databases, runs a small write and read workload in
each, disconnects and waits for the janitor to drop them. 10,000 databases
in total. Raw data: `results/churn-06178aafd1/`.

| After cycle | Footprint | RSS | Threads | File descriptors | Databases left | Data directory |
|---|---|---|---|---|---|---|
| start | 55 MB | — | 9 | 106 | 0 | 70 MB |
| 1 | 142 MB | 51 MB | 9 | 106 | 0 | 70 MB |
| 5 | 394 MB | 62 MB | 9 | 106 | 0 | 70 MB |
| 9 | 625 MB | 48 MB | 9 | 107 | 0 | 70 MB |
| 10 | 643 MB | 70 MB | 9 | 106 | 0 | 70 MB |
| 20 | 682 MB | 52 MB | 9 | 106 | 0 | 70 MB |
| 40 | 748 MB | 51 MB | 9 | 106 | 0 | 70 MB |
| 60 | 814 MB | 62 MB | 9 | 106 | 0 | 70 MB |
| 80 | 881 MB | 72 MB | 9 | 107 | 0 | 70 MB |
| 100 | 949 MB | 51 MB | 9 | 106 | 0 | 70 MB |

Threads, file descriptors and disk are flat. Memory is not, and it has two
distinct parts.

**Part 1: about 57 MB per 100 databases until roughly 650 MB, then it stops.**
This is PgRust's process-wide shared catalog cache
(`crates/backend/utils/cache/l2cache`). It holds catalog and relation
entries for every database and is not purged when a database is dropped.
Its only bound is a global cap of 262,144 entries, after which it evicts
arbitrary entries. The source comment sizes that cap at about 75 MB; here it
is reached near 600 MB.

- With `shared_catalog_cache = off` the first three cycles end at 82, 85,
  90 MB instead of 142, 201, 257 MB.
- With the cap lowered to 20,000 entries (environment variable
  `PGRUST_L2_CACHE_MAX_ENTRIES`) the first cycle ends at 120 MB.
- The server's own ledger agrees that this memory is outside its tracked
  memory contexts: 2.5–6.5 MB in contexts while allocator-committed memory
  rises from 336 to 458 MB.

Classification: **cached**, live and reachable, but for databases that no
longer exist. Not a leak in the strict sense; in a churning runtime it
behaves like one until the cap is hit.

**Part 2: about 3.3 MB per 100 databases (33 KB each), linear from cycle 10
to cycle 100, with no sign of a plateau.** Turning off the shared catalog
cache, statistics collection, autovacuum or prewarming does not remove it
(`results/churn-06178aafd1/attribution.txt`). Its owner was **not
identified**. At this rate a runtime gains roughly 1 GB per 30,000 databases
created.

Classification: unknown. It could be a true leak, an unbounded per-database
table, or allocator fragmentation; these tests cannot tell which. Finding it
needs allocation profiling, which mimalloc on macOS does not offer out of
the box (Linux with heap profiling, or a build on the system allocator,
would).

The resident set size stays at 50–70 MB throughout because the OS compresses
the untouched pages; that is why RSS alone would have hidden all of this.

## Noisy neighbour and failure containment

Database B runs `SELECT 1` and a small indexed query in a loop; a second
thread opens a new connection to B four times a second. Database A
misbehaves. Raw data: `results/noisy-neighbor-bd666920c8/`.

Database B's indexed query, PGX profile:

| What A does | B p50 | B p95 | B p99 | B max | B errors | A's outcome |
|---|---|---|---|---|---|---|
| nothing (baseline) | 0.10 ms | 0.34 ms | 0.56 ms | 6.9 ms | 0 | — |
| sorts and aggregates 4M rows | 0.09 | 0.21 | 0.46 | 5.2 | 0 | finished, 8.3 s |
| open transaction, 400k-row insert, table lock held 8 s | 0.09 | 0.19 | 0.31 | 1.8 | 0 | rolled back |
| builds a 1.2 GB array (limit set to 1 GB) | 0.10 | 0.37 | 0.76 | 11.4 | 0 | **finished; not stopped** |
| sort forced to spill to temporary files | 0.09 | 0.19 | 0.47 | 3.4 | 0 | finished, 5.3 s |
| 10-billion-row cross join | 0.09 | 0.21 | 0.44 | 7.9 | 0 | stopped by `statement_timeout` |
| 200 failing statements | 0.08 | 0.16 | 0.24 | 0.8 | 0 | each error reported |
| 30 rolled-back transactions | 0.10 | 0.26 | 0.57 | 3.7 | 0 | — |
| long query, `pg_cancel_backend` | 0.09 | 0.26 | 0.45 | 1.2 | 0 | cancelled |
| long query, `pg_terminate_backend` | 0.09 | 0.24 | 0.58 | 3.9 | 0 | session killed |

Under default settings (runtime and parallel query on) the table is the
same within noise: p50 0.09–0.11 ms, p99 0.37–0.69 ms, no errors. New
connections to B kept working in every scenario (p99 under 20 ms). The
server stayed up throughout.

What remains database-local: query errors, transaction aborts, statement
timeouts, cancellation, session termination, table locks, temporary-file IO.

What does not:

- **Memory.** Nothing stopped A from taking 1.2 GB with
  `pgrust.memory_watchdog_limit` at 1 GB. In this fork the watchdog logs and
  dumps memory contexts; no enforcing per-query or per-database limit was
  found. A single database can therefore exhaust the runtime's memory, and
  an out-of-memory kill takes every database with it. Real exhaustion was
  not provoked on this laptop.
- **Lightweight-lock deadlocks**, as the VACUUM hang showed: no detector, no
  timeout, whole-runtime effect.
- **A crash**: one process, so any abort or panic in a critical section
  restarts everything. Not provoked here.

Limits of this test: A was a single session on a 10-core machine, so B
always had idle cores. CPU contention with more busy databases than cores,
and PgRust's admission controls (`max_active_queries`, the connection
queue), were not tested.

## Scale: 1,000 databases

Raw data: `results/scale-bd666920c8/`. Template 14.5 MB, cloned.

| Databases, none connected | Footprint | Threads | File descriptors | Mint p50 | Idle CPU, autovacuum on | Idle CPU, autovacuum off |
|---|---|---|---|---|---|---|
| 0 | 57 MB | 8 | 162 | — | 0.06% | 0.05% |
| 100 | 122 MB | 8 | 113 | 89 ms | 0.62% | — |
| 300 | 234 MB | 8 | 109 | 85 ms | 2.1% | 0.08% |
| 1,000 | 659 MB | 9 | 106 | 93 ms | 4.1% | 0.14% |

- Memory: about 0.60 MB per idle database, consistent with earlier runs.
  Most of it is the shared catalog cache described above.
- Threads and file descriptors do not grow with database count.
- Mint latency does not grow with database count.

With 1,000 databases existing:

| Active databases | Query p50 | p99 | Errors | Footprint | Server CPU |
|---|---|---|---|---|---|
| 20 | 0.26 ms | 3.9 ms | 0 | 700 MB | 24% of a core |
| 100 | 6.1 ms | 18.5 ms | 0 | 870 MB | 61% of a core |

Each active database ran a read and a single-row update about 100 times a
second. The 100-client figure is limited by the Python load generator as
much as by the server and should be read as "no errors", not as a latency
measurement.

## Idle CPU

The earlier jump between 100 and 300 databases was not leftover work. Idle
CPU is steady over a 5-minute window (3.1–5.2% in every 10-second interval
at 1,000 databases) and rises with database count.

Cause: autovacuum. Sampling the idle server shows the on-CPU time in the
autovacuum workers and launcher. The launcher visits every database once per
`autovacuum_naptime` (60 s), so with N databases it starts a worker every
60/N seconds, each of which connects to a database and inspects it.

With `autovacuum = off` idle CPU at 1,000 databases is 0.14% of a core. The
small remaining slope is the janitor, which scans the database list every
500 ms.

Autovacuum is on in the PGX profile because turning it off changes query
plans (see `phase1a-config.md`). For a runtime holding hundreds of mostly
idle databases that trade-off reverses. Options, none tested: a much longer
`autovacuum_naptime`; autovacuum off with `ANALYZE` run by the janitor when
a database is minted or after bulk loads; or teaching the launcher to skip
databases with no activity since its last visit.

## Warm pool

Algorithm (`postmaster/janitor/src/pool.rs`, `main_loop.rs`): one janitor
thread does everything, on a 500 ms tick. It serves waiting connections by
renaming a ready spare (about 3 ms), mints cold when no spare of that
template is ready, and refills the pool afterwards, at most 8 spares per
tick, each batch inside one transaction around a pair of checkpoints. The
first mint of a template is always cold and registers the template.

Simultaneous mint requests, PGX profile, 14.5 MB template, cloned
(`results/warm-pool-bd666920c8/`):

| Pool size | Burst of 1 | Burst of 10 | Burst of 50 | Burst of 100 |
|---|---|---|---|---|
| 0 | 95 ms | 603 ms | 2.0 s | 3.9 s |
| 1 | 3.5 ms | 802 ms | 2.1 s | 4.4 s |
| 4 | 3.5 ms | 183 ms | 2.3 s | 3.8 s |
| 8 | 3.4 ms | 7.3 ms (p95 113 ms) | 1.9 s | 2.7 s |
| 32 | 7.3 ms | 12 ms | 475 ms | 2.2 s |

Median latency per request. Refill after a burst took 0.1–0.7 s for pools
up to 8 and 3.5 s for a pool of 32.

- A burst no larger than the pool is served in milliseconds.
- A burst larger than the pool is slow for everyone, including the requests
  that could have had a spare, because the single janitor serves the whole
  batch in one pass. A burst of 100 cold mints takes 4–6 s to drain, about
  17–25 databases per second.
- The occasional 200–300 ms warm mint seen earlier (one in eight, in every
  run, including with 58 MB and 461 MB templates) fits this design: a
  request that arrives while the janitor is inside a refill batch waits for
  that batch's checkpoints. This explanation follows from the code and the
  timing; it was not confirmed with a trace.

Sizing rule that follows: the pool must be at least as large as the largest
burst expected within one refill period, per template.

## Realistic templates

`ephemeral.py` with larger templates, cloned
(`results/realistic-template-bd666920c8/`). The nominal 100 MB and 1 GB
targets came out at 58 MB and 461 MB.

| | 14.5 MB | 58 MB | 461 MB |
|---|---|---|---|
| Build / seal the template | 0.5 s / 0.1 s | 3.4 s / 0.4 s | 30 s / 2.1 s |
| Cold mint p50 / p95 | 102 / 136 ms (copy) | 104 / 156 ms | 133 / 200 ms |
| Warm mint p50 | 3.7 ms | 7.9 ms | 9.8 ms |
| Reconnect to an idle database, p50 | 5.4 ms | 5.9 ms | 5.1 ms |
| Memory per idle database | 0.67 MB | 0.74 MB | 0.88 MB |
| Memory per active database | 2.0 MB | 1.8 MB | 1.9 MB |
| Runtime footprint with only the template | 54 MB | 59 MB | 94 MB |

Memory per database depends on the number of tables and indexes, which is
the same in all three, not on the data volume. A schema with more tables
will cost more per database; that was not measured.

## Work that follows from this

1. Purge the shared catalog cache for a database when it is dropped, and
   make its cap a setting sized for the runtime. Removes part 1 of the
   retained memory.
2. Find the owner of the remaining 33 KB per database lifecycle.
3. Add an enforced memory limit per query or per database.
4. Decide the autovacuum policy for a many-database runtime.
5. Repeat on Linux, on the filesystem PGRun uses, with CPU oversubscription
   in the noisy-neighbour test.
6. Until 1–3 are done, the multi-runtime layout in the plan (several
   runtimes per host, each holding a bounded number of databases, restarted
   on a schedule) is the safe way to deploy: it caps both the blast radius
   and the retained memory.
