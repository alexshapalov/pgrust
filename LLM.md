# PGX — LLM Implementation Instructions

## Mission

This repository is a fork of PgRust:

`https://github.com/alexshapalov/pgrust`

We are using it to build and evaluate **PGX**.

PGX is a lightweight, disposable, Postgres-compatible database runtime designed specifically for:

- AI coding agents
- CI
- tests
- preview environments
- development
- short-lived isolated databases

PGX is not intended to replace production PostgreSQL.

Production remains PostgreSQL.

PGX should provide enough PostgreSQL compatibility that normal applications, ORMs, migrations, test suites, and Postgres clients can use it without needing a separate database implementation.

The main hypothesis is:

> Production PostgreSQL is optimized to live for years.  
> PGX should be optimized to live for minutes or hours.

The long-term goal is:

> Make a Postgres-compatible database cheap enough to create and destroy like a temporary file.

PGX will eventually run underneath PGRun.

PGRun is the infrastructure/orchestration layer.

PGX is the database engine/runtime.

---

# 1. Do not redesign everything immediately

We must work in two phases.

Do not start Phase 2 until Phase 1 is complete and measured.

## Phase 1

Use the existing PgRust architecture.

Remove or disable everything that is unnecessary for short-lived agent/test databases.

Tune startup, memory, background work, and durability.

Measure the result.

Goal:

> Determine how lightweight PgRust can become without fundamentally changing its architecture.

## Phase 2

Only after Phase 1 measurements exist, investigate architectural changes inspired by lightweight database systems:

- one shared runtime
- many isolated databases
- lazy loading
- scale-to-zero
- database state eviction
- copy-on-write-aware storage
- database-as-an-object

Goal:

> Make the cost of an inactive PGX database approach the cost of storing its data rather than the cost of running a database server.

---

# 2. Important rule

Do not optimize for generic database benchmark performance first.

PGX is not trying to win ClickBench.

PGX should optimize for:

1. low idle RAM
2. low incremental RAM per database
3. fast startup
4. fast first connection
5. fast first query
6. cheap database creation
7. cheap branch creation
8. cheap branch destruction
9. zero or near-zero idle CPU
10. very little idle disk IO
11. very high database density
12. compatibility with normal Postgres applications

The most important question is:

> How many isolated Postgres-compatible databases can we run on one server?

---

# 3. Product architecture

Conceptually:

```text
                     PGRun
          infrastructure for agents
                       |
          +------------+------------+
          |            |            |
         PGX          PGX          PGX
       agent A      agent B      agent C
          |            |            |
          +---- Postgres protocol ---+
```

Expected PGRun workflow:

```text
Production PostgreSQL
        |
        v
safe / masked / sanitized copy
        |
        v
golden database
        |
        v
PGX branch
        |
        v
postgres://...
        |
        v
agent runs migrations / writes / tests
        |
        v
branch deleted
```

Typical database lifetime:

```text
seconds
minutes
hours
one test run
one CI job
one agent task
one development session
```

This assumption should influence every PGX design decision.

---

# 4. Compatibility philosophy

Do not build “Postgres Lite”.

Build:

> Postgres application semantics without production infrastructure.

We should preserve as much application-facing PostgreSQL behavior as possible.

Important functionality to keep includes:

- Postgres wire protocol
- SQL parser
- planner
- executor
- transactions
- MVCC
- locks
- isolation
- schemas
- tables
- views
- indexes
- foreign keys
- constraints
- sequences
- identity columns
- JSON
- JSONB
- arrays
- enums
- timestamps
- COPY
- triggers
- PL/pgSQL where supported
- prepared statements
- CTEs
- subqueries
- window functions
- joins
- common catalog queries
- EXPLAIN
- normal DDL
- temporary tables where possible
- common ORM behavior

A Rails, Django, Prisma, SQLAlchemy, Node, Go, or other normal Postgres application should ideally not need special code for PGX.

---

# 5. Features we probably do not need

PGX is not a production database.

Primary candidates for disabling or removing from the PGX profile include:

- streaming replication
- physical replicas
- standby mode
- synchronous replication
- WAL sender
- WAL receiver
- replication slot synchronization
- production replication management
- WAL archiving
- archive_command
- PITR
- timeline management used only by recovery/replication
- production backup infrastructure
- pg_basebackup server functionality
- incremental production backups
- HA machinery
- failover machinery
- long-term WAL retention
- production-oriented background workers that are unnecessary for ephemeral databases

Do not delete these blindly.

First inspect dependencies and determine whether disabling the subsystem affects application behavior.

Prefer:

- runtime flags
- Cargo features
- PGX-specific build profile
- conditional compilation
- alternate startup path

before permanently deleting code.

---

# 6. Features that must not be removed blindly

Do not remove these merely because PGX databases are temporary:

- transactions
- WAL-related logic required for transaction correctness
- MVCC
- locking
- vacuum
- analyze
- statistics
- catalogs
- checkpoints
- recovery-related code that is necessary for correct startup
- buffer management
- sequence durability semantics
- catalog invalidation
- transaction ID handling

Some of these may be simplified or changed later.

First measure and understand them.

---

# 7. Phase 0 — establish baseline

Before any PGX optimization, collect a clean baseline.

We need three initial variants:

```text
PostgreSQL 18
PgRust current fork
PGX experiments
```

Use the same:

- machine
- CPU
- RAM
- OS
- kernel
- filesystem
- filesystem options
- dataset
- connection client
- test scripts
- compiler mode
- release build

Record the exact Git commit for every run.

Do not compare results collected on different machines unless clearly labeled.

---

# 8. Create PGX benchmark harness first

Create:

```text
benchmarks/pgx/
```

Suggested structure:

```text
benchmarks/pgx/
  README.md
  run-all.sh
  run-startup.sh
  run-memory.sh
  run-density.sh
  run-branch.sh
  run-workload.sh
  collect-process-metrics.sh

  workloads/
    basic.sql
    transactions.sql
    migration/
    agent-loop/

  results/
```

Every benchmark must emit structured output.

Prefer JSON.

Example:

```json
{
  "engine": "pgrust",
  "profile": "baseline",
  "git_sha": "...",
  "database_count": 100,
  "startup_ms": 240,
  "first_query_ms": 270,
  "rss_bytes": 123456789,
  "cpu_idle_percent": 0.2
}
```

Do not rely only on terminal output.

---

# 9. Metrics to collect

For every configuration collect:

## Startup

Measure:

```text
process launch
-> socket listening
-> connection accepted
-> authentication complete
-> SELECT 1 returns
```

Record:

- p50
- p95
- p99

The most important metric is:

> process start → successful first useful query

Not merely “process started”.

---

# 10. Memory tests

Measure RSS for:

```text
0 connections
1 connection
10 connections
100 connections
```

Where possible measure:

```text
1 database
10 databases
100 databases
500 databases
1000 databases
```

Important metrics:

```text
base runtime RSS
incremental RSS per connection
incremental RSS per database
peak RSS during startup
peak RSS during migration
RSS after becoming idle
```

Do not estimate per-database RAM from one instance if multiple actual instances can be measured.

---

# 11. Idle tests

After startup and after all connections close:

measure for at least several minutes:

- CPU usage
- wakeups
- disk writes
- fsync calls
- worker/thread activity
- RSS
- open file descriptors

Goal:

> An unused PGX database should do almost nothing.

---

# 12. Dataset sizes

Test at minimum:

```text
empty
~100 MB
~1 GB
```

Optionally:

```text
~10 GB
```

The 100 MB and 1 GB cases are most important initially.

---

# 13. Representative application schema

Create a realistic schema approximately similar to a normal SaaS application.

Suggested:

```text
50–100 tables
foreign keys
indexes
JSONB
timestamps
enums
sequences
unique constraints
join tables
some triggers
```

Do not benchmark only `SELECT 1`.

---

# 14. Agent workload benchmark

Create a workload that simulates how PGX will actually be used.

Example:

```text
create database
connect
inspect schema
run migration
insert test data
run reads
run writes
open transaction
rollback
run application tests
alter schema
run tests again
destroy database
```

Measure total wall-clock time.

This benchmark matters more than generic OLTP benchmark scores.

---

# 15. Phase 1A — configuration-only PGX

Do not modify core architecture yet.

First create a PGX ephemeral configuration.

Suggested file:

```text
configs/pgx-ephemeral.conf
```

Investigate these settings.

Do not assume they all exist or behave identically in PgRust.

Test before using.

Candidates:

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

Also experiment with:

```text
shared_buffers
max_connections
work_mem
maintenance_work_mem
temp_buffers
checkpoint behavior
logging
statistics
JIT
parallel workers
autovacuum
background workers
IO mode
```

The objective is to find the cheapest configuration that still runs normal application workloads correctly.

---

# 16. Run benchmark after configuration-only changes

Compare:

```text
Postgres
PgRust baseline
PgRust + PGX config
```

Do not remove code yet.

Create a result document:

```text
docs/pgx/experiments/001-ephemeral-config.md
```

Include:

```text
Hypothesis
Configuration
Hardware
Results
Compatibility
Conclusion
Next step
```

---

# 17. Inspect where memory actually goes

Before deleting features, profile PgRust.

Determine memory usage for:

- process/runtime base
- thread stacks
- connection state
- buffers
- catalog caches
- relation caches
- planner
- executor
- JIT
- WAL
- background workers
- statistics
- logging
- IO workers
- transaction structures

If necessary, add temporary instrumentation.

Do not optimize from guesses.

Produce:

```text
docs/pgx/memory-profile.md
```

---

# 18. Inspect startup path

Add timing instrumentation around major initialization stages.

We want output similar to:

```text
process exec                0 ms
config loaded               4 ms
PGDATA opened              13 ms
control loaded             25 ms
catalog initialized        72 ms
WAL initialized            90 ms
workers initialized       110 ms
socket listening          125 ms
connection accepted       140 ms
SELECT 1                  170 ms
```

The exact stages depend on PgRust.

Produce:

```text
docs/pgx/startup-profile.md
```

Identify the top three startup costs.

---

# 19. Build a feature inventory

Create:

```text
docs/pgx/feature-inventory.md
```

For every major subsystem document:

```text
name
purpose
required for application compatibility?
startup cost
memory cost
background CPU cost
disk IO cost
dependencies
can disable at runtime?
can remove from PGX build?
compatibility risk
decision
```

Classify each feature.

## Class A

Must remain.

Example:

```text
parser
planner
executor
transactions
MVCC
locks
catalogs
heap
indexes
types
JSONB
protocol
```

## Class B

Potentially optional or lazy.

Example:

```text
autovacuum
analyze
stats
JIT
parallel workers
logging
```

## Class C

Production infrastructure.

Example:

```text
streaming replication
backup
archive
standby
PITR
HA
replication slot synchronization
```

---

# 20. Phase 1B — disable production subsystems

Once the inventory exists, start with low-risk production-only functionality.

Candidates include PgRust workspace areas related to:

```text
replication
logical replication
walreceiver
walsender
slotsync
backup
basebackup
archive
standby
recovery-specific production paths
```

Prefer creating a PGX build profile.

Potential conceptual Cargo feature:

```text
pgx-ephemeral
```

And optional features such as:

```text
pgx-no-replication
pgx-no-backup
pgx-no-archive
```

Do not over-engineer the feature system.

One PGX profile is enough initially if that is simpler.

---

# 21. Measure after every feature group

Example progression:

```text
A0 PostgreSQL
A1 PgRust
A2 PgRust + PGX config
A3 + replication disabled
A4 + backup/archive disabled
A5 + durability changes
A6 + worker/background changes
A7 + memory/startup tuning
```

Do not make all changes first and benchmark at the end.

We need to understand what each change buys.

---

# 22. Phase 1C — durability experiment

PGX databases are disposable.

This means PGX may make a different durability tradeoff from production PostgreSQL.

The desired property is:

> Transactions behave normally while PGX is alive, but catastrophic host/process failure may require recreating the database from PGRun.

This is acceptable for agent/test branches.

Investigate:

```text
fsync removal
sync commit removal
full-page write removal
WAL retention reduction
checkpoint reduction
recovery simplification
```

But do not blindly remove WAL.

Understand exactly which PostgreSQL semantics depend on it.

Run:

- transaction tests
- rollback tests
- constraint tests
- crash tests
- restart tests
- corruption checks

Document the exact durability contract.

Suggested:

```text
docs/pgx/durability.md
```

---

# 23. PGX durability contract

Eventually we should be able to state something like:

> PGX ephemeral mode provides normal transactional semantics while the database instance is running.

> PGX does not guarantee survival of host or process failure.

> If the runtime fails, PGRun may discard the branch and recreate it from its source/golden snapshot.

Do not publish this wording until implementation matches it.

---

# 24. Phase 1D — minimize background work

Inspect all background workers/threads.

For each:

```text
why does it exist?
how often does it wake?
RAM cost?
CPU cost?
disk write cost?
can it run on demand?
can it be disabled?
```

Investigate at least:

- checkpointer
- autovacuum
- statistics
- logging
- IO workers
- replication launchers
- background schedulers

Important goal:

> idle means idle

No unnecessary wakeups.

No periodic writes if avoidable.

---

# 25. Autovacuum

Do not simply disable autovacuum permanently.

Test:

```text
autovacuum ON
autovacuum OFF
autovacuum lazy/delayed
```

Short-lived branches may often not need normal production maintenance.

But some tests may create many dead rows or depend on statistics.

Measure:

- memory
- CPU
- writes
- application behavior
- performance after many updates

Decide from data.

---

# 26. Analyze/statistics

Migration/test workloads may depend on reasonable query plans.

Determine whether:

- ANALYZE should remain
- automatic ANALYZE should remain
- statistics can be inherited from the golden image
- statistics collection can be delayed
- statistics can be shared/copied

This may become important later for instant branches.

---

# 27. JIT

PgRust heavily uses JIT.

Do not assume JIT is beneficial for PGX workloads.

Agent workloads often consist of many short queries.

Benchmark:

```text
JIT ON
JIT OFF
JIT lazy
```

Measure:

- first query latency
- migration workload
- short query latency
- CPU
- memory
- long query performance

PGX may prefer faster startup and lower per-query setup over peak throughput.

---

# 28. Thread stack memory

PgRust uses threads.

Investigate stack configuration carefully.

Measure:

```text
reserved stack memory
resident stack memory
stack per connection
stack per worker
```

The current PgRust quickstart uses a large Rust stack.

This may be unacceptable for hundreds/thousands of isolated databases or connections.

Test lower values carefully.

Run:

- regression suite
- deeply nested SQL
- recursive CTEs
- complex planner cases
- PL/pgSQL recursion if applicable

Never reduce stack size based only on startup success.

---

# 29. Connection memory

Profile one connection.

Then:

```text
10
100
500
```

Determine whether PgRust eagerly allocates:

- planner structures
- catalog state
- buffers
- stacks
- caches
- session state

Convert expensive eager state to lazy allocation where practical.

---

# 30. Cache strategy

PGX should prefer bounded and reclaimable caches.

A database that was active and then becomes unused should not permanently retain large memory caches.

Investigate:

- catalog cache
- relation cache
- plan cache
- buffer cache
- JIT cache

Question:

> Can this memory be dropped when a PGX database becomes idle?

This becomes more important in Phase 2.

---

# 31. Phase 1 benchmark result table

At the end of Phase 1 produce something like:

```text
Metric                 PG18   PgRust   PGX-config   PGX-final
--------------------------------------------------------------
cold start ms
first query ms
idle RSS MB
1 connection RSS
10 connections RSS
idle CPU
idle writes/min
100 MB workload
1 GB workload
migration time
test suite time
binary size
```

For multiple instance tests:

```text
Instances              PG18   PgRust   PGX
-------------------------------------------
1
10
100
500
1000
```

Record total RSS and average incremental RSS.

---

# 32. Application compatibility suite

Create a separate PGX compatibility suite.

At minimum eventually test:

```text
Rails / ActiveRecord
Django
Prisma
SQLAlchemy
node-postgres
Go PostgreSQL client
```

Use real migrations and CRUD.

Test:

- create schema
- migrate
- insert
- update
- delete
- joins
- transactions
- rollback
- constraints
- indexes
- JSONB
- schema introspection
- test suite

Record pass/fail.

---

# 33. Phase 1 go/no-go decision

After Phase 1, answer:

## Question 1

Did we materially reduce startup time?

## Question 2

Did we materially reduce idle memory?

## Question 3

Did database density improve by multiples, not merely a few percent?

## Question 4

Did application compatibility remain acceptable?

## Question 5

Where is the remaining fixed cost?

If PgRust with PGX tuning is already cheap enough for PGRun, stop and use it.

Do not redesign architecture unnecessarily.

If a large per-instance cost remains, continue to Phase 2.

---

# 34. Phase 2 — fundamental architecture goal

Phase 2 should pursue the Turso-like idea:

> Database is an object, not a server.

Instead of:

```text
database
=
one full server instance
```

we want:

```text
one PGX runtime
=
many isolated databases
```

Concept:

```text
                    pgxd
              shared PGX runtime

          /            |            \
         /             |             \
      DB A            DB B            DB C

    agent A          agent B          agent C
```

Each database remains isolated.

Clients still use PostgreSQL connections.

---

# 35. Phase 2 target

Long-term desired behavior:

```text
10,000 databases exist
100 are warm
20 are active
9,880 consume almost no RAM
```

Do not treat those numbers as already achievable.

They are the architectural direction.

---

# 36. Database lifecycle

Define an internal database object with a lifecycle similar to:

```text
CREATE
STORED
WARMING
ACTIVE
IDLE
EVICTED
DESTROYED
```

Possible API:

```text
create()
open()
resume()
connect()
idle()
evict()
snapshot()
fork()
destroy()
```

The key concept is:

> Database existence should not require a dedicated running database server.

---

# 37. Scale-to-zero

When no clients use a database:

```text
ACTIVE
  |
  v
IDLE
  |
  v
EVICTED
```

Eviction should release:

- caches
- unnecessary buffers
- query state
- JIT state
- worker state
- file descriptors where possible

Keep only enough metadata to resume quickly.

When a client reconnects:

```text
EVICTED
  |
  v
WARMING
  |
  v
ACTIVE
```

Measure:

- wake latency
- first query latency
- RAM reclaimed
- CPU required to wake
- concurrent wake behavior

---

# 38. Shared runtime

Investigate what can be global/shared.

Candidates:

- network listener
- IO runtime
- scheduler
- common immutable built-in metadata
- timezone data
- built-in functions
- parser tables
- common libraries
- JIT infrastructure
- telemetry
- allocators

Do not share tenant data or mutable state unsafely.

---

# 39. Database-local state

Likely must remain isolated:

- user catalogs
- relation state
- table data
- transaction state
- locks
- temporary state
- session state
- mutable planner/catalog state
- credentials
- local configuration

Never trade away isolation merely to improve a benchmark.

---

# 40. Lazy initialization

Creation should perform almost no unnecessary work.

Desired:

```text
create database
-> allocate identity
-> attach storage
-> return connection information
```

Do not load every relation/catalog/cache until needed.

First query can load required metadata lazily.

Measure:

```text
create time
pre-query RAM
first query time
warm query time
RAM after query
RAM after eviction
```

---

# 41. Storage architecture

PGRun already has snapshot/COW infrastructure.

PGX should eventually be able to understand the concept directly.

Desired conceptual model:

```text
Golden Database
       |
       +--- Branch A overlay
       |
       +--- Branch B overlay
       |
       +--- Branch C overlay
```

If golden database is 10 GB and Agent A modifies 10 MB:

```text
physical branch cost ≈ changed blocks
```

not 10 GB.

Do not immediately rewrite storage.

First integrate with the COW mechanism PGRun already has.

Later consider a native storage abstraction.

---

# 42. Storage interface idea

Current database systems often conceptually operate on:

```text
open(PGDATA)
```

PGX may eventually benefit from something more like:

```text
open(base_snapshot, writable_overlay)
```

This is a conceptual target.

Do not implement it until profiling justifies it.

---

# 43. Clone benchmark

We need a branch benchmark.

Test golden database sizes:

```text
100 MB
1 GB
10 GB
```

Measure:

```text
clone/create time
time until connection
time until first query
incremental physical storage
memory consumed
delete time
```

This benchmark should model PGRun directly.

---

# 44. PGRun density benchmark

Create the most important test.

Start with one golden database.

Then:

```text
create 100 branches
start all
connect to all
keep 80 idle
run workload on 20
run migrations on 5
delete 50
create another 50
```

Measure continuously:

- total RSS
- peak RSS
- CPU
- IO
- startup latency
- branch latency
- errors

Run against:

```text
PostgreSQL
PgRust
PGX Phase 1
PGX Phase 2
```

This is the primary product benchmark.

---

# 45. Noisy neighbor control

A shared runtime introduces a new risk.

One agent must not kill all other agents.

PGX needs limits for:

- memory
- CPU
- query concurrency
- temp files
- statement duration
- lock waits
- result size
- IO

Test:

```text
DB A runs huge sort
DB B runs SELECT 1
```

DB B should remain responsive.

Also test:

```text
DB A allocates too much memory
DB B–Z continue working
```

PgRust's existing scheduler and OOM-killer architecture may be useful here.

Reuse it where possible.

---

# 46. Wake storm

Test:

```text
100 databases wake simultaneously
```

Measure:

```text
p50 wake
p95 wake
p99 wake
peak RAM
peak CPU
errors
```

Then eventually:

```text
500
1000
```

This matters because many coding agents may start at the same time in CI.

---

# 47. Stored database test

Eventually test:

```text
10,000 databases exist
0 active
```

They should not require 10,000 fully resident database runtimes.

Measure:

```text
total RAM
metadata RAM / DB
open FDs
CPU
disk IO
```

This is a major Phase 2 success criterion.

---

# 48. Experiment discipline

Every change must have an experiment document.

Template:

```markdown
# Experiment XXX — title

## Hypothesis

What do we believe?

## Current behavior

What happens before this change?

## Change

Exactly what was changed.

## Compatibility risk

What could break?

## Benchmark environment

Hardware
OS
kernel
filesystem
commit
compiler

## Results

Raw results.

## Delta

Comparison against baseline.

## Compatibility

Regression suite status.

Application suite status.

## Decision

KEEP
REVERT
INVESTIGATE

## Next experiment

Exactly one next step.
```

Do not make undocumented performance changes.

---

# 49. Required docs

Create and maintain:

```text
docs/pgx/
  architecture.md
  feature-inventory.md
  compatibility.md
  durability.md
  memory-profile.md
  startup-profile.md

  experiments/
    000-baseline.md
    001-ephemeral-config.md
    002-replication.md
    003-backup-archive.md
    004-durability.md
    005-background-workers.md
    006-memory.md
    007-startup.md
```

Add more experiments as needed.

---

# 50. Do not optimize binary size as a primary metric

Binary size is interesting but not a product goal.

Do not celebrate:

```text
binary -40%
```

if:

```text
idle RAM unchanged
startup unchanged
database density unchanged
```

The product wins when running databases become cheaper.

---

# 51. Do not remove features based only on source code size

A subsystem may contain thousands of lines but cost almost nothing at runtime.

Another tiny subsystem may allocate 20 MB per database.

Profile first.

---

# 52. Do not rewrite PgRust

PgRust already provides an enormous amount of PostgreSQL compatibility.

Use it.

The purpose of this fork is not:

> build another PostgreSQL implementation.

The purpose is:

> adapt PgRust for a radically different lifecycle and workload.

---

# 53. Do not optimize for production

If an optimization only helps:

```text
7-day uptime
24/7 OLTP
multi-TB database
replica failover
PITR
HA
long-lived vacuum stability
```

it is probably not our current priority.

PGX should still be correct for its supported workload.

But production operations are not the target.

---

# 54. Do not break developer expectations unnecessarily

Normal tooling should continue to work whenever possible.

Examples:

```text
psql
Rails
Django
Prisma
SQLAlchemy
migration tools
database clients
```

A developer should receive:

```text
postgres://host/database
```

and use it normally.

---

# 55. Never fake benchmark conclusions

Do not write claims like:

```text
PGX uses 5 MB RAM
PGX starts in 20 ms
PGX supports 10,000 databases
```

unless measurements actually demonstrate them.

Clearly distinguish:

```text
goal
hypothesis
measurement
```

---

# 56. Target direction, not guaranteed numbers

Possible aspirational targets:

```text
cold start: <100 ms
idle incremental RAM: single-digit MB
100 databases/server: trivial
1000 databases/server: practical
inactive database: near-zero resident memory
```

These are goals for experimentation.

They are not current claims.

---

# 57. First concrete tasks

Do these now, in this order.

## Task 1

Build current fork in release mode.

Verify:

```text
cargo build --release --locked --bin postgres
```

Record:

```text
commit SHA
binary size
build environment
```

## Task 2

Run existing PgRust conformance/regression tests.

Record baseline status.

Do not start optimization work from a broken baseline.

## Task 3

Create:

```text
benchmarks/pgx/
```

Implement startup benchmark.

Measure:

```text
process launch -> SELECT 1
```

Run at least 30 iterations.

Output p50/p95/p99.

## Task 4

Implement idle memory benchmark.

Measure:

```text
startup
connect
SELECT 1
disconnect
wait
measure RSS
```

Repeat consistently.

## Task 5

Implement connection memory benchmark.

Measure:

```text
0
1
10
100
```

connections.

## Task 6

Create realistic test database.

Approximately:

```text
100 MB
50–100 tables
indexes
FK
JSONB
```

Document generation.

## Task 7

Measure baseline PostgreSQL.

Use PostgreSQL 18.x.

Store results.

## Task 8

Measure baseline PgRust.

Store results.

## Task 9

Create:

```text
configs/pgx-ephemeral.conf
```

Start with configuration-only changes.

No core code modification yet.

## Task 10

Measure PGX config.

Compare against PgRust baseline.

Determine what improved.

---

# 58. Next concrete tasks

After config experiment:

## Task 11

Profile startup.

Create:

```text
docs/pgx/startup-profile.md
```

## Task 12

Profile memory.

Create:

```text
docs/pgx/memory-profile.md
```

## Task 13

Create feature inventory.

Create:

```text
docs/pgx/feature-inventory.md
```

## Task 14

Identify production-only subsystems with measurable runtime cost.

Do not choose based only on intuition.

## Task 15

Disable the first production subsystem.

Recommended starting candidates:

```text
replication workers
WAL sender
WAL receiver
slot synchronization
```

Run tests.

Run benchmarks.

Document.

## Task 16

Continue with:

```text
backup
archive
standby/recovery operational paths
```

one group at a time.

## Task 17

Run durability experiment.

Measure:

```text
fsync
synchronous commit
WAL volume
checkpoint work
physical writes
```

## Task 18

Investigate background worker removal/lazy behavior.

## Task 19

Investigate JIT tradeoffs.

## Task 20

Investigate thread stack and per-connection memory.

---

# 59. Phase 1 output

At the end of Phase 1 produce:

```text
docs/pgx/phase1-results.md
```

It must contain:

## Baseline

PostgreSQL and PgRust.

## Final PGX configuration

Exact settings/build features.

## Features disabled

Exact list.

## Compatibility impact

Exact failures.

## Startup results

p50/p95/p99.

## Memory results

1/10/100 DB.

## Connection results

1/10/100 connections.

## Idle behavior

CPU and IO.

## Application results

Framework/test compatibility.

## Conclusion

Answer:

> Is this already good enough for PGRun?

If yes, stop architecture work and integrate it.

If no, explain exactly why.

---

# 60. Phase 2 start condition

Begin Phase 2 only if Phase 1 demonstrates a fixed per-database cost that prevents desired PGRun density.

Examples:

```text
large unavoidable process/runtime base
large per-server cache
large eager catalog state
large thread/runtime footprint
background machinery that cannot be removed
```

Phase 2 should attack measured costs.

Not theoretical costs.

---

# 61. Phase 2 first prototype

Do not build the entire final architecture.

The first experiment should answer one question:

> Can multiple isolated database objects exist inside one PgRust/PGX runtime without paying the full runtime cost per database?

Prototype only enough to measure this.

Do not combine:

```text
new storage
new WAL
new networking
new scheduler
new COW
new cache
```

into one patch.

---

# 62. Phase 2 second prototype

If shared runtime works, implement idle state eviction.

Goal:

```text
database exists
but no clients
-> release almost all database-local memory
```

Measure wake latency.

---

# 63. Phase 2 third prototype

Integrate COW branch storage.

Reuse PGRun/ZFS first.

Do not build a new distributed storage system.

---

# 64. Phase 2 fourth prototype

Test:

```text
1000 existing databases
20 active
```

Measure total host economics.

---

# 65. Key question for every change

Before implementing anything, ask:

> Does this reduce the cost of a database that may live for only 5 minutes?

If no, it is probably not important now.

Then ask:

> Does this reduce the cost of an inactive database?

If yes, it may be extremely important.

Then ask:

> Does this break normal Postgres application behavior?

If yes, reconsider.

---

# 66. Long-term North Star

The long-term architecture should make this normal:

```text
Agent #1 -> its own PGX
Agent #2 -> its own PGX
Agent #3 -> its own PGX
...
Agent #1000 -> its own PGX
```

without treating each database as heavyweight infrastructure.

Eventually:

> Database creation becomes a cheap runtime primitive.

---

# 67. Final product principle

PGX exists because agent infrastructure changes the lifetime of databases.

Traditional model:

```text
few databases
live for years
care deeply about crash durability
HA
replication
backup
operations
```

PGX model:

```text
many databases
live for minutes/hours
isolated per task
easy to recreate
easy to destroy
cheap while idle
```

We should not optimize an old architecture for the wrong lifecycle.

The main vision is:

> **PGX — Postgres for agents.**

And:

> **PGRun — infrastructure that runs PGX at scale.**

The final success condition is simple:

> Every agent can get its own isolated Postgres-compatible database without the developer thinking about database infrastructure cost.
