# PGX benchmark harness

Measures what PGX cares about (LLM.md §2, §8–§11): how cheap a short-lived
database server is to start and to keep around. Not query throughput.

Python 3 standard library only. The client speaks the Postgres wire protocol
directly over a Unix socket, so no psql or driver start-up time is included in
any number.

## Prerequisites

- PostgreSQL 18 tools (`initdb`, `pg_config`, `pg_regress`): on macOS
  `brew install postgresql@18`; elsewhere set `PG18_PREFIX`.
- A server binary: `cargo build --release --locked --bin postgres`.

## Benchmarks

```bash
# everything, for the binary at target/release/postgres
python3 benchmarks/pgx/pgxbench.py all

# one benchmark
python3 benchmarks/pgx/pgxbench.py startup --iterations 30

# stock PostgreSQL 18 with the same harness, for reference
python3 benchmarks/pgx/pgxbench.py all --engine postgres

# a PGX experiment: same binary, extra settings, separate result directory
python3 benchmarks/pgx/pgxbench.py all --conf configs/pgx-ephemeral.conf --label pgx-ephemeral-<sha>
```

| command   | what is measured |
|-----------|------------------|
| `startup` | fork/exec of the server → first successful `SELECT 1`. Fresh copy of an `initdb` template per iteration, 2 warm-up runs discarded, 30 measured by default (`--iterations`). Reports p50/p95/p99 and also time to socket and time to connection. |
| `idle`    | start, connect, `SELECT 1`, disconnect, wait 10 s, then a 20 s window with no clients: memory at the end of it, plus CPU time, wakeups and disk bytes written during it. |
| `conns`   | memory with 0 / 1 / 10 / 100 idle connections that have each run `SELECT 1`. Fresh server per measurement, 3 repetitions, median reported, with bytes per connection. |
| `env`     | commit, branch, toolchain, machine, binary size and hash, exact launch arguments. Written for every run. |

pgrust is launched exactly as its README quickstart says (`io_method=sync`,
`max_stack_depth=60000`, `RUST_MIN_STACK=33554432`, 64 MB stack limit). Those
settings are recorded in `env.json`; changing them is an experiment, not a
baseline.

### Memory numbers

Each sample records every process in the server's tree. Two sums are given:

- `rss_bytes_sum`: resident set size. For a multi-process server (stock
  Postgres) this double-counts shared memory.
- `phys_footprint_bytes_sum` (macOS) / `pss_bytes_sum` (Linux): memory charged
  to the process, without double-counting. Use this one to compare engines.

## Regression baseline

```bash
python3 benchmarks/pgx/regress-baseline.py --out benchmarks/pgx/results/<label>/regress
```

Runs the vendored PostgreSQL 18.6 regression suite with stock `pg_regress`
against the built server; `classify-regress-diffs.py <dir>` then sorts each
differing file into plan-only (only `EXPLAIN` output changed) or semantic.
`--server-arg` turns a run into a diagnostic. See the docstring for how its verdict differs from
upstream pgrust's own gate, whose driver script is not part of this fork.

## Configuration experiments

`run-config-groups.sh` runs a fresh baseline and then each file in
`configs/pgx/groups/` on its own through the full benchmark set and the
regression suite, one result directory per group. Pass specific `.conf`
files to run only those.

## Results

`results/<label>/` holds `env.json`, `startup.json`, `idle.json`,
`conns.json` (summary plus every raw sample) and `regress/`. The default
label is `<engine>-<short commit>`. Numbers from different machines are not
comparable; `env.json` says where each set came from.
