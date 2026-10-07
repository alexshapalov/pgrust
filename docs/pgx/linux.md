# Running the PGX benchmarks on Linux / ZFS

Status: the suite has been run on a dedicated 4 vCPU / 8 GB OVH VPS with a
real ZFS pool on its own partition. Results and the answers to the open
questions: `linux-results.md`. The section on the branch host below is the
earlier inspection, kept for reference; nothing was run there.

## What exists

- `benchmarks/pgx/linux/setup-host.sh [zfs-dataset]` — installs build tools,
  RE2, PostgreSQL 18 (for `initdb` and `pg_regress`), gdb and Rust 1.96 for
  the current user; builds `target/release/postgres` at reduced priority
  with a capped job count; optionally creates one ZFS dataset owned by the
  current user and reports whether block cloning is enabled.
- `benchmarks/pgx/linux/run-suite.sh [step ...]` — runs regress, baseline,
  profile, ephemeral, cow, churn, noisy-neighbour (with and without a
  session memory limit), scale and warm-pool into
  `benchmarks/pgx/results/<hostname>-<sha>/`. `PGXBENCH_WORKDIR` selects the
  filesystem under test. Defaults are sized for 4 CPUs and 8 GB.
- `benchmarks/pgx/linux/limits.py` — session/database/runtime memory limits
  with bystander checks; `--mode cgroup` starts each server in its own
  systemd scope with a hard `memory.max` (needs passwordless sudo).
- `benchmarks/pgx/linux/multiruntime.py` — 1×1000 vs 2×500 vs 4×250.
- `benchmarks/pgx/linux/cpu-noisy.py` — quiet database latency with 0…N
  CPU-bound neighbours.
- `benchmarks/pgx/linux/memory-breakdown.py` — idle memory by Linux-native
  metrics (PSS split into anonymous / file / shared, huge pages, and PSS by
  kind of mapping) for PostgreSQL, PgRust defaults, each profile setting
  added in turn, and the profile. `PGXBENCH_THP_DISABLE=1` measures with
  transparent huge pages off for the server process.
- The harness itself: `/proc`-based memory (PSS), thread, file-descriptor
  and per-thread CPU readings; gdb backtraces in the hang reproducer; ZFS
  pool allocation as the physical-space measure in `cow.py`.

On Linux the memory figure is PSS (proportional set size) where macOS uses
`phys_footprint`. They are the closest equivalents but not the same
quantity; compare Linux with Linux.

## Validation

Ubuntu 26.04 arm64 container on the development laptop, Docker Desktop,
overlay filesystem:

- `setup-host.sh` completed; the build took 15 minutes with 6 jobs.
- All nine suite steps completed without error at reduced sizes.
- One portability bug found and fixed: Debian and Ubuntu ship no PostgreSQL
  timezone directory, so the server refused to start until the harness
  pointed `PGRUST_TZDIR` at `/usr/share/zoneinfo`.
- Regression suite on Linux arm64: 219 of 231, the same as macOS.
- The session memory limit behaved the same: the 1.2 GB query failed with
  the out-of-memory error at a 378 MB peak.

Two things seen there that must be checked on a real host, because a
container on a busy laptop cannot settle them:

1. **Idle memory looked much higher on Linux. Most of that is transparent
   huge pages, and the rest is a difference in what is counted.** Measured
   with `linux/memory-breakdown.py` (PSS, `/proc/<pid>/smaps`), idle server:

   | | Huge pages `always` | Huge pages off for the process |
   |---|---|---|
   | PostgreSQL 18 | 21.7 MB | 21.6 MB |
   | PgRust defaults | 297.4 MB (232 MB in huge pages) | 77.8 MB |
   | PGX profile | 93.9 MB (56 MB in huge pages) | 44.6 MB |

   - With huge pages on `always` (the Docker Desktop kernel's setting), the
     kernel backs each thread's stack reservation and each allocator arena
     with 2 MB pages on first touch. 36 threads and a handful of arenas turn
     into 232 MB. The fix on such a host is the kernel setting or a
     per-process opt-out, not anything in PgRust's configuration.
   - With them off, the PGX profile is 44.6 MB: 16.7 MB of anonymous memory
     (close to the 23.7 MB macOS footprint) plus 27.9 MB of the server
     binary's own pages. The macOS footprint does not count clean
     file-backed pages; PSS does. That 27.9 MB is shared between runtimes on
     the same host and is reclaimable under pressure.
   - The branch host is set to `madvise`, not `always` (read from
     `/sys/kernel/mm/transparent_hugepage/enabled`), so it should behave
     like the right-hand column. To be confirmed on a real host.
   - These are from an arm64 container (64 KB-page kernels differ; this one
     reports its page size in the JSON). Treat them as an explanation of the
     effect, not as the Linux baseline.
2. **`file_copy_method = clone` did nothing on overlayfs**, as expected for
   a filesystem without reflinks: each clone cost its full 19 MB and minting
   was slower than a plain copy. Whether it shares blocks on ZFS is the
   first thing to measure on a real host. The branch host's pool has the
   prerequisites: `feature@block_cloning` is active and
   `zfs_bclone_enabled` is 1.

## The branch host

Read-only inspection of the host named `branchhost` in the local SSH
configuration:

| | |
|---|---|
| OS | Ubuntu 26.04 LTS, kernel 7.0, x86_64 |
| CPU / RAM | 4 CPUs, 7 GB |
| ZFS | 2.4.1, pool `tank`, 19.5 GB, 17.4 GB free |
| PostgreSQL | 18 installed; one live branch server running |
| Load | idle at the time |
| Data | golden copies and branches under `tank/pgrun/...`; two existing `tank/pgrun-benchmark-pgrust-*` datasets |

Nothing was installed, created or changed on it.

Running the suite there would: install packages and a Rust toolchain; build
for roughly 20–30 minutes on its 4 CPUs; create one ZFS dataset; write up to
about 4 GB during the full-copy comparison; and hold up to about 1 GB of
memory during the churn and scale steps, on a 7 GB machine that is serving a
live branch.

Open decision: whether this host may be loaded like that while it serves
live branches, or whether a separate host of the same shape should be used.

## To run it

```bash
git clone --branch pgx-experimental-plan https://github.com/alexshapalov/pgrust.git && cd pgrust
benchmarks/pgx/linux/setup-host.sh tank/pgx-bench
PGXBENCH_WORKDIR=/tank/pgx-bench benchmarks/pgx/linux/run-suite.sh
```

Then commit `benchmarks/pgx/results/<hostname>-<sha>/`.
