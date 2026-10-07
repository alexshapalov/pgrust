# Database churn on Linux

`churn.py`: one PGX runtime, a sealed 50-table template; each cycle mints
100 databases (mint-on-connect), runs a small workload in each (insert,
update, join, CREATE TABLE), disconnects, and waits for the janitor to drop
them all. 100 cycles = 10,000 database lifecycles. Footprint = PSS after
each cycle with no ephemeral databases left. Host: `linux-host.md`.

## Result

| Run | Build | Start | Cycle 1 | 10 | 25 | 50 | 75 | 100 |
|---|---|---|---|---|---|---|---|---|
| A | before fixes `1781903578` | 84.5 | 126.8 | 159.5 | 177.1 | 203.3 | 227.1 | **261.5 MB** |
| B | fixed `999a78baff` | 84.1 | 123.9 | 148.1 | 148.8 | 155.0 | 156.4 | **154.9 MB** |
| C | fixed, autovacuum idle-skip on | 83.8 | 124.2 | 148.5 | 151.5 | 156.7 | 159.0 | **159.8 MB** |

Growth per 100-database cycle (least-squares slope per quarter):

| Run | 1–25 | 26–50 | 51–75 | 76–100 |
|---|---|---|---|---|
| A | 1.56 | 1.07 | 1.04 | 1.27 MB |
| B | 0.77 | 0.15 | 0.11 | **0.006 MB** |
| C | 0.73 | 0.21 | 0.10 | **0.007 MB** |

- **Memory plateaus.** Two independent runs on the fixed build flatten to
  ~0.006–0.007 MB per 100 lifecycles over the last quarter: ~60–70 bytes per
  lifecycle, within measurement noise. Before the fixes growth was linear at
  ~11 KB per lifecycle (~1 GB per 90,000 lifecycles).
- Most of the early rise happens in the first ten cycles, consistent with
  shared, bounded state filling up (not itemized).
- Threads stay at 9 and open files between 37 and 45 in every run; the data
  directory returns to 73 MB after every cycle.
- Each cycle (100 mints + workloads + reap) takes ~26–28 s.

What was retained and how it was fixed: `lifecycle-memory.md`.

## Stage by stage (`linux/lifecycle.py`, fixed build)

Explicit `CREATE DATABASE` → connect → workload → disconnect → `DROP
DATABASE`, 100 databases per batch, PSS change per database:

| Stage | Per database |
|---|---|
| CREATE DATABASE | ≈ 0 |
| First connection (session + caches) | +1.77 MB |
| Workload | +0.67 MB |
| Disconnect | −1.58 MB |
| DROP DATABASE | −0.78 MB |

Opening and closing 100 connections to one existing database, ten batches:
no growth (the per-connection leaks are gone). PSS at this resolution
swings by tens of MB between batches as the allocator returns memory, so
the per-lifecycle figure comes from the 10,000-lifecycle runs above, not
from these deltas.

## Raw data

`benchmarks/pgx/results/vps-d2a3c460-1781903578/churn/` (A),
`vps-d2a3c460-999a78baff/churn/` (B), `.../churn-skipidle/` (C),
`.../lifecycle/`.
