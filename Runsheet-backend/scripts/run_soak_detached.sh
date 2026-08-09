#!/bin/bash
# Bring up the local stack and start the persona soak fully detached.
#
# The point of setsid + nohup is that the first attempt at a 12-hour run died
# after 2.5 hours when the terminal that launched it went away. A long soak must
# not be a child of an interactive shell or an IDE-managed terminal, or the run
# ends whenever that does.
#
# Usage:  SOAK_PASSWORD='...' bash scripts/run_soak_detached.sh [HOURS]
#
# Check on it later with:
#   cat soak-out/<run-id>/summary.json | python -m json.tool | head -40
#   tail -f soak-out/<run-id>/../soak.log

set -euo pipefail

HOURS="${1:-12}"
: "${SOAK_PASSWORD:?set SOAK_PASSWORD}"

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
mkdir -p soak-out

echo "== dependencies"
docker compose up -d postgres >/dev/null 2>&1 || true
docker start runsheet-es >/dev/null 2>&1 || true
pgrep -q redis-server || (setsid nohup redis-server --port 6379 >soak-out/redis.log 2>&1 & )

for i in $(seq 1 30); do
  curl -fsS --max-time 5 http://127.0.0.1:9200/_cluster/health >/dev/null 2>&1 && break
  sleep 5
done
echo "   elasticsearch $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9200)"
echo "   postgres      $(docker exec runsheet-postgres pg_isready -U runsheet -d runsheet 2>&1 | tail -1)"

echo "== backend"
if ! curl -fsS --max-time 5 http://127.0.0.1:8080/health/live >/dev/null 2>&1; then
  setsid nohup env ENVIRONMENT=development LOG_LEVEL=WARNING \
    "$ROOT/venv/bin/uvicorn" main:app --host 127.0.0.1 --port 8080 \
    >soak-out/backend.log 2>&1 &
  for i in $(seq 1 40); do
    curl -fsS --max-time 5 http://127.0.0.1:8080/health/live >/dev/null 2>&1 && break
    sleep 3
  done
fi
echo "   /health/live  $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/health/live)"
echo "   /health/ready $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/health/ready)"

echo "== soak (${HOURS}h, detached)"
# caffeinate -s also holds off sleep on AC power, not just idle sleep.
setsid nohup caffeinate -s env ENVIRONMENT=development \
  "$ROOT/venv/bin/python" -m scripts.soak_personas \
  --password "$SOAK_PASSWORD" --duration-hours "$HOURS" --out-dir ./soak-out \
  --orders-per-hour 15 --llm-per-hour 6 --think-min 4 --think-max 20 \
  >soak-out/soak.log 2>&1 &

sleep 25
echo "   pid(s): $(pgrep -f soak_personas | tr '\n' ' ')"
sed -n '1,4p' soak-out/soak.log 2>/dev/null || true
echo "== detached. Closing this shell will not stop it."
