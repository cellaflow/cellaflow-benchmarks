#!/usr/bin/env bash
# The whole matrix: six scenarios x four arms x five runs.
#
# One cell on its own:
#   BENCH_SCENARIOS=two-workers BENCH_ARMS=cellaflow ./run.sh
# Fewer runs, to see it move:
#   BENCH_RUNS=1 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

export ERP_URL="${ERP_URL:-http://localhost:${ERP_PORT:-8080}}"
export CELLAFLOW_TARGET="${CELLAFLOW_TARGET:-localhost:${CELLAFLOW_PORT:-50051}}"

exec ./venv/bin/python harness.py "$@"
