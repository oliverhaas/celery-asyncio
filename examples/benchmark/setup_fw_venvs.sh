#!/usr/bin/env bash
# Venvs for the cross-framework comparison, one per (framework x build).
# Kept separate from setup_venvs.sh so the celery matrix is unaffected.
set -u
cd "$(dirname "$0")"

declare -A DEPS=(
  [arq]="arq redis"
  [dramatiq]="dramatiq[redis] redis"
  [taskiq]="taskiq taskiq-redis redis"
  [djangoq]="django-q2 django redis"
)

for fw in "${!DEPS[@]}"; do
  for py in 3.14 3.14t; do
    name=".venv-$fw-${py/./}"
    name="${name//3.14/314}"
    rm -rf "$name"
    uv venv -q -p "$py" "$name" || { echo "SKIP $name (no interpreter)"; continue; }
    # shellcheck disable=SC2086
    if uv pip install -q --python "$name/bin/python" ${DEPS[$fw]} psutil; then
      echo "OK   $name"
    else
      echo "FAIL $name"
    fi
  done
done
