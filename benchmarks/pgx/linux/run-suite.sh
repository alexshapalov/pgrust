#!/usr/bin/env bash
# Run the whole PGX benchmark suite on this host and collect the results in
# benchmarks/pgx/results/<hostname>-<short sha>/.
#
#   PGXBENCH_WORKDIR=/tank/pgx-bench benchmarks/pgx/linux/run-suite.sh [step ...]
#
# PGXBENCH_WORKDIR is where every scratch cluster lives, so it selects the
# filesystem under test. Steps (default: all, in this order):
#   cow memory regress baseline profile churn scale noisy ephemeral pool
#   extra (not in the default list): limits multiruntime cpunoisy
#
# Sized for a small host by default (4 CPUs, 8 GB); override with the
# PGX_* variables below. Everything runs at nice 5.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
: "${PGXBENCH_WORKDIR:?set PGXBENCH_WORKDIR to a directory on the filesystem under test}"
export PGXBENCH_WORKDIR
OUT="$HERE/results/$(hostname -s)-$(git -C "$REPO" rev-parse --short=10 HEAD)"
PROFILE="$REPO/configs/pgx-ephemeral.conf"
mkdir -p "$OUT"
STEPS=("$@")
[ ${#STEPS[@]} -eq 0 ] && STEPS=(cow memory regress baseline profile churn scale noisy ephemeral pool)
PY="nice -n 5 python3"

for step in "${STEPS[@]}"; do
    echo "=== $step  $(date -u +%H:%M:%S)"
    case "$step" in
    memory)
        $PY "$HERE/linux/memory-breakdown.py" --with-postgres --out "$OUT/memory"
        PGXBENCH_THP_DISABLE=1 $PY "$HERE/linux/memory-breakdown.py" --with-postgres --out "$OUT/memory-thp-disabled" ;;
    regress)
        $PY "$HERE/regress-baseline.py" --out "$OUT/regress" > "$OUT/regress-summary.json"
        $PY "$HERE/classify-regress-diffs.py" "$OUT/regress" ;;
    baseline)
        $PY "$HERE/pgxbench.py" all --iterations "${PGX_STARTUP_ITERATIONS:-50}" --out "$OUT/baseline"
        $PY "$HERE/pgxbench.py" all --iterations "${PGX_STARTUP_ITERATIONS:-50}" --engine postgres --out "$OUT/postgres18-reference" ;;
    profile)
        $PY "$HERE/pgxbench.py" all --iterations "${PGX_STARTUP_ITERATIONS:-50}" --conf "$PROFILE" \
            --conn-counts 0,1,10 --out "$OUT/profile" ;;
    ephemeral)
        $PY "$HERE/ephemeral.py" --conf "$PROFILE" --counts "${PGX_EPHEMERAL_COUNTS:-1,10,100,300}" --out "$OUT/ephemeral"
        $PY "$HERE/ephemeral.py" createdb --engine postgres --out "$OUT/ephemeral-postgres18" ;;
    cow)
        $PY "$HERE/cow.py" --sizes "${PGX_COW_SIZES:-15,100,1000}" --out "$OUT/cow" ;;
    churn)
        $PY "$HERE/churn.py" --cycles "${PGX_CHURN_CYCLES:-30}" --out "$OUT/churn" ;;
    noisy)
        $PY "$HERE/noisy-neighbor.py" --label profile --out "$OUT/noisy-neighbor"
        $PY "$HERE/noisy-neighbor.py" --label profile+session-limit --guc pgrust.session_memory_limit=256 \
            --scenarios baseline,memory,sort --out "$OUT/noisy-neighbor" ;;
    scale)
        $PY "$HERE/scale.py" --counts "${PGX_SCALE_COUNTS:-0,100,300,1000}" --active "${PGX_SCALE_ACTIVE:-20,100}" --out "$OUT/scale"
        $PY "$HERE/scale.py" --counts "${PGX_SCALE_COUNTS:-0,100,300,1000}" --active "" --long-idle-seconds 60 \
            --guc autovacuum=off --out "$OUT/scale-autovacuum-off" ;;
    limits)
        $PY "$HERE/linux/limits.py" --out "$OUT/limits" ;;
    multiruntime)
        $PY "$HERE/linux/multiruntime.py" --layouts "${PGX_LAYOUTS:-1x1000,2x500,4x250}" --out "$OUT/multiruntime" ;;
    cpunoisy)
        $PY "$HERE/linux/cpu-noisy.py" --levels "${PGX_NOISY_LEVELS:-0,1,2,4,6}" --out "$OUT/cpu-noisy" ;;
    pool)
        $PY "$HERE/warm-pool.py" --out "$OUT/warm-pool" ;;
    *) echo "unknown step $step"; exit 2 ;;
    esac
    echo "    exit=$?"
done 2>&1 | tee -a "$OUT/suite.log"
echo "results in $OUT"
