# Lifecycle memory on Linux: what was retained, what was fixed

Question: does a PGX runtime's memory reach a plateau when databases are
created, used and dropped over and over, or does it grow with every
lifecycle? Host: `linux-host.md`. Benchmarks: `churn.py` (100 databases ×
100 cycles = 10,000 lifecycles), `linux/lifecycle.py` (stage by stage), and
`alloc-trace.py` (live allocations by call site, tracker build).

## Result

| | Before (`1781903578`) | After (`999a78baff`) |
|---|---|---|
| Footprint at start | 85 MB | 84 MB |
| After 10,000 lifecycles | 262 MB | 155 MB |
| Growth, cycles 1–25 / 26–50 / 51–75 / 76–100 (MB per 100-database cycle) | 1.56 / 1.07 / 1.04 / 1.27 | 0.77 / 0.15 / 0.11 / **0.006** |
| Shape | linear | rises while shared caches fill, then flat |
| Retained per lifecycle, steady state | ~11 KB | **~0.06 KB** (within noise) |

Memory now plateaus. The rise over the first ~25 cycles is consistent with
shared, bounded state filling up (the process-wide catalog cache is the
largest bounded consumer in the trace); it has not been itemized. After it,
the last 2,500 lifecycles moved the footprint by less than 1 MB.

Later builds (density and DML work, `dedfdf9ca5` onward) start lower but
do not stay flat: a 30,000-lifecycle run on the final build keeps growing
~0.9 KB per lifecycle (`linux-churn.md`, run F). Cause: the shared
catalog cache kept each dropped database's `pg_database` row (DROP
DATABASE purged only per-database keys). Fixed in `727f31e49f`; run G
grows ~0.2 KB per lifecycle.

Tracked live bytes per lifecycle (tracker build, debug-size allocations,
50 databases × 6 measured cycles):

| | Before | After shell reclaim | Notes |
|---|---|---|---|
| Session-root shells | 10.6 KB | 0 | fixed `dda9c1be6f` |
| Relcache entries (backend / bgworker) | 2.7 KB | 0.3 KB | fell with the shell fix |
| Relcache entries (autovacuum worker) | 3.8 KB | 2.7 KB | open, see below |
| Snapshot manager state | 1.2 KB | 1.2 KB | fixed `b736a6d0ec` (after this trace) |
| Shared catalog cache | 1.2 KB | 0.8 KB | bounded: plateaus by design |
| Other | 2.0 KB | 0.9 KB | |
| **Total** | **20.5 KB** | **4.9 KB** | |

## What was retained, and the fixes

### 1. Session-root shells (the main term) — fixed

Every backend thread creates per-thread root memory contexts lazily, at
about 30 call sites (relcache, catcache, snapmgr, portals, locks, …). At
session end their arenas are released, but the shell structs were kept
forever: the code hands out `&'static` references to them, and a freed
shell could leave one dangling. A database lifecycle uses three threads
(client backend, prewarm worker, autovacuum worker), so ~10 KB per lifecycle.

Fix (`dda9c1be6f`): at retirement a shell is filed under its owning thread's
`ThreadId`; when the reaper's `JoinHandle::join` for that thread returns,
its shells are freed. Why that is safe:

- The handles cannot leave their thread. `MemoryContext` is `!Sync`, so a
  `&MemoryContext` or `Mcx` cannot be sent to another thread in safe code.
  All 41 `unsafe impl Send/Sync` in the server were checked: they wrap
  shared-memory pointers, global-heap catalog cache copies (`CatL2Entry`,
  `RelCoreShared`, `AlignedBytes`) and per-query parallel handoffs; none
  carries a context handle. Plain statics cannot hold one (the compiler
  rejects a `!Sync` static), and every `static … MemoryContext` found is
  inside `thread_local!`.
- `join` returns only after the thread has finished, thread-local
  destructors included, so no code that could hold a handle remains. The
  join is also the happens-before edge for the non-atomic accounting cells
  touched by the drop.
- Until the join nothing changed: a late TLS destructor on the dying thread
  still finds the shell retired and poisoned in place, as before.
- The memory-context debug registry (`mcxt_stats` ROOTS) is thread-local
  and holds weak references, so it cannot dangle.
- Threads that are never joined by the reaper keep their shells as before.

Tested by a unit test (shell freed exactly once after join, second reclaim
finds nothing), the full `mcx` suite (122), and the regression suite
(219/231 byte-exact, the same 12 plan-only differences, on `999a78baff`).

### 2. Snapshot manager state — fixed

`SnapMgrState` lives in a `ManuallyDrop` thread-local and was never
dropped; its static snapshots and registered entries are `Rc`s on the
global heap (~1 KB per thread). Fix (`b736a6d0ec`): a State-phase session
cleanup drops it, after portals release their snapshot references and
before the SnapMgr root's arena goes.

### 3. Builtin-backfill bookkeeping — fixed

The janitor remembered every database oid it had backfilled builtin
`pg_proc` rows into and never forgot dropped ones (a few bytes per
lifecycle, unbounded). Fix (`48398cc0b5`): pruned to live databases on each
reap tick.

### 4. Transparent huge pages — fixed (Linux-specific)

Not a leak, but it inflated churn growth on Linux (1.75 MB/cycle vs 1.1 on
macOS): mimalloc marks its arenas `MADV_HUGEPAGE`, and a 2 MB page cannot be
partially returned. Fix (`680fab7c8a`): THP off for the process.

## Still open

- **Relcache entries in autovacuum workers** (~2.7 KB per lifecycle in the
  tracker run, fractional: ~0.4 entries per database). Catalog-index
  relations opened while the worker builds its catalog cache survive its
  exit; no FATAL or "leaking still-referenced relcache entry" warning is
  logged, so it is not the error path. Not yet explained. It is no longer
  visible in the release-build churn (the slope is flat), because the shared
  caches' plateau dominates; it scales with autovacuum worker launches, which
  `pgrust.autovacuum_skip_idle_databases` cuts to databases that were
  written.
- The shared catalog cache keeps entries for shared catalogs across
  databases; bounded by `PGRUST_L2_CACHE_MAX_ENTRIES`, so it plateaus.

## Tools

- `alloc-trace.py` works on Linux now: x86_64 frame-pointer backtraces and
  errno preservation in the tracker (`060f516fcb`; the tracker used to turn
  ENOENT into EAGAIN on a contended lock), `addr2line` symbolization, and a
  density mode (`TRACE_MODE=density`).
- `pgrust_runtime_status()` reports `retired_session_roots_pending` and
  `..._reclaimed`, so a control plane can see shells being freed in
  production.
