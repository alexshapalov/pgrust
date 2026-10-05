#!/usr/bin/env bash
# Prepare a Debian/Ubuntu host (a PGRun branch host) to build PgRust and run
# the PGX benchmark suite. Idempotent. Run from a checkout of this repository.
#
#   benchmarks/pgx/linux/setup-host.sh                 # packages, toolchain, build
#   benchmarks/pgx/linux/setup-host.sh tank/pgx-bench  # also create that ZFS dataset
#
# What it changes on the host:
#   - apt packages: build tools, RE2, PostgreSQL 18 server + dev files (for
#     initdb and pg_regress), gdb (thread backtraces in the hang reproducer)
#   - Rust 1.96.0 under ~/.rustup and ~/.cargo for the current user
#   - target/ inside this checkout
#   - optionally one ZFS dataset, owned by the current user
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
DATASET="${1:-}"

sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    build-essential pkg-config libre2-dev python3 curl ca-certificates gdb
if ! [ -x /usr/lib/postgresql/18/bin/initdb ] || ! [ -e /usr/lib/postgresql/18/lib/pgxs/src/test/regress/pg_regress ]; then
    # Ubuntu 26.04 ships PostgreSQL 18; older releases need the PGDG repository.
    sudo apt-get install -y --no-install-recommends postgresql-18 postgresql-server-dev-18
fi

if ! command -v rustup >/dev/null 2>&1 && ! [ -x "$HOME/.cargo/bin/rustup" ]; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none
fi
export PATH="$HOME/.cargo/bin:$PATH"
# rust-toolchain.toml pins the version; this fetches it.
(cd "$REPO" && rustup show active-toolchain >/dev/null)

# Thin LTO peaks at about 1 GB per rustc and the workspace is ~1,000 crates:
# cap the job count on small hosts so the build cannot starve live branches.
JOBS="${PGX_BUILD_JOBS:-$(( $(nproc) > 2 ? $(nproc) - 1 : 1 ))}"
(cd "$REPO" && nice -n 10 cargo build --release --locked --bin postgres -j "$JOBS")
ls -l "$REPO/target/release/postgres"

if [ -n "$DATASET" ]; then
    if ! zfs list -H "$DATASET" >/dev/null 2>&1; then
        sudo zfs create "$DATASET"
    fi
    MP="$(zfs get -H -o value mountpoint "$DATASET")"
    sudo chown "$(id -un):$(id -gn)" "$MP"
    echo "dataset $DATASET mounted at $MP"
    echo "block cloning: pool feature $(zpool get -H -o value feature@block_cloning "${DATASET%%/*}"), zfs_bclone_enabled=$(cat /sys/module/zfs/parameters/zfs_bclone_enabled 2>/dev/null || echo unknown)"
fi
