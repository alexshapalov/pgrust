# PgRust's built-in ephemeral databases

Evaluation of the existing "many disposable databases in one server" feature
before designing any new shared-runtime architecture (LLM.md Phase 2, §34–§47).
Same machine and binary as `docs/pgx/baseline.md`; no engine code changed.

Raw data: `benchmarks/pgx/results/ephemeral-823d8d0738/`.
Benchmark: `benchmarks/pgx/ephemeral.py`.

## What the feature is

Source: `crates/backend/postmaster/janitor/` (about 8,500 lines, PgRust-only).

- **Template.** An ordinary database, prepared once and sealed with
  `SELECT pgrust_seal_template('name')` (vacuum-freeze, then marked as a
  non-connectable template; `seal.rs`).
- **Mint on connect.** A client connects to `<prefix><template>__<token>`.
  If that database does not exist, the server clones the template and lets
  the connection in (`mint.rs`, `grammar.rs`). No `CREATE DATABASE` and no
  API call: the connection string is the request.
- **Reap on idle.** A background worker, the janitor, drops any database
  under the prefix that has had no connections for
  `pgrust.ephemeral_db_grace` seconds (`lib.rs`, `reap.rs`). At server start
  it drops every database under the prefix.
- **Warm pool.** With `pgrust.ephemeral_db_pool_size = N` the janitor keeps
  N spare clones per template and hands one out by renaming it (`pool.rs`).
- **Pin.** `pgrust_pin_database` / `pgrust_unpin_database` exempt a database
  from reaping; pins are lost on restart.
- **Access control.** Only roles in `pgrust.ephemeral_db_mint_roles` can
  mint, with an optional per-role cap.

Settings used here: `pgrust.ephemeral_db_prefix = 'tdb_'`,
`pgrust.ephemeral_db_mint_roles = 'postgres'`.

## What it means for PGX

| | One PGX instance per database | Ephemeral databases in one server |
|---|---|---|
| Memory per idle database | 23.7 MB | about 0.7 MB |
| 300 idle databases | about 7.1 GB (extrapolated) | 251 MB (measured) |
| Create | about 80 ms start + a data directory copy | 100 ms cold, 3–6 ms from the warm pool |
| Reconnect to an idle one | — | 5 ms |
| Destroy | stop the process, delete the directory | automatic after the grace period |
| Isolation | separate process | separate database, shared process |

The first column uses the Phase 1A profile figures. The feature already
delivers most of what Phase 2 set out to build: one runtime, many databases,
cheap creation, automatic destruction. It does not deliver three things
Phase 2 wants, listed under "Gaps".

## Measurements

Template: 50 tables, each with a primary key and two indexes, 200 rows per
table, JSONB and foreign keys; 14.5 MB on disk. Building it takes 0.5 s and
sealing 0.1 s. "Default" is untouched PgRust plus the two settings above;
"profile" adds `configs/pgx-ephemeral.conf`.

### Density

300 databases minted one after another, no clients connected when sampled.

| Databases | Footprint, default | Footprint, profile | Data directory |
|---|---|---|---|
| 0 (template only) | 119 MB | 54 MB | 70 MB |
| 1 | 124 MB | 58 MB | 84 MB |
| 10 | 143 MB | 63 MB | 217 MB |
| 30 | 188 MB | 81 MB | 511 MB |
| 100 | 295 MB | 127 MB | 1,540 MB |
| 300 | 418 MB | 251 MB | 4,480 MB |
| per database | 1.0 MB | 0.67 MB | 14.7 MB |

- Memory grows with the number of databases that exist, even though none has
  a client. Turning off prewarming or autovacuum does not change it (about
  0.73 MB per database either way, measured at 100).
- The server with only the template loaded is already at 54 MB under the
  profile, against 23.7 MB for an empty idle server: building the template
  fills the buffer pool and caches.
- Disk is a full copy of the template per database. Nothing is shared.
- Thread count does not grow: 9 under the profile, 38 at defaults.

### Latency

| | Default | Profile |
|---|---|---|
| Cold mint (connect to a new name, first query), p50 / p95 / p99 | 126 / 167 / 193 ms | 102 / 136 / 154 ms |
| Same, first 30 vs last 30 of 300 (p50) | 130 vs 136 ms | 97 vs 99 ms |
| Warm-pool mint, p50 | 6.2 ms | 3.7 ms |
| Reconnect to an existing idle database, p50 / p95 | 5.6 / 13.3 ms | 5.4 / 6.4 ms |
| Burst: 12 clients ask for 12 new databases at once, all served in | 730 ms | 741 ms |
| Plain `CREATE DATABASE ... TEMPLATE` + connect (file_copy), p50 | 111 ms | 89 ms |
| Same on PostgreSQL 18.6 | 100 ms | — |

- Cold mint costs the same as a plain `CREATE DATABASE` with the file-copy
  strategy, on PgRust and on PostgreSQL alike. The time is the file copy and
  its checkpoints, and it will grow with template size.
- Mint latency does not degrade as databases accumulate.
- Warm-pool mints are 3–6 ms, but one of the eight in each run took over
  200 ms (222 and 280 ms). The cause was not investigated.

### Active databases

With 300 databases existing, opening one connection to each of 10 of them
and running a query adds about 2.0 MB per active database (footprint
263 → 283 MB under the profile).

### Idle cost

| | Default | Profile |
|---|---|---|
| CPU with 300 idle databases (one core) | 3.1% | 2.7% |
| CPU with 100 idle databases | — | 0.10% |
| Disk written in 20 s, 300 databases | 229 KB | 98 KB |
| Disk written in 20 s, 100 databases | — | 0 |

An empty idle server uses 0.03%. The jump between 100 and 300 databases is
not explained: at 100 it is 0.10% with or without autovacuum and prewarming.
The 300-database window started 10 s after the last mint, so leftover
background work is one possibility. It needs a longer observation window.

### Reaping

30 idle databases, grace 5 s: all dropped 11.5 s after the last disconnect.

| | Before | With 30 databases | After reap |
|---|---|---|---|
| Data directory | 70 MB | 511 MB | 70 MB |
| Footprint, profile | 54 MB | 81 MB | 80 MB |
| Footprint, default | 118 MB | 192 MB | 188 MB |

Disk comes back completely. **Memory does not**: the server keeps what the
dropped databases had grown it by.

### Isolation

A row inserted and a table created in one clone were not visible in a
sibling clone, and a database minted afterwards still matched the template.

## Gaps against the Phase 2 goals

1. **No copy-on-write.** Every database is a full file copy of its template:
   14.7 MB each here, and proportionally more for a realistic 100 MB
   dataset, in both disk and creation time. LLM.md §41 wants branches that
   share unchanged data. On PGRun this could come from the filesystem (ZFS
   or reflink copies) rather than from the engine; not tested.
2. **Idle databases are not free in memory, and memory is not returned.**
   About 0.7 MB per existing database with no clients, and nothing given
   back after a reap. LLM.md §37 wants an inactive database to cost close to
   its storage. What holds the memory was not identified.
3. **No resource isolation between databases.** One process, one buffer
   pool, one WAL, one crash domain. A crash or hang takes every database
   with it. LLM.md §45 (noisy neighbours) is not addressed by this feature;
   `max_active_queries` and the connection queue exist but were not tested.

Smaller points:

- Restart drops every ephemeral database by design, and pins do not survive
  a restart.
- The clone can only come from a template in the same server. Seeding from a
  production copy means loading it as a template first.
- The name must carry the template: `tdb_<template>__<token>`, 63 bytes
  at most.

## Stability finding

A database-wide `VACUUM ANALYZE` after building the template intermittently
wedged the whole server. This has since been diagnosed and fixed; see
`docs/pgx/vacuum-analyze-hang.md`.

## Not measured

- Templates of realistic size (100 MB and up): mint time, disk, pool refill.
- More than 300 databases, and the idle CPU behaviour over minutes.
- Reflink or ZFS-backed copies.
- A wake storm (LLM.md §46) beyond 12 simultaneous mints.
- Noisy-neighbour behaviour.
- Linux.

## Recommendation

Do not design a new shared runtime yet. The existing feature covers the core
of it, and the open questions are narrower than "build Phase 2":

1. ~~Reproduce and diagnose the `VACUUM ANALYZE` hang.~~ Done.
2. Find what holds about 0.7 MB per idle database and why reaping does not
   release it.
3. Test whether a reflink or ZFS copy can replace the file copy in the mint
   path, which would address both disk and mint time.
4. Rerun density with a 100 MB template and 1,000 databases.
