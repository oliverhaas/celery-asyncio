#!/usr/bin/env bash
# Create the four venvs the benchmark needs.
#
# Layout:
#   .venv-async-314    -- celery-asyncio on Python 3.14
#   .venv-async-314t   -- celery-asyncio on Python 3.14t (free-threaded)
#   .venv-classic-314  -- upstream celery on Python 3.14
#   .venv-classic-314t -- upstream celery on Python 3.14t (best-effort)
#
# Run from this directory: ./setup_venvs.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
cd "$HERE"

PY314=3.14
PY314T=3.14t

# Common deps for the runner side (publisher + psutil + redis client).
COMMON_DEPS=(psutil redis)
CLASSIC_DEPS=(celery redis psutil)

# The repo's pyproject.toml overrides "celery" to never resolve (so flower
# doesn't pull in the upstream package). We need to bypass that when
# installing the classic venvs, or the upstream celery package is silently
# skipped.
export UV_NO_CONFIG=1

make_async_venv() {
    local name="$1"
    local pyver="$2"
    if [[ -d "$name" ]]; then
        echo "[setup] $name exists, skipping"
        return
    fi
    echo "[setup] creating $name with python $pyver"
    uv venv -p "$pyver" "$name"
    # Install the local celery-asyncio package + runner deps.
    # uvloop is installed in both async venvs so benchmarks can toggle it
    # via BENCH_UVLOOP=1.
    uv pip install --python "$name/bin/python" -e "$REPO_ROOT" "${COMMON_DEPS[@]}" uvloop
    # Override the published kombu-asyncio with the local checkout so the
    # bench picks up unreleased fixes (e.g. the long-lived consume tasks
    # that eliminate the strand bug this bench originally surfaced).
    local local_kombu="$REPO_ROOT/../kombu-asyncio"
    if [[ -f "$local_kombu/pyproject.toml" ]]; then
        uv pip install --python "$name/bin/python" -e "$local_kombu"
    fi
}

make_classic_venv() {
    local name="$1"
    local pyver="$2"
    if [[ -d "$name" ]]; then
        echo "[setup] $name exists, skipping"
        return
    fi
    echo "[setup] creating $name with python $pyver"
    if ! uv venv -p "$pyver" "$name"; then
        echo "[setup] python $pyver unavailable, skipping $name"
        return
    fi
    if ! uv pip install --python "$name/bin/python" "${CLASSIC_DEPS[@]}"; then
        echo "[setup] classic celery install failed on $pyver, removing $name"
        rm -rf "$name"
    fi
}

make_async_venv .venv-async-314  "$PY314"
make_async_venv .venv-async-314t "$PY314T"
make_classic_venv .venv-classic-314  "$PY314"
make_classic_venv .venv-classic-314t "$PY314T"

echo "[setup] done"
for d in .venv-async-314 .venv-async-314t .venv-classic-314 .venv-classic-314t; do
    if [[ -x "$d/bin/celery" ]]; then
        echo "  $d/bin/celery: $($d/bin/celery --version 2>&1 | head -1)"
    fi
done
