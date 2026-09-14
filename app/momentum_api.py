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
    except HTTPError:
        return True  # bridge answers (no /api/health route) = alive
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


def read_qr():
    """Latest QR block from the bridge log (emitted between QR_BEGIN/END)."""
    try:
        st = os.stat(BRIDGE_LOG)
        with open(BRIDGE_LOG, errors='ignore') as f:
            txt = f.read()
    except Exception:
        return None, None
    if 'QR_BEGIN' not in txt:
        return None, None
    block = txt.rsplit('QR_BEGIN', 1)[1]
    if 'QR_END' in block:
        block = block.split('QR_END')[0]
    lines = [l.rstrip() for l in block.strip().splitlines() if l.strip()]
    if len(lines) < 5:
        return None, None
    return '\n'.join(lines), datetime.datetime.fromtimestamp(
        st.st_mtime, datetime.timezone.utc).isoformat()


def read_provision():
    for p in (PROVISION_FILE, os.path.join(STORE_DIR, 'provision.json')):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            continue
    return {'stage': 'starting', 'detail': 'booting', 'messages': 0}


def api_status():
    st = read_provision()
    try:
        d = load_data()
        ntasks = len(d.get('tasks') or [])
    except Exception:
        ntasks = 0
    stage = st.get('stage', 'starting')
    if stage == 'ready' and not ntasks:
        stage = 'analyzing'
    store_ok = os.path.isdir(STORE_DIR) and os.access(STORE_DIR, os.W_OK)
    # On Railway, persistence == data lives on the mounted Volume (/data).
    # A writable dir elsewhere (e.g. container layer) vanishes on redeploy.
    persistent = STORE_DIR.startswith('/data') and store_ok
    return {
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
    }


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
        if self.path == '/api/health' or self.path == '/api/status':
            return self._json(api_status())
        if self.path == '/api/owner':
            return self._json({'owner': OWNER_NAME})
        if self.path == '/api/qr':
            qr, updated = read_qr()
            return self._json({'qr': qr, 'updated': updated})
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
            else:
                return self._json({'error': 'not found'}, 404)
            return self._json(out, 200 if 'error' not in out else 400)
        except json.JSONDecodeError:
            return self._json({'error': 'bad JSON'}, 400)
        except Exception as e:
            return self._json({'error': str(e)}, 500)


if __name__ == '__main__':
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
