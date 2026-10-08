# PGX on Linux / ZFS: consolidated results

One PGX runtime holding hundreds to thousands of short-lived
Postgres-compatible databases, measured on a small Linux host with real ZFS
copy-on-write. This report stands on its own; each section points to the
focused document with the method and raw data.

**Host** (`linux-host.md`): OVH VPS `vps-d2a3c460`, 4 vCPU x86_64, 7.6 GiB
RAM, no swap, Ubuntu 26.04 / kernel 7.0, $8.50/month. ZFS 2.4 pool `tank`
on a 48.9 GiB partition (ashift 12, compression off, atime off, ARC capped
at 1 GiB, block cloning on). Every benchmark ran inside a 6 GiB no-swap
cgroup with a 5 s host monitor; the host never swapped.

**Scope.** These results answer whether the architecture works and what it
costs on Linux/ZFS. They are not production capacity numbers: the load
generator shares the same four vCPUs as the server. Maximum-capacity tests
belong on 8–16+ cores with a separate load generator.

**Builds.** Final engine build `727f31e49f` (all fixes below; most
measurements are on `6fe0b1ad65`, which differs only by the last churn
fix); profile defaults `696318b79e`. Earlier passes are named by build where their
numbers are quoted. Raw data: `benchmarks/pgx/results/vps-d2a3c460-<build>/`.

## Summary

| | Before this work | Now |
|---|---|---|
| Memory after 10,000 database lifecycles | 85 → 262 MB, linear (~11 KB per lifecycle) | 95 → 108 MB; 111 MB at 30,000, ~0.2 KB per lifecycle |
| Memory, 1000 idle databases | 669 MB | 128–155 MB (213 MB if each was queried once) |
| Idle CPU, 1000 idle databases | 6.5 % of a core | 0.33–0.43 % (profile default) |
| Bulk INSERT…SELECT / UPDATE of 3M rows | +290 MB to refused / +400 MB | flat (≤ 89 MB / 11 MB) |
| Warm / cold mint p50 | 7 / 72 ms | 6.2 / 74 ms (unchanged; disk-bound) |
| 500 simultaneous new databases | 90 of 500 failed after an earlier burst | 0 errors, drains in 12.6–13.8 s |
| Regression suite | 219 / 231 | 219 / 231, 0 result differences |

## Answers

### Memory

**1. Does memory plateau under long-lived database churn?**
Close to it, after one more fix. A 30,000-lifecycle run on `60fca8427f`
(run F) did not flatten: ~0.9 KB per lifecycle, 93 → 126 MB. The cause
was the shared catalog cache keeping each dropped database's `pg_database`
row (fixed in `727f31e49f`, below). On the fixed build (run G) 30,000
create → use → drop lifecycles go from 95 MB (after the first 100) to
108 MB at 10,000 and 111 MB at 30,000. The slope falls each quarter, to
0.014–0.020 MB per 100 lifecycles over the last half (~0.2 KB each,
~2 MB per 10,000). Before all the fixes growth was linear at 1.1 MB per
100 (~11 KB each). (`linux-churn.md`, runs F and G)

**2. What caused the previous lifecycle growth?**
Per-session state that outlived its session (`lifecycle-memory.md`):

- retired session-root shells that were never freed;
- the snapshot manager's per-thread state;
- janitor bookkeeping for dropped databases;
- statistics and connection-cache entries for dropped databases;
- huge pages that mimalloc requested by default;
- the shared catalog cache keeping each dropped database's `pg_database`
  row: DROP DATABASE purged the database's own entries but not the shared
  one keyed by its oid, ~2 entries per lifecycle until the 262k-entry cap
  (`727f31e49f`, found with a cache census in `pgrust: memctx`).

The tracker build measured retention per lifecycle at 20.5 KB → 4.9 KB
after the fixes. What remains in the tracker (~2.7 KB of autovacuum-worker
relcache) does not show in the release churn runs.

**3. Retained memory per lifecycle now?**
~0.2 KB over the last half of a 30,000-lifecycle run (release build,
`727f31e49f`), against ~0.9 KB before the `pg_database` fix and ~11 KB
before any fix. The tracker build with the shared cache off puts the rest
at ~0.17 KB: small per-session strings (data-directory and database path)
and query-descriptor slots. Not fixed.

**4. PGX idle runtime memory?**
45.8 MB PSS for the PGX profile alone (16.8 MB anonymous + 29 MB of binary
pages shared between runtimes). With the janitor, a sealed 50-table
template and the warm pool: 85 MB. Stock PostgreSQL 18: 16.6 MB.

**5. Idle memory per database?**
43–70 KB for an untouched idle database; 127 KB once a session has used it
(its relation and catalog caches). Before the density fixes: 550–580 KB.
(`linux-density.md`)

**6. What do 1000 databases consume?**
128–155 MB for the whole runtime with 1000 idle databases; 213 MB when
each has been queried once. With 100 of the 1000 active: 283 MB (was
808 MB). Data directory: 16 MB apparent per database, ~2 MB physical
(block clones).

### Creating databases

**7. Warm mint p50 / p95 / p99?**
6.2 / 8.5 / 9.4 ms, from the connection to the new name to `SELECT 1`
answered. A reconnect to an existing database is 3.8 ms, which is the
floor. (`linux-mint.md`)

**8. Cold mint p50 / p95 / p99?**
74 / 94.5 / 101.6 ms (19 MB, ~600-file template). Larger templates: 113 ms
at 674 MB, 135 ms at 1 GB, 674 ms at 10 GB.

**9. What dominates cold mint time?**
The per-file directory clone: 57.3 of the janitor's 59.5 ms. ZFS on this
host creates ~11,000 files a second (~90 µs each) and threads do not speed
it up. The first session in the new database adds ~15 ms.

**10. Warm pool under 10 / 50 / 100 / 250 / 500-request bursts?**
Up to the pool size, requests are served in milliseconds (pool 32, burst
10: p50 21 ms). Every request beyond the pool is a cold mint. Cold mints
drain at ~35–40 per second, so 500 simultaneous requests finish in
12.6–13.8 s with 0 errors. A pool refills at ~16 spares per second. Size
the pool to the burst that must be served warm.

| Pool | Burst 10 p50 | Burst 50 p50 | Burst 100 p50 | Burst 250 p99 | Burst 500 p99 |
|---|---|---|---|---|---|
| 0 | 267 ms | 1.00 s | 1.95 s | 6.78 s | 12.5 s |
| 32 | 21 ms | 235 ms | 1.07 s | 6.13 s | 13.7 s |

### Autovacuum and statistics

**11. Idle CPU with 1000 databases under the new autovacuum policy?**
0.33 % of one core with the profile defaults
(`pgrust.autovacuum_skip_idle_databases = on`, `autovacuum_naptime = 300`),
against 5.4–6.7 % with stock autovacuum and 0.22 % with autovacuum off.
On the final build it measured 0.43 % (±0.1 between runs; 0.72 % at
naptime 60). Right after all 1000 databases have been written, idle CPU
is 1.5–2.9 % while autovacuum makes its first pass over them
(`linux-analyze.md`).

**12. Does ANALYZE-on-change preserve good plans?**
Plans hold up without it at branch scale. With autovacuum off, a branch
grew a table by 100 % into a value the template held at 1 %. The estimate
was 50× off (4,067 vs 202,000 rows), yet the count and join plans did not
change and latency matched the freshly analyzed case within noise. The
skip-idle autovacuum still analyzes every database that changes; a fix
this work found (`6fe0b1ad65`) makes sure it does. Branches do not need a
forced ANALYZE.

### Query performance and compatibility

**13. PGX query performance vs PostgreSQL 18?**
(`linux-query-performance.md`)

- **Short statements:** 0.05–0.15 ms slower (point SELECT 0.25 vs 0.13 ms;
  0.18 ms with a 128 MB buffer pool). Throughput with 4 clients is equal or
  higher (7,878 vs 7,662 point SELECTs/s).
- **Large scans:** single-threaded, 1.5–2.2× slower (500k-row aggregate
  100 vs 58 ms). With the PgRust runtime and parallel query on, PGX is
  faster (36 vs 58 ms; sort 71 vs 83 ms), at +95 MB of memory.
- **Bulk and DDL:** COPY is equal; CREATE INDEX is 1.1–1.3× slower.
- **Real framework lifecycles:** 0–15 % slower end to end, and node-pg is
  faster.

**14. Does the regression suite stay at or above the baseline?**
Yes: 219 / 231 byte-exact on every final build, and the 12 differences are
EXPLAIN-plan text only, with 0 result differences. One fix in this work
(`daf827c0ff` / `67269453b2`) briefly broke 14 tests; the suite caught it
and `d48f587da0` fixed it before anything else was built on it. Also
219 / 231 with the final profile defaults (skip-idle autovacuum, naptime
300), and again on `727f31e49f`.

**15. Rails?** Yes. Rails 8.1.4 + pg 1.7.0 on Ruby 3.3.8, 3/3 runs:

- `db:prepare` runs the migrations: FKs, unique and composite indexes,
  jsonb with a GIN index.
- `bin/rails test` with fixtures: jsonb queries, joins, `RecordNotUnique`
  and `InvalidForeignKey` from the database, a nested transaction rolled
  back to a savepoint, `insert_all` / `update_all`, cascade delete.
- A schema-change migration, then tests again, then `db:schema:load` from
  the dumped `schema.rb`, then tests again.

Every phase is within 0–5 % of PostgreSQL 18 (`agent-workloads.md`).

**16. Django?** Yes. Django 5.2.9 + psycopg 3, 3/3 runs: migrate with 20
models (FKs, unique, composite, JSON indexes, M2M), `manage.py test` (it
creates, migrates and destroys its own test database; 60 tests with
savepoints and IntegrityError inside `atomic`), a generated schema
migration, tests again. 7–15 % slower than PostgreSQL 18
(`agent-workloads.md`).

**17. node-postgres?** Yes, 3/3: named prepared statements over the
extended protocol, savepoints, jsonb, bulk INSERT…SELECT. 17 % faster than
PostgreSQL 18.

**18. Prisma?** Yes. Prisma 6.19.3, 3/3: `migrate dev` (it creates and
drops a shadow database), `generate`, nested creates, an interactive
`$transaction`, P2002 unique violation, raw JSON, then a schema change and
`migrate dev` again. SQLAlchemy 2.0.45 also passes 3/3.

### Storage

**19. What does a 1 / 2 / 5 / 10 GB copy-on-write branch cost?**
(`zfs-cow.md`, recordsize 128K)

| Template (logical) | Clone mint p50 | Physical per new branch |
|---|---|---|
| 1.0 GB | 135 ms | 2.3 MB |
| 2.0 GB | 198 ms | 3.7 MB |
| 5.2 GB | 417 ms | 7.0 MB |
| 10.1 GB | 674 ms | 13.6 MB |

A full copy costs the template's whole size; a copy mint of a 674 MB
template takes 933 ms vs 113 ms for a clone.

**20. Storage amplification after writes?**

- **Bulk inserts:** 1.1–1.2× their logical size.
- **Scattered single-row updates:** ~0.45 MB per updated row at recordsize
  128K, because each dirtied page rewrites a whole 128 KB record (1000
  updates cost +420–505 MB).
- **Smaller records:** 16K cuts that 7× and 8K cuts it 14×, but makes
  clones slower and larger (1 GB template: 369 / 893 ms and 11 / 20 MB per
  clone).

The bench keeps 128K. Short agent branches favour it; long-lived branches
with many scattered updates favour 16K.

### Isolation and limits

**21. How many active databases can four vCPUs sustain comfortably?**
About 20 with p99 under 2.5 ms, and 50 with p99 12.5 ms, measured with
1000 databases present and the load generator on the same 4 vCPUs. At 100
active, p99 is 46 ms: queueing, not errors (0 at every level).

**22. How well is CPU noisy-neighbour behaviour contained?**
(`linux-noisy-neighbor.md`)

- **Saturated host:** with all 4 cores saturated by other databases, a
  quiet database keeps its median (0.6–0.9 ms) and 0 errors, but its p99
  goes from 1.1 to 6–8 ms.
- **Connection limits are the knob:** one agent with 50 connections
  running a migration/test mix pushes a neighbour's p99 to 9.8 ms. Capped
  at 1 / 2 / 5 connections per database, the neighbour's p99 is 1.2 /
  2.1 / 4.0 ms.
- **Misbehaving database:** sorts, spills, cancels, terminates, timeouts
  and runaway memory in one database never caused an error in another.

**23. Are session / database / runtime memory limits safe?**
Yes.

- Every refusal is SQLSTATE 53200 with a hint, and the session stays
  usable. Bystanders see 0 errors, nothing panics, and the server stays up.
- Memory-heavy work under a 256 MB session limit: in-memory sort, hash,
  CTE and aggregates are refused. COPY, INSERT…SELECT, CREATE INDEX,
  ALTER TABLE and spilling sorts complete.
- This work fixed one panic path (`dde6c13de4`) and three per-row DML
  leaks that hit the limit on ordinary statements. (`linux-limits.md`)

**24. Does the cgroup backstop work?**
Yes. With `pgrust.runtime_memory_limit = 700` under a 1 GiB `memory.max`,
the runaway query is refused (peak 674 MB, 0 OOM kills). Without the
PgRust limit, the kernel kills the whole runtime and the host is
unaffected. So the runtime limit must sit at 70–80 % of `memory.max`.

### PGRun integration

**25. What PGX control/status API exists for PGRun?**
(`pgrun-interface.md`)

- **Mint:** connecting to `<prefix><template>__<token>` mints a database.
- **Lifecycle:** `CREATE` / `DROP DATABASE`, `pgrust_seal_template()`,
  `pgrust_pin_database()` / `pgrust_unpin_database()`.
- **Status:** `pgrust_runtime_status()`, JSON with databases, sessions,
  memory against limits, pool hits, cold mints, refills, mint failures and
  cold-mint latency.
- **Settings:** per-database connection limit
  (`pgrust.ephemeral_db_connection_limit`), the session / database /
  runtime memory limits, statement timeouts.
- **Credentials:** ordinary roles and grants.

**26. What remains necessary for PGRun integration?**
All of it is PGRun-side work:

- a persistent host agent instead of one SSH call per branch (earlier
  measured at ~3.9 s per branch);
- the branch → (host, runtime, database) mapping in the control plane;
- gateway routing to a database inside a shared runtime;
- per-branch credentials and revocation;
- durable golden templates in object storage, with restore after a host
  crash (the profile runs `fsync = off`);
- a launcher that applies the cgroup and limits;
- an end-to-end benchmark `API request → DATABASE_URL → SELECT 1`, with a
  target of < 500 ms p50 and a stretch goal of < 200 ms.

The engine's share of that is 6–100 ms.

**27. Recommended production topology?**

- One PGX runtime per host, in its own cgroup.
- Set `pgrust.runtime_memory_limit` to 70–80 % of `memory.max`, the session
  limit to a few hundred MB, and the database limit to 2–4× the session
  limit.
- Use the PGX profile, including skip-idle autovacuum with naptime 300.
- Use a ZFS pool with block cloning for the data directory, with golden
  templates cached locally and backed up durably.
- A warm pool sized to the expected burst.
- 2–5 connections per agent database.

Several runtimes per host buy failure isolation at ~35–55 MB base each,
not density or latency.

**28. Is remote compute/storage separation needed now?**
No. Local ZFS block cloning already gives the branching benefit: a few MB
per branch, sub-second clones of 10 GB templates. Durable copies of golden
templates belong in object storage, restored to hosts as caches. Separating
hot storage from compute has no measured need here.

**29. Is $0.012 per branch-hour economically reasonable?**
Yes on this evidence, before control-plane costs
(`linux-pricing-economics.md`). One $8.50 host holds ~250 concurrent agent
branches at 20 % active, CPU-bound; RAM would allow 14,000. Margin over host
cost is 71 % at 1000 five-minute branches a day and 95–99 % from 1000
thirty-minute branches a day upward. At small volume the $8.50 host plus a
~$50/month control plane needs ~5,000 branch-hours a month to break even.
Not included: support, spare hosts, egress, the operator's time. Repeat on
production hardware before fixing prices.

### Claims

**30. Numbers safe to show investors** (each reproducible with the named
script on the stated host):

- 1000 databases in one runtime use 128–155 MB idle and 213 MB after each
  is queried once (`scale.py`, 4 vCPU / 8 GB VPS).
- ~0.13 MB of memory per used idle database, against ≥ 16.6 MB for one
  idle PostgreSQL 18 server per database: about 130× better density than
  separate database servers. A single PostgreSQL server holding many
  databases was not measured.
- Warm database mint 6.2 ms p50, cold 74 ms p50 (engine-level, Unix socket,
  connection to `SELECT 1`).
- A fresh copy-on-write branch uses ~2 MB of disk for a 1 GB template and
  13.6 MB for a 10 GB one. COW branches initially use hundreds of times
  less physical storage than full-copy branches.
- 30,000 database create/use/drop cycles in one runtime end at 111 MB
  (~0.2 KB retained per cycle; not perfectly flat).
- 500 simultaneous new-database requests: 0 errors.

**31. Numbers safe to show customers:**

- Branch creation: say "a new branch database is ready in under 100 ms on
  the engine". A PGRun end-to-end number must come from the end-to-end
  benchmark (Q26), not from mint timings. Do not say "6 ms branches".
- Compatibility: Rails, Django, Prisma, SQLAlchemy and node-postgres lifecycles
  pass; the regression suite passes 219 of 231 with no wrong results.
- Isolation: per-branch memory and connection limits, and one branch's
  runaway query does not error other branches.
- Storage: branches start at a few MB regardless of template size.

**32. Which competitive claims against Xata / Neon / Turso / Supabase are
supported by measurements?**
None directly: no competitor was measured here. What the measurements
support is the PGX side of the positioning: many databases per runtime
(1000 in ~150–213 MB), millisecond mints, and few-MB COW branches with
Postgres compatibility. Any comparison needs the competitor measured under
the same method.

### Status

**33. What remains unsolved?**

- **Churn residue:** ~0.2 KB per lifecycle remains on the final build
  (~20 MB per 100,000), mostly small per-session strings. Bounded growth
  for practical purposes, not zero.
- **Cold mint:** ~60 ms of per-file ZFS cloning (600 files). Faster needs
  fewer files per template or a filesystem-level snapshot clone. That is an
  architectural choice, not a contained fix.
- **First session:** ~15 ms more in a new database than a reconnect
  (catalog bootstrap).
- **Short-statement latency:** 0.05–0.15 ms behind PostgreSQL.
- **Unexplained density figure:** autovacuum-default density at 1000
  untouched databases (128 MB) is below the skip-idle run (155 MB).
- **Catalog-cache entries are per database:** sharing them like relcache
  cores is the next density candidate.
- **Constants that could become settings:** the warm-pool refill cap
  (8 per 500 ms) is a constant, and ~4 MB of huge pages appear before
  `main` runs.
- **Bigger hardware:** capacity on larger hosts with a separate load
  generator.
- **PGRun-side:** everything in Q26.

**34. Commits.** All pushed to `pgx-experimental-plan`
(`github.com/alexshapalov/pgrust`). Engine changes:

| Commit | Change |
|---|---|
| `dde6c13de4` | sort/tuplestore `grow_memtuples` returns the out-of-memory error instead of panicking (XX000 → 53200) |
| `680fab7c8a` | huge pages off for the process on Linux (idle profile 83 → 46 MB) |
| `060f516fcb` | allocation tracker: x86_64 frame-pointer backtraces |
| `dda9c1be6f` | free retired session-root shells once their thread is joined |
| `b736a6d0ec` | drop snapshot-manager per-thread state at session teardown |
| `48398cc0b5` | janitor forgets dropped databases' backfill state |
| `28c83164ab` | autovacuum launcher can skip databases with no writes since the last visit |
| `9dca02b2c1` | launcher reuses its database list between wakes under skip-idle |
| `5a3090e793` | a never-written database counts as visited |
| `23ede965a7` | launcher schedule lookup is a map, not a linear search per database |
| `6fe0b1ad65` | skip-idle no longer starves databases that change |
| `727f31e49f` | DROP DATABASE evicts the dropped database's `pg_database` row from the shared catalog cache (`4cefb2fc05`: cache census in `pgrust: memctx`) |
| `040abf2d27` | opt-in per-stage mint timing |
| `b2180ac15b`, `95f7c542bf` | `pgrust_runtime_status()` with pool and mint counters |
| `c9a2a024cc` | `pgrust.ephemeral_db_connection_limit` |
| `a95801a13b` | mint request table counts pending requests only (90 failed connections in back-to-back bursts → 0) |
| `e9322867b6` | shared-stats backend entry boxed (slot ~2.9 → ~0.3 KB) |
| `ad235e24cb` | identical relcache cores shared across databases |
| `ea6d055cb2`, `67269453b2`, `daf827c0ff`, `d48f587da0` | INSERT…SELECT, UPDATE, MERGE UPDATE and ON CONFLICT DO UPDATE no longer keep one tuple per row until the statement ends |
| `c60148c48b` | test: GUC count includes the memory-limit settings |
| `696318b79e` | PGX profile: skip-idle autovacuum, naptime 300 |

Benchmark harness, results and documentation are in the same branch
(`benchmarks/pgx/linux/`, `benchmarks/pgx/results/`, `docs/pgx/`).

## Before → after

| Optimization | Before | After |
|---|---|---|
| Lifecycle memory retention (`dda9c1be6f`, `b736a6d0ec`, `48398cc0b5`, `727f31e49f`) | 11 KB per lifecycle, linear; 262 MB after 10k | ~0.2 KB; 108 MB after 10k, 111 MB after 30k |
| Huge pages off (`680fab7c8a`) | idle profile 83 MB; 100-active 1,265 MB | 46 MB; 844 MB |
| Stats slot boxing + relcache sharing (`e9322867b6`, `ad235e24cb`) | 1000 idle DBs 640 MB; 100 active 808 MB | 128 MB; 283 MB |
| Skip-idle autovacuum + naptime 300 (`28c83164ab` … `6fe0b1ad65`, `696318b79e`) | 1000 idle DBs 5.4–6.7 % of a core | 0.33–0.43 % |
| Mint request table (`a95801a13b`) | 90 / 500 connections failed on a second burst | 0 |
| DML per-row memory (`ea6d055cb2` … `d48f587da0`) | 3M-row INSERT…SELECT refused at 256 MB; UPDATE +400 MB | +89 MB; +11 MB |
| Sort out-of-memory (`dde6c13de4`) | refusal surfaced as XX000 panic | 53200 |

## Bugs found by testing, and how they were caught

| Found by | Bug | Fix |
|---|---|---|
| limits runtime case | sort growth `.expect()` turned a refusal into a panic | `dde6c13de4` |
| idle memory on Linux | mimalloc asked for huge pages | `680fab7c8a` |
| 10k churn + tracker build | session shells, snapmgr state, janitor maps retained | `dda9c1be6f`, `b736a6d0ec`, `48398cc0b5` |
| back-to-back 250/500 bursts | mint table counted finished requests | `a95801a13b` |
| memory-heavy workloads | three DML paths kept a tuple per row | `ea6d055cb2`, `67269453b2`, `daf827c0ff` |
| regression suite | the first version of those fixes reused a virtual slot's buffer across rows, which corrupted partitioned multi-row INSERT | `d48f587da0` |
| `skipidle-check.py` | skip-idle starved written databases | `6fe0b1ad65` |
| 30k-lifecycle churn + cache census | shared catalog cache kept every dropped database's `pg_database` row (~0.9 KB per lifecycle) | `727f31e49f` |

## Documents

`linux-host.md` (host and ZFS setup) · `linux-churn.md` ·
`lifecycle-memory.md` · `linux-density.md` · `linux-mint.md` ·
`linux-analyze.md` · `linux-query-performance.md` · `agent-workloads.md` ·
`zfs-cow.md` · `linux-limits.md` · `linux-noisy-neighbor.md` ·
`linux-pricing-economics.md` · `pgrun-interface.md`

v0.1 freeze: `v0.1-baseline.md` (final numbers) · `v0.1-readiness.md`
(ready / limitations / approved use) · `compatibility-gate.md` ·
`pgrun-integration.md` · reproduction index
`benchmarks/pgx/linux/README.md`
