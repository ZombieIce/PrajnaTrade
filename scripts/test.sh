#!/usr/bin/env bash
# Run the PrajnaTrade test suite.
#
#   scripts/test.sh fast   # skip tests marked "isolation" (nested/sandboxed sessions)
#   scripts/test.sh full   # entire suite, including real-sandbox tests
#   scripts/test.sh <pytest args>  # pass everything through to pytest
#
# Set PYTHON to pick an interpreter, e.g. PYTHON=/path/to/.venv/bin/python.
set -euo pipefail

PYTHON="${PYTHON:-python}"
export PYTHONPATH=src
export PYTHONDONTWRITEBYTECODE=1

mode="${1:-full}"
xdist_args=()
if [ "${PYTEST_WORKERS:-auto}" != "0" ] && "$PYTHON" -c "import xdist" 2>/dev/null; then
  if [ "$mode" = "full" ] && [ -z "${PYTEST_WORKERS:-}" ]; then
    xdist_args=(-n auto --maxprocesses=4 --dist worksteal)
  else
    xdist_args=(-n "${PYTEST_WORKERS:-auto}" --dist worksteal)
  fi
fi

case "$mode" in
  fast)
    echo "mode: fast (excludes tests marked 'isolation')"
    echo "+ $PYTHON -m pytest -q ${xdist_args[*]} -m \"not isolation\""
    exec "$PYTHON" -m pytest -q "${xdist_args[@]}" -m "not isolation"
    ;;
  full)
    echo "mode: full (entire suite)"
    echo "+ $PYTHON -m pytest -q ${xdist_args[*]}"
    exec "$PYTHON" -m pytest -q "${xdist_args[@]}"
    ;;
  *)
    echo "mode: pytest passthrough"
    echo "+ $PYTHON -m pytest $*"
    exec "$PYTHON" -m pytest "$@"
    ;;
esac
