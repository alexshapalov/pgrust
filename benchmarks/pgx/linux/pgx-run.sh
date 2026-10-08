#!/usr/bin/env bash
# PGX Linux suite driver: runs run-suite.sh steps inside a 6 GB / no-swap
# cgroup scope with a 5 s host monitor, then the cgroup backstop test
# (limits.py --mode cgroup) unless SKIP_CGROUP=1. Written for the bench host
# in linux-host.md (checkout ~/pgrust, ZFS dataset tank/pgx-bench mounted at
# /tank/pgx-bench, passwordless sudo); copy it to the host home and run it
# from there. PGX_BENCH_HOST overrides the host-name guard.
#   bash pgx-run.sh [step ...]     (default order follows the operator's priorities)
set -uo pipefail
[ "$(hostname)" = "${PGX_BENCH_HOST:-vps-d2a3c460}" ] || { echo "wrong host (set PGX_BENCH_HOST)"; exit 1; }
REPO=$HOME/pgrust; cd "$REPO"
[ -x target/release/postgres ] || { echo "binary not built"; exit 1; }
OUT="$REPO/benchmarks/pgx/results/$(hostname -s)-$(git rev-parse --short=10 HEAD)"; mkdir -p "$OUT"
STEPS=("$@"); [ ${#STEPS[@]} -eq 0 ] && STEPS=(memory baseline cow scale churn limits pool ephemeral cpunoisy noisy multiruntime regress)
UNIT=pgx-bench
# host facts
{ date -u; uname -a; nproc; free -b; df -B1 /; lsblk -o NAME,SIZE,TYPE,FSTYPE,PARTLABEL,MOUNTPOINTS; losetup -a;
  sudo zpool status -P tank; sudo zpool get all tank | grep -E "ashift|block_cloning|size|alloc"; sudo zfs get compression,recordsize,atime,primarycache tank tank/pgx-bench;
  cat /sys/module/zfs/parameters/{zfs_arc_max,zfs_bclone_enabled}; cat /sys/kernel/mm/transparent_hugepage/enabled; } > "$OUT/host-facts.txt" 2>&1
# monitor: epoch,mem_avail_kb,swap_used_kb,load1,arc_bytes,cgroup_bytes,root_free_bytes,tank_alloc_bytes
( echo "epoch,mem_avail_kb,swap_used_kb,load1,arc_bytes,cgroup_bytes,root_free_bytes,tank_alloc_bytes"
  while :; do
    m=$(awk '/MemAvailable/{a=$2}/SwapTotal/{t=$2}/SwapFree/{f=$2}END{print a","t-f}' /proc/meminfo)
    c=$(cat /sys/fs/cgroup/system.slice/$UNIT.scope/memory.current 2>/dev/null || echo "")
    echo "$(date +%s),$m,$(cut -d' ' -f1 /proc/loadavg),$(awk '$1=="size"{print $3}' /proc/spl/kstat/zfs/arcstats),$c,$(df -B1 --output=avail / | tail -1 | tr -d ' '),$(zpool get -Hpo value allocated tank)"
    sleep 5
  done ) >> "$OUT/host-monitor.csv" &
MON=$!; trap 'kill $MON 2>/dev/null' EXIT
sudo systemd-run --scope --unit=$UNIT --uid="$(id -u)" --gid="$(id -g)" \
  -p MemoryMax=6G -p MemorySwapMax=0 --setenv=HOME="$HOME" \
  env HOME="$HOME" PATH="$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin" \
      PGXBENCH_WORKDIR=/tank/pgx-bench \
      PGX_COW_SIZES=15,100,500,1000 PGX_SCALE_COUNTS=0,100,300,500,1000 PGX_CHURN_CYCLES=100 PGX_SCALE_ACTIVE=5,20,50,100 PGX_POOL_SIZES=0,8,32 PGX_BURSTS=1,10,50,100,250,500 PGX_COW_BIG=1500,3000,7500,15000 PGX_NAPTIMES="60 300" PGX_QB_ENGINES=postgres-nodurable,pgx,pgx-sb128,pgx-runtime \
  bash benchmarks/pgx/linux/run-suite.sh "${STEPS[@]}"
echo "suite exit=$?"
if [ -z "${SKIP_CGROUP:-}" ]; then
  echo "=== cgroup backstop  $(date -u +%H:%M:%S)" | tee -a "$OUT/suite.log"
  env PGXBENCH_WORKDIR=/tank/pgx-bench python3 benchmarks/pgx/linux/limits.py --mode cgroup --out "$OUT/limits" 2>&1 | tee -a "$OUT/suite.log"
fi
echo "ALL DONE results in $OUT"
