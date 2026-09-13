"""
First-boot provisioner: WhatsApp pair -> message sync -> analysis -> ready.

Stages reported to /tmp/provision.json (mirrored into STORE_DIR) for the
boot-loader UI and /api/status:
  awaiting_bridge -> awaiting_qr -> syncing -> analyzing -> ready (+ error)
"""
import json, os, sqlite3, subprocess, sys, time, datetime, socket
from urllib.parse import urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STORE_DIR = os.environ.get('STORE_DIR', os.path.join(os.path.dirname(APP_DIR), 'store-fallback'))
BRIDGE_URL = os.environ.get('WA_BRIDGE_URL', 'http://localhost:8080').rstrip('/')
BRIDGE_LOG = os.environ.get('BRIDGE_LOG', os.path.join(STORE_DIR, '..', 'bridge.log'))
MESSAGES_DB = os.path.join(STORE_DIR, 'messages.db')
STATE_FILE = '/tmp/provision.json'
STABLE_POLLS = 3
STABLE_GAP = 15


def save(stage, detail='', messages=0):
    st = {'stage': stage, 'detail': detail, 'messages': messages,
          'updated': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    for p in (STATE_FILE, os.path.join(STORE_DIR, 'provision.json')):
        try:
            os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
            json.dump(st, open(p, 'w'))
        except Exception:
            pass
    print(f'[provision] {stage}: {detail} ({messages} msgs)', flush=True)
    return st


def bridge_up():
    try:
        parts = urlparse(BRIDGE_URL)
        s = socket.create_connection((parts.hostname or '127.0.0.1', parts.port or 8080), timeout=3)
        s.close()
        return True
    except Exception:
        return False


def msg_count():
    try:
        c = sqlite3.connect(MESSAGES_DB)
        n = c.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        c.close()
        return n
    except Exception:
        return 0


def _ping_key(key):
    try:
        from google import genai
        it = genai.Client(api_key=key).models.list(config={'page_size': 1})
        next(iter(it), None)
        return True
    except Exception as e:
        log(f'env key check failed ({type(e).__name__})')
        return False


_key_ok = {}


def has_key():
    env = (os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY') or '').strip()
    if env:
        if env not in _key_ok:
            _key_ok[env] = _ping_key(env)
        if _key_ok[env]:
            return True
        log('env Gemini key rejected by Google — will ask in UI instead')
        return False
    try:
        sys.path.insert(0, APP_DIR)
        import export_data as ed
        return bool(ed.get_gemini_key())
    except Exception:
        return False


def log_hint_paired():
    try:
        tail = open(BRIDGE_LOG, errors='ignore').read()[-20000:]
        low = tail.lower()
        return ('connected' in low or 'logged in' in low or 'login' in low
                or 'qrcode' not in low and 'pair' in low)
    except Exception:
        return False


def ensure_themes():
    """First run: map every active group into one 'Chats' theme so the board
    is useful immediately. A customized theme_groups.json is left untouched."""
    import json as J
    tf = os.path.join(APP_DIR, 'theme_groups.json')
    try:
        data = J.load(open(tf))
    except Exception:
        data = {}
    if any((v.get('groups') for v in data.values() if isinstance(v, dict))):
        return 'custom themes kept'
    try:
        c = sqlite3.connect(MESSAGES_DB)
        c.execute(f"ATTACH DATABASE '{os.path.join(STORE_DIR, 'whatsapp.db')}' AS wa")
        rows = c.execute("""
            SELECT DISTINCT ch.name FROM chats ch
            JOIN messages m ON m.chat_jid = ch.jid
            LEFT JOIN wa.whatsmeow_chat_settings cs ON ch.jid = cs.chat_jid
            WHERE ch.jid LIKE '%@g.us' AND ch.jid != 'status@broadcast'
              AND ch.name IS NOT NULL AND ch.name != ''
              AND (cs.archived IS NULL OR cs.archived = 0)
            ORDER BY ch.name""").fetchall()
        c.close()
        groups = [r[0] for r in rows][:200]
        data = {'chats': {'label': 'Chats', 'owner': '',
                          'description': 'auto-mapped on first run — edit me',
                          'groups': groups}}
        J.dump(data, open(tf, 'w'), indent=1)
        return f'auto-mapped {len(groups)} groups into Chats'
    except Exception as e:
        return f'theme auto-map skipped ({e})'


def run_build():
    r = subprocess.run([sys.executable, os.path.join(APP_DIR, 'build_data.py')],
                       cwd=APP_DIR, capture_output=True, text=True, timeout=7200)
    print(r.stdout[-3000:], flush=True)
    if r.returncode != 0:
        print(r.stderr[-3000:], flush=True)
    return r.returncode == 0


def main():
    os.makedirs(STORE_DIR, exist_ok=True)
    if os.environ.get('DEMO_MODE', '0') == '1':
        save('analyzing', 'seeding demo workspace', 0)
        sys.path.insert(0, APP_DIR)
        import demo_seed
        out = demo_seed.write(os.path.join(APP_DIR, 'frontend', 'frontend_data.json'))
        save('ready', f'demo workspace: {out}', 0)
        return

    t0 = time.time()
    save('awaiting_bridge', 'starting WhatsApp bridge', 0)
    while not bridge_up():
        if time.time() - t0 > 300:
            save('error', 'bridge did not come up — check deploy logs')
            return
        time.sleep(3)

    # Gemini key first: analysis and drafts need it. User pastes it in the
    # UI (validated + saved to the volume); env key skips this entirely.
    # SKIP_KEY_CHECK=1 (or the UI "skip" choice) runs heuristic-only.
    if not has_key() and os.environ.get('SKIP_KEY_CHECK', '0') != '1' \
            and not os.path.exists(os.path.join(STORE_DIR, '.skip-key')):
        save('awaiting_key', 'paste a Gemini API key to enable AI analysis', 0)
        while not has_key():
            time.sleep(10)
        save('awaiting_key', 'key accepted — continuing', 0)

    n = msg_count()
    stable = 0
    last = -1
    save('awaiting_qr', 'scan the QR code above with WhatsApp → Linked devices', n)
    qr_announced = False
    while True:
        n = msg_count()
        paired = n > 0 or log_hint_paired()
        if not paired:
            if not qr_announced or int(time.time() - t0) % 120 == 0:
                save('awaiting_qr', 'scan the QR code above with WhatsApp → Linked devices', n)
                qr_announced = True
            if time.time() - t0 > 1800:
                save('error', 'no pairing after 30 min — a fresh QR is shown above')
                return
            time.sleep(10)
            continue
        if n == last and n > 0:
            stable += 1
        else:
            stable = 0
        last = n
        save('syncing', f'history sync in progress — {n} messages so far', n)
        if stable >= STABLE_POLLS:
            break
        time.sleep(STABLE_GAP)

    save('analyzing', ensure_themes() + f' — running analysis over {n} messages', n)
    if run_build():
        save('ready', f'live with {n} messages synced', n)
    else:
        save('error', 'analysis failed — see rebuild.log, retry via /api/rebuild')


if __name__ == '__main__':
    main()
