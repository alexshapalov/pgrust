# Autovacuum and ANALYZE for ephemeral databases (Linux)

Two questions:

1. What does autovacuum cost a runtime holding many idle databases?
2. Does a branch need fresh statistics after it grows?

Host: `linux-host.md`. Density and idle-CPU detail: `linux-density.md`.

## 1. Autovacuum's idle cost

The stock launcher divides `autovacuum_naptime` by the number of databases
and wakes for one database each time. At 1000 databases with the default
60 s that is a wake every 60 ms, each one starting a worker that connects
to the database and checks its tables. On the pass 4 build this was nearly
all of the runtime's idle CPU: 6.7 % of a core at 1000 idle databases.
With autovacuum off it was 0.23 %.

Changes (all GUC-gated or behaviour-preserving):

| Commit | Change |
|---|---|
| `28c83164ab` | `pgrust.autovacuum_skip_idle_databases` (default off): the launcher skips a database whose tuple counters (inserts + updates + deletes, from pgstat) have not changed since its last visit |
| `9dca02b2c1` | the launcher's database list is cached between wakes under skip-idle instead of re-read from `pg_database` |
| `5a3090e793` | a database never written since it was minted counts as visited (a fresh clone of a vacuumed template needs no visit) |
| `23ede965a7` | the per-wake schedule lookup is a HashMap instead of a linear search per database (O(N²) per cycle) |

Result at 1000 idle databases (builds `999a78baff` → `0131cc9b12`):

| Configuration | Idle CPU |
|---|---|
| default | 6.66 % → 5.40 % |
| skip-idle, naptime 60 | 2.16 % → 1.21 % |
| skip-idle, naptime 300 | **0.33 %** |
| autovacuum off | 0.22 % |

Skip-idle plus a 300 s naptime keeps autovacuum for databases that change
at almost the cost of turning it off. Databases written after minting are
visited as usual; a written-then-abandoned database is visited once more,
then skipped. Under these settings a newly written database waits up to
5 minutes for its first visit instead of 1 minute.

Coverage: the regression suite runs with the setting at its default
(off), so it shows the default path is unchanged, not skip-idle itself.
Skip-idle is exercised by the scale runs and by `scale.py --touch`, where
every database is written once: idle CPU is 1.45 % while the first visits
complete, against 0.33 % untouched.

**Starvation bug, found by the check below and fixed in `6fe0b1ad65`.**
A skipped database never gets a worker, so its `last_autovac_time` stayed
0. The launcher chooses the candidate with the oldest `last_autovac_time`.
When its schedule spans more than `autovacuum_naptime`, idle databases
were always candidates and always won, and written databases later in
`pg_database` order were never vacuumed or analyzed. The schedule spans
more than naptime once the per-database spacing hits its floor:
from 600 databases at naptime 60, 3,000 at naptime 300 (spacing = naptime ÷ databases, raised to 110 ms once it falls to 100 ms or below). Before the fix,
`skipidle-check.py` with 50 idle + 5 written databases and naptime 5 s
autovacuumed 0 of the 5 written databases in 180 s; the stock launcher
did all 5 in 2 s. The launcher now records when it skipped each database
and uses that time as if a worker had visited.
`benchmarks/pgx/linux/skipidle-check.py` (suite step `skipidle`) checks
that written databases are vacuumed and analyzed, and visited again after
new writes. Pass 8 verifies the fix.

## 2. Does a branch need ANALYZE?

`benchmarks/pgx/linux/analyze-policy.py` (pass 5, build `dedfdf9ca5`):

- **Template:** `orders` has 200k rows, with a skewed `status` column
  ('done' 97 %, 'new' 1 %, 'paid' 1 %, 'shipped' 1 %) and indexes on
  `status` and `account_id`. `accounts` has 20k rows. The template's
  statistics are fresh.
- **Branch:** `orders` grows by 0 / 10 / 25 / 50 / 100 %, and every new row
  has status 'new'. This is the agent-workload shape: new rows land where
  the template was rare.
- **Autovacuum is off**, so the branch keeps the template's statistics
  unless it runs ANALYZE explicitly.

Planner estimate vs actual for `status = 'new'`:

| Growth | Stale statistics | After ANALYZE | Actual |
|---|---|---|---|
| 0 % | 2,033 | 1,920 | 2,000 |
| 10 % | 2,238 | 21,289 | 22,000 |
| 25 % | 2,542 | 52,100 | 52,000 |
| 50 % | 3,050 | 102,520 | 102,000 |
| 100 % | 4,067 | 201,413 | 202,000 |

Execution time, median of 5 (stale → analyzed):

| Growth | count where new | join + group by region | 50 newest for accounts < 500 |
|---|---|---|---|
| 0 % | 1.21 → 1.26 ms | 6.02 → 6.01 ms | 1.03 → 1.11 ms |
| 10 % | 5.57 → 5.21 ms | 14.1 → 12.5 ms | 1.72 → 4.84 ms |
| 25 % | 12.2 → 12.0 ms | 26.4 → 24.6 ms | 3.09 → 2.52 ms |
| 50 % | 20.6 → 21.4 ms | 50.4 → 52.0 ms | 4.34 → 4.51 ms |
| 100 % | 76.1 → 73.9 ms | 109 → 118 ms | 7.54 → 6.28 ms |

- Stale statistics are wrong by up to 50× (4,067 estimated vs 202,000
  actual), yet plans for the count and the join do not change: the same
  aggregate and the same hash join over a sequential scan of `accounts`.
- The LIMIT query does change plan after ANALYZE (from a bitmap AND of both
  indexes to a different access path), and is not consistently faster:
  slower at 10 %, faster at 25 % and 100 %, the same at 50 %.
- At branch scale (≤ 400k rows), stale statistics cost no measurable
  latency on these queries. A branch that grows a table many times over,
  or runs join-order-sensitive queries over several such tables, can still
  get a bad plan. The skip-idle autovacuum above analyzes any database
  that changes; this measurement says a branch does not need to force
  ANALYZE itself.

## Recommendation for the PGX profile

```
pgrust.autovacuum_skip_idle_databases = on
autovacuum_naptime = 300
```

Do not add an explicit ANALYZE to the branch lifecycle. Keep autovacuum
on: it bounds bloat for long-lived branches and refreshes statistics for
the ones that grow.
