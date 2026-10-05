# Running the PGX benchmarks on Linux / ZFS

Status: the suite is ported and runs end to end on Ubuntu 26.04. It has
**not yet been run on a PGRun branch host**; that is waiting on a decision
(below). No Linux numbers in this repository should be read as results.

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

1. **Idle memory is much higher on Linux.** Default configuration: about
   303 MB PSS (67 MB footprint on macOS). PGX profile: about 97 MB (23.7 MB
   on macOS). Part of this may be the metric, part may be real (how the
   allocator commits memory, the buffer pool, transparent huge pages). Not
   investigated. Every memory number in the other reports is macOS-only
   until this is understood.
2. **`file_copy_method = clone` did nothing on overlayfs**, as expected for
   a filesystem without reflinks: each clone cost its full 19 MB and minting
   was slower than a plain copy. Whether it shares blocks on the branch
   host's ZFS is the first thing to measure there.

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
