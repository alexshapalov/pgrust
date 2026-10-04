# PGX Experimental Plan

## Purpose

This repository is a fork of `pgrust` used to evaluate a new database runtime called **PGX**.

PGX is not intended to replace production PostgreSQL.

PGX is intended to be a **small, fast, low-memory, Postgres-compatible database runtime for AI agents, tests, CI, previews, and short-lived development environments**. It will be used underneath **PGRun**, where every agent, test run, or isolated task can receive its own disposable Postgres-compatible database.

The core product hypothesis is:

> Production PostgreSQL is optimized to live for years.  
> PGX should be optimized to live for minutes or hours.

A PGX database should be cheap enough that creating one is not an infrastructure decision.

The long-term goal is:

> **Make a Postgres-compatible database almost as cheap to create and throw away as a file.**

This document is the implementation and benchmarking plan. Work must happen in two approaches, in order.

1. **Approach 1 — PGX Ephemeral Profile:** keep the current pgrust architecture, remove/disable production-only work, tune aggressively for short-lived databases, and measure the result.
2. **Approach 2 — PGX Lightweight Runtime:** only after Approach 1 is measured, investigate architecture changes that make idle databases almost free: shared runtime, lazy state, scale-to-zero, and copy-on-write-aware storage.

Do not jump to Approach 2 before Approach 1 numbers exist.

---

# 1. Product model

PGRun is the infrastructure layer.

PGX is the database runtime.

Conceptually:

```text
                    PGRun
          agent database infrastructure
                      |
        +-------------+-------------+
        |             |             |
       PGX           PGX           PGX
    agent #1      agent #2      agent #3
        |             |             |
        +------ Postgres-compatible-+
                      |
              production Postgres
```

The expected PGRun lifecycle is:

```text
production PostgreSQL
        |
        v
sanitized / masked snapshot
        |
        v
PGRun golden database
        |
        v
cheap isolated PGX branch
        |
        v
agent gets postgres://...
        |
        v
migrate / read / write / test
        |
        v
destroy
```

Typical PGX database lifetime:

- a few seconds;
- a few minutes;
- one CI run;
- one agent task;
- one coding session;
- occasionally several hours or a few days.

PGX is **not** designed for:

- primary production databases;
- years of uptime;
- high-availability clusters;
- replicas;
- disaster recovery;
- PITR;
- long-term WAL archival;
- cross-region failover;
- production backup orchestration.

This difference in lifetime is the source of the optimization opportunity.

---

# 2. Compatibility goal

Do not define PGX as “Postgres Lite”.

Define PGX as:

> **Postgres application semantics without production operations.**

The application-facing surface should remain as close to PostgreSQL as practical.

High-priority compatibility includes:

- PostgreSQL wire protocol;
- SQL syntax;
- transactions;
- MVCC behavior;
- locks;
- transaction isolation used by normal applications;
- schemas;
- tables;
- views;
- constraints;
- foreign keys;
- indexes;
- sequences / identity;
- JSON / JSONB;
- arrays;
- timestamps;
- common data types;
- CTEs;
- window functions;
- subqueries;
- joins;
- triggers;
- PL/pgSQL where available;
- COPY;
- prepared statements;
- common DDL;
- `EXPLAIN`;
- connection behavior expected by normal Postgres drivers;
- enough catalog compatibility for ORMs and migration tools.

Target compatibility is not a marketing percentage yet. Do not claim “95%”, “98%”, or “99%” until we have a defined application-compatibility corpus and results.

The upstream PostgreSQL regression/conformance suite remains an important guardrail, but PGX also needs a workload-focused compatibility suite representative of real application frameworks.

Recommended application suites:

- Rails + ActiveRecord;
- Django;
- Prisma;
- Drizzle;
- SQLAlchemy;
- node-postgres;
- pgx/Go only as one client among many;
- migration-heavy workflows;
- transactional test suites.

The goal is not to support every PostgreSQL operational feature. The goal is that normal application code should not need to know it is connected to PGX.

---

# 3. North-star metrics

PGX optimization is **not primarily a TPS benchmark project**.

The most important question is:

> How many isolated Postgres-compatible databases can PGRun provide on one host?

Primary metrics, in priority order:

1. **idle RSS per database**
2. **incremental RSS per additional database**
3. **cold start to accepting connections**
4. **cold start to successful `SELECT 1`**
5. **time to create a new empty database**
6. **time to create a database from an existing golden image**
7. **incremental disk bytes per branch**
8. **RAM with 10 / 100 / 500 / 1,000 mostly-idle databases**
9. **RAM under realistic concurrent agent load**
10. **time to destroy a database**
11. **first-query latency after idle / scale-to-zero**
12. **common migration/test workload duration**
13. **CPU consumed by an idle database**
14. **background write I/O from an idle database**

Secondary metrics:

- simple query latency;
- write latency;
- transactional throughput;
- migration throughput;
- test suite wall-clock time.

Do not optimize analytical benchmark throughput at the expense of density, startup, compatibility, or simplicity.

---

# 4. Required baseline

Before PGX-specific changes, create a reproducible baseline.

Compare these engines/configurations on the **same hardware, OS, filesystem, dataset, build type, client, and test harness**:

1. PostgreSQL 18.x
2. upstream-compatible pgrust from this fork before PGX changes
3. PGX Approach 1
4. later, PGX Approach 2 prototypes

Use release builds. Record the exact commit SHA, compiler, kernel, CPU, memory, filesystem, mount options, and all database settings.

## 4.1 Baseline database sizes

At minimum test:

- empty database;
- ~100 MB;
- ~1 GB;
- ~10 GB if hardware permits.

The 100 MB and 1 GB tests are especially important because they approximate common development/test datasets.

## 4.2 Baseline concurrency

Measure:

- 1 database;
- 10 databases;
- 100 databases;
- 500 databases when possible;
- 1,000 databases when possible.

For each count measure two states:

### Idle

Database is running and accepts connections, but there are no active queries.

### Active

Each database has at least one simulated agent doing a bounded workload.

Do not extrapolate 10-database measurements to 1,000. Measure actual density where possible.

## 4.3 Baseline output

Every benchmark run should produce machine-readable output, preferably JSON or CSV, containing:

```text
engine
git_sha
profile
host
cpu
ram_total
kernel
filesystem
dataset_size
database_count
connection_count
cold_start_ms
ready_ms
first_query_ms
idle_rss_bytes
active_rss_bytes
cpu_idle_pct
cpu_active_pct
disk_physical_bytes
disk_logical_bytes
create_ms
clone_ms
destroy_ms
migration_ms
test_suite_ms
read_p50_ms
read_p95_ms
write_p50_ms
write_p95_ms
timestamp
```

Raw data must be committed or attached to the experiment. Do not report only screenshots or selected numbers.

---

# 5. Approach 1 — PGX Ephemeral Profile

## Goal

Answer this question first:

> **How much cheaper can pgrust become for disposable databases without changing its fundamental server/storage architecture?**

This is deliberately the low-risk approach.

Use configuration changes, runtime profiles, compile-time feature exclusion where practical, and narrowly scoped code changes.

Do **not** initially redesign the storage engine or multiplex thousands of databases into a new shared daemon.

Approach 1 exists to find the actual resource floor of the current architecture.

---

# 6. Approach 1 principles

## 6.1 Preserve application semantics

Do not disable a feature merely because it consumes resources.

Ask:

1. Is this feature visible to normal application SQL?
2. Is this feature required by Rails/Django/Prisma/etc.?
3. Is it only required for production operations?
4. Does disabling it change transaction semantics?
5. Does disabling it prevent ordinary migrations or tests?

Production-only machinery is the main target.

Application-facing PostgreSQL behavior is not.

## 6.2 Separate durability from transaction semantics

PGX may use weaker crash durability while still preserving transactions during the lifetime of the process.

For example:

```sql
BEGIN;
UPDATE accounts SET balance = balance - 10 WHERE id = 1;
ROLLBACK;
```

must still behave correctly.

However, a PGX ephemeral mode may explicitly allow:

> if the host loses power or the PGX process is killed unexpectedly, recreate the branch.

That allows us to investigate avoiding expensive durability work without changing normal SQL behavior.

## 6.3 Make changes reversible

Prefer:

- a `pgx_ephemeral` profile;
- feature flags;
- runtime GUC defaults;
- isolated modules;
- documented build flags;

over deleting large subsystems immediately.

We need A/B comparisons and must be able to re-enable a feature when an application compatibility test requires it.

---

# 7. Approach 1 — workstream A: no-code configuration profile

Before removing any code, test the cheapest possible profile.

Create a documented PGX ephemeral configuration.

Candidates to test include, subject to what pgrust currently implements:

```text
fsync = off
synchronous_commit = off
full_page_writes = off
archive_mode = off
wal_level = minimal
max_wal_senders = 0
max_replication_slots = 0
hot_standby = off
```

Also investigate:

- minimum sensible `shared_buffers`;
- lower `max_connections` matching agent workloads;
- whether JIT helps or hurts small/short-lived queries;
- worker counts;
- parallel worker counts;
- stats collection overhead;
- logging overhead;
- checkpoint behavior;
- autovacuum behavior for short-lived instances;
- memory context defaults;
- stack requirements;
- IO method;
- temp buffers;
- prepared statement/cache behavior;
- catalog/cache sizing.

Important: **do not assume disabling autovacuum is automatically correct.**

For very short-lived databases it may save work, but real test workloads can still create enough dead tuples or depend on analyze/statistics behavior. Benchmark it both ways.

Likewise, do not assume JIT should be enabled or disabled. Short-lived agent workloads may prefer lower compilation/startup overhead. Measure.

## Deliverable

Add a repeatable profile such as:

```text
configs/pgx-ephemeral.conf
```

or the closest structure suitable for this repository.

Document every setting and the reason for it.

Benchmark:

- upstream pgrust defaults;
- PGX config only.

This establishes the first delta before code is removed.

---

# 8. Approach 1 — workstream B: identify production-only subsystems

The current workspace already contains explicit areas for production functionality, including replication, backup, WAL/archive/recovery, checkpointer, autovacuum, walsender/walreceiver, slots, subscriptions, and related systems.

Create an inventory before changing anything.

Classify each subsystem:

### Class A — application compatibility critical

Keep.

Examples:

- parser;
- planner;
- executor;
- MVCC;
- transactions;
- heap/table access;
- indexes;
- locks;
- catalogs;
- types;
- JSONB;
- sequences;
- constraints;
- triggers;
- COPY;
- protocol.

### Class B — useful but optional in agent/test workloads

Keep initially, benchmark disable/lazy behavior.

Examples may include:

- autovacuum;
- analyze/statistics collection;
- JIT;
- parallel query;
- some logging/statistics;
- some background maintenance.

### Class C — production operations

Primary candidates for exclusion in PGX ephemeral builds.

Investigate at minimum:

- streaming replication;
- WAL sender;
- WAL receiver;
- synchronous replication;
- replication slots;
- replication origin;
- logical replication workers;
- publication/subscription execution paths;
- slot synchronization;
- standby machinery;
- backup subsystem;
- basebackup;
- incremental backup;
- WAL archive commands;
- archive recovery;
- PITR-related operational paths;
- timeline management needed only for recovery/replication;
- HA-specific background work.

Do not simply delete the source directories.

First determine:

- startup cost;
- idle memory cost;
- background CPU cost;
- binary size contribution;
- dependency coupling;
- application-visible catalog/API consequences.

## Deliverable

Create:

```text
docs/pgx/feature-inventory.md
```

with columns:

```text
subsystem
class
application_visible
startup_cost
idle_ram_cost
background_cpu
disk_io
can_disable_runtime
can_disable_compile_time
compatibility_risk
decision
notes
```

No subsystem should be removed merely because its name sounds production-oriented.

---

# 9. Approach 1 — workstream C: compile-time PGX build

After the inventory and config benchmark, create a PGX build/profile that excludes production-only code where it gives measurable benefit.

Possible implementation forms:

- Cargo features;
- conditional compilation;
- alternate top-level binary;
- PGX-specific startup path;
- no-op implementations for unsupported operational APIs when required by dependencies.

Example conceptual flags:

```text
pgx-ephemeral
pgx-no-replication
pgx-no-backup
pgx-no-archive
pgx-minimal-background
```

The exact structure should follow the existing workspace and avoid unnecessary invasive changes.

### Important

The goal is not binary size by itself.

A 30% smaller binary with identical startup/RAM economics is not a meaningful PGX success.

Every removal must be connected to one or more measured benefits:

- startup;
- RSS;
- CPU;
- disk writes;
- clone/create latency;
- runtime complexity.

---

# 10. Approach 1 — workstream D: ephemeral durability mode

Implement or formalize an explicit PGX ephemeral durability mode.

This mode should communicate clearly:

> PGX guarantees normal transaction behavior while the instance is running, but the database is disposable and may be recreated after host/process failure.

Investigate the minimum WAL/durability machinery required for correct in-process transactional semantics.

Do **not** remove WAL blindly.

PostgreSQL uses WAL for more than “backup”. Some execution/storage behavior can depend on WAL and recovery assumptions.

Proceed incrementally:

1. start with configuration-level durability relaxation;
2. measure;
3. profile CPU/syscalls/disk writes;
4. identify the remaining WAL/checkpoint cost;
5. change code only where semantics remain understood;
6. run conformance and application suites after each material change.

Potential targets:

- avoid fsync;
- avoid sync commit waits;
- avoid unnecessary full-page writes;
- avoid archival;
- avoid replication-oriented WAL retention;
- reduce checkpoint work;
- simplify startup recovery for branches that can be regenerated;
- avoid production durability barriers that do not protect anything we care about.

A failed PGX database should be replaceable from the PGRun golden image.

This is a product-level property, not merely a Postgres setting.

---

# 11. Approach 1 — workstream E: background work

An idle PGX database should be genuinely idle.

Measure all threads/workers/processes after startup.

For each one, determine:

- why it exists;
- RSS;
- stack reservation;
- wakeup frequency;
- CPU;
- writes;
- whether it can be shared;
- whether it can be lazy;
- whether it can be disabled in ephemeral mode.

Target:

> an unused branch should produce almost no CPU wakeups and no periodic disk churn.

Pay special attention to:

- checkpointer;
- autovacuum launcher/workers;
- stats machinery;
- logging;
- IO workers;
- replication launchers;
- background schedulers.

Do not optimize by breaking required behavior; convert periodic work to demand-driven work when possible.

---

# 12. Approach 1 — workstream F: connection and thread memory

PgRust already differs from PostgreSQL by using a thread-based concurrency model.

This is highly relevant for PGX.

Profile memory associated with:

- one server with zero clients;
- 1 connection;
- 10 connections;
- 100 connections;
- N isolated databases if current architecture supports them as separate instances.

Measure:

- reserved thread stack;
- committed stack;
- per-session catalog/cache state;
- planner state;
- executor state;
- prepared statements;
- TLS state if applicable;
- buffers;
- per-query arenas / memory contexts.

For short-lived agent workloads, large stack reservations or eager per-connection state can dominate.

Investigate:

- smaller safe stack defaults;
- lazy per-session allocation;
- connection reuse;
- shared immutable state;
- aggressive release of query-local memory;
- bounded caches.

Do not change stack sizes until the regression suite and representative recursive/deep-query tests pass.

---

# 13. Approach 1 — workstream G: startup path

Profile startup end-to-end.

Break the timeline into explicit stages.

Example:

```text
process exec
config read
PGDATA open
control/catalog initialization
WAL/recovery initialization
shared state allocation
background workers
socket listen
accept connection
authentication
first SELECT 1
```

Add instrumentation if necessary.

Report both:

- **server-ready time**
- **first-useful-query time**

PGRun cares about the latter.

Avoid optimizing an internal “started” event while the first client still waits for expensive lazy initialization.

---

# 14. Approach 1 benchmark matrix

After each major optimization group, run the same benchmark matrix.

Do not combine ten changes and benchmark only once.

Suggested experiment sequence:

### A0 — PostgreSQL baseline

PostgreSQL 18.x normal configuration.

### A1 — pgrust baseline

Current pgrust fork before PGX changes.

### A2 — pgrust + PGX config

Only configuration changes.

### A3 — PGX config + production subsystem disablement

Replication/backup/archive/etc. disabled or excluded.

### A4 — PGX ephemeral durability

Durability changes.

### A5 — PGX background minimization

Workers / periodic maintenance changes.

### A6 — PGX memory/startup tuning

Stacks, buffers, eager caches, JIT decision, etc.

For each step show the delta from A1 and previous step.

Example result table:

```text
Metric                    PG18   pgrust   A2    A3    A4    A5    A6
----------------------------------------------------------------------
cold start ms
first SELECT 1 ms
idle RSS MB
1 connection RSS MB
10 DB RSS MB
100 DB RSS MB
idle CPU
idle writes/min
100 MB clone ms
1 GB clone ms
migration time
test suite time
```

Never replace missing numbers with estimates.

---

# 15. Approach 1 success criteria

Approach 1 is successful if it materially improves PGRun unit economics without unacceptable compatibility loss.

We should not hard-code fake expected results, but directional goals are:

- clearly lower idle memory than stock PostgreSQL;
- clearly lower idle memory than baseline pgrust;
- low or near-zero idle CPU;
- minimal idle write I/O;
- faster start to first query;
- higher database density on the same host;
- application suites continue to pass;
- branch lifecycle remains simple.

A useful milestone would be a **multiple-x density improvement**, not merely a few percent.

If Approach 1 yields only marginal gains, that is still valuable: it proves the fixed-cost floor belongs to the architecture and justifies Approach 2.

---

# 16. Compatibility gates for Approach 1

After every material change run:

1. relevant unit tests;
2. upstream PostgreSQL regression/conformance suite used by pgrust;
3. PGX application compatibility suite;
4. PGRun representative workload.

Do not accept an optimization merely because benchmarks improve.

Every benchmark result must be accompanied by compatibility status.

Suggested status:

```text
PASS      no known application regression
PARTIAL   expected unsupported production feature only
FAIL      normal application behavior changed
```

Production-only failures can be acceptable if documented.

Application-facing failures are blockers unless explicitly scoped.

---

# 17. Approach 2 — PGX Lightweight Runtime

## Start condition

Do not begin major Approach 2 work until Approach 1 has:

- a stable benchmark harness;
- measured baselines;
- profiling evidence;
- a known idle memory floor;
- a known startup floor;
- a list of costs that configuration/feature stripping cannot remove.

Approach 2 answers a different question:

> **Can thousands of mostly-idle Postgres-compatible databases share one lightweight runtime instead of paying the fixed cost of one server instance each?**

This is the Turso-like architectural direction.

The desired mental model is:

> database = lightweight isolated data object

rather than:

> database = heavyweight independent server.

---

# 18. Approach 2 target architecture

Conceptual only; validate before implementation.

```text
                         pgxd
                shared PGX runtime
                         |
       +-----------------+-----------------+
       |                 |                 |
    database A        database B        database C
       |                 |                 |
   catalog/state      catalog/state      catalog/state
   overlay storage    overlay storage    overlay storage
       |                 |                 |
       +---------- shared runtime ---------+
                         |
                 common code/caches/io
```

External clients should still see ordinary Postgres endpoints:

```text
postgres://host/db_a
postgres://host/db_b
postgres://host/db_c
```

Isolation must remain strict.

A bug or transaction in database A must not expose or mutate database B.

---

# 19. Approach 2 — workstream A: database as an object

Define an internal database lifecycle API.

Conceptually:

```text
create()
open()
resume()
connect()
quiesce()
evict()
snapshot()
fork()
destroy()
```

A database that has no active clients should not require a dedicated server process/thread set or large fixed memory allocation.

Desired state model:

```text
ABSENT
  |
  v
STORED
  |
  v
WARMING
  |
  v
ACTIVE
  |
  v
IDLE
  |
  v
EVICTED
```

The important transition is:

```text
IDLE -> EVICTED
```

where volatile caches are dropped while durable/branch state remains available.

Target:

> inactive database RAM approaches zero or a very small metadata footprint.

Do not claim literal zero; measure the actual floor.

---

# 20. Approach 2 — workstream B: shared runtime

Investigate what can safely be shared across databases:

- networking;
- accept loop;
- scheduler;
- IO runtime;
- code/JIT infrastructure;
- immutable type metadata;
- immutable built-in function metadata;
- timezone data;
- common parsing tables;
- global allocators;
- telemetry;
- common read-only catalog templates where valid.

Investigate what must remain isolated:

- user catalogs;
- transaction state;
- locks;
- relation metadata that can change;
- buffers containing tenant data;
- temporary state;
- prepared/session state;
- credentials;
- database-local settings;
- write sets.

Do not prematurely share mutable Postgres state merely to save memory.

Isolation is more important than a benchmark.

---

# 21. Approach 2 — workstream C: lazy loading

No database should eagerly materialize everything at creation time.

Creation should ideally become a metadata operation.

First connection/query can load what is required.

Candidates for lazy loading:

- user catalog entries;
- relation metadata;
- index metadata;
- table pages;
- statistics;
- compiled plans;
- JIT code;
- caches.

After inactivity, bounded caches should be evictable.

Benchmark:

- cold first query;
- warm query;
- memory before first query;
- memory after load;
- memory after eviction;
- resume latency.

---

# 22. Approach 2 — workstream D: copy-on-write storage

PGRun already uses snapshots/branching. PGX should eventually expose a storage abstraction that understands this directly.

Desired model:

```text
golden snapshot
    |
    +-- branch A overlay
    +-- branch B overlay
    +-- branch C overlay
```

A 10 GB golden image with a branch that changes 20 MB should not require another physical 10 GB copy.

The storage API should move conceptually from:

```text
open(PGDATA)
```

toward something like:

```text
open(base_snapshot, writable_overlay)
```

The exact API is an implementation detail.

Investigate:

- page-level COW;
- file-level COW;
- filesystem snapshots;
- reflinks;
- object-backed immutable base layers;
- local mutable overlays;
- overlay compaction;
- branch destruction as metadata cleanup.

Do not reimplement ZFS functionality inside PGX without evidence it is needed.

The first architecture prototype can use the capabilities PGRun already has.

The important goal is to make storage/runtime boundaries explicit enough that PGX can later support efficient native branching.

---

# 23. Approach 2 — workstream E: scale to zero

When an agent stops using a database:

1. wait for a small idle threshold;
2. ensure no active transaction;
3. flush only what the selected PGX durability mode requires;
4. release database-local caches and workers;
5. retain minimal metadata;
6. close expensive resources.

On the next connection:

1. resolve database identity;
2. restore/open storage;
3. rebuild only necessary state;
4. accept query quickly.

Measure:

- RAM reclaimed;
- resume p50/p95;
- first query p50/p95;
- CPU cost of suspend/resume;
- behavior with 100/1,000 databases waking concurrently.

Scale-to-zero is useful only if resume latency is acceptable for agents and CI.

---

# 24. Approach 2 — workstream F: scheduler and noisy-neighbor control

A shared runtime means one agent must not destroy the experience for others.

PgRust already has scheduler/OOM work that may be useful here.

PGX needs per-database or per-agent resource boundaries for:

- memory;
- CPU;
- query concurrency;
- temp space;
- result size;
- statement timeout;
- lock timeout;
- IO.

A pathological query in database A should be killable without killing databases B–Z.

This is essential for PGRun density.

Benchmark adversarial cases:

- one DB allocates aggressively;
- one DB runs a huge sort;
- one DB locks itself;
- one DB executes a long query;
- one DB causes temp spill;
- 100 DBs wake at once.

---

# 25. Approach 2 benchmark matrix

Approach 2 must use the same baseline harness plus new density tests.

Required scenarios:

### 1 DB

Measure overhead versus Approach 1.

A shared runtime must not make the normal single-database experience unusable.

### 100 idle DBs

Measure:

- total RSS;
- per-DB metadata;
- CPU wakeups;
- open file descriptors;
- background IO.

### 1,000 idle DBs

This is a primary architecture test.

### 10,000 stored but inactive DBs

Where feasible, databases do not all need to be resident. This tests whether “database existence” has become cheap.

### Wake storm

Start 100 databases/agents simultaneously.

Measure:

- p50/p95/p99 resume;
- peak RSS;
- CPU;
- errors.

### Mixed workload

Example:

- 1,000 databases exist;
- 50 active;
- 100 warm;
- 850 evicted.

This approximates an agent platform better than a single benchmark DB.

---

# 26. Phase comparison

At the end, produce a direct comparison.

```text
                        PG18      pgrust     PGX A1      PGX A2
----------------------------------------------------------------
architecture             proc      threads     tuned       shared/lazy
application compat
idle RSS / DB
100 idle DB RSS
1000 idle DB RSS
cold start
resume from zero
clone 100 MB
clone 1 GB
destroy
idle CPU
idle disk writes
migration suite
Rails tests
Django tests
Prisma tests
physical bytes / branch
```

The decision to continue Approach 2 must be based on measured economics.

---

# 27. Benchmark workloads

Synthetic `SELECT 1` is necessary but insufficient.

Use at least these workload classes.

## 27.1 Startup

- start;
- connect;
- `SELECT 1`;
- disconnect;
- destroy.

Repeat enough times for p50/p95/p99.

## 27.2 Schema/migration

Create a realistic application schema with:

- 50–100 tables;
- foreign keys;
- indexes;
- JSONB;
- enums;
- sequences;
- triggers if supported.

Run full migrations from empty.

## 27.3 Agent coding loop

Representative sequence:

```text
create branch
connect
inspect schema
run migration
insert fixtures
run 20-100 queries
modify schema
run tests
destroy
```

## 27.4 Transaction workload

Mix:

- SELECT;
- INSERT;
- UPDATE;
- DELETE;
- transactions;
- rollback;
- constraint violations;
- lock waits.

## 27.5 Framework tests

Real test suites are preferred over microbenchmarks.

Track pass rate and wall clock.

---

# 28. Observability for experiments

PGX development needs internal instrumentation separate from production observability features.

Add lightweight experiment metrics where needed:

- startup stage timings;
- allocated/resident memory by major subsystem;
- number of active databases;
- number of evicted databases;
- catalog cache bytes;
- buffer bytes;
- query memory;
- thread stack reservation;
- WAL bytes generated;
- physical bytes written;
- fsync count;
- worker/thread count;
- open FD count;
- suspend/resume counters.

Instrumentation must be possible to disable in normal builds if it changes results materially.

---

# 29. What not to do

## Do not rewrite PostgreSQL from scratch

PgRust has already solved a huge compatibility problem. Reuse it.

## Do not optimize for production first

PGX is explicitly an agent/test runtime.

## Do not chase ClickBench

PGX wins on density and lifecycle economics.

## Do not remove SQL features just because they are complicated

Application compatibility is the product.

## Do not delete WAL blindly

Understand dependencies first.

## Do not delete autovacuum blindly

Short-lived workloads still need correct statistics/maintenance in some cases.

## Do not put every database in a Docker container and call that PGX

PGRun already knows how to orchestrate Postgres. PGX must change database economics.

## Do not claim compatibility percentages without a corpus

Publish concrete suites and pass/fail results.

## Do not claim RAM/startup targets as achieved numbers

Targets are hypotheses until benchmarks exist.

## Do not begin shared-runtime work because it sounds elegant

Profile first.

---

# 30. Initial implementation order

Follow this order unless measurements give a strong reason not to.

## Step 0 — freeze baseline

- record current commit;
- build release binary;
- run conformance;
- add PGX benchmark harness;
- collect PostgreSQL and pgrust numbers.

## Step 1 — PGX config only

- add ephemeral configuration;
- benchmark;
- commit raw results.

## Step 2 — profile startup + idle memory

- identify top costs;
- produce subsystem report.

## Step 3 — disable obvious production operations

Start with the least application-visible candidates:

- replication launchers/senders/receivers;
- logical replication workers;
- backup;
- archive;
- standby-only paths;
- PITR operational paths.

Prefer runtime/compile-time flags over deletion.

Run compatibility + benchmark.

## Step 4 — durability experiment

- relaxed sync durability;
- measure WAL/syscalls/write reduction;
- preserve live transaction semantics;
- run crash/recreate tests.

## Step 5 — background minimization

- checkpointer behavior;
- autovacuum experiment;
- stats/logging;
- idle worker wakeups.

Benchmark idle economics.

## Step 6 — memory/startup optimization

- stacks;
- buffers;
- lazy caches;
- JIT on/off;
- eager initialization.

Benchmark density.

## Step 7 — publish Approach 1 report

Must answer:

- where RAM goes;
- where startup time goes;
- what was successfully removed;
- what could not be removed;
- compatibility regressions;
- best density;
- best first-query time;
- likely architecture floor.

## Step 8 — decide on Approach 2

Only then choose the first architectural prototype.

Recommended first prototype:

> multiple isolated database objects managed by one PGX daemon with database-local state that can be evicted when idle.

Do not attempt COW storage, shared runtime, new WAL semantics, and scale-to-zero all in one change.

---

# 31. Suggested repository structure

Use existing repository conventions where possible.

Possible additions:

```text
LLM.md
docs/pgx/
  architecture.md
  feature-inventory.md
  compatibility.md
  experiments/
    000-baseline.md
    001-ephemeral-config.md
    002-no-replication.md
    003-durability.md
    ...
configs/
  pgx-ephemeral.conf
benchmarks/pgx/
  README.md
  run.sh
  workloads/
  results/
```

If the existing repository has a better location for benchmarks/configs, use it. Do not create parallel infrastructure unnecessarily.

---

# 32. Experiment document template

Every material experiment should use this structure:

```markdown
# Experiment XXX — title

## Hypothesis

What resource/cost do we expect to reduce?

## Change

Exact code/config change.

## Compatibility risk

What behavior could change?

## Environment

Hardware, OS, filesystem, commit, compiler.

## Results

Raw + summarized numbers.

## Compatibility

Regression suite / application suite status.

## Conclusion

KEEP / REVERT / INVESTIGATE.

## Next

One next experiment.
```

This prevents PGX from becoming an unmeasured collection of “optimizations”.

---

# 33. PGRun-specific target workload

PGX must be evaluated in the environment it is actually being built for.

PGRun currently creates disposable database branches for agents/tests. Therefore include a PGRun-like benchmark:

1. prepare golden database;
2. create 100 branches;
3. start all;
4. connect once to all;
5. keep 80 idle;
6. actively query 20;
7. run migrations on 5;
8. destroy 50;
9. recreate 50;
10. record total host RSS and latency throughout.

Repeat for:

- PostgreSQL;
- pgrust baseline;
- PGX Approach 1;
- PGX Approach 2 prototype.

This benchmark is more important to PGX than a generic database benchmark.

---

# 34. Definition of success

The project is successful if PGX makes this economically reasonable:

```text
one coding agent
        =
one isolated Postgres-compatible database
```

and eventually:

```text
one PGRun host
        =
hundreds or thousands of isolated agent databases
```

without requiring application developers to rewrite their applications for a new SQL dialect or database API.

A user should receive:

```text
postgres://...
```

and normal Postgres tooling should mostly work.

The user should not need to care whether PGRun selected production PostgreSQL or PGX for a disposable branch.

---

# 35. Final design principle

When evaluating any PGX change, ask:

> Does this help a database that lives for minutes instead of years?

And:

> Does this make the cost of an inactive database closer to the cost of stored data rather than the cost of a running server?

If yes, investigate and measure.

If it only makes PGX theoretically cleaner, faster on an unrelated benchmark, or more production-capable, it is probably not the current priority.

The long-term vision is simple:

> **PGX is Postgres for agents.**  
> **PGRun runs them at scale.**
