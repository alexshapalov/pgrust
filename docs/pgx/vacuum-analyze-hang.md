# The VACUUM ANALYZE whole-server hang

Root cause found and fixed. One database's `VACUUM ANALYZE` could block
every new connection to every database in the server, permanently.

Evidence: `benchmarks/pgx/results/vacuum-hang-823d8d0738/`.
Reproducer: `benchmarks/pgx/repro-vacuum-hang.py`.

## Result

| Build | Settings | Tries | Hangs |
|---|---|---|---|
| Before fix | default | 40 | 12 |
| Before fix | `pgrust.runtime_vacuum_pool = off` | 40 | 0 |
| Before fix | `pgrust.runtime = off` | 40 | 0 |
| After fix | default | 100 | 0 |

Each try: fresh data directory, 50 tables with indexes and 200 rows each,
then a database-wide `VACUUM ANALYZE` with a 10 s timeout (it normally takes
0.35 s). The regression suite is unchanged by the fix: 219 of 231, the same
12 plan-only differences as the baseline.

The earlier estimate of "4 in about 43" came from the stripped build; the
symbolized build on a busier machine hit it 30% of the time.

## Blast radius

Measured while the server was hung (`before-fix-hang/report.json`):

| Probe | Result |
|---|---|
| Existing connection, same database, `SELECT 1` | answered, 1.4 ms |
| Existing connection, other database, `SELECT 1` | answered, 0.5 ms |
| New connection, same database | blocked |
| New connection, other database | blocked |
| New connection, existing ephemeral database | blocked |
| Mint a new ephemeral database | blocked |

Also blocked: the checkpointer (so the janitor, which waits on checkpoints)
and every runtime worker thread. CPU use while hung was 0.3% of a core: a
true deadlock, not a spin.

Classification: **lock-manager-global**. It is a deadlock between two
server-wide lightweight locks. Sessions already connected keep answering
simple queries, but nothing new can connect to any database, and the server
never recovers on its own. `pg_locks` shows nothing, because lightweight
locks are not in it; `pg_stat_activity` shows the waits
(`LWLock / RelCacheInit` and `LWLock / BufferContent`).

## Root cause

Two threads take the same two locks in opposite order.

**Thread A — the backend running `VACUUM ANALYZE`:**

```
commands_vacuum::vac_update_relstats
genam::systable_inplace_update_finish
heapam::inplace::heap_inplace_update_and_unlock   holds the pg_class page lock
relcache::initfile::RelationCacheInitFilePreInvalidate
lwlock::LWLockAcquire(RelCacheInitLock)           waits
```

**Thread B — a runtime worker connecting to the same database:**

```
parallel::standing::warm_connect
postinit::InitPostgres
relcache::initfile::RelationCacheInitializePhase3
relcache::initfile::write_relcache_init_file      holds RelCacheInitLock
inval::local::AcceptInvalidationMessages
relcache::invalidate::RelationRebuildRelation
relcache_build::pg_class::scan_pg_relation
bufmgr::ops::LockBuffer(pg_class page)            waits
```

Everything else then queues behind one of the two locks: new backends in
`load_relcache_init_file` or scanning `pg_class`, the other runtime workers
in `write_relcache_init_file`, the checkpointer in `BufferSync`.

Both lock orders also exist in PostgreSQL's C code. PostgreSQL does not
deadlock there because thread B's step never reads the catalog: when a
backend writes its relation-cache init file, none of its cached relations
is in use, so an incoming invalidation only marks the entry stale.

PgRust broke that assumption by accident. `write_relcache_init_file`
(`crates/backend/utils/cache/relcache/src/initfile.rs`) took a snapshot of
every cache entry as reference-counted clones in order to serialize them,
and that snapshot was still alive when it rechecked invalidations under the
lock. PgRust decides "is this relation in use?" by counting references
(`store::refcount_zero`), so every entry looked in use, and each incoming
invalidation triggered an immediate rebuild — a `pg_class` scan while
holding `RelCacheInitLock`.

Why `VACUUM ANALYZE` hit it so often: with `pgrust.runtime_vacuum_pool` on
(the default), the vacuum engages the runtime worker pool, and every worker
that has not yet connected to that database does a full connection startup
at that moment (`warm_connect`), each writing the init file, exactly while
the vacuum is doing in-place `pg_class` updates. On this 10-core machine
that is up to 12 threads racing the vacuum.

## Fix

Drop the snapshot before taking the lock (one statement plus a comment in
`write_relcache_init_file`). The recheck then behaves as in C: invalidated
entries are marked stale and rebuilt on next use, with no catalog access
under `RelCacheInitLock`.

## What this does and does not establish

- The fix removes this deadlock. 100 clean runs against a 30% failure rate
  is strong evidence, not proof of absence.
- Turning the runtime off (as the PGX profile does) avoided the trigger but
  not the bug: before the fix, any ordinary client connecting while another
  session ran `VACUUM`/`ANALYZE` could have hit the same inversion, just
  rarely.
- The class of bug is the lasting concern. PgRust replaces PostgreSQL's
  explicit reference counts with `Rc` clone counts, so any code that holds a
  temporary clone across an invalidation point changes behaviour. Other such
  sites were not audited.
- A lightweight-lock deadlock has no detector and no timeout. Nothing in the
  server notices; an external health check that opens a *new* connection is
  the only way to see it.
- Not yet reported upstream (`malisper/pgrust`).

## Reproducing

```bash
# symbolized optimized build
CARGO_PROFILE_RELEASE_STRIP=false CARGO_PROFILE_RELEASE_DEBUG=line-tables-only \
  cargo build --release --locked --bin postgres --target-dir target-sym

# loop until the first hang; writes blast radius, pg_stat_activity,
# thread backtraces (macOS `sample`) and the server log
python3 benchmarks/pgx/repro-vacuum-hang.py --out /tmp/hang \
  --binary target-sym/release/postgres

# measure a rate
python3 benchmarks/pgx/repro-vacuum-hang.py --out /tmp/rate --tries 100 --max-hangs 0 --timeout 10
```
