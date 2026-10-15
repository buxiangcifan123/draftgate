#!/usr/bin/env bash
# Submit a draft over HTTP and wait for the human decision.
# Mirrors what an n8n "HTTP Request" node would do.
#
# Usage:
#   export DRAFTGATE_TOKEN=$(draftgate token)
#   ./examples/submit_and_wait.sh

set -euo pipefail

DG_URL="${DRAFTGATE_URL:-http://127.0.0.1:8010}"
DG_TOKEN="${DRAFTGATE_TOKEN:?Set DRAFTGATE_TOKEN first (run: draftgate token)}"

response=$(curl -s -X POST "$DG_URL/api/items" \
  -H "Authorization: Bearer $DG_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"Hello from shell","content":"Human, approve me?","tags":["demo"]}')

item_id=$(python -c "import sys,json;print(json.loads(sys.argv[1])['id'])" "$response")
echo "submitted item #$item_id, waiting for review..."

while true; do
  sleep 3
  status=$(curl -s "$DG_URL/api/items/$item_id" \
    -H "Authorization: Bearer $DG_TOKEN" \
    | python -c "import sys,json;print(json.load(sys.stdin)['status'])")
  case "$status" in
    pending) ;;
    *) echo "final status: $status"; exit 0 ;;
  esac
done
