#!/bin/bash
# Single-container stack: Go WhatsApp bridge + Python provisioner + API/UI.
set -u
STORE="${STORE_DIR:-/data/store}"
mkdir -p "$STORE"

# Bridge expects ./store (relative) — point it at the persistent volume.
if [ ! -e store ]; then
  ln -s "$STORE" store
fi

if [ ! -w "$STORE" ]; then
  echo "!! STORE_DIR $STORE is not writable — data will be EPHEMERAL." >&2
fi

echo "-> starting WhatsApp bridge (QR will print below on first run)..."
./bridge/wabridge > "${BRIDGE_LOG:-/data/bridge.log}" 2>&1 &
echo "$!" > /tmp/bridge.pid

if [ "${DEMO_MODE:-0}" = "1" ]; then
  echo "-> DEMO_MODE: seeding synthetic workspace, bridge not required."
fi

echo "-> starting provisioner (pair -> sync -> analyze)..."
python3 app/provision.py >> "${STORE}/provision_run.log" 2>&1 &

echo "-> starting Momentum API on :${PORT:-8080} ..."
exec python3 app/momentum_api.py
