# PGX Phase 1A — configuration-only profile

What configuration alone can do to an untouched PgRust build (LLM.md §15,
§16; tasks 9 and 10). No engine code was changed. Same binary, machine and
harness as `docs/pgx/baseline.md`.

Raw data: `benchmarks/pgx/results/phase1a-7a7c305df8/<group>/`.
Group files: `configs/pgx/groups/`. Combined profile: `configs/pgx-ephemeral.conf`.
Rerun: `benchmarks/pgx/run-config-groups.sh`.

## Result

| | Baseline PgRust | PGX profile | PostgreSQL 18.6 |
|---|---|---|---|
| Idle footprint | 66.7 MB | **23.7 MB** | 40.0 MB |
| Idle RSS | 18.4 MB | 18.2 MB | 21.3 MB |
| Threads at idle | 36 | **7** | 9 processes |
| Idle CPU (one core) | 0.052% | 0.032% | 0.035% |
| Idle disk writes (20 s) | 16 KB | 0 | 16 KB |
| Footprint, 1 connection | 68.7 MB | 24.5 MB | 41.0 MB |
| Footprint, 10 connections | 81.9 MB | 38.4 MB | 85.2 MB |
| Startup p50 | 83.0 ms | 74.9 ms | 30.2 ms |
| Regression files passing | 219 / 231 | 223 / 231 | — |

Configuration removes about 43 MB (64%) of the fixed instance cost and 29 of
36 threads. It does not fix startup: every group lands between 73 and 87 ms
p50, against 30 ms for PostgreSQL. Startup needs profiling (task 11), not
settings.

## Each group on its own

Each row is one group applied alone to the default configuration. Startup:
100 runs. Idle: 5 runs. Connections: median of 3.

| Group | Startup p50 / p95 / p99 (ms) | Idle footprint | Idle RSS | Threads | Idle CPU | Idle disk writes | 1 conn | 10 conns | 100 conns | Regress pass |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 baseline | 83.0 / 116.1 / 258.9 | 66.7 MB | 18.4 MB | 36 | 0.052% | 16 KB | 68.7 | 81.9 | 217.7 | 219 |
| 1 durability | 87.1 / 107.8 / 125.8 | 67.4 MB | 18.3 MB | 36 | 0.058% | 0 | 68.8 | 81.6 | 217.6 | 218 |
| 2 replication off | 78.1 / 88.7 / 108.2 | 65.1 MB | 18.4 MB | 35 | 0.036% | 0 | 66.7 | 80.1 | 216.2 | 219 |
| 3a JIT off | 83.2 / 111.1 / 132.3 | 66.9 MB | 18.5 MB | 36 | 0.048% | 16 KB | 69.0 | 81.2 | 217.7 | 219 |
| 3b stock parallel defaults | 76.6 / 88.8 / 97.4 | 59.6 MB | 18.5 MB | 28 | 0.047% | 16 KB | 60.9 | 74.2 | 210.8 | 230 |
| 3c parallel off | 76.1 / 85.7 / 88.9 | 53.0 MB | 18.5 MB | 20 | 0.048% | 16 KB | 53.9 | 68.4 | 204.4 | 224 |
| 3d runtime off | 73.3 / 81.7 / 89.8 | 54.9 MB | 18.3 MB | 24 | 0.050% | 16 KB | 56.9 | 69.6 | 206.4 | 219 |
| 3e max_connections=20 | 78.0 / 87.9 / 92.1 | 61.0 MB | 18.5 MB | 36 | 0.043% | 16 KB | 62.2 | 75.7 | n/a | 219 |
| 4a autovacuum off | 78.4 / 87.5 / 90.0 | 65.2 MB | 18.3 MB | 35 | 0.049% | 16 KB | 66.7 | 80.3 | 217.0 | 209 |
| 4b stats collection off | 76.4 / 86.0 / 92.8 | 65.7 MB | 18.4 MB | 35 | 0.049% | 16 KB | 66.9 | 81.0 | 218.2 | 206 |
| 4c lazy checkpoints | 80.3 / 91.3 / 100.8 | 67.2 MB | 22.6 MB | 36 | 0.035% | 16 KB | 68.3 | 81.8 | 217.6 | 219 |
| 5 shared_buffers=16MB | 79.4 / 110.4 / 216.1 | 55.1 MB | 18.4 MB | 36 | 0.042% | 16 KB | 56.0 | 69.4 | 205.5 | 219 |
| **combined profile** | 74.9 / 95.5 / 119.1 | 23.7 MB | 18.2 MB | 7 | 0.032% | 0 | 24.5 | 38.4 | n/a | 223 |

Connection columns are footprint in MB. Startup differences of a few
milliseconds between groups are within noise: the machine was 70–80% idle
during the group runs, and 47% idle during the combined-profile run, which
overlapped with them.

### What each group actually changes

- **Durability** (`fsync`, `synchronous_commit`, `full_page_writes` off):
  nothing measurable at idle or startup. Its value is write throughput,
  which this benchmark set does not exercise yet (task 17).
- **Replication off**: one thread (the logical replication launcher), about
  1.6 MB, no idle disk writes.
- **JIT off**: nothing measurable for an idle instance.
- **Parallelism**: the largest thread effect. PgRust pre-spawns one parked
  worker thread per `max_parallel_workers` (16 by default). Stock values
  remove 8 threads and 7 MB and bring the regression suite to 230/231;
  turning parallel query off removes all 16 and 13.7 MB.
- **Runtime off** (`pgrust.runtime`): removes the 12-thread analytics worker
  pool, 11.8 MB.
- **max_connections=20**: 5.7 MB.
- **Autovacuum off**: one thread, 1.5 MB, and ten more regression files
  differ because plans change without automatic statistics.
- **Stats collection off**: about 1 MB, and it removes application-visible
  behaviour (`pg_stat_*` counters, COPY progress reporting).
- **Lazy checkpoints**: nothing measurable in a 20-second idle window.
- **shared_buffers=16MB**: 11.6 MB.

## The combined profile

`configs/pgx-ephemeral.conf` = parallel off + runtime off + `shared_buffers=16MB`
+ `wal_buffers=64kB` + `max_connections=20` + replication off + durability off.

Left out on purpose:

| Setting | Saves | Why not |
|---|---|---|
| `autovacuum = off` | ~1 MB, 1 thread | No automatic statistics; plans change in real applications |
| `track_activities` / `track_counts = off` | ~1 MB | Breaks `pg_stat_*` views and progress reporting |
| `max_connections = 10`, `max_locks_per_transaction = 10` | ~1.5 MB | Connection pools and large migrations would fail |
| `shared_buffers` below 16 MB | under 1 MB | Slows any real workload |
| `jit = off`, lazy checkpoints | nothing measured | No evidence yet |

### Regression status of the profile

223 of 231 files pass byte-for-byte. Of the 8 that differ:

- 6 are plan-only (`select_distinct`, `portals`, `incremental_sort`,
  `plpgsql`, `partition_prune`, `partition_aggregate`).
- `select_parallel` checks that parallel workers were launched; with
  parallel query off they are not.
- `stats` checks `current_setting('synchronous_commit') = 'on'`.

No query returns different rows.

## Configuration floor

Adding settings one at a time (single measurement per step, so differences
under about 1 MB are noise; `idle-investigation/ladder.json`):

| Step added | Idle footprint | Threads |
|---|---|---|
| Defaults | 65.9 MB | 36 |
| `max_parallel_workers = 0` | 53.2 MB | 20 |
| `pgrust.runtime = off` | 41.4 MB | 8 |
| `shared_buffers = 16MB` | 29.0 MB | 8 |
| `wal_buffers = 64kB` | 29.4 MB | 8 |
| `max_connections = 20` | 24.9 MB | 8 |
| `shared_buffers = 1MB` | 24.5 MB | 8 |
| `max_connections = 10` | 23.3 MB | 8 |
| `max_locks_per_transaction = 10` | 23.3 MB | 8 |
| replication off | 21.3 MB | 7 |
| `max_worker_processes = 1`, one autovacuum slot | 19.5 MB | 7 |
| `autovacuum = off` | 18.3 MB | 6 |
| watchdog off, shared catalog cache off, `jit = off` | 17.4–19.0 MB | 6 |
| `shared_buffers = 128kB` (minimum) | 16.9 MB | 6 |

About 17 MB and 6 threads is what configuration can reach. Below that
requires code changes; see `docs/pgx/idle-instance-cost.md`.

## Not yet measured

- Write-heavy and migration workloads, where the durability group matters.
- 100 connections under the profile (it caps connections at 20).
- Startup breakdown.
- Query performance with parallel query and the runtime pool off. Analytic
  queries will be slower; whether that matters for agent and test workloads
  needs the application benchmark (LLM.md §14).
