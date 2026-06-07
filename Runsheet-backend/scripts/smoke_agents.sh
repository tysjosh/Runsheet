#!/usr/bin/env bash
#
# smoke_agents.sh — confirm every Runsheet agent is alive via curl.
#
# Exercises the live backend's agent surface end-to-end:
#   * signs in as the seeded dev user to obtain a real SuperTokens session
#   * GET  /api/agent/health            — autonomous + overlay agent status
#   * GET  /api/agent/config/autonomy   — current autonomy level
#   * GET  /api/agent/activity/stats    — proof agents have logged actions
#   * GET  /api/agent/approvals         — actions agents have proposed
#   * POST /api/chat                    — conversational orchestrator + specialists
#   * POST /api/fuel/mvp/plan/generate  — full 5-agent fuel-distribution pipeline
#
# Prereqs (local dev only):
#   * Backend running:  ENVIRONMENT=development ./venv/bin/python main.py   (port 8080)
#   * Dev user has a known password:
#       ENVIRONMENT=development ./venv/bin/python -m scripts.set_user_password \
#           admin@runsheet.com --password 'Demo1234!'
#
# Usage:
#   bash scripts/smoke_agents.sh
#
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
EMAIL="${EMAIL:-admin@runsheet.com}"
PASSWORD="${PASSWORD:-Demo1234!}"
HDR_FILE="$(mktemp)"
trap 'rm -f "$HDR_FILE"' EXIT

echo "== Signing in as $EMAIL =="
curl -s -D "$HDR_FILE" -o /dev/null \
  -X POST "$BASE/auth/signin" \
  -H "Content-Type: application/json" \
  -H "rid: emailpassword" \
  -d "{\"formFields\":[{\"id\":\"email\",\"value\":\"$EMAIL\"},{\"id\":\"password\",\"value\":\"$PASSWORD\"}]}"

ACCESS="$(grep -i '^st-access-token:' "$HDR_FILE" | sed 's/^[Ss]t-access-token: //' | tr -d '\r')"
if [ -z "$ACCESS" ]; then
  echo "ERROR: no access token returned — is the dev user provisioned with the given password?" >&2
  exit 1
fi
AUTH=(-H "Authorization: Bearer $ACCESS")

echo; echo "== GET /api/agent/health =="
curl -s "${AUTH[@]}" "$BASE/api/agent/health"; echo

echo; echo "== GET /api/agent/config/autonomy =="
curl -s "${AUTH[@]}" "$BASE/api/agent/config/autonomy"; echo

echo; echo "== GET /api/agent/activity/stats =="
curl -s "${AUTH[@]}" "$BASE/api/agent/activity/stats"; echo

echo; echo "== GET /api/agent/approvals (first 5) =="
curl -s "${AUTH[@]}" "$BASE/api/agent/approvals?page=1&size=5"; echo

echo; echo "== POST /api/chat (conversational orchestrator + specialists) =="
curl -s -N --max-time 60 "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d '{"message":"How many trucks are in the fleet and what is their status?","mode":"chat","session_id":"smoke-1"}' \
  "$BASE/api/chat"; echo

echo; echo "== POST /api/fuel/mvp/plan/generate (5-agent pipeline) =="
curl -s "${AUTH[@]}" -H "Content-Type: application/json" -d '{}' \
  "$BASE/api/fuel/mvp/plan/generate"; echo

echo; echo "All agent smoke checks completed."
