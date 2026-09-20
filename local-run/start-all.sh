#!/bin/bash
# Start everything backgrounded (bridge + provisioner + API), each logging.
# Prefer the three-terminal version (bridge.sh/provision.sh/api.sh) if you want
# to SEE the QR directly. This version keeps the QR in local-run/bridge.log and
# in the UI boot loader (GET /api/qr) instead.
set -u
export V3_DIR="/Users/sameerhoda/Projects/wa-wunderlist/v3"
cd "$V3_DIR"
set -a
source "$V3_DIR/local-run/local.env"
set +a
mkdir -p "$STORE_DIR"
ln -sfn "$STORE_DIR" "$V3_DIR/local-run/store"

# No silent key auto-load: the journey must ask the user for a valid,
# live-tested key (or an explicit skip) at the awaiting_key stage.
# To restore the old convenience, export GEMINI_API_KEY before running.

export WA_BRIDGE_URL="http://localhost:8080"

"$V3_DIR/local-run/bridge.sh" > "$V3_DIR/local-run/bridge.out.log" 2>&1 &
echo $! > "$V3_DIR/local-run/bridge.pid"
"$V3_DIR/local-run/provision.sh" > "$V3_DIR/local-run/provision.out.log" 2>&1 &
echo $! > "$V3_DIR/local-run/provision.pid"
"$V3_DIR/local-run/api.sh" > "$V3_DIR/local-run/api.out.log" 2>&1 &
echo $! > "$V3_DIR/local-run/api.pid"
sleep 2
BP=$(cat "$V3_DIR/local-run/bridge.pid"); PP=$(cat "$V3_DIR/local-run/provision.pid"); AP=$(cat "$V3_DIR/local-run/api.pid")
echo "started: bridge($BP) provision($PP) api($AP)"
echo "UI: http://localhost:$PORT | QR: tail -f local-run/bridge.log | audit: ./local-run/audit.sh"
