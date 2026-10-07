# Database minting on Linux: cold, warm, bursts

A database is minted by connecting to `<prefix><template>__<token>`: the
janitor either renames a warm spare (a catalog-only operation) or clones the
sealed template (`CREATE DATABASE … STRATEGY file_copy` with
`file_copy_method = clone`, i.e. ZFS block cloning). Host: `linux-host.md`.
Template: 50 tables, ~600 files, 19 MB.

## Single requests (`linux/mint-breakdown.py`, 40 sequential requests each)

Client time = connect to the new name → `SELECT 1` answered.

| Path | min | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| Cold mint | 68.6 | **74.1** | 94.5 | 101.6 | 104.2 ms |
| Warm mint (pool of 8, 1 s apart) | 4.9 | **6.2** | 8.5 | 9.4 | 9.8 ms |
| Reconnect to an existing database (floor) | 3.3 | **3.8** | 4.8 | 6.5 | 7.4 ms |

Cold mint, by stage (`PGRUST_MINT_TIMING=1`):

| Stage | p50 | p95 |
|---|---|---|
| Pre-checkpoint | 0.9 | 1.3 ms |
| Directory clone (~600 files) | **57.3** | 72.3 ms |
| Post-checkpoint | 0.8 | 1.4 ms |
| Rest of the mint transaction | 0.4 | 0.5 ms |
| Janitor total | 59.5 | 74.5 ms |
| Connection + first session in the new database | 15.4 | 18.6 ms |

- 96% of the janitor's time is the per-file clone. ZFS on this host creates
  ~11,000 files a second (~90 µs each) and does not go faster with threads
  (`zfs-cow.md`), so ~60 ms is this host's floor for a 600-file template.
- The first session in a brand-new database costs ~10 ms more than a
  reconnect. By the prewarm design (`pgrust.ephemeral_db_prewarm`) that is
  the new database's catalog/relcache bootstrap, which the post-mint prewarm
  removes for later connections but not for the first; not separately
  timed here.
- Warm mint is a rename plus a connection: 0.4 ms of janitor work.

## Bursts (`warm-pool.py`, pass 4, build `83fbf8d06e`)

N clients connect to N new names at the same instant. "Hits" = requests
served from a spare (database OID older than the burst). Drain = first
request sent → last `SELECT 1` answered. Refill = time for the pool to be
full again afterwards.

| Pool | Burst | Hits | p50 | p95 | p99 | Drain | Refill |
|---|---|---|---|---|---|---|---|
| 0 | 1 | 0 | 72 | 72 | 72 ms | 0.07 s | — |
| 0 | 10 | 0 | 267 | 271 | 272 ms | 0.28 s | — |
| 0 | 50 | 0 | 1.00 | 1.32 | 1.53 s | 1.53 s | — |
| 0 | 100 | 0 | 1.95 | 2.91 | 2.92 s | 2.97 s | — |
| 0 | 250 | 0 | 3.73 | 6.75 | 6.78 s | 6.81 s | — |
| 0 | 500 | 0 | 6.29 | 11.8 | 12.5 s | 12.6 s | — |
| 8 | 1 | 1 | 4.7 | 4.7 | 4.7 ms | 0.01 s | 0.56 s |
| 8 | 10 | 8 | 12.9 ms | 145 | 146 ms | 0.15 s | 0.61 s |
| 8 | 50 | 8 | 0.93 | 1.01 | 1.02 s | 1.04 s | 0.05 s |
| 8 | 100 | 8 | 1.19 | 2.07 | 2.62 s | 2.64 s | 0.16 s |
| 8 | 500 | 8 | 7.10 | 12.8 | 13.6 s | 13.8 s | 0.16 s |
| 32 | 1 | 1 | 7.0 | 7.0 | 7.0 ms | 0.01 s | 0.56 s |
| 32 | 10 | 10 | 21.0 | 26.3 | 26.3 ms | 0.03 s | 0.73 s |
| 32 | 50 | 32 | 235 | 519 | 528 ms | 0.54 s | 2.19 s |
| 32 | 100 | 32 | 1.07 | 2.61 | 2.65 s | 2.66 s | 2.12 s |
| 32 | 250 | 32 | 2.86 | 6.11 | 6.13 s | 6.17 s | 2.23 s |
| 32 | 500 | 32 | 7.09 | 13.6 | 13.7 s | 13.8 s | 2.08 s |

Every burst completed without errors (500 concurrent clients included).

- A burst up to the pool size is served at single-digit to tens of
  milliseconds; every request beyond it is a cold mint, and cold mints drain
  at **~35–40 databases per second** on this host (batched: up to 32 per
  transaction sharing one checkpoint pair, directory copies fanned over a few
  threads, which helps ~1.3× here).
- Refill is ~16 spares per second: at most 8 per 500 ms janitor tick
  (`POOL_REPLENISH_MAX`), which on this host is about what the disk can do
  anyway. On storage with faster file creation that cap would become the
  limit; it is a constant, not a setting, today.
- The practical rule: size the pool to the burst you want served warm.
  A pool of 32 spares costs ~32 × 0.55 MB of memory and ~32 × 2 MB of pool
  space.

### Bug found and fixed by this test

Before `a95801a13b`, two back-to-back bursts (250 then 500) made 90 of the
500 clients fail with `FATAL XX000 … the mint request table is full`. The
table is sized `max_connections + 64` because every pending request has a
parked connecting backend, but the capacity check also counted completed
requests lingering 5 s for the fresh-mint shield. Only pending requests
count now (regression test in `registry.rs`); the rerun above has zero
errors.

## Not on the engine path

These are engine-level timings over a Unix socket on the same host. A
product request (`PGRun API → DATABASE_URL → SELECT 1`) adds the control
plane, the host agent and the network; see `pgrun-interface.md` for the
budget. 6 ms is a warm-mint primitive, not a branch-creation time.
