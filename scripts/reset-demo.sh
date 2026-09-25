#!/bin/sh
# Returns market, bookings, intent and metrics to the initial condition (new Tinybird run id).
#   scripts/reset-demo.sh            -> reset, wait for the user to hand over the trip in the UI
#   scripts/reset-demo.sh --intent   -> reset and submit the demo intent immediately
set -eu
API="${API:-http://localhost:8000}"
Q=""
[ "${1:-}" = "--intent" ] && Q="?with_intent=true"
curl -sf -X POST "$API/api/demo/reset$Q" && echo
curl -sf "$API/api/health" && echo
