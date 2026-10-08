# ZFS copy-on-write for PGX databases (Linux)

Host: `linux-host.md`. Mechanism: `file_copy_method = clone` (PGX profile)
makes `CREATE DATABASE … STRATEGY file_copy` clone every file of the
template with `FICLONE`; on ZFS 2.4 with `zfs_bclone_enabled=1` that shares
the template's blocks (block cloning) instead of copying them.

Measured physical cost = the pool's `allocated` after a forced txg sync and
after deferred frees have drained (`benchmarks/pgx/cow.py`, fixed in
`33035c540d`; an earlier version read the pool mid-free and reported
negative costs). `bcloneused`/`bclonesaved` are recorded alongside.

## Fresh branch cost

Second pass, `vps-d2a3c460-1781903578/cow/`, 5 clones per size:

| Template (logical) | Clone mint p50 | Copy mint p50 | Physical per clone | Physical per copy | Block-cloned per clone | First real query |
|---|---|---|---|---|---|---|
| 19 MB | 71 ms | 87 ms | 0.38 MB | 20 MB | 19.7 MB | 2.4 ms |
| 74 MB | 74 ms | 171 ms | 1.94 MB | 89 MB | 86.7 MB | 3.5 ms |
| 339 MB | 95 ms | 465 ms | 2.01 MB | 350 MB | 348 MB | 6.9 ms |
| 674 MB | 113 ms | 933 ms | 1.98 MB | 680 MB | 678 MB | 9.8 ms |

- A fresh branch costs ~2 MB of new pool space whatever the template's
  size: a 35× larger template costs the same 2 MB. It is not template
  data (the block-cloned figure accounts for all of that); what it is made
  of — files written by the new database's first sessions, ZFS metadata —
  has not been broken down.
- Clone time grows only 1.6× across a 35× size range; copy time grows with
  size (87 → 933 ms).
- (The harness's targets were 15/100/500/1000 MB; the schema builds to the
  logical sizes shown.)

## Growth after writes (one clone of each template)

| Write into the clone | Logical growth | Physical growth |
|---|---|---|
| +1 MB inserted | 1.1 MB | 1.5–2.5 MB |
| +10 MB inserted | 12.3 MB | 20–30 MB |
| +100 MB inserted | 123 MB | 132–148 MB |
| UPDATE every row of one table (13 MB table) | 12.3 MB | 26.8 MB |
| `DROP DATABASE` of a clone | | returns 0.4–2 MB in 33–69 ms |

- Bulk inserts cost ~1.1–1.2× their logical size in pool space.
- Small writes cost more: ZFS writes whole 128 KB records, and a few
  scattered pages dirty whole records of cloned files (copy-on-write at
  record grain); measured below. Compression would reduce all of these
  and was deliberately left off for exact numbers.

## Larger templates and agent-style writes (passes 4 and 5)

`cow.py` with 50 tables per template (10 for 10 GB), 3 clones each, at the
dataset's default `recordsize = 128K`. Agent writes run in a fresh clone:
1000 single-row UPDATEs at random rows across all tables; one
`CREATE INDEX`; one `ALTER TABLE … ALTER COLUMN … TYPE` that rewrites a
table. Results: `vps-d2a3c460-83fbf8d06e/cow-big/`,
`vps-d2a3c460-dedfdf9ca5/cow-10g/`.

| Template (logical) | Build | Clone mint p50 | Physical per clone | First real query | 1000 scattered updates | CREATE INDEX | Rewriting ALTER |
|---|---|---|---|---|---|---|---|
| 1.0 GB | 196 s | 135 ms | 2.3 MB | 13 ms | +452 MB | +3.8 MB | +23 MB |
| 2.0 GB | 382 s | 198 ms | 3.7 MB | 22 ms | +420 MB | +5.7 MB | +43 MB |
| 5.2 GB | 991 s | 417 ms | 7.0 MB | 53 ms | +505 MB | +12 MB | +103 MB |
| 10.1 GB (10 tables) | 1,873 s | 674 ms | 13.6 MB | 921 ms | +496 MB | +113 MB | +1,058 MB |

- A fresh branch of a 10 GB template costs 13.6 MB of new pool space and
  mints in 0.67 s; a full copy would cost the full 10 GB.
- Clone time grows with template size (it is per-file and per-block-pointer
  work): 135 ms at 1 GB → 674 ms at 10 GB.
- The 10 GB template has 1 GB tables, so its first query (a full scan) and
  its index/rewrite costs are those of a 1 GB table, not a property of
  cloning: a rewrite writes a new copy of the table.
- **Scattered small writes are the expensive case**: 1000 random
  single-row updates dirty ~1000 distinct 128 KB records and cost
  420–505 MB (≈ 0.45 MB per updated row), independent of template size.

### recordsize

The same 1 GB template on datasets with smaller records
(`vps-d2a3c460-dedfdf9ca5/cow-recordsize-{16K,8K}/`):

| recordsize | Clone mint p50 | Physical per clone | 1000 scattered updates | CREATE INDEX | Rewriting ALTER | +1 / +10 / +100 MB inserted |
|---|---|---|---|---|---|---|
| 128K | 135 ms | 2.3 MB | +452 MB | +3.8 MB | +23 MB | +2.5 / +14 / +133 MB |
| 16K | 369 ms | 11.1 MB | +65 MB | +2.5 MB | +21 MB | noisy (see below) |
| 8K | 893 ms | 20.4 MB | +33 MB | +2.4 MB | +21 MB | +1.1 / +12.5 / +133 MB |

- Smaller records cut scattered-write amplification 7× (16K) and 14× (8K)
  — at 8K, PostgreSQL's page size, one dirty page costs one record.
- They make every clone slower and larger: a clone references every
  block of the template, and 8K records mean 16× more block pointers than
  128K (mint 135 → 893 ms, 2.3 → 20.4 MB).
- The 16K insert series read +140 / +313 / −9 MB — the pool figure moved
  with frees from the preceding run still draining; the scattered,
  index and rewrite figures in the same run are consistent with the 8K
  run. Not re-run.
- Choice depends on the branch's life: short agent branches that touch
  few rows and are dropped favour 128K (fast, small clones; the write
  amplification is bounded by what is touched); long-lived branches with
  many scattered updates favour 16K. The PGX bench keeps 128K. A
  per-template dataset (recordsize set at template creation) would allow
  both; not built.

## Why a cold mint costs ~60 ms on this host

Mint stage timing (`PGRUST_MINT_TIMING=1`, `mint-breakdown.py`, 30 cold
mints of the 50-table template, ~600 files):

| Stage | p50 |
|---|---|
| Pre-checkpoint | 0.9 ms |
| Directory clone (~600 files) | 57.3 ms |
| Post-checkpoint | 0.8 ms |
| Rest of the mint transaction | 0.4 ms |
| Connection + first-session init of the new database | 15.4 ms |
| **Client: connect to new name → SELECT 1** | **74 ms** |

The directory clone is 96% of the janitor's work, and it is per-file, not
per-byte (`benchmarks/pgx/linux/zfs-clone-scaling.py` and two variants run
by hand):

| 600 files cloned into one directory | Time |
|---|---|
| 1 thread | 62–66 ms |
| 2 / 4 / 8 threads | 73–78 / 70–79 / 80–85 ms |
| Small files created + copied instead of cloned | no faster |
| Plain read/write copy of all | 140–165 ms |

| Whole database directories cloned in parallel | Per database |
|---|---|
| 1 at a time | 67–78 ms |
| 2 | 47–50 ms |
| 4 | 54–59 ms |
| 8 | 66–68 ms |

ZFS serializes these metadata operations: about 90 µs per file created on
this host, ~15–20 cold mints per second in total, whatever the parallelism.
Parallelizing the per-mint clone (an obvious idea) would not help here and
was not built. What does help is not creating files on the request path:
a warm-pool handout is a catalog rename and costs 6 ms end to end.

On faster storage the per-file cost should fall; the number to re-measure on
production hardware is "files created per second on the pool".
