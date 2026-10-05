# Memory retained under database churn: causes and fixes

Follow-up to the churn finding in `shared-runtime-findings.md`. Three causes
were fixed; what remains is identified and bounded or small.

Raw data: `benchmarks/pgx/results/churn-*/`, `results/alloc-trace/`.
Tools: `benchmarks/pgx/churn.py`, `benchmarks/pgx/alloc-trace.py`.

## Result

Same test each time: cycles of 100 databases minted, used and dropped; PGX
profile; footprint with no databases left.

| Build | Start | Cycle 1 | 10 | 20 | 40 | 60 | Growth per cycle (10→60) | Per database |
|---|---|---|---|---|---|---|---|---|
| Before (`06178aafd1`) | 55 MB | 142 | 643 | 682 | 748 | 813 | 3.4 MB | 34 KB |
| + cache purge (`d2076729c5`) | 54 MB | 100 | 157 | 193 | 268 | 338 | 3.6 MB | 36 KB |
| + two leak fixes (`4c8ef2636b`) | 56 MB | 106 | 131 | 143 | 171 | 186 | 1.1 MB | 11 KB |

After 6,000 databases the runtime is at 186 MB instead of 813 MB. Growth
has not stopped: about 11 KB per database lifecycle remains.

The regression suite is unchanged by all three fixes (219 of 231, the same
12 plan-only differences).

## Cause 1: shared catalog cache not purged on DROP DATABASE (fixed)

PgRust's process-wide catalog cache keys entries by database but was never
told a database had been dropped. Entries stayed until a global cap of
262,144 entries evicted arbitrary ones, which in this test was near 650 MB.

Fix: `dropdb` calls `l2cache::purge_database`, next to the buffer-pool drop.
Removing entries is always safe; a later lookup rebuilds from the catalog.

Effect: the 57 MB per cycle of fast growth is gone (cycle 10: 643 → 157 MB).

## How the rest was traced

PgRust has an allocation tracker for debug builds (`PGRUST_ALLOC_TRACK`): it
records a backtrace for every live allocation and dumps the live set on
SIGWINCH. Built with optimizations plus debug assertions, a dump was taken
after 3 warm-up cycles and again after 6 more, and the difference summed by
call site (`alloc-trace.py`). The tracker grouped by full thread name, which
split one call site into a row per short-lived thread and hid the pattern;
it now groups by thread kind and lists every row.

Tracked surviving bytes per database lifecycle: 118 KB before, 21 KB after
the two fixes below. (These are allocation sizes in a debug build, so they
run higher than the footprint figures above.)

## Cause 2: statistics entries of dropped databases (fixed)

`pgstat::shmem::ensure_entry_for_pending` was the largest single site: one
hash map that only grew. Dropping a database removes its tables' statistics
entries only if the database's own statistics entry exists in the shared
map. That mirrors PostgreSQL, where it always exists by then. In PgRust the
table entries are created at first use and can exist without it, so every
dropped database left its ~150 entries behind.

Fix: drop the contents whenever a database's statistics are dropped
(`pgstat/src/shmem.rs`, `drop_entry`).

## Cause 3: a leak on every connection (fixed)

`catcache::init::catalog_cache_initialize_cache` leaked a 96-byte tuple
descriptor header per catalog cache, per backend thread, as a global-heap
`Box`. The comment called it "C's never-freed CacheMemoryContext copy". In
PostgreSQL that memory dies with the backend process; here the backend is a
thread, and the box outlives it. About 41 headers, 4 KB, per connection.

This one is not about database churn at all: any workload with many
short-lived connections leaks it. One million connections is about 4 GB.

Fix: allocate the header in `CacheMemoryContext`, which is released when the
thread ends.

## What remains: about 11 KB per database lifecycle

From the trace on the fixed build (21 KB tracked per database, all of it
attributed):

| Call site | Per database | Kind |
|---|---|---|
| `mcx::session_root` / `session_root_mut` and the context nodes created under them | 14.5 KB, 65 blocks | **leak per thread, by design** |
| Shared catalog cache entries for shared catalogs (`l2cache::insert`, `relcache::l2core`, `catcache::l2`) | 6 KB | bounded cache |
| `snapmgr::init_state` and snapshot copies | 1.2 KB | leak per thread |

**Session roots.** Each backend thread creates per-thread root memory
contexts on first use, at about 30 call sites. At thread end the root's
memory is released but its 256-byte shell is deliberately kept: the code
hands out a `&'static` reference to it and retires it in place "so the
reference stays valid (poisoned) forever" (`mcx/src/lib.rs`,
`session_root_mut`). In a process-per-connection server that is free. In a
thread-per-connection server it is a few KB leaked per thread, and a
database lifecycle here uses about four threads (the client session, the
prewarm worker, autovacuum, the janitor's helpers).

This was **not fixed**. Freeing the shells safely needs a point after which
nothing on the dying thread can touch them, and thread-local destructors run
in an order Rust does not specify. A wrong guess is a use-after-free in a
process shared by every database, which is worse than the leak. It needs a
decision by people who own those invariants; the practical options are to
free shells from the thread that reaps the exited backend, or to pool and
reuse shells across threads.

**Shared-catalog cache entries.** Lookups in shared catalogs (`pg_database`
and friends) for each new database are cached under "no database" and so
are not purged with it. They are bounded by the same global cap, so this
part plateaus, at a level that depends on the cap. Lowering the cap
(`PGRUST_L2_CACHE_MAX_ENTRIES`) lowers the plateau.

Turning autovacuum off does not change the residual slope (26 cycles:
about 1.4 MB per cycle).

## Operating guidance until the rest is fixed

At 11 KB per database lifecycle, a runtime gains about 1 GB per 90,000
databases created and dropped. Restarting a runtime on a schedule, or after
a set number of mints, bounds it; the multi-runtime layout already assumed
restarts are cheap.

## Not established

- Whether the session-root leak per *connection* (as opposed to per database
  lifecycle) matches the per-thread estimate; no connection-only churn test
  was run.
- Linux behaviour. The tracker and the footprint metric used here are
  macOS-specific in practice; the fixes are platform-independent.
