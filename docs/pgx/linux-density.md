# Density: how many databases one PGX runtime holds (Linux)

Host: `linux-host.md` (4 vCPU, 7.6 GiB, no swap, ZFS pool for the data
directory). Harness: `benchmarks/pgx/scale.py` — one runtime, the PGX
profile (`configs/pgx-ephemeral.conf`), a 50-table template (200 rows per
table), databases minted up to each count, then measured with no client
connected:

- footprint = PSS of the runtime process, which counts its share of the
  binary's pages;
- idle CPU = the runtime's CPU over a 60 s window, as a percentage of one
  core;
- the data directory's apparent size (block clones share the space; see
  `zfs-cow.md` for physical cost).

By default each new database answers one `SELECT 1` (it has been connected
to once). `--touch` instead runs the churn workload in each (an INSERT, an
UPDATE, a join and a `CREATE TABLE`), which loads relation and catalog
caches for its tables the way a real first session does.

## Memory per idle database: before → after

1000 databases, autovacuum on (PGX profile default):

| Build | What changed | 0 DBs | 1000 DBs | Per database |
|---|---|---|---|---|
| `1781903578` (pass 2) | — | 86 MB | 669 MB | 0.58 MB |
| `999a78baff` (pass 4) | lifecycle fixes (`linux-churn.md`) | 86 MB | 640 MB | 0.55 MB |
| `dedfdf9ca5` (pass 5) | `e9322867b6` pgstat slots boxed; `ad235e24cb` relcache cores interned across databases | 85 MB | **128 MB** | **43 KB** |

- `e9322867b6`: pgstat's shared-stats slots (one per database, and per
  relation and function with statistics) were all sized for the largest
  variant, the per-backend entry (~2.9 KB). Boxing that variant brings a
  slot to ~0.3 KB.
- `ad235e24cb`: every database built from the same template has
  byte-identical relcache entries for its catalog relations. They are now
  one shared copy per distinct content (`l2core::intern_core`), charged to
  the first database that loads it.
- Autovacuum off, same builds: 590 MB → 153 MB at 1000.

The autovacuum-on figure at 1000 (128 MB) is lower than the skip-idle
and autovacuum-off figures on the same build (153–156 MB). This is not
explained, and the gap is larger than the run-to-run noise at lower
counts (±2 MB). Plan on ~70 KB per untouched idle database, the
conservative figure.

## Density by count (build `dedfdf9ca5` / `0131cc9b12`)

| DBs | Untouched, autovacuum default | Untouched, skip-idle + naptime 300 | Touched once, skip-idle + naptime 300 | Data directory (apparent) |
|---|---|---|---|---|
| 0 | 85 MB | 86 MB | 86 MB | 71 MB |
| 100 | 93 MB | 91 MB | 101 MB | 1.6 GB |
| 300 | 99 MB | 104 MB | 124 MB | 4.8 GB |
| 500 | 107 MB | 120 MB | 155 MB | 7.9 GB |
| 1000 | 128 MB | 155 MB | **213 MB** | 15.7 GB |

- A touched database costs ~127 KB of runtime memory; an untouched one
  ~43–70 KB.
- The data directory's apparent size is 16 MB per database (the
  template's files); physical cost on ZFS is ~2 MB per clone at this
  template size (`zfs-cow.md`).
- Threads stay at 8 and file descriptors at ~37 at every count: databases
  cost no threads or descriptors while idle.
- 1000 touched databases use 213 MB of a 7.6 GiB host.

For comparison only (arithmetic, not a measured fleet): one stock
PostgreSQL 18 server idles at 16.6 MB PSS (`linux-results.md`), so 1000
separate servers would need ≥ 16.6 GB before any data or connections.

## Idle CPU

1000 idle databases:

| Configuration | Build | Idle CPU (one core) |
|---|---|---|
| autovacuum default (naptime 60 s) | `999a78baff` | 6.66 % |
| autovacuum default | `dedfdf9ca5` | 5.40 % |
| `pgrust.autovacuum_skip_idle_databases = on`, naptime 60 | `999a78baff` (`28c83164ab`) | 2.16 % |
| skip-idle, naptime 60 | `83fbf8d06e` (+ `9dca02b2c1` db-list cache, `5a3090e793` first visit) | 3.67 % |
| skip-idle, naptime 60 | `dedfdf9ca5` | 1.33 % |
| skip-idle, naptime 60 | `0131cc9b12` (+ `23ede965a7` schedule map) | 1.21 % |
| skip-idle, naptime 300 | `dedfdf9ca5` / `0131cc9b12` | **0.33 %** |
| autovacuum off | `dedfdf9ca5` | 0.22 % |
| skip-idle, naptime 300, every DB touched once | `0131cc9b12` | 1.45 % |

- The default launcher wakes once per `naptime / N` and visits a database
  each time; at 1000 databases that is a visit every 60 ms, almost all of
  the runtime's idle CPU.
- Skip-idle (`28c83164ab`) passes over databases whose tuple counters
  (inserts + updates + deletes) have not changed since the last visit.
  `5a3090e793` counts a never-written database as visited.
- The O(N²) schedule lookup fix (`23ede965a7`) moved the naptime-60 figure
  only 1.33 → 1.21 %: per-database wake work, not the lookup, dominates.
- Naptime 300 with skip-idle costs 0.33 % at 1000 — within 0.11 points of
  autovacuum off, with autovacuum still running for databases that change.
- Touched databases have changed counters until autovacuum has visited
  them once, which is the 1.45 %; it falls as visits complete.

Run-to-run variation at 300–500 databases is large (e.g. skip-idle naptime
60 at 500: 0.40–1.20 % across passes) because a window may or may not
contain an autovacuum worker run.

## Active databases among 1000

5 / 20 / 50 / 100 databases each running `SELECT count(*) … ; UPDATE …` in
a closed loop, client on the same 4 vCPU host:

| Active | p50 | p95 | p99 | Server CPU | Footprint (`999a78baff`) | Footprint (`dedfdf9ca5`) |
|---|---|---|---|---|---|---|
| 5 | 0.64 ms | 1.18 ms | 1.66 ms | 31 % | 648 MB | **135 MB** |
| 20 | 0.64 ms | 1.60 ms | 2.44 ms | 97 % | 673 MB | **159 MB** |
| 50 | 1.53 ms | 7.59 ms | 12.5 ms | 202 % | 723 MB | **205 MB** |
| 100 | 15.6 ms | 35.9 ms | 45.8 ms | 201 % | 808 MB | **283 MB** |

- Zero errors at every level.
- Latency is unchanged by the density fixes (the 999a78baff run had
  p99 1.63 / 2.50 / 13.5 / 52.2 ms).
- Above ~50 active the host is CPU-bound: server ~2 cores plus the client
  load generator on the same 4 vCPUs. 100 active is a queueing figure for
  this host, not a per-query cost.
- Each active database adds ~1.5 MB (its session, executor memory, the
  caches the session loads).

## One runtime or several

Pass 2 (`1781903578`, before the density fixes), 1000 databases split
across runtimes:

| Layout | Base (empty) | 1000 idle DBs | Idle CPU | Threads |
|---|---|---|---|---|
| 1 × 1000 | 54 MB | 649 MB | 5.2 % | 8 |
| 2 × 500 | 85 MB | 724 MB | 10.2 % | 16 |
| 4 × 250 | 138 MB | 821 MB | 9.4 % | 32 |

Each extra runtime adds its base memory and its own autovacuum launcher.
Smaller runtimes buy failure isolation (a runtime OOM kill takes only its
databases, `linux-limits.md`), not density or latency. Not re-run on the
fixed build; with ~43–70 KB per database the base cost dominates even
more.

## Recommended profile settings

```
pgrust.autovacuum_skip_idle_databases = on
autovacuum_naptime = 300
```

Ephemeral databases that see writes are still vacuumed and analyzed, on
builds with `6fe0b1ad65`. Before that fix, skip-idle could starve written
databases at high counts (`linux-analyze.md`). A
database written once and then left gets its first visit within 5 minutes
instead of 1. `analyze-policy` results (`linux-analyze.md`) show stale
statistics barely change plans at agent-branch scale.

## Open items

- Untouched autovacuum-default density (128 MB) below the skip-idle and
  autovacuum-off runs (153–156 MB) on the same build: unexplained.
- Catalog-cache (catcache L2) entries are per database; interning them
  like relcache cores is the next density candidate (touched database
  127 KB vs untouched 43–70 KB).
- ~4 MB of transparent huge pages appear before `main` runs (`680fab7c8a`
  disables THP only once the process starts).
