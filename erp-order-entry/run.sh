#!/usr/bin/env bash
# The whole matrix: six scenarios x four arms x five runs.
#
# One cell on its own:
#   BENCH_SCENARIOS=two-workers BENCH_ARMS=cellaflow ./run.sh
# Fewer runs, to see it move:
#   BENCH_RUNS=1 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

# Export only what was actually asked for. Exporting a default here used to
# beat the URL `setup.sh` recorded in the credentials file, so a reader who
# moved the ERP off 8080 and then ran this without repeating ERP_PORT sent
# every call, API token included, to whatever else was holding 8080.
if [ -n "${ERP_URL:-}" ]; then
  export ERP_URL
elif [ -n "${ERP_PORT:-}" ]; then
  export ERP_URL="http://localhost:${ERP_PORT}"
fi

if [ -n "${CELLAFLOW_TARGET:-}" ]; then
  export CELLAFLOW_TARGET
elif [ -n "${CELLAFLOW_PORT:-}" ]; then
  export CELLAFLOW_TARGET="localhost:${CELLAFLOW_PORT}"
fi

exec ./venv/bin/python harness.py "$@"
