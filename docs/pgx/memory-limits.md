# Enforced memory limits

Three settings that refuse memory instead of only reporting it. All default
to 0 (off) and can be changed with a reload.

| Setting | Limits |
|---|---|
| `pgrust.session_memory_limit` | what one client session holds |
| `pgrust.database_memory_limit` | what all sessions of one database hold together |
| `pgrust.runtime_memory_limit` | what the whole server holds; applied only to sessions already above 16 MB |

Code: `crates/_support/mcx/src/lib.rs`, module `limits`. Commit `4bf2fc8665`.

## What happens at a limit

The allocation that would cross the limit is refused. The statement fails
with PostgreSQL's ordinary out-of-memory error, and a hint names the limit:

```
ERROR:  out of memory
DETAIL:  Failed on request of size 20 in memory context "tuplestore tuples".
HINT:  The session reached pgrust.session_memory_limit.
```

The transaction aborts and its memory is released. The session stays
connected and can run the next statement. No session is cancelled or
terminated, and no other session is affected.

Measured on the query that previously ran to completion at 1.25 GB
(`array_agg` over 30 million rows, `work_mem = 8GB`):

| Limit set | Outcome | Time to fail | Peak server footprint |
|---|---|---|---|
| none | completed | — | 1,245 MB |
| session 256 MB | out-of-memory error | 0.2 s | 347 MB |
| database 256 MB | out-of-memory error | 0.3 s | 341 MB |
| runtime 300 MB | out-of-memory error | 0.2 s | 393 MB |

In each case the same session then ran `SELECT 1`, a second session was
unaffected, the server stayed up and the log contains no PANIC. The
regression suite gives the same 219 of 231 with a 512 MB session limit as
without one.

## How it works

PgRust allocates query memory through memory contexts, which take memory
from the system in blocks. Every block is already counted in a process-wide
total. The limits add a per-thread count and a per-database count at the
same points, and a check before each block is taken.

- **Session**: the thread's own count.
- **Database**: threads join a group when they connect to a database
  (`InitPostgres`); the group's count is the sum. Parallel workers and
  autovacuum workers are counted in their database's group.
- **Runtime**: the process-wide total. Once it is over the limit, sessions
  holding more than 16 MB cannot grow; smaller ones carry on. This keeps a
  full runtime usable for the databases that did not fill it.

Safeguards:

- Only client sessions are ever refused. The checkpointer, WAL writer,
  autovacuum, the janitor and other internal threads are counted but never
  refused.
- No refusal inside a critical section, where an allocation failure would be
  a PANIC that takes the whole server down.
- Requests under 64 KB are allowed until the limit is exceeded by 25%, so
  that error reporting and transaction abort, which allocate a little, can
  run for a session that is already at its limit.

Cost: one thread-local add per block allocated or freed, and one load of
three atomics per block when any limit is set. Nothing per row or per
allocation within a block.

The watchdog thread copies the settings to the allocator once a second, so a
changed limit takes effect within about a second of the reload.

## What it does not cover

- **Memory outside memory contexts.** Plain Rust heap allocations, thread
  stacks, and allocator overhead are not charged to any session. That is why
  the peak in the table is 340–390 MB with a 256–300 MB limit: the limit
  bounds query memory, and the server's own base sits on top. The shared
  catalog cache, statistics tables and buffer pool are in this category.
- **The runtime worker pool.** With `pgrust.runtime = on`, engine memory
  allocated on worker threads is counted in the process-wide total (so the
  runtime limit sees it) but not in any session or database. The PGX profile
  turns the runtime off.
- **A hard guarantee against the operating system's out-of-memory killer.**
  (Linux, measured: with `runtime_memory_limit=700` inside a 1 GiB cgroup
  the query fails with 53200 at a 674 MB cgroup peak; with no limit the
  kernel kills the whole runtime. See `linux-results.md`.)
  These are budgets on the largest and most variable part of memory, not a
  cgroup. A cgroup limit around the runtime is still the backstop, and
  `pgrust.runtime_memory_limit` should be set below it.
- **Fairness.** The runtime limit refuses whichever large session asks next,
  not necessarily the largest.
- **Free-and-move accounting.** If a context is freed by a different thread
  from the one that grew it, the per-thread figures drift (they saturate at
  zero rather than going negative). Not observed in these tests; not ruled
  out.

## Suggested starting values for a PGX runtime

Not measured as a policy, only consistent with the results so far: a session
limit of a few hundred MB, a database limit of 2–4 times that, and a runtime
limit of 70–80% of the runtime's cgroup limit.
