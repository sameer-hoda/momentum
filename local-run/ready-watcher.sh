#!/bin/bash
# Watches the manual build_data.py run and flips provision state to ready the
# moment frontend_data.json lands (the old provisioner is dead so nothing else
# will). Also refreshes the message count so the boot meter stays truthful.
set -u
V3_DIR="/Users/sameerhoda/Projects/wa-wunderlist/v3"
OUT="$V3_DIR/app/frontend/frontend_data.json"
echo "== ready-watcher: waiting for $OUT"
while [ ! -f "$OUT" ]; do sleep 10; done
sleep 2
MSGS=$(python3 -c "import sqlite3; print(sqlite3.connect('$V3_DIR/local-store/messages.db').execute('SELECT COUNT(*) FROM messages').fetchone()[0])" 2>/dev/null || echo 0)
TASKS=$(python3 -c "import json; print(len(json.load(open('$OUT')).get('tasks',[])))" 2>/dev/null || echo 0)
python3 -c "
import json, datetime
st = {'stage': 'ready', 'detail': 'live with $MSGS messages synced', 'messages': $MSGS, 'updated': datetime.datetime.now(datetime.timezone.utc).isoformat()}
for p in ('/tmp/provision.json', '$V3_DIR/local-store/provision.json'):
    json.dump(st, open(p, 'w'))
print('flipped to ready: $MSGS msgs, $TASKS tasks')
"
