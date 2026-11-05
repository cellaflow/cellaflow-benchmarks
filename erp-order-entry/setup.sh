#!/usr/bin/env bash
# Brings up ERPNext and the engine, creates the site, seeds it, and writes the
# API credentials. Run once. Safe to run again: every step checks first.
set -euo pipefail
cd "$(dirname "$0")"

ERP_PORT="${ERP_PORT:-8080}"
PROJECT="${COMPOSE_PROJECT_NAME:-erp-order-entry}"

echo "==> starting containers"
docker compose -p "$PROJECT" up -d

echo "==> waiting for the site to be created (first run takes several minutes)"
for _ in $(seq 1 180); do
  if docker compose -p "$PROJECT" exec -T backend test -d sites/frontend 2>/dev/null; then
    break
  fi
  sleep 5
done
docker compose -p "$PROJECT" exec -T backend test -d sites/frontend || {
  echo "The site was never created. docker compose -p $PROJECT logs create-site" >&2
  exit 1
}

# `bench new-site --install-app erpnext` leaves a site with no units of
# measure, item groups, customer groups or territories: those come from the
# setup wizard, which is interactive. This is the same code the wizard calls,
# so the records are ERPNext's own rather than ones invented here.
echo "==> installing ERPNext's default records"
docker compose -p "$PROJECT" exec -T backend \
  bench --site frontend execute \
  erpnext.setup.setup_wizard.operations.install_fixtures.install \
  --kwargs "{'country': 'United Kingdom'}" >/dev/null

echo "==> creating the python environment"
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q -r requirements.txt

echo "==> seeding the company, customers and items"
./venv/bin/python seed.py "http://localhost:${ERP_PORT}"

echo
echo "Ready. Run ./run.sh"
