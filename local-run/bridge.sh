#!/bin/bash
# Terminal 1 — WhatsApp bridge. Prints the pairing QR here AND into
# local-run/bridge.log (the UI also renders it via GET /api/qr on :8099).
# Databases land in local-store/ (messages.db + whatsapp.db). Keeps running,
# re-prints a fresh QR whenever codes expire (they're short-lived — no rush).
set -u
source "$(dirname "$0")/local.env"

# Bridge expects ./store relative to ITS cwd -> symlink it at the persistent dir.
mkdir -p "$STORE_DIR"
ln -sfn "$STORE_DIR" "$V3_DIR/local-run/store"

cd "$V3_DIR/local-run"
echo "== bridge: QR prints below — scan with WhatsApp → Settings → Linked devices =="
echo "== logs:  local-run/bridge.log | db: local-store/messages.db =="
./wabridge 2>&1 | tee "$BRIDGE_LOG"
