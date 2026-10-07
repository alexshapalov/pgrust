# Linux benchmark host

Every Linux/ZFS number under `docs/pgx/linux-*.md`, `zfs-cow.md` and
`lifecycle-memory.md` comes from this machine unless a page says otherwise.

| | |
|---|---|
| Provider / plan | OVH VPS, $8.50/month |
| Hostname | `vps-d2a3c460` |
| CPU | 4 vCPU, x86_64 (shared virtual cores) |
| RAM | 7.6 GiB, no swap |
| Kernel / OS | Linux 7.0.0-14-generic, Ubuntu 26.04 |
| THP | `madvise` (system); PgRust turns THP off for its own process since `680fab7c8a` |
| cgroup | v2, memory controller |
| Disk | one 75 GiB virtual disk (`/dev/sda`) |

## Disk layout

The provider image used the whole disk as ext4. It was re-partitioned in
OVH rescue mode (`e2fsck`, `resize2fs` to 24 G, `sfdisk` to shrink
partition 1 to 25 GiB at the same start sector, a new partition 2 for the
rest, `resize2fs` to fill):

| Partition | Size | Use |
|---|---|---|
| sda13 / sda14 / sda15 | 1 GiB / 4 MiB / 106 MiB | /boot, BIOS boot, EFI (untouched) |
| sda1 | 25 GiB | ext4 root: OS, toolchain, checkout, builds |
| sda2 (`pgx-zfs`) | 48.9 GiB | ZFS pool `tank`, no loop devices |

## ZFS

| | |
|---|---|
| Version | 2.4.1 (zfsutils-linux 2.4.1-1ubuntu5.1) |
| Pool | `tank` on `/dev/disk/by-partlabel/pgx-zfs`, ashift 12, 48.5 G |
| Dataset under test | `tank/pgx-bench`, mounted at `/tank/pgx-bench` |
| compression | off (so logical vs physical numbers are exact) |
| recordsize | 128K (default) |
| atime | off |
| block cloning | `feature@block_cloning` enabled, `zfs_bclone_enabled=1` |
| ARC | capped at 1 GiB (`/etc/modprobe.d/pgx-zfs.conf`), so it cannot take the benchmark's memory budget |

Block cloning was checked by hand before any benchmark: a 256 MiB file
copied with `cp --reflink=always` added 660 KiB of pool allocation and
256 MiB to `bcloneused`; the copy was byte-identical.

## Safety rules applied to every run

- All benchmark processes run inside one transient systemd scope
  (`pgx-bench.scope`) with `MemoryMax=6G` and `MemorySwapMax=0`
  (`~/pgx-run.sh`, which wraps `benchmarks/pgx/linux/run-suite.sh`).
- A 5-second monitor writes `host-monitor.csv` next to each run's results:
  available RAM, swap used, load, ARC size, the scope's memory, root free
  space, pool allocation. Across every run so far swap was never used and
  available RAM never fell below 4.4 GB.
- The cgroup backstop test puts each server in its own scope with a 1 GiB
  `memory.max`, so an OOM kill can only reach the server under test.
- Root keeps ≥ 14 GB free; the pool stays far below capacity (benchmarks
  clean up their datasets).

## Caveats

- Four shared vCPUs: CPU-bound results saturate early, and the Python load
  generator runs on the same cores as the server, so throughput figures are
  host ceilings, not runtime ceilings.
- One virtual disk behind the hypervisor: file-creation latency on ZFS is
  ~90 µs per file here (`zfs-cow.md`), which sets the cold-mint floor.
- Production capacity has to be re-measured on dedicated hardware with a
  separate load-generator host.
