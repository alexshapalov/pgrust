#!/usr/bin/env bash
# Phase 1A: measure each configuration group on its own against the same
# binary. One result directory per group; a fresh baseline run comes first so
# every group has a same-session control.
#
# Usage: run-config-groups.sh [group.conf ...]   (default: baseline + all groups)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/results/phase1a-$(git -C "$REPO" rev-parse --short=10 HEAD)"

run() { # $1 = label, $2 = conf file or empty
    local label="$1" conf="${2:-}" dir="$OUT/$1"
    local conf_args=() conn_args=() regress_args=()
    [ -n "$conf" ] && conf_args=(--conf "$conf")
    # A profile that lowers max_connections cannot hold 100 clients, and
    # pg_regress must not run 20 tests at once against it.
    if [ -n "$conf" ] && grep -q '^max_connections' "$conf"; then
        conn_args=(--conn-counts 0,1,10)
        regress_args=(--max-connections 10)
    fi
    echo "=== $label"
    python3 "$HERE/pgxbench.py" all --iterations 100 --out "$dir" \
        ${conf_args[@]+"${conf_args[@]}"} ${conn_args[@]+"${conn_args[@]}"} | grep -v '^startup\|^idle\|^conns'
    python3 "$HERE/regress-baseline.py" --out "$dir/regress" \
        ${conf_args[@]+"${conf_args[@]}"} ${regress_args[@]+"${regress_args[@]}"} > "$dir/regress-summary.json"
    python3 "$HERE/classify-regress-diffs.py" "$dir/regress" > /dev/null
}

if [ $# -eq 0 ]; then
    run 0-baseline
    set -- "$REPO"/configs/pgx/groups/*.conf
fi
for conf in "$@"; do
    run "$(basename "$conf" .conf)" "$conf"
done
