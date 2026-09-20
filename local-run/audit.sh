#!/bin/bash
# Live nudge-quality audit — run in a 4th terminal while the stack runs.
# Loops: provision stage -> message/task counts -> latest /api/nudges verdicts
# -> newest tasks + their states. Ctrl+C to stop. No auth needed until you set
# APP_PASSWORD (then export APP_PASSWORD before running).
set -u
source "$(dirname "$0")/local.env"
API="http://localhost:$PORT"
AUTH=()
[ -n "${APP_PASSWORD:-}" ] && AUTH=(-u ":$APP_PASSWORD")

echo "== nudge audit loop @ $API (5s cadence) =="
n=0
while true; do
  n=$((n+1))
  # shellcheck disable=SC2128 — ${AUTH[@]:-} is empty-but-unbound-safe under set -u
  if [ "${#AUTH[@]}" -gt 0 ]; then ST=$(curl -s -m 5 "${AUTH[@]}" "$API/api/status" 2>/dev/null || echo '{}')
  else ST=$(curl -s -m 5 "$API/api/status" 2>/dev/null || echo '{}'); fi
  if [ "${#AUTH[@]}" -gt 0 ]; then NU=$(curl -s -m 5 "${AUTH[@]}" "$API/api/nudges?window=5" 2>/dev/null || echo '{}')
  else NU=$(curl -s -m 5 "$API/api/nudges?window=5" 2>/dev/null || echo '{}'); fi
  python3 - "$ST" "$NU" "$n" <<'EOF'
import json, sys
n = sys.argv[3]
try: st = json.loads(sys.argv[1])
except Exception: st = {}
try: nu = json.loads(sys.argv[2])
except Exception: nu = {}
stage = st.get('stage', '?')
if not stage or stage == '?':
    stage = st.get('error', '?')[:60] if st.get('error') else 'unreachable'
msgs, tasks = st.get('messages', '?'), st.get('tasks', '?')
ns = nu.get('nudges', []) if isinstance(nu, dict) else []
print(f"[{n}] stage={stage} msgs={msgs} tasks={tasks} nudges(5m)={len(ns)}")
for g in ns[:7]:
    print(f"    - [{g.get('reason','?')}] {g.get('group','?')}: {(g.get('why_now') or '')[:110]}")
    print(f"      move: {(g.get('suggested_move') or '')[:110]}")
EOF
  # newest tasks straight from the built data (when analysis has run)
  if [ -f "$V3_DIR/app/frontend/frontend_data.json" ]; then
    python3 - <<'EOF'
import json
try:
    d = json.load(open('/Users/sameerhoda/Projects/wa-wunderlist/v3/app/frontend/frontend_data.json'))
    ts = sorted(d.get('tasks', []), key=lambda t: t.get('last_ts', ''), reverse=True)[:3]
    for t in ts:
        print(f"    # task [{t.get('state','?')}/{t.get('status','?')}] {t.get('title','?')[:70]} (owner={t.get('owner','?')}, waiting_on={t.get('waiting_on','?')}, needs_you={t.get('needs_my_action')})")
except Exception as e:
    print(f"    # frontend_data.json unreadable: {e}")
EOF
  fi
  sleep 5
done
