# Why an idle PgRust instance costs 67 MB and 36 threads

Investigation of the fixed per-instance cost (default configuration, one
connection has run `SELECT 1` and disconnected). Apple M1 Pro, 10 cores.
Source paths are relative to `crates/backend/`. Raw data:
`benchmarks/pgx/results/phase1a-7a7c305df8/idle-investigation/`.

## Short answer

Roughly 50 MB of the 67 MB is sized by configuration, not by need: two
pre-spawned worker pools (28 threads, about 25 MB), a 128 MB buffer pool of
which about 14 MB is touched at startup, and tables sized for 100
connections. `configs/pgx-ephemeral.conf` removes 43 MB of it without code
changes. The remaining floor is about 17 MB and 6 threads.

## The 36 threads

| Threads | Count | What | Controlled by | Lifetime |
|---|---|---|---|---|
| `pg:standby:<pid>` | 16 | Parked threads waiting to become parallel-query workers | `max_parallel_workers`; env `PGRUST_NO_WORKER_POOL` | Eager, permanent |
| `pg:rtworker<N>` | 12 | Worker pool of the morsel runtime (parallel analytics engine) | `pgrust.runtime`; env `PGRUST_RUNTIME_WORKERS` (default: cores), `PGRUST_RUNTIME_STANDBYS` (default 2) | Eager, permanent |
| `pg:checkpointer`, `pg:bgwriter`, `pg:wal_writer` | 3 | Standard auxiliary processes, as threads | — | Permanent |
| `pg:autovacuum launcher` | 1 | Standard | `autovacuum` | Permanent |
| `pg:bgworker` | 1 | Logical replication launcher (identified by measurement) | `max_logical_replication_workers = 0` | Permanent |
| `pg:memwatchdog` | 1 | Samples process memory every second, logs before an OOM kill | `pgrust.memory_watchdog*` | Permanent |
| `pg-timeout-timer` | 1 | Shared timer replacing SIGALRM | none | Lazy: created on first timeout use |
| main | 1 | Postmaster | — | — |

- Standby pool: `postmaster/launch_backend/src/lib.rs:1033-1050`; its size
  is `max_parallel_workers` (`:1119-1123`), topped up by `maintain()` (`:1145`).
- Runtime workers: `executor/runtime/src/lib.rs:246-249` (cores) plus
  `DEFAULT_STANDBYS = 2` (`:234`); named at `launch_backend/src/lib.rs:2150`.
- Watchdog: `postmaster/memwatchdog/src/lib.rs:12-13`, `:390-391`.
- Timer: `utils/misc/timeout/src/lib.rs:113-134`.

No thread at idle is JIT-specific or IO-specific (`io_method=sync` is the
README launch setting, so there are no IO workers).

## Stacks

Every backend-type thread reserves 128 MB of address space
(`child_thread_stack_size()`, `launch_backend/src/lib.rs:948-959`: the
larger of the stack rlimit and scaled `max_stack_depth` plus 8 MB, capped at
512 MB). That is address space only. Memory actually charged to stacks for
all 36 threads together is about 1.7 MB. **Stacks are not the problem.**

Each pooled thread does cost about 0.8–1 MB, but in heap, not stack: its
per-thread backend state (the standby docs mention a GUC store and, once
used, retained relation and catalog caches).

Client backends that sit idle for `idle_passivate_timeout` (60 s) drop their
caches and return memory (`tcop/postgres/src/passivate.rs`). That mechanism
applies to client sessions, not to the pooled worker threads.

## Memory

Allocator: mimalloc (`main/main_main/src/bin/postgres.rs:12-13`). macOS tools
label its arenas "IOAccelerator" because of the VM tag mimalloc uses.

| Category (macOS `footprint`) | Default | At the configuration floor |
|---|---|---|
| mimalloc heap | 49.0 MB | 8.1 MB |
| System malloc | 8.7 MB | 2.7 MB |
| Binary data (`__DATA*`) | 3.3 MB | 3.3 MB |
| Stacks | 1.6 MB | under 0.5 MB |
| Page tables | 1.2 MB | 0.6 MB |

Attribution, one setting changed at a time from the defaults
(`probe.json`; savings overlap and do not add up exactly):

| Change | Saved |
|---|---|
| `shared_buffers` 128 MB → 16 MB | 13.9 MB |
| `max_parallel_workers = 0` (16 standby threads) | 13.1 MB |
| `pgrust.runtime = off` (12 runtime workers) | 12.3 MB |
| `max_connections` 100 → 20 | 6.6 MB |
| `wal_buffers = 64kB` (default is 1/32 of `shared_buffers` = 4 MB) | 5.1 MB |
| `max_locks_per_transaction` 64 → 10 | 3.9 MB |
| First connection (catalog caches) | about 3 MB |
| `max_logical_replication_workers = 0` | 2.2 MB |
| `autovacuum = off` | 2.0 MB |
| `wal_level = minimal`, no senders or slots | 1.5 MB |
| `pgrust.memory_watchdog = off` | 1.3 MB |
| `shared_catalog_cache = off` | 1.1 MB |
| `jit = off` | 0.6 MB |
| `pg_prewarm.autoprewarm = off`, `pg_stat_statements.max = 100` | negligible |

Resident set size stays near 18 MB throughout because on this machine the
idle pages are compressed by the OS; `phys_footprint` counts them, RSS does
not. That is why RSS understates PgRust's cost and why footprint is the
figure used everywhere.

## What can be removed or made lazy

| Cost | Size | Config today | Code change that would help |
|---|---|---|---|
| Parallel-worker standby pool | 13 MB, 16 threads | `max_parallel_workers = 0` (loses parallel query) | Spawn standbys on first parallel plan instead of at startup |
| Runtime worker pool | 12 MB, 12 threads | `pgrust.runtime = off` (loses the analytics engine) | Start the pool on first engagement; size it below core count |
| Buffer pool touched at startup | 14 MB | `shared_buffers = 16MB` | Find what touches about 11% of the pool eagerly and make it lazy |
| WAL buffers | 4–5 MB | `wal_buffers = 64kB` | — |
| Connection-sized tables | 6.6 MB per 80 slots | `max_connections = 20` | Lazy per-slot allocation |
| Lock table | 3.9 MB | — (lowering it risks migrations) | Lazy growth |
| Floor: 6 threads, heap, binary data | 17 MB | — | Needs a heap profile |

The two worker pools are the clearest "make it lazy" candidates: an agent or
test database that never runs a parallel or analytic query would pay
nothing, and one that does would behave as today.

## Not determined

- How `shared_buffers` memory is allocated and what touches about 14 MB of
  a 128 MB pool at startup.
- What the ten ~0.9 MB system-malloc regions are (about 9 MB at defaults,
  3 MB at the floor). Their count matches the core count; the RE2/abseil
  C++ dependency is a guess, not a finding.
- The breakdown of the ~1 MB each pooled thread holds.
- The composition of the 8 MB heap floor.
- Linux numbers. Everything here is macOS on Apple Silicon; thread counts
  scale with core count, so a larger server will show bigger pools.

## Already in PgRust: ephemeral databases

PgRust has a feature for many disposable databases inside one server
(`utils/misc/guc_tables/src/tables.rs`):

- `pgrust.ephemeral_db_prefix` (`:1235`): database-name prefix owned by an
  ephemeral-database janitor; empty disables it.
- `pgrust.ephemeral_db_mint_roles` (`:1251`): roles that create an ephemeral
  database by connecting to it.
- `pgrust.ephemeral_db_grace` (`:1018`): idle time before the janitor drops one.
- `pgrust.ephemeral_db_pool_size` (`:1032`): pre-made spare clones per template.
- `pgrust.ephemeral_db_max_per_role` (`:1023`), `pgrust.ephemeral_db_prewarm`
  (`:766`), `pgrust.ephemeral_db_wal_log_threshold` (`:1042`).

This is the Phase 2 idea in LLM.md (one runtime, many cheap databases). It
has not been tested here. It should be evaluated before any Phase 2 design.
