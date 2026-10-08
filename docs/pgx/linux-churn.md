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
| D | + density fixes `dedfdf9ca5` | 82.1 | 94.5 | 98.1 | 99.4 | 103.8 | 108.6 | **108.8 MB** |
| E | + DML per-row fixes `d48f587da0` | 66.7 | 94.3 | 99.0 | 100.2 | 103.3 | 107.9 | **108.6 MB** |
| F | final `60fca8427f`, 300 cycles | 82.1 | 93.5 | — | — | 101.6 | — | **108.8 MB** |

Growth per 100-database cycle (least-squares slope per quarter):

| Run | 1–25 | 26–50 | 51–75 | 76–100 |
|---|---|---|---|---|
| A | 1.56 | 1.07 | 1.04 | 1.27 MB |
| B | 0.77 | 0.15 | 0.11 | **0.006 MB** |
| C | 0.73 | 0.21 | 0.10 | **0.007 MB** |
| D | 0.11 | 0.15 | 0.18 | **0.018 MB** |
| E | 0.35 | 0.13 | 0.19 | **0.060 MB** |

Run F, 30,000 lifecycles on the final build (engine `6fe0b1ad65`, profile
`696318b79e`: autovacuum skip-idle on, naptime 300):

| Cycles | 1–75 | 76–150 | 151–225 | 226–300 |
|---|---|---|---|---|
| Mean footprint | 101.3 | 109.8 | 116.0 | 122.6 MB |
| Slope per 100-database cycle | 0.159 | 0.065 | 0.087 | **0.093 MB** |

Footprint after 50 / 100 / 150 / 200 / 250 / 300 cycles: 101.6 / 108.8 /
112.9 / 117.2 / 121.7 / 126.1 MB.

- **Builds B and C plateau.** Two independent runs on the fixed build flatten to
  ~0.006–0.007 MB per 100 lifecycles over the last quarter: ~60–70 bytes per
  lifecycle, within measurement noise. Before the fixes growth was linear at
  ~11 KB per lifecycle (~1 GB per 90,000 lifecycles).
- Runs D and E, on the builds with the density fixes (pgstat slots boxed,
  relcache cores interned; `linux-density.md`), end ~46–51 MB lower: 109
  MB after 10,000 lifecycles. Their last-quarter slope (0.018–0.060 MB
  per cycle) looked close to flat.
- **Run F shows the later builds do not plateau.** Over 30,000 lifecycles
  the slope settles at 0.065–0.093 MB per cycle (~0.9 KB per lifecycle)
  after the first quarter and does not decline: 109 MB at 10,000, 126 MB
  at 30,000, ~90 MB more per 100,000. Runs B and C, on the build before
  the density work, were flat at 10,000; so something in `999a78baff..`
  `60fca8427f` retains a little per lifecycle. Not attributed yet. Until
  it is, a long-lived runtime relies on the memory watchdog or a planned
  restart.
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
`.../lifecycle/`, `vps-d2a3c460-dedfdf9ca5/churn/` (D),
`vps-d2a3c460-d48f587da0/churn/` (E),
`vps-d2a3c460-60fca8427f/churn-30k/` (F).
