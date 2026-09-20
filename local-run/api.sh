#!/bin/bash
# Terminal 3 — UI + API on :8099. Boot loader polls /api/status (QR stage shows
# the code in-page too). First visit: create email + password, paste Gemini key.
set -u
source "$(dirname "$0")/local.env"
cd "$V3_DIR"
echo "== api: UI at http://localhost:$PORT (log: local-run/api.log) =="
python3 app/momentum_api.py 2>&1 | tee "$V3_DIR/local-run/api.log"
