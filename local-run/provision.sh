#!/bin/bash
# Terminal 2 — provisioner: waits for pairing, then for history sync to settle
# (3 stable 15s polls), auto-maps groups into themes, runs the analysis.
# Progress: /tmp/provision.json + local-store/provision.json + GET /api/status.
set -u
source "$(dirname "$0")/local.env"
cd "$V3_DIR"
echo "== provisioner: pair -> sync-stable -> analyze (log: local-store/provision_run.log) =="
python3 app/provision.py 2>&1 | tee -a "$STORE_DIR/provision_run.log"
