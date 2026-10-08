# PGX Linux benchmarks: how to reproduce the v0.1 numbers

Everything behind `docs/pgx/linux-results.md` and `docs/pgx/v0.1-baseline.md`.
Python 3 standard library only, plus the PostgreSQL 18 tools (`initdb`,
`pg_regress`) for the reference runs. The shared client and server helpers
are in `../pgxbench.py`.

## Host setup

1. A Linux host with a ZFS pool and block cloning: `setup-host.sh` records
   what it does (packages, ZFS module options, ARC cap, dataset), and
   `docs/pgx/linux-host.md` describes the bench host.
2. Build: `cargo build --release --locked --bin postgres` (~5 min
   incremental on 4 vCPU).
3. Run a step: `PGXBENCH_WORKDIR=/tank/pgx-bench run-suite.sh <step> …`.
   Results go to `benchmarks/pgx/results/<hostname>-<short sha>/`.
   `pgx-run.sh` wraps that in a 6 GB no-swap cgroup scope with a 5 s host
   monitor (`host-monitor.csv`, `host-facts.txt`) and then runs the cgroup
   backstop test. Every v0.1 result was produced this way.

## Headline → script → raw result

`R` = `benchmarks/pgx/results/vps-d2a3c460-`. The build in the directory name
is the commit the binary was built from.

| Headline | Suite step | Script | Raw result |
|---|---|---|---|
| 1000 idle DBs, untouched (128–155 MB) | `scale`, `scale-naptime` | `../scale.py` | `R dedfdf9ca5/scale`, `…/scale-skipidle-naptime300` |
| 1000 idle DBs, each queried once (213 MB) | `scale-touched` | `../scale.py --touch` | `R 0131cc9b12/scale-touched` (217 MB on `R bb3a8ab54b/scale-touched`) |
| Active DBs among 1000 (p99, footprint) | `scale` | `../scale.py --active` | `R dedfdf9ca5/scale` |
| Idle CPU, 1000 DBs (0.33–0.43 %) | `scale-naptime` | `../scale.py` | `R 0131cc9b12/scale-skipidle-naptime300`, `R bb3a8ab54b/scale-skipidle-naptime300` |
| Skip-idle still vacuums written DBs | `skipidle` | `skipidle-check.py` | `R bb3a8ab54b/skipidle`, `R 1c5a9fc74f/skipidle` |
| 30,000-lifecycle churn (111 MB) | — (`churn.py --cycles 300`) | `../churn.py` | `R 727f31e49f/churn-30k` (run G); `R 60fca8427f/churn-30k` (run F, before the last fix) |
| 10,000-lifecycle churn, runs A–E | `churn` | `../churn.py` | `R 1781903578/churn` … `R d48f587da0/churn` (`docs/pgx/linux-churn.md`) |
| Retention by call site | — | `../alloc-trace.py` (tracker build, see its docstring) | printed; `/tmp/leaktrace.json` on the host |
| Shared-cache census | — | `SELECT` of `pgrust: memctx` (debug report) during a churn | printed |
| Warm / cold mint p50 / p95 / p99 | `pool`, `mintbreak` | `../warm-pool.py`, `mint-breakdown.py` | `R 83fbf8d06e/warm-pool`, `R 999a78baff/mint-breakdown` |
| Bursts of 10–500 new databases | `pool` | `../warm-pool.py` | `R 83fbf8d06e/warm-pool` |
| COW branch of a 1 / 2 / 5 / 10 GB template | `cowbig`, `cow10g` | `../cow.py` | `R 83fbf8d06e/cow-big`, `…/cow-big-copy`, `R dedfdf9ca5/cow-10g` |
| Recordsize 128K / 16K / 8K | `cow-recordsize` | `../cow.py` | `R dedfdf9ca5/cow-recordsize-16K`, `…-8K`; 128K from `cow-big` |
| Session / database / runtime limits | `limits` | `limits.py` | `R d48f587da0/limits/limits.json` |
| Memory-heavy workloads under a 256 MB session limit | `workloads` | `limits.py --mode workloads` | `R d48f587da0/limits/workloads.json` |
| cgroup backstop (1 GiB `memory.max`) | `pgx-run.sh` epilogue | `limits.py --mode cgroup` | `R d48f587da0/limits/cgroup.json` |
| INSERT…SELECT / UPDATE memory by row count | `insertmem` | `insert-memory-repro.py` | `R d48f587da0/insert-memory` |
| Which statement allocates what | — | `stmt-alloc-trace.py` (tracker build) | printed |
| Query latency vs PostgreSQL 18 | `querybench` | `query-bench.py` | `R dedfdf9ca5/query-bench` |
| Framework lifecycles incl. real Rails (36/36) | `agents` | `agent-workload.py`; Rails app from `rails-app-setup.sh` | `R 60fca8427f/agents` |
| Noisy neighbour, memory and misbehaviour | `noisy` | `../noisy-neighbor.py` | `R 9727706fe7/noisy-neighbor` |
| CPU noisy neighbour, connection limits | `cpunoisy`, `cpuagent` | `cpu-noisy.py` | `R dedfdf9ca5/cpu-noisy` |
| Several runtimes per host | `multiruntime` | `multiruntime.py` | `R 9727706fe7/multiruntime` |
| Memory breakdown, huge pages | `memory` | `memory-breakdown.py` | `R dedfdf9ca5/memory`, `…/memory-thp-disabled` |
| ANALYZE policy | `analyze` | `analyze-policy.py` | `R dedfdf9ca5/analyze-policy` |
| Regression suite (219 / 231, 0 semantic) | `regress` | `../regress-baseline.py`, `../classify-regress-diffs.py` | `R 727f31e49f/regress` |
| Branch economics | — | `economics.py` (prints its inputs and assumptions) | printed; `docs/pgx/linux-pricing-economics.md` |

Rails needs Ruby 3.3, Bundler and the `pg` gem build dependencies; run
`rails-app-setup.sh ~/rails-bench/app` once, then
`agent-workload.py --workloads rails --rails-dir ~/rails-bench/app --out <dir>`.

## What is not in the repository

Generated clusters, templates and branch data directories (they live under
`PGXBENCH_WORKDIR` and are deleted by each script), multi-GB server logs,
build outputs (`target/`, `target-track/`) and any host credentials. Long
server logs are left out of the committed results where they are larger
than a few MB.
