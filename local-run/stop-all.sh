#!/bin/bash
# Stop everything started by start-all.sh.
cd "$(dirname "$0")"
for p in bridge provision api; do
  if [ -f "local-run/$p.pid" ]; then kill "$(cat "local-run/$p.pid")" 2>/dev/null && echo "stopped $p"; rm -f "local-run/$p.pid"; fi
done
pkill -f "local-run/wabridge" 2>/dev/null
pkill -f "app/provision.py" 2>/dev/null
pkill -f "app/momentum_api.py" 2>/dev/null
echo done
