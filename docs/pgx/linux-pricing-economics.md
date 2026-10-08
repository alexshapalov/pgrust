# Branch economics on PGX (an estimate, not a quote)

`benchmarks/pgx/linux/economics.py` turns the measured resource use on the
bench host into per-host branch capacity and monthly margin for a few
workload shapes. Measured inputs and assumptions are kept apart, and every
assumption below changes the answer, so the script prints them all.

## Inputs

**Measured on the bench host** (OVH VPS, 4 vCPU, 7.6 GiB, 47 GiB ZFS pool,
$8.50/month; `linux-host.md`):

| Input | Value | Source |
|---|---|---|
| Runtime with no databases | 86 MB | `linux-density.md` |
| Per idle database, touched once | 0.127 MB | 1000 touched databases: 213 MB |
| Extra per active database | ~1.5 MB | 1000 databases with 100 active: 283 MB |
| Active databases a host serves at p99 < 13 ms | 50 | `linux-density.md`, client on the same host |
| New branch, physical disk | ~2 MB | `zfs-cow.md`, 0.4–2.3 MB up to a 1 GB template |
| Bulk-write disk amplification | 1.2× | `zfs-cow.md` |
| Cold mint / warm-pool mint, p50 | 74 ms / 6.2 ms | `linux-mint.md` |
| Cold mint drain rate | ~35–40 per second per host | `linux-mint.md` |

**Assumed** (change these for a real plan):

| Assumption | Value |
|---|---|
| RAM budget for the runtime | 6 GiB of 7.6 (OS and ZFS ARC headroom) |
| Pool kept free | 30 % |
| Share of a branch's life spent running queries | 20 % |
| Data an agent branch writes | 10 MB |
| Templates cached per host | 5 GiB logical |
| Peak-to-average concurrency | 3× |
| Price per branch-hour | $0.012 |
| Price per GiB-month of branch data | $0.28 |
| Control plane (API + metadata DB), all hosts | $50/month |
| Object storage for golden templates | 5 GiB at $0.015/GiB-month |

## Capacity of one host

| Limit | Concurrent branches |
|---|---|
| RAM (6 GiB ÷ (0.127 MB + 20 % × 1.5 MB)) | 14,187 |
| Disk ((70 % of 47 GiB − 5 GiB) ÷ (2 MB + 10 MB × 1.2)) | 2,040 |
| CPU (50 active ÷ 20 % active) | **250** |
| Largest count tested | 1,000 |

**CPU is the binding limit.** Since the density fixes (`linux-density.md`),
memory stopped being the constraint: before them, an idle database cost
0.55 MB, which capped RAM at ~11,000. The other limits move as follows:

| Share of life active | Branches per host |
|---|---|
| 5 % | 1,000 (the tested maximum) |
| 10 % | 500 |
| 20 % | 250 |
| 50 % | 100 |

| Data written per branch | Disk limit |
|---|---|
| 10 MB | 2,040 |
| 50 MB | 460 |
| 200 MB | 118 |

Large writes per branch make disk the limit. Scattered small updates cost
far more than their logical size on a 128 K-record dataset
(`zfs-cow.md`: ~0.45 MB per updated row).

## Scenarios

| Scenario | Branch-hours / month | Average / peak concurrent | Hosts | Revenue / month | Host cost / month | Margin before control plane |
|---|---|---|---|---|---|---|
| 1000 × 5-minute branches/day | 2,500 | 3.5 / 10.4 | 1 | $30.01 | $8.57 | 71 % |
| 1000 × 30-minute branches/day | 15,000 | 20.8 / 62.5 | 1 | $180.08 | $8.57 | 95 % |
| 100 × 1-hour branches/day | 3,000 | 4.2 / 12.5 | 1 | $36.02 | $8.57 | 76 % |
| 10k creates/day, 15-minute life | 75,000 | 104 / 313 | 2 | $900.40 | $17.07 | 98 % |
| 100k creates/day, 15-minute life | 750,000 | 1,042 / 3,125 | 13 | $9,003.99 | $110.58 | 99 % |

- At small volume the $8.50 host and the $50 control plane dominate: the
  first scenario covers its host but not a share of the control plane.
- Mint rate is not a limit at these volumes. 100k creates/day is 1.2 per
  second on average, against ~35–40 cold mints per second per host.
- Not in the cost: control plane ($50/month assumed), bandwidth, support,
  hosts kept spare for failure, and the operator's time. With
  `fsync = off`, templates must be restorable from durable storage after a
  host crash (`linux-limits.md`). The object storage for that is in the
  cost; the restore tooling is not built.

## What this estimate does not tell you

- It assumes agent branches look like the measured workloads: short
  queries, small writes, 20 % active. A branch running analytics all its
  life is a different product.
- Active-database capacity was measured with the load generator on the
  same 4 vCPUs. A dedicated host serves more.
- PostgreSQL comparison: a full-copy branch on the same pool costs the
  template's whole size in disk (20 MB to 10 GB measured) and, as one
  server per branch, ≥ 16.6 MB of RAM idle. COW branches initially use
  hundreds of times less physical storage than full-copy branches. Memory
  per idle branch is ~130× lower (0.127 vs 16.6 MB) only when compared
  with one PostgreSQL server per branch; one PostgreSQL server holding many
  databases was not measured here.
