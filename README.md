<h1 align="center">PGX</h1>

<p align="center">
  <strong>One Postgres-compatible runtime, thousands of disposable databases.</strong>
</p>

<p align="center">
  <img alt="Postgres 18.6" src="https://img.shields.io/badge/Postgres-18.6-336791">
  <img alt="Version: v0.1" src="https://img.shields.io/badge/version-v0.1-blue">
  <img alt="Regression suite: 0 result differences" src="https://img.shields.io/badge/regression_suite-0_result_differences-brightgreen">
  <a href="LICENSE"><img alt="License: AGPL-3.0" src="https://img.shields.io/badge/license-AGPL--3.0-blue"></a>
</p>

PGX is a database runtime for short-lived Postgres databases: agent
sandboxes, CI and per-PR databases, test and preview environments. One PGX
process holds many isolated databases. Each costs tens of kilobytes of
memory while idle, is created in milliseconds from a sealed template, and
is backed by a ZFS copy-on-write clone that starts at a few megabytes,
whatever the template's size.

PGX speaks the Postgres wire protocol and SQL dialect. Rails, Django,
Prisma, SQLAlchemy and node-postgres run their full migrate → test →
schema-change lifecycles on it unchanged.

PGX is the database engine behind PGRun branches.

## Benchmarks

Measured on a 4 vCPU / 8 GB / $8.50-a-month VPS with ZFS block cloning. The
load generator ran on the same CPUs, so these are architecture numbers,
not capacity limits. Every figure has a script and a committed raw result
([`benchmarks/pgx/linux/README.md`](benchmarks/pgx/linux/README.md)).

### Density

| | PGX |
|---|---|
| 1000 idle databases in one runtime | **128–155 MB** in total |
| 1000 databases, each queried once | **213 MB** |
| Per idle database | ~70 KB untouched, ~128 KB once queried |
| 1000 databases with 100 active | 283 MB |
| Idle CPU with 1000 databases | 0.33–0.43 % of one core |

For comparison, one idle PostgreSQL 18 server uses 16.6 MB.

### Creating databases

| | p50 | p99 |
|---|---|---|
| Database from the warm pool | 6.2 ms | 9.4 ms |
| Cold database (copy-on-write clone of a 600-file template) | 74 ms | 102 ms |
| 500 simultaneous new-database requests | all served, 0 errors | drained in 12.6–13.8 s |

Times run from the client's connection to its first `SELECT 1` answer,
on the same host.

### Branch storage (ZFS copy-on-write)

| Template | Branch ready in | New physical storage |
|---|---|---|
| 1 GB | 135 ms | 2.3 MB |
| 2 GB | 198 ms | 3.7 MB |
| 5.2 GB | 417 ms | 7.0 MB |
| 10 GB | 674 ms | 13.6 MB |

A branch of a 10 GB database is ready in under a second and initially uses
hundreds of times less physical storage than a full copy.

### Running for a long time

| | Runtime memory |
|---|---|
| After 10,000 create → use → drop lifecycles | 108 MB |
| After 30,000 lifecycles | 111 MB (~0.2 KB retained per lifecycle) |

### Compatibility

| | Result |
|---|---|
| PostgreSQL regression suite | 219 / 231 byte-exact. The other 12 differ only in EXPLAIN plan text: **0 result differences**. |
| Real Rails 8.1 app (migrations, fixtures, model tests, schema change, `db:schema:load`) | passes; within 0–5 % of PostgreSQL 18 per phase |
| Django 5.2, Prisma 6, SQLAlchemy 2, node-postgres, Rails-shaped SQL | pass |
| All framework runs | **36 / 36** |

### Isolation

- **Memory limits.** Session, database and runtime limits refuse runaway
  work with SQLSTATE 53200. The session stays usable, other databases see
  0 errors, and the runtime does not crash.
- **Connection limits.** A neighbour running a 50-connection migration
  mix pushes another database's p99 latency to 9.8 ms. Capped at 1 / 2 / 5
  connections per database, it stays at 1.2 / 2.1 / 4.0 ms.
- **cgroups.** The kernel is the last line: under a cgroup memory limit,
  the worst case is that one runtime is killed. The host survives.

### Query speed

PGX is not trying to beat Postgres on single queries.

- **Short statements:** about 0.05–0.15 ms slower than PostgreSQL 18; a
  point SELECT takes 0.25 vs 0.13 ms.
- **Large single-threaded scans:** 1.5–2.2× slower.
- **Equal:** COPY and 4-client throughput.

Whole framework lifecycles take 0–15 % longer than on PostgreSQL 18.

The full report:
[`docs/pgx/linux-results.md`](docs/pgx/linux-results.md). The frozen v0.1
numbers: [`docs/pgx/v0.1-baseline.md`](docs/pgx/v0.1-baseline.md).

## How it works

- **Shared runtime.** Databases are isolated by name, roles and limits
  inside one process with a thread per connection. A database costs its
  catalog caches, not a server.
- **Shared catalog state.** Databases cloned from the same template share
  identical relation-cache entries. Idle databases are skipped by
  autovacuum.
- **Mint-on-connect.** Connecting to `<prefix><template>__<token>` creates
  that database from a sealed template and connects you to it. A warm pool
  of pre-created spares serves bursts. A janitor drops databases a grace
  period after their last session.
- **Copy-on-write storage.** `file_copy_method = clone` turns database
  creation into ZFS block clones, so a branch shares every unchanged block
  with its template.
- **Limits at every level.** Session, database and runtime memory limits
  inside the engine, connection limits per database, and a Linux cgroup
  around the runtime.
- **Machine-readable status.** `pgrust_runtime_status()` returns JSON with
  databases, sessions, memory against limits, and warm-pool and mint
  counters.

## Status

**PGX v0.1 is for disposable databases:** agent, CI, PR, test and preview
databases built from sanitized copies of production. Use it where losing
a branch is acceptable.

It is **not** a production primary, a system of record, or an HA or PITR
database:

- **Not crash-safe.** The PGX profile runs with `fsync = off`.
- **One process.** A runtime is one crash domain.
- **No C extensions.** Existing PostgreSQL C extensions cannot load; only
  the built-in contrib modules exist, pgvector among them. PL/Python,
  PL/Perl and PL/Tcl are not available.

Keep Postgres as the source of truth for data you can't afford to lose.
The full list is in [`docs/pgx/v0.1-readiness.md`](docs/pgx/v0.1-readiness.md).

## Quickstart

### Build

You need the Rust toolchain (`rust-toolchain.toml` pins Rust 1.96.0;
rustup fetches it), the RE2 regex library, and the PostgreSQL 18 client
tools for `initdb` and `psql`:

- Debian/Ubuntu: `sudo apt-get install -y build-essential pkg-config libre2-dev`,
  plus `postgresql-18` and `postgresql-client-18` from the
  [PGDG apt repo](https://www.postgresql.org/download/linux/ubuntu/)
  (then `export PATH="/usr/lib/postgresql/18/bin:$PATH"`).
- macOS: `brew install re2 pkg-config postgresql@18`, then
  `export PATH="$(brew --prefix postgresql@18)/bin:$PATH"`.

```bash
cargo build --release --locked --bin postgres
```

The server binary is `target/release/postgres`. Release builds refuse to
compile without RE2 on purpose: the fallback regex engine is much slower.

### Run a PGX runtime

```bash
# Create a cluster and apply the PGX profile.
initdb -D /tmp/pgx-data --no-locale --encoding UTF8 -U postgres
cat configs/pgx-ephemeral.conf >> /tmp/pgx-data/postgresql.conf

# The server reads timezone and other data files from a Postgres share dir.
export PGRUST_PGSHAREDIR=/usr/share/postgresql/18      # macOS: "$(brew --prefix postgresql@18)/share/postgresql"
export PGRUST_TZDIR=/usr/share/zoneinfo                # macOS: "$PGRUST_PGSHAREDIR/timezone"

ulimit -s 65520
RUST_MIN_STACK=33554432 target/release/postgres \
  -D /tmp/pgx-data -k /tmp -p 5432 \
  -c listen_addresses= -c io_method=sync -c max_stack_depth=60000 \
  -c pgrust.ephemeral_db_prefix=tdb_ \
  -c pgrust.ephemeral_db_mint_roles=postgres
```

Put the data directory on a ZFS dataset with block cloning enabled to get
copy-on-write branches (the profile sets `file_copy_method = clone`). On a
filesystem without block cloning, remove that line from the profile and
databases are created by copying files.

### Make a template and mint databases

```bash
# Build a template once, then seal it.
psql -h /tmp -U postgres -c "CREATE DATABASE tpl_app"
psql -h /tmp -U postgres -d tpl_app -c "CREATE TABLE users (id bigserial PRIMARY KEY, email text UNIQUE)"
psql -h /tmp -U postgres -c "SELECT pgrust_seal_template('tpl_app')"

# Connecting to <prefix><template>__<token> creates that database.
psql -h /tmp -U postgres -d tdb_tpl_app__agent42 -c "INSERT INTO users (email) VALUES ('a@example.com')"
psql -h /tmp -U postgres -d tdb_tpl_app__agent43 -c "SELECT count(*) FROM users"   # 0: isolated

# Runtime status as JSON.
psql -h /tmp -U postgres -Atc "SELECT pgrust_runtime_status()"
```

Minted databases are dropped `pgrust.ephemeral_db_grace` seconds after
their last session ends, unless they are pinned with
`pgrust_pin_database()`. All control functions are listed in
[`docs/pgx/pgrun-interface.md`](docs/pgx/pgrun-interface.md).

## Reproducing the benchmarks

The harness is Python 3 standard library only and talks the wire protocol
directly. On a Linux host with a ZFS pool:

```bash
PGXBENCH_WORKDIR=/tank/pgx-bench benchmarks/pgx/linux/run-suite.sh scale churn pool cow regress
```

Results land in `benchmarks/pgx/results/<host>-<commit>/`.
[`benchmarks/pgx/linux/README.md`](benchmarks/pgx/linux/README.md) maps
every headline number above to its step, script and raw result.

The regression suite is PostgreSQL's own `src/test/regress`, vendored
unmodified at `crates/postgres-18.6-reference/src/test/regress`. It runs
with stock `pg_regress` (`benchmarks/pgx/regress-baseline.py`), and each
difference is classified as plan-only or semantic.

## Documentation

| Document | What's in it |
|---|---|
| [`v0.1-baseline.md`](docs/pgx/v0.1-baseline.md) | the frozen v0.1 numbers and the build each came from |
| [`v0.1-readiness.md`](docs/pgx/v0.1-readiness.md) | what is ready, known limitations, approved use |
| [`linux-results.md`](docs/pgx/linux-results.md) | the full benchmark report |
| [`compatibility-gate.md`](docs/pgx/compatibility-gate.md) | how to decide whether a database can run on PGX |
| [`pgrun-integration.md`](docs/pgx/pgrun-integration.md) | the host-agent API PGRun builds against |
| [`lifecycle-memory.md`](docs/pgx/lifecycle-memory.md), [`linux-churn.md`](docs/pgx/linux-churn.md) | memory under long churn, and what was fixed |
| [`zfs-cow.md`](docs/pgx/zfs-cow.md) | copy-on-write branch cost and the recordsize trade-off |
| [`linux-limits.md`](docs/pgx/linux-limits.md) | memory limits and the cgroup backstop |

## License

PGX is licensed under [AGPL-3.0](LICENSE). Portions derived from PostgreSQL
remain under the PostgreSQL License; see [`NOTICE`](NOTICE).

---

PGX is a fork of [pgrust](https://github.com/malisper/pgrust), a Rust
re-implementation of Postgres.
