#!/bin/sh
# Deploy to $TINYBIRD_HOST. With no TINYBIRD_TOKEN, use Tinybird Local's default workspace admin token.
set -eu
HOST="${TINYBIRD_HOST:-http://localhost:7181}"
TOKEN="${TINYBIRD_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  TOKEN=$(curl -sf "$HOST/tokens" | python3 -c 'import json,sys; print(json.load(sys.stdin)["workspace_admin_token"])')
fi
tb --cloud --host "$HOST" --token "$TOKEN" deploy --wait
