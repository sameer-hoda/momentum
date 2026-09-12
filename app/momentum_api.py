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

PORT = int(os.environ.get('PORT', '8082'))
LIVE = os.environ.get('MOMENTUM_LIVE', '0') == '1'
DEMO = os.environ.get('DEMO_MODE', '0') == '1'
BRIDGE_URL = os.environ.get('WA_BRIDGE_URL', 'http://localhost:8080').rstrip('/')
APP_PASSWORD = os.environ.get('APP_PASSWORD', '')
OWNER_NAME = os.environ.get('OWNER_NAME', '')

_lock = threading.Lock()
_data_cache = {'ts': 0, 'data': None}

INTENTS = ['nudge', 'escalate', 'update', 'close_loop', 'align', 'unblock']

SYSTEM_PROMPT = """You are the user's AI chief of staff for their WhatsApp work groups.
Draft replies THEY would send: punchy, concise, no fluff, confident but never rude.
They are senior — replies should drive action, name owners where clear, and keep groups moving.
Never fabricate metrics or commitments. Keep each reply 1-4 sentences unless a list is natural.
Return STRICT JSON only: [{"label": "...", "tone": "...", "text": "..."}] with 4-6 drafts.
"label" is 2-4 words naming the intent. "tone" is one word like direct/diplomatic/firm/warm/urgent."""


def authed(handler):
    if not APP_PASSWORD:
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
    return secrets.compare_digest(given, APP_PASSWORD)


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
    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not key:
        return None
    return genai.Client(api_key=key)


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
        'gemini': bool(os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')),
        'owner': OWNER_NAME,
        'auth': bool(APP_PASSWORD),
        'persistent': persistent,
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

    def do_GET(self):
        if not self._guard():
            return
        if self.path == '/api/health' or self.path == '/api/status':
            return self._json(api_status())
        if self.path == '/api/owner':
            return self._json({'owner': OWNER_NAME})
        return super().do_GET()

    def do_POST(self):
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
     Auth       ->  {'password protected' if APP_PASSWORD else 'OPEN (set APP_PASSWORD!)'}
     Mode       ->  {'LIVE — messages will be delivered' if LIVE else 'DRY-RUN — sends logged only (set MOMENTUM_LIVE=1 to deliver)'}
""", flush=True)
    srv.serve_forever()
