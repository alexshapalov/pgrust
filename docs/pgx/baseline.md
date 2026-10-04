# PGX baseline — untouched PgRust

Baseline for every later PGX experiment (LLM.md §7, tasks 1–5 and 8). No
PGX changes of any kind are included: no config file, no disabled subsystems.
PostgreSQL 18.6 was run through the same harness as a reference (task 7).

Raw data: `benchmarks/pgx/results/pgrust-17813ef9d8/` and
`benchmarks/pgx/results/postgres18.6-reference/`. Harness and definitions:
`benchmarks/pgx/README.md`.

## Environment

| | |
|---|---|
| Commit | `17813ef9d8ab849548d2dfcd4720a7236bd696bf` (`pgx-experimental-plan`) |
| Build | `cargo build --release --locked --bin postgres`, Rust 1.96.0, 5m 43s |
| Binary | 55,897,472 bytes (stock PG 18.6 `postgres`: 9,143,464 bytes) |
| Machine | Apple M1 Pro, 10 cores, 16 GB, macOS 14.4, APFS |
| Reference | PostgreSQL 18.6 (Homebrew), also the source of `initdb` and `pg_regress` |
| Launch | pgrust README quickstart: `io_method=sync`, `max_stack_depth=60000`, `RUST_MIN_STACK=33554432`, `ulimit -s 65520` |
| Date | 2026-10-03 / 2026-10-04 |

**The machine was not quiet.** It is a developer laptop running Docker
Desktop, browsers and Spotlight indexing: 68–74% CPU idle, load average
15–23 during every run. Memory figures were stable across runs; startup
percentiles, especially p95/p99, carry that noise. Two consecutive 100-run
startup measurements gave p50 81.1 / 78.6 ms (pgrust) and 30.3 / 30.2 ms
(PostgreSQL); the second is the one stored.

## Regression suite

PostgreSQL's own suite (`crates/postgres-18.6-reference/src/test/regress`,
`parallel_schedule`, 231 files) with stock `pg_regress --use-existing`,
compared byte-for-byte against the vendored expected output.

| Run | Pass | Differ |
|---|---|---|
| PgRust, default settings (the baseline) | 219 | 12 |
| PgRust, stock PostgreSQL parallel-planner settings (diagnostic) | 230 | 1 |

All 12 differences are **plan-only**: every changed line (1,156 of them) is
inside `EXPLAIN` output; no statement result, row count or error message
differs (`regress/classification.json`, produced by
`classify-regress-diffs.py`). The server did not crash.

Cause: PgRust ships more aggressive parallel-query defaults than PostgreSQL,
so the planner picks `Gather` / `Parallel Seq Scan` plans where the expected
output has serial ones.

| Setting | PgRust | PostgreSQL |
|---|---|---|
| `max_parallel_workers_per_gather` | 4 | 2 |
| `max_parallel_workers` / `max_worker_processes` | 16 | 8 |
| `min_parallel_table_scan_size` | 128 (1 MB) | 1024 (8 MB) |
| `min_parallel_index_scan_size` | 8 | 64 |
| `parallel_setup_cost` | 100 | 1000 |
| `parallel_tuple_cost` | 0.01 | 0.1 |

With those seven settings put back to PostgreSQL's values, 11 of the 12
files pass. The remaining one, `portals`, shows no `Materialize` node on top
of two `SCROLL` cursor plans; the fetched rows are identical. Upstream marks
exactly these two statements as a deliberate divergence
(`pgrust:ruled SCROLL-MATERIALIZE-WRAP` in `regress/overlay/sql/portals.sql`).

Files that differ at baseline: `create_index`, `select_distinct`,
`subselect`, `join`, `portals`, `tidscan`, `incremental_sort`, `limit`,
`partition_join`, `partition_prune`, `partition_aggregate`, `memoize`.

Not covered: upstream's own gate (`scripts/pg-regress-fast.sh`, which is not
in this fork), the isolation suite, and TAP tests.

## Startup: process launch → `SELECT 1`

100 runs after 2 discarded warm-ups; fresh copy of an `initdb` cluster each run.

| | min | p50 | p95 | p99 |
|---|---|---|---|---|
| PgRust | 68.1 ms | 78.6 ms | 98.0 ms | 109.0 ms |
| PostgreSQL 18.6 | 24.1 ms | 30.2 ms | 43.8 ms | 51.1 ms |

PgRust's socket appears at p50 39.2 ms; the remaining ~39 ms is spent before
the first connection is accepted and answered. The PgRust number includes a
`/bin/sh` exec that sets the stack limit. Fast shutdown: 18.6 ms vs 8.8 ms (p50).

## Idle: no clients

One connection runs `SELECT 1` and disconnects; 10 s wait; 20 s window. 5 runs.

| | Footprint | RSS | CPU (one core) | Processes / threads |
|---|---|---|---|---|
| PgRust | 67.1 MB | 18.4 MB | 0.060% | 1 / 36 |
| PostgreSQL 18.6 | 40.0 MB | 21.3 MB | 0.035% | 9 / 9 |

Footprint is macOS `phys_footprint` summed over the process tree; it does not
double-count shared memory and is the figure to compare. Why PgRust's RSS is
so far below its footprint is not yet explained (task 12, memory profile).
Both engines wrote 16 KB to disk during the 20 s window.

## Memory with idle connections

Fresh server per measurement, each connection has run `SELECT 1`; median of 3.

| Connections | PgRust | PostgreSQL 18.6 |
|---|---|---|
| 0 | 66.4 MB | 35.8 MB |
| 1 | 68.2 MB | 41.0 MB |
| 10 | 81.8 MB | 85.2 MB |
| 100 | 218.4 MB | 527.0 MB |
| per connection | 1.5 MB | 4.9 MB |

## Reading

Untouched PgRust starts about 2.6× slower than PostgreSQL and its empty server
is about 27 MB heavier, while each connection costs about a third as much.
The fixed per-server base and the startup path are the measured costs Phase 1
should go after first.

## Reproduce

```bash
cargo build --release --locked --bin postgres
python3 benchmarks/pgx/regress-baseline.py --out benchmarks/pgx/results/<label>/regress
python3 benchmarks/pgx/classify-regress-diffs.py benchmarks/pgx/results/<label>/regress
python3 benchmarks/pgx/pgxbench.py all --iterations 100
python3 benchmarks/pgx/pgxbench.py all --iterations 100 --engine postgres --label postgres18.6-reference
```
