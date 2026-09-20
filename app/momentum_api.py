"""
Momentum API — drafts + send backend for the Mission Control UI.

Railway template version. Zero extra gateway deps: stdlib http.server.

Serves the UI and API on one origin (PORT, Railway injects it):
  GET  /                    static files from app/frontend/
  GET  /api/health          bridge + gemini + mode status
  GET  /api/status          onboarding stage for the boot loader
  POST /api/rebuild         rerun the analysis pipeline (auth required)
  POST /api/replies         {task_id, hint?, intents?} -> {drafts:[...]}
  POST /api/send            {task_id, text, attach_context} -> via WA bridge

Env:
  PORT             what we listen on (Railway sets this)
  APP_PASSWORD     basic-auth password for the whole UI/API (strongly recommended)
  GEMINI_API_KEY   or GOOGLE_API_KEY — LLM extraction + reply drafts
  OWNER_NAME       your display name in WhatsApp (powers "needs you" ranking)
  STORE_DIR        persistent dir for messages.db/whatsapp.db (Railway Volume -> /data/store)
  BRIDGE_URL       default http://localhost:8080
  MOMENTUM_LIVE    0 (default, dry-run) or 1 (actually deliver WhatsApp messages)
  DEMO_MODE        1 to serve synthetic demo data (no WhatsApp needed)
"""
import json, os, re, secrets, sqlite3, subprocess, sys, datetime, time, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.request import urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse, parse_qs

APP_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(APP_DIR, 'frontend')
STORE_DIR = os.environ.get('STORE_DIR', os.path.join(os.path.dirname(APP_DIR), 'store-fallback'))
DATA_FILE = os.path.join(FRONTEND_DIR, 'frontend_data.json')
MESSAGES_DB = os.path.join(STORE_DIR, 'messages.db')
SENT_LOG = os.path.join(STORE_DIR, 'sent_log.jsonl')
PROVISION_FILE = '/tmp/provision.json'
BRIDGE_LOG = os.environ.get('BRIDGE_LOG', os.path.join(STORE_DIR, '..', 'bridge.log'))

PORT = int(os.environ.get('PORT', '8082'))
LIVE = os.environ.get('MOMENTUM_LIVE', '0') == '1'
DEMO = os.environ.get('DEMO_MODE', '0') == '1'
BRIDGE_URL = os.environ.get('WA_BRIDGE_URL', 'http://localhost:8080').rstrip('/')
APP_PASSWORD = os.environ.get('APP_PASSWORD', '')
APP_EMAIL = os.environ.get('APP_EMAIL', '').strip().lower()
OWNER_NAME = os.environ.get('OWNER_NAME', '')

SESSION_TTL = 30 * 24 * 3600
_sessions = {}  # token -> expiry epoch
_sessions_lock = threading.Lock()

AUTH_FILE = os.path.join(STORE_DIR, 'auth.json')


def _load_local_auth():
    try:
        with open(AUTH_FILE) as f:
            d = json.load(f)
        if d.get('email') and d.get('salt') and d.get('hash'):
            return d
    except Exception:
        pass
    return None


def _password_required():
    """True when some credential exists (env var or local setup file)."""
    return bool(APP_PASSWORD) or _load_local_auth() is not None


def _hash_password(password, salt=None):
    import hashlib
    if salt is None:
        salt = secrets.token_bytes(16)
    else:
        salt = bytes.fromhex(salt)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=64)
    return salt.hex(), dk.hex()


def _verify_password(password, salt_hex, hash_hex):
    import hashlib, hmac
    try:
        _, dk = _hash_password(password, salt_hex)
        return hmac.compare_digest(dk, hash_hex)
    except Exception:
        return False


def _new_session():
    tok = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[tok] = time.time() + SESSION_TTL
    return tok


def _valid_session(handler):
    cookies = handler.headers.get('Cookie') or ''
    m = re.search(r'momentum_session=([A-Za-z0-9_\-]+)', cookies)
    if not m:
        return False
    with _sessions_lock:
        exp = _sessions.get(m.group(1), 0)
        if exp < time.time():
            _sessions.pop(m.group(1), None)
            return False
        return True


def _drop_session(handler):
    cookies = handler.headers.get('Cookie') or ''
    m = re.search(r'momentum_session=([A-Za-z0-9_\-]+)', cookies)
    if m:
        with _sessions_lock:
            _sessions.pop(m.group(1), None)


def _check_password(given):
    return bool(APP_PASSWORD) and secrets.compare_digest(given or '', APP_PASSWORD)


def _check_email(given):
    if not APP_EMAIL:
        return True  # email collected for identity; password is the gate
    return (given or '').strip().lower() == APP_EMAIL


def authed(handler):
    if not _password_required():
        return True
    if _valid_session(handler):
        return True
    auth = handler.headers.get('Authorization') or ''
    if not auth.startswith('Basic '):
        return False
    import base64
    try:
        decoded = base64.b64decode(auth[6:]).decode()
    except Exception:
        return False
    _, _, given = decoded.partition(':')
    return _check_password(given)

_lock = threading.Lock()
_data_cache = {'ts': 0, 'data': None}

INTENTS = ['nudge', 'escalate', 'update', 'close_loop', 'align', 'unblock']

SYSTEM_PROMPT = """You are the user's AI chief of staff for their WhatsApp work groups.
Draft replies THEY would send: punchy, concise, no fluff, confident but never rude.
They are senior — replies should drive action, name owners where clear, and keep groups moving.
Never fabricate metrics or commitments. Keep each reply 1-4 sentences unless a list is natural.
Return STRICT JSON only: [{"label": "...", "tone": "...", "text": "..."}] with 4-6 drafts.
"label" is 2-4 words naming the intent. "tone" is one word like direct/diplomatic/firm/warm/urgent."""


def do_login(payload):
    email = (payload.get('email') or '').strip().lower()
    password = payload.get('password') or ''
    if APP_PASSWORD:
        if _check_email(payload.get('email')) and _check_password(password):
            return {'token': _new_session()}
        return {'error': 'wrong email or password'}
    local = _load_local_auth()
    if not local:
        return {'error': 'no sign-in set up yet — create one first'}
    if email == local['email'] and _verify_password(password, local['salt'], local['hash']):
        return {'token': _new_session()}
    return {'error': 'wrong email or password'}


def do_setup(payload):
    """First-run credentials creation. Only works when nothing is set yet."""
    if _password_required():
        return {'error': 'sign-in already exists'}
    email = (payload.get('email') or '').strip().lower()
    pw = payload.get('password') or ''
    confirm = payload.get('confirm') or ''
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return {'error': 'enter a valid email address'}
    if len(pw) < 10:
        return {'error': 'password needs at least 10 characters'}
    if pw != confirm:
        return {'error': "passwords don't match — type them again"}
    salt, h = _hash_password(pw)
    try:
        os.makedirs(STORE_DIR, exist_ok=True)
        fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'email': email, 'salt': salt, 'hash': h,
                       'created': datetime.datetime.now(datetime.timezone.utc).isoformat()}, f)
    except Exception as e:
        return {'error': f'could not save credentials ({e})'}
    return {'token': _new_session()}


def auth_state():
    if _password_required():
        return {'mode': 'login'}
    return {'mode': 'setup'}


LOGIN_PAGE = os.path.join(FRONTEND_DIR, 'login.html')


def load_data():
    with _lock:
        if not os.path.exists(DATA_FILE):
            return {'tasks': [], 'map': [], 'bulletins': [], 'days': [], 'week': None}
        mtime = os.path.getmtime(DATA_FILE)
        if _data_cache['data'] is None or mtime > _data_cache['ts']:
            _data_cache['data'] = json.load(open(DATA_FILE))
            _data_cache['ts'] = mtime
        return _data_cache['data']


def get_task(task_id):
    for t in load_data().get('tasks', []):
        if t['id'] == task_id:
            return t
    return None


def resolve_jid(group_name):
    gjids = load_data().get('group_jids') or {}
    jid = gjids.get((group_name or '').strip().lower())
    if jid:
        return jid
    try:
        conn = sqlite3.connect(MESSAGES_DB)
        row = conn.execute(
            "SELECT jid FROM chats WHERE LOWER(TRIM(name))=? AND jid LIKE '%@g.us' LIMIT 1",
            [(group_name or '').strip().lower()]).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def gemini_key():
    """Env first, then the key saved via the in-UI key step (volume file)."""
    key = (os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY') or '').strip()
    if key:
        return key
    try:
        sys.path.insert(0, APP_DIR)
        import export_data as ed
        return ed.get_gemini_key()
    except Exception:
        return ''


def gemini_client():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    try:
        import google.genai as genai
    except Exception:
        return None
    key = gemini_key()
    if not key:
        return None
    return genai.Client(api_key=key)


def validate_gemini_key(key):
    """True if Google accepts the key (cheap list-models ping)."""
    try:
        import google.genai as genai
        client = genai.Client(api_key=(key or '').strip())
        it = client.models.list(config={'page_size': 1})
        next(iter(it), None)
        return True, ''
    except Exception as e:
        msg = str(e)
        if 'API_KEY_INVALID' in msg or 'API key not valid' in msg or '401' in msg or '403' in msg:
            return False, 'Google rejected this key — check it and try again'
        return False, f'could not reach Google ({type(e).__name__}) — try again'


def save_gemini_key(key):
    try:
        os.makedirs(STORE_DIR, exist_ok=True)
        fd = os.open(os.path.join(STORE_DIR, 'gemini.key'),
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write((key or '').strip())
        return True
    except Exception:
        return False


def build_thread_context(t):
    msgs = t.get('messages') or []
    lines = []
    for m in msgs[-10:]:
        d = parse_ts(m.get('time'))
        ts = d.strftime('%d %b %H:%M') if d else ''
        lines.append(f"[{ts}] {m.get('sender','?')}: {m.get('body','')}")
    parts = [
        f"Thread title: {t.get('title','')}",
        f"Group(s): {', '.join(t.get('groups') or [t.get('group','')])}",
        f"Status: {t.get('status','')} · age: {t.get('age','')}",
    ]
    if t.get('recap'):
        parts.append(f"Recap: {t['recap'][:900]}")
    elif t.get('fact'):
        parts.append(f"Facts: {t['fact'][:600]}")
    if t.get('participants'):
        parts.append(f"Participants: {', '.join(t['participants'])}")
    parts.append("Recent messages:\n" + ("\n".join(lines) or "(none captured)"))
    return "\n".join(parts)


def parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace(' ', 'T'))
    except Exception:
        return None


NUDGE_OVERRIDES_FILE = 'nudge_overrides.json'


def load_nudge_overrides():
    """{task_id: {status: snoozed|closed, ts}}; empty dict on any error."""
    try:
        with open(os.path.join(STORE_DIR, NUDGE_OVERRIDES_FILE)) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_nudge_override(task_id, action):
    """Persist a dismiss ({task_id: {status, ts}}). Returns {'ok': True} or {'error': ...}."""
    if action not in ('snoozed', 'snooze', 'closed', 'mark_closed'):
        return {'error': 'action must be snooze or mark_closed'}
    status = 'snoozed' if action in ('snoozed', 'snooze') else 'closed'
    if not task_id:
        return {'error': 'nudge_id or task_id required'}
    try:
        os.makedirs(STORE_DIR, exist_ok=True)
        path = os.path.join(STORE_DIR, NUDGE_OVERRIDES_FILE)
        try:
            with open(path) as f:
                d = json.load(f)
            if not isinstance(d, dict):
                d = {}
        except Exception:
            d = {}
        d[str(task_id)] = {'status': status,
                           'ts': datetime.datetime.now(
                               datetime.timezone.utc).isoformat()}
        with open(path, 'w') as f:
            json.dump(d, f)
        return {'ok': True, 'status': status}
    except Exception as e:
        return {'error': str(e)}


def api_nudges(window_min=5):
    """Scan recent tasks/messages for high-bar nudges. Stdlib only, no LLM."""
    try:
        sys.path.insert(0, APP_DIR)
        from nudge_scan import scan_nudges
    except Exception as e:
        return {'error': f'nudge scan unavailable ({e})'}
    try:
        window_min = max(1, min(int(window_min), 120))
    except (TypeError, ValueError):
        window_min = 5
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        tasks = load_data().get('tasks', []) or []
    except Exception:
        tasks = []
    messages = []
    for t in tasks:
        for m in (t.get('messages') or []):
            mm = dict(m)
            mm.setdefault('group', t.get('group', ''))
            mm.setdefault('task_id', t.get('id', ''))
            messages.append(mm)
    try:
        nudges = scan_nudges(tasks, messages, now, window_min=window_min,
                             owner_name=OWNER_NAME,
                             overrides=load_nudge_overrides())
    except Exception as e:
        return {'error': f'nudge scan failed ({e})'}
    return {'nudges': nudges, 'window_min': window_min,
            'scanned_at': now.isoformat(), 'bar': 'high'}


def gen_replies(payload):
    t = get_task(payload.get('task_id', ''))
    if not t:
        return {'error': f"unknown task_id '{payload.get('task_id')}'"}
    client = gemini_client()
    if not client:
        return {'error': 'GEMINI_API_KEY is not set on this instance'}

    intents = payload.get('intents') or []
    hint = (payload.get('hint') or '').strip()
    who = f" (the sender is {OWNER_NAME})" if OWNER_NAME else ""
    prompt = build_thread_context(t)
    prompt += "\n\n=== TASK ===\n"
    prompt += f"Write 4-6 reply options the user{who} could send to this group right now.\n"
    if intents:
        prompt += f"Cover these intents across the set: {', '.join(intents)}.\n"
    else:
        prompt += ("Vary intent across the set — e.g. a nudge to the owner, a crisp status ask, "
                   "an escalation-ready version, a closing-the-loop ack, an alignment proposal.\n")
    if hint:
        prompt += f"The user's guidance on what they want to convey: \"{hint}\" — reflect it in most drafts.\n"
    prompt += "JSON array only, no markdown fences."

    try:
        import time as _t
        raw = None
        last_err = None
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model='gemini-3-flash-preview',
                    contents=prompt,
                    config={'system_instruction': SYSTEM_PROMPT, 'temperature': 0.7},
                )
                raw = (resp.text or '').strip()
                if raw:
                    break
            except Exception as e:
                last_err = e
                if attempt < 2:
                    _t.sleep(1.5 * (attempt + 1))
        if not raw:
            return {'error': f'gemini failed after 3 attempts: {last_err}'}
        raw = re.sub(r'^```(?:json)?|```$', '', raw, flags=re.M).strip()
        start, end = raw.find('['), raw.rfind(']')
        drafts = json.loads(raw[start:end + 1])
        drafts = [{'label': str(d.get('label', ''))[:40], 'tone': str(d.get('tone', ''))[:20],
                   'text': str(d.get('text', '')).strip()}
                  for d in drafts if isinstance(d, dict) and d.get('text')]
        return {'drafts': drafts[:6]}
    except Exception as e:
        return {'error': f'gemini failed: {e}'}


def compose_message(t, text, attach_context):
    if not attach_context:
        return text
    th = t.get('theme_label') or ''
    bits = [f"*Context — {t.get('title','')}*"]
    meta = []
    if th:
        meta.append(th)
    if t.get('age'):
        meta.append(f"{t['age']} old")
    if t.get('groups'):
        meta.append(t['groups'][0])
    if meta:
        bits.append(' · '.join(meta))
    body = (t.get('recap') or t.get('one_liner') or t.get('fact') or '').strip()
    if body:
        first_lines = [l.strip() for l in re.split(r'[\n•]+', body) if l.strip()][:2]
        snippet = ' '.join(first_lines)[:280]
        bits.append(snippet + ('…' if len(body) > 280 else ''))
    sep = '\n' + '—' * 12 + '\n'
    return text.rstrip() + sep + '\n'.join(bits)


_last_send = {}


def do_send(payload):
    t = get_task(payload.get('task_id', ''))
    if not t:
        return {'error': 'unknown task_id'}
    text = (payload.get('text') or '').strip()
    if not text:
        return {'error': 'empty message'}
    groups = t.get('groups') or ([t.get('group')] if t.get('group') else [])
    if not groups:
        return {'error': 'task has no source group'}
    jid = resolve_jid(groups[0])
    if not jid:
        return {'error': f"could not resolve JID for group '{groups[0]}'"}

    final = compose_message(t, text, bool(payload.get('attach_context', True)))

    key = (jid, text[:80])
    now = time.time()
    if now - _last_send.get(key, 0) < 60:
        return {'error': 'duplicate guard: same message sent <60s ago'}
    _last_send[key] = now

    result = {'jid': jid, 'group': groups[0], 'mode': 'live' if LIVE else 'dry-run',
              'composed': final}
    if LIVE:
        try:
            req = urlopen(urllib_request(jid, final), timeout=15)
            ok = req.status == 200
            result['sent'] = ok
            if not ok:
                result['error'] = f'bridge returned {req.status}'
        except URLError as e:
            result['sent'] = False
            result['error'] = f'bridge unreachable ({e.reason})'
    else:
        result['sent'] = True

    try:
        os.makedirs(os.path.dirname(SENT_LOG) or '.', exist_ok=True)
        with open(SENT_LOG, 'a') as f:
            f.write(json.dumps({'ts': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                'task_id': t['id'], **result}, default=str) + '\n')
    except Exception:
        pass
    return result


def urllib_request(jid, message):
    import urllib.request as u
    req = u.Request(f'{BRIDGE_URL}/api/send',
                    data=json.dumps({'recipient': jid, 'message': message}).encode(),
                    headers={'Content-Type': 'application/json'}, method='POST')
    return req


def bridge_ok():
    try:
        r = urlopen(f'{BRIDGE_URL}/api/health', timeout=3)
        return r.status == 200
    except HTTPError as e:
        # An HTTP answer is decisive: the bridge has no /api/health route,
        # so the Go default mux answers 404 (verified live) = bridge is up.
        # Any other error status (500 from a broken bridge, 401, ...) is
        # NOT alive — never mask it, and never fall through to the TCP
        # probe below (a listener on a colliding port would lie).
        return e.code == 404
    except Exception:
        # last resort: is anything listening on the bridge port?
        try:
            import socket
            from urllib.parse import urlparse
            parts = urlparse(BRIDGE_URL)
            s = socket.create_connection((parts.hostname or '127.0.0.1',
                                          parts.port or 8080), timeout=3)
            s.close()
            return True
        except Exception:
            return False


_client_log_hits = {}


def client_log_ok(ip):
    now = time.time()
    hits = _client_log_hits.get(ip, [])
    hits = [h for h in hits if now - h < 60]
    if len(hits) >= 20:
        return False
    hits.append(now)
    _client_log_hits[ip] = hits
    return True


def do_client_log(payload, ip):
    try:
        msg = str(payload.get('message') or '')[:300]
        stack = str(payload.get('stack') or '')[:800]
        url = str(payload.get('url') or '')[:200]
        ua = str(payload.get('ua') or '')[:150]
        print(f"[client-error] ip={ip} url={url} ua={ua} msg={msg} stack={stack}", flush=True)
    except Exception:
        pass
    return {'ok': True}


def ports_collide(api_port, bridge_url):
    """True when the API and the bridge would bind the same TCP port
    (the bridge default is 8080; the API must live elsewhere)."""
    try:
        port = urlparse(bridge_url or '').port or 8080
    except Exception:
        port = 8080
    try:
        return int(api_port) == int(port)
    except (TypeError, ValueError):
        return False


def read_qr():
    """Latest QR as {'qr','updated','age_s','stale','trusted'}.

    Primary: bracketed [QR_BEGIN QR_AT=..] blocks per the G3 gate contract
    (newest COMPLETE block wins; mid-write tails without QR_END are
    ignored so the previous code survives). Fallback: legacy bare
    QR_BEGIN/END blocks from older bridges (untrusted, file-mtime age,
    still mid-write safe). qr is None when no complete block exists.
    """
    try:
        st = os.stat(BRIDGE_LOG)
        with open(BRIDGE_LOG, errors='ignore') as f:
            txt = f.read()
    except Exception:
        return {'qr': None, 'updated': None, 'age_s': None,
                'stale': True, 'trusted': False}
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        sys.path.insert(0, APP_DIR)
        from journey_gates import (parse_qr_cache, qr_age_seconds,
                                   QR_REFRESH_WINDOW_S)
        cache = parse_qr_cache(txt)
        if cache.get('code'):
            age = qr_age_seconds(cache, now)
            return {
                'qr': cache['code'],
                'updated': cache.get('issued_at_raw'),
                'age_s': age,
                'stale': (age is None) or (age > QR_REFRESH_WINDOW_S),
                'trusted': cache.get('trusted', False),
            }
    except Exception:
        pass
    # Legacy bare blocks (pre-QR_AT bridges). Bracket-aware: a bare
    # QR_BEGIN followed only by a bracketed [QR_END] is still mid-write.
    blocks = re.findall(r'(?<!\[)QR_BEGIN\n(.*?)(?<!\[)QR_END(?!\])',
                        txt, re.DOTALL)
    for block in reversed(blocks):
        lines = [l.rstrip() for l in block.strip().splitlines()
                 if l.strip()]
        if len(lines) >= 5:
            mtime = datetime.datetime.fromtimestamp(
                st.st_mtime, datetime.timezone.utc)
            age = int((now - mtime).total_seconds())
            return {'qr': '\n'.join(lines),
                    'updated': mtime.isoformat(), 'age_s': age,
                    'stale': age > 20, 'trusted': False}
    return {'qr': None, 'updated': None, 'age_s': None,
            'stale': True, 'trusted': False}


def read_provision():
    # STORE first: PROVISION_FILE (/tmp) is machine-global, so a scratch
    # stack would otherwise clobber the live stack's stage (observed:
    # a DEMO run flipped the live portal to ready). The per-stack STORE
    # file is the owning stack's truth; /tmp is only a fallback mirror.
    for p in (os.path.join(STORE_DIR, 'provision.json'), PROVISION_FILE):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            continue
    return {'stage': 'starting', 'detail': 'booting', 'messages': 0}


def public_health():
    """Unauthenticated liveness for the Railway healthcheck, the boot
    loader's pre-login probe, and the audit loop. Minimal by design:
    stage + build only — never owner identity, key presence, counts,
    or bridge URL. Full detail stays behind auth on /api/status."""
    st = read_provision()
    return {
        'ok': True,
        'stage': st.get('stage', 'starting'),
        'build': os.environ.get('GIT_SHA', 'unknown'),
        'updated': st.get('updated'),
    }


def read_build_heartbeat():
    """Live build progress beacon written by build_data.py (G5 spec). Returns
    {} when no build has ever reported (e.g. fresh boot, old build running)."""
    try:
        with open(os.path.join(STORE_DIR, 'build_heartbeat.json')) as f:
            hb = json.load(f)
        return hb if isinstance(hb, dict) else {}
    except Exception:
        return {}


# QR-sync progress history (process-local): (epoch, count) samples fed to
# journey_gates.sync_eta_estimate so /api/status carries an honest,
# never-early ETA. Two lanes — message counts while syncing, heartbeat
# thread counts while analyzing — never mixed.
_SYNC_MSG_SAMPLES = []
_SYNC_BUILD_SAMPLES = []
_SYNC_SAMPLES_MAX = 120


def _track_sample(buf, count):
    try:
        count = float(count)
    except (TypeError, ValueError):
        return buf
    now = time.time()
    buf.append((now, count))
    cutoff = now - 600
    while buf and buf[0][0] < cutoff:
        buf.pop(0)
    while len(buf) > _SYNC_SAMPLES_MAX:
        buf.pop(0)
    return buf


def sync_progress_fields(st, data, heartbeat):
    """Hold + ETA + partial-portal fields for /api/status.

    Never fakes: percent/eta are None unless a measurable rate and a
    known total exist in the same unit; the hold countdown always runs
    from the server-stamped scan moment.
    """
    try:
        sys.path.insert(0, APP_DIR)
        from journey_gates import (SYNC_MIN_HOLD_S, partial_sections,
                                   scan_hold_state, sync_eta_estimate)
    except Exception:
        return {}
    now = datetime.datetime.now(datetime.timezone.utc)
    sync_started_at = (st or {}).get('sync_started_at')
    hold = scan_hold_state(sync_started_at, now)
    stage = (st or {}).get('stage', 'starting')
    try:
        messages = float((st or {}).get('messages', 0) or 0)
    except (TypeError, ValueError):
        messages = 0.0
    percent, eta_s, rate = None, None, 0.0
    if stage == 'syncing':
        _track_sample(_SYNC_MSG_SAMPLES, messages)
        # totals are unknown while syncing: rate only, never a fake ETA
        samples = [(datetime.datetime.fromtimestamp(t, datetime.timezone.utc), c)
                   for t, c in _SYNC_MSG_SAMPLES]
        est = sync_eta_estimate(samples, None, now)
        rate = est['rate_per_s']
    elif stage == 'analyzing':
        try:
            done = float(heartbeat.get('done'))
            total = float(heartbeat.get('total'))
        except (TypeError, ValueError):
            done, total = None, None
        if done is not None and total:
            _track_sample(_SYNC_BUILD_SAMPLES, done)
            samples = [(datetime.datetime.fromtimestamp(t, datetime.timezone.utc), c)
                       for t, c in _SYNC_BUILD_SAMPLES]
            est = sync_eta_estimate(samples, total, now)
            percent, eta_s, rate = est['percent'], est['eta_s'], est['rate_per_s']
    try:
        sections = partial_sections(data if isinstance(data, dict) else {})
    except Exception:
        sections = {'shell_ready': True, 'partial': False, 'sections': {}}
    progress = None
    try:
        progress = (data or {}).get('progress')
    except Exception:
        progress = None
    return {
        'sync_started_at': sync_started_at,
        'sync_elapsed_s': hold.get('elapsed_s', 0),
        'sync_hold_s': SYNC_MIN_HOLD_S,
        'sync_hold_remaining_s': hold.get('remaining_s', 0),
        'sync_rate_per_s': round(rate, 1),
        'sync_percent': percent,
        'sync_eta_s': None if eta_s is None else round(eta_s, 1),
        'partial': bool((data or {}).get('partial', False)),
        'progress': progress,
        'sections': sections.get('sections', {}),
    }


def is_persistent_store(path):
    """True only when data lives on the mounted Volume (/data).

    A missing/mis-mounted volume must read False (never silently
    ephemeral): the entrypoint warns, provision keeps booting for local
    runs, and the flag is surfaced on /api/status for the deploy check.
    """
    try:
        return bool(path) and path.startswith('/data') \
            and os.path.isdir(path) and os.access(path, os.W_OK)
    except Exception:
        return False


def api_status():
    st = read_provision()
    d = {}
    try:
        d = load_data()
        ntasks = len(d.get('tasks') or [])
    except Exception:
        ntasks = 0
    stage = st.get('stage', 'starting')
    if stage == 'ready' and not ntasks:
        stage = 'analyzing'
    # On Railway, persistence == data lives on the mounted Volume (/data).
    # A writable dir elsewhere (e.g. container layer) vanishes on redeploy.
    persistent = is_persistent_store(STORE_DIR)
    heartbeat = read_build_heartbeat()
    out = {
        'stage': stage,
        'detail': st.get('detail', ''),
        'bridge': bridge_ok(),
        'bridge_url': BRIDGE_URL,
        'live_mode': LIVE,
        'demo_mode': DEMO,
        'gemini': bool(gemini_key()),
        'owner': OWNER_NAME,
        'auth': bool(APP_PASSWORD),
        'persistent': persistent,
        'build': os.environ.get('GIT_SHA', 'unknown'),
        'messages': st.get('messages', 0),
        'tasks': ntasks,
        'updated': st.get('updated'),
        'build_progress': heartbeat,
    }
    try:
        out.update(sync_progress_fields(st, d, heartbeat))
    except Exception:
        pass
    return out


def trigger_rebuild():
    log = os.path.join(STORE_DIR, 'rebuild.log')
    try:
        os.makedirs(STORE_DIR, exist_ok=True)
        fh = open(log, 'a')
        subprocess.Popen([sys.executable, os.path.join(APP_DIR, 'build_data.py')],
                         cwd=APP_DIR, stdout=fh, stderr=subprocess.STDOUT,
                         start_new_session=True)
        return {'started': True, 'log': log}
    except Exception as e:
        return {'error': str(e)}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=FRONTEND_DIR, **kw)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        # Local mission control must never serve stale truth: a normal
        # reload always revalidates instead of needing a hard refresh.
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def _json(self, obj, code=200):
        try:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _post_body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n) or b'{}') if n else {}

    def _guard(self):
        if authed(self):
            return True
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Momentum"')
        self.send_header('Content-Type', 'application/json')
        body = b'{"error":"unauthorized"}'
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return False

    def _serve_login(self):
        try:
            body = open(LOGIN_PAGE, 'rb').read()
        except Exception:
            body = b'<h1>login.html missing</h1>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _set_session(self, token):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Set-Cookie',
                         f'momentum_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}')
        body = b'{"ok":true}'
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        # Public liveness: Railway healthcheck + pre-login probes have no
        # session. Serves before every gate (even first-run setup).
        # /api/status stays authed: the boot loader's 401 -> login redirect
        # depends on it.
        if self.path == '/api/health':
            return self._json(public_health())
        if self.path == '/login':
            return self._serve_login()
        if self.path == '/api/auth-state':
            return self._json(auth_state())
        if not _password_required():
            # First run: the ONLY thing available is the setup page.
            if self.path.startswith('/api/'):
                return self._json({'error': 'no sign-in yet — create one first'}, 403)
            return self._serve_login()
        if self.path == '/' and not authed(self):
            return self._serve_login()
        if not self._guard():
            return
        if self.path == '/api/status':
            return self._json(api_status())
        if self.path == '/api/owner':
            return self._json({'owner': OWNER_NAME})
        if self.path == '/api/nudges' or self.path.startswith('/api/nudges?'):
            q = parse_qs(urlparse(self.path).query)
            try:
                window = int((q.get('window') or ['5'])[0])
            except (TypeError, ValueError):
                window = 5
            out = api_nudges(window)
            return self._json(out, 200 if 'error' not in out else 500)
        if self.path == '/api/qr':
            return self._json(read_qr())
        return super().do_GET()

    def do_POST(self):
        if self.path == '/api/client-log':
            if not client_log_ok(self.client_address[0]):
                return self._json({'ok': False}, 429)
            try:
                out = do_client_log(self._post_body(), self.client_address[0])
            except json.JSONDecodeError:
                return self._json({'error': 'bad JSON'}, 400)
            return self._json(out)
        if self.path == '/api/setup':
            try:
                out = do_setup(self._post_body())
            except json.JSONDecodeError:
                return self._json({'error': 'bad JSON'}, 400)
            if 'token' in out:
                return self._set_session(out['token'])
            return self._json(out, 400)
        if not _password_required():
            return self._json({'error': 'no sign-in yet — create one first'}, 403)
        if self.path == '/api/login':
            try:
                out = do_login(self._post_body())
            except json.JSONDecodeError:
                return self._json({'error': 'bad JSON'}, 400)
            if 'token' in out:
                return self._set_session(out['token'])
            return self._json(out, 401)
        if self.path == '/api/gemini-key':
            # Open only until a key exists (first-run step); afterwards, authed.
            if gemini_key() and not authed(self):
                return self._json({'error': 'a key is already set — sign in to replace it'}, 403)
            try:
                body = self._post_body()
                if body.get('skip'):
                    try:
                        os.makedirs(STORE_DIR, exist_ok=True)
                        open(os.path.join(STORE_DIR, '.skip-key'), 'w').write('1')
                    except Exception:
                        return self._json({'error': 'could not save that choice'}, 500)
                    return self._json({'ok': True, 'skipped': True})
                key = (body.get('key') or '').strip()
            except json.JSONDecodeError:
                return self._json({'error': 'bad JSON'}, 400)
            ok, err = validate_gemini_key(key)
            if not ok:
                return self._json({'error': err or 'invalid key'}, 400)
            if not save_gemini_key(key):
                return self._json({'error': 'could not save the key on this instance'}, 500)
            return self._json({'ok': True})
        if self.path == '/api/logout':
            _drop_session(self)
            return self._json({'ok': True})
        if not self._guard():
            return
        try:
            payload = self._post_body()
            if self.path == '/api/replies':
                out = gen_replies(payload)
            elif self.path == '/api/send':
                out = do_send(payload)
            elif self.path == '/api/rebuild':
                out = trigger_rebuild()
            elif self.path == '/api/nudges/dismiss':
                action = payload.get('action', '')
                key = payload.get('task_id') or payload.get('nudge_id') or ''
                out = save_nudge_override(key, action)
            else:
                return self._json({'error': 'not found'}, 404)
            return self._json(out, 200 if 'error' not in out else 400)
        except json.JSONDecodeError:
            return self._json({'error': 'bad JSON'}, 400)
        except Exception as e:
            return self._json({'error': str(e)}, 500)


if __name__ == '__main__':
    if ports_collide(PORT, BRIDGE_URL):
        print(f'!! PORT collision: API :{PORT} == bridge {BRIDGE_URL} — '
              f'the bridge will fail to bind (or the API will). Move the '
              f'API off with PORT=8099 (local) or set WA_BRIDGE_PORT.',
              flush=True)
    os.chdir(FRONTEND_DIR)
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f"""
  Momentum API
     UI + API   ->  :{PORT}/
     Bridge     ->  {BRIDGE_URL}  ({'reachable' if bridge_ok() else 'offline'})
     Store      ->  {STORE_DIR}
     Auth       ->  {'first-run setup (no credentials yet)' if not _password_required() else ('env password' if APP_PASSWORD else 'local setup file')}
     Mode       ->  {'LIVE — messages will be delivered' if LIVE else 'DRY-RUN — sends logged only (set MOMENTUM_LIVE=1 to deliver)'}
""", flush=True)
    srv.serve_forever()
