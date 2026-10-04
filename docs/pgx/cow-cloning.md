# Copy-on-write cloning for ephemeral databases

A one-line setting turns ephemeral-database creation from a full file copy
into a filesystem clone. No engine change and no new storage layer.

```
file_copy_method = clone
```

Raw data: `benchmarks/pgx/results/cow-06178aafd1/cow.json`.
Benchmark: `benchmarks/pgx/cow.py`. Machine as in `docs/pgx/baseline.md`
(macOS 14.4, APFS). Server settings: `configs/pgx-ephemeral.conf`.

## What was tested

`file_copy_method` is a PostgreSQL 18 setting. PgRust honours it both in
`CREATE DATABASE ... STRATEGY file_copy` (`storage/file/fd/src/copydir.rs`)
and in the janitor's parallel copy used for mint-on-connect
(`postmaster/janitor/src/parcopy.rs`). With `clone` it calls `clonefile` on
macOS and `copy_file_range` on Linux, so the new database's files share
blocks with the template until one side writes.

This is option B (filesystem reflink) from the plan. Option A (ZFS snapshot
and clone) could not be tested on this machine; see "Not tested".

## Results

Template sizes are the measured logical sizes; the nominal 100 MB and 1 GB
targets came out at 74 MB and 674 MB.

### Creating a clone

| Template | Method | Mint to `SELECT 1` (median of 5) | Physical space per clone | First real query |
|---|---|---|---|---|
| 19 MB | copy | 175 ms | 20.1 MB | 1.9 ms |
| 19 MB | clone | 90 ms | 0.3 MB | 3.7 ms |
| 74 MB | copy | 192 ms | 77.0 MB | 1.4 ms |
| 74 MB | clone | 91 ms | 0.2 MB | 5.3 ms |
| 674 MB | copy | 1,005 ms | 674.7 MB | 4.8 ms |
| 674 MB | clone | 85 ms | 0.2 MB | 6.1 ms |

- With `clone`, creation time and disk cost no longer depend on template
  size: 85–91 ms and about 0.2 MB from 19 MB up to 674 MB.
- With `copy`, both scale with the template: one second and 675 MB at 674 MB.
- The remaining ~85 ms is not copying. It is the rest of the database
  creation path (catalog work and checkpoints). The warm pool hides it from
  the client; reducing it is a separate question.

### Writing to a clone

Rows inserted into one clone, cumulative. Physical growth is the change in
used space on the volume, excluding `pg_wal`.

| Template | Method | After ~1 MB | After ~12 MB | After ~124 MB |
|---|---|---|---|---|
| | | logical / physical | logical / physical | logical / physical |
| 19 MB | clone | 1.1 / 1.2 MB | 12.4 / 19.2 MB | 123.9 / 131.8 MB |
| 74 MB | clone | 1.1 / 1.7 MB | 12.4 / 36.4 MB | 123.8 / 148.8 MB |
| 674 MB | clone | 1.1 / 2.5 MB | 12.3 / 29.1 MB | 123.4 / 148.1 MB |
| 674 MB | copy | 1.1 / 1.8 MB | 12.3 / 28.7 MB | 123.4 / 148.0 MB |

Physical growth follows the amount written, not the template size, and is
the same for a cloned database as for a fully copied one. The 10–25 MB by
which physical exceeds logical appears in both methods, so it is not a
copy-on-write cost; it was not attributed (the volume is shared with the
rest of the system, so these figures are good to a few MB at best).

Rewriting every row of one existing 13.4 MB table in a clone of the 674 MB
template added 12.3 MB logically and 26.5 MB physically (12.9 MB for the
copied database): old and new row versions both exist until vacuum, and the
old ones no longer share blocks with the template.

### Deleting a clone

| Template | Method | `DROP DATABASE` (median) | Physical space returned |
|---|---|---|---|
| 19 MB | copy / clone | 43 / 40 ms | 19.1 / 0.1 MB |
| 74 MB | copy / clone | 45 / 29 ms | 74.8 / 0.2 MB |
| 674 MB | copy / clone | 160 / 30 ms | 674.7 / 0.3 MB |

## What it means

The storage objection to the existing ephemeral-database system — a full
copy per database — is removed by configuration on a filesystem that
supports block cloning. Against the example in the plan (a 10 GB template
for 100 agents), the cost becomes one template plus what each agent writes,
instead of a terabyte. That is an extrapolation from 674 MB, not a
measurement.

`file_copy_method = clone` is now part of `configs/pgx-ephemeral.conf`.

## Not tested

- **Linux.** The Linux path is `copy_file_range`, which shares blocks only
  on filesystems with reflink support (XFS with reflink, Btrfs, ZFS 2.2 or
  later with block cloning enabled, bcachefs). On ext4 it silently performs
  a full copy. PGRun's hosts need to be checked before relying on this.
- **ZFS snapshot + clone** as a separate mechanism (option A). On ZFS with
  block cloning, `file_copy_method = clone` may already be enough; if not,
  a dataset-per-database layout would need PgRust to accept a database
  directory that is a mount point, which was not examined.
- **10 GB templates.** There was not enough free disk on this machine for
  the full-copy comparison.
- **Many clones of one template** (hundreds) with `clone`, and their
  behaviour over time as vacuum and hint-bit writes un-share blocks.
- **Template rebuild** while clones of the old version still exist.
