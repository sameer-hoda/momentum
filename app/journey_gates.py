"""Journey V2 gate contracts (G0..G6) — pure functions, stdlib only.

TDD GREEN phase for tests/test_journey_gates.py. No network, no LLM, no
genai import in this module. Only side effects: key-cache file read/write
(in-memory here) and read-only sqlite opens.
"""
import datetime
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

QR_REFRESH_WINDOW_S = 20
SYNC_STABLE_POLLS = 3
STALL_CALL_S = 60
SYNC_MIN_HOLD_S = 10

_KEY_CACHE = {}

# ------------------------------------------------------------------ G3 PAIRED

_QR_BLOCK_RE = re.compile(r'\[QR_BEGIN([^\]]*)\](.*?)\[QR_END\]',
                           re.DOTALL)
_QR_AT_RE = re.compile(r'QR_AT=([^\s\]]+)')

_PAIRED_HINTS = ('connected', 'logged in')
_LOGOUT_HINTS = ('logged out', 'log out', 'logout')


def _parse_ts(value):
    """Parse an ISO-8601 timestamp (trailing Z ok) -> aware datetime or None."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=datetime.timezone.utc)
        return value
    try:
        text = str(value).strip()
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def parse_qr_cache(log_text):
    """Return the newest COMPLETE QR block: code + issued_at + trusted.

    Mid-write (unterminated, no QR_END) tails are ignored so the previous
    complete code survives. Blocks without a QR_AT marker are untrusted.
    """
    log_text = log_text or ''
    best = {'code': None, 'issued_at': None, 'issued_at_raw': None,
            'trusted': False}
    for match in _QR_BLOCK_RE.finditer(log_text):
        attrs, body = match.group(1), match.group(2)
        at_match = _QR_AT_RE.search(attrs or '')
        raw = at_match.group(1) if at_match else None
        best = {'code': body.strip(), 'issued_at': _parse_ts(raw),
                'issued_at_raw': raw, 'trusted': raw is not None}
    return best


def qr_age_seconds(cache, now):
    """Age of the cached QR in whole seconds (None when untrusted/unknown)."""
    issued = (cache or {}).get('issued_at')
    if issued is None:
        return None
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    now = _parse_ts(now)
    issued = _parse_ts(issued)
    if now is None or issued is None:
        return None
    return int((now - issued).total_seconds())


def _log_paired_hint(log_text):
    low = (log_text or '').lower()
    paired_idx = max((low.rfind(h) for h in _PAIRED_HINTS), default=-1)
    if paired_idx < 0:
        return False
    logout_idx = max((low.rfind(h) for h in _LOGOUT_HINTS), default=-1)
    return logout_idx < paired_idx


def gate_g3(message_count=0, log_text='', now=None):
    """G3 PAIRED: count>0 OR connected/logged-in hint (no later logout)."""
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    cache = parse_qr_cache(log_text or '')
    age = qr_age_seconds(cache, now)
    qr_code = cache.get('code') or ''
    qr_stale = (age is None) or (age > QR_REFRESH_WINDOW_S)
    if (message_count or 0) > 0:
        return {'pass': True, 'paired_via': 'messages', 'qr_code': qr_code,
                'qr_stale': qr_stale, 'qr_age_s': age,
                'trusted': cache.get('trusted', False)}
    if _log_paired_hint(log_text or ''):
        return {'pass': True, 'paired_via': 'log_hint', 'qr_code': qr_code,
                'qr_stale': qr_stale, 'qr_age_s': age,
                'trusted': cache.get('trusted', False)}
    return {'pass': False, 'paired_via': None, 'qr_code': qr_code,
            'qr_stale': qr_stale, 'qr_age_s': age,
            'trusted': cache.get('trusted', False)}


def qr_etag(code, issued_at):
    """ETag for a QR payload (code + issued-at marker)."""
    body = f'{code}\n{issued_at}'.encode('utf-8')
    return hashlib.sha256(body).hexdigest()


def qr_cached_response(client_etag, code, issued_at):
    """304 when the client's etag matches the current code, else 200."""
    current = qr_etag(code, issued_at)
    if client_etag == current:
        return 304, None
    return 200, {'code': code, 'issued_at': issued_at, 'etag': current}


# ------------------------------------------------------------------ G4 SYNCED

def record_sync_poll(st, count=0, bridge_alive=True, now=None):
    """Fold one 15s poll into sync state. Bridge-down polls pause the streak."""
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    if st is None:
        st = {'last_count': None, 'streak': 0, 'bridge_alive': True,
              'last_time': None}
    state = dict(st)
    state['last_time'] = now
    if not bridge_alive:
        state['bridge_alive'] = False
        return state
    state['bridge_alive'] = True
    if state.get('last_count') == count:
        state['streak'] = int(state.get('streak', 0)) + 1
    else:
        state['streak'] = 1
    state['last_count'] = count
    return state


def sync_passes(st):
    """3 consecutive stable polls AND count>0 AND bridge alive."""
    if not st:
        return False
    return (int(st.get('streak', 0)) >= SYNC_STABLE_POLLS
            and (st.get('last_count') or 0) > 0
            and bool(st.get('bridge_alive')))


def stall_cause(bridge_alive=True, count_changed=False, phone_awake=True,
                net_ok=True, source_has_new=True, stalled_s=0, count=None,
                history_threshold=100000):
    """Name the G4 stall cause. Bridge state wins, then phone, net, source."""
    if not bridge_alive:
        return 'bridge_down'
    if not phone_awake:
        return 'phone_asleep'
    if not net_ok:
        return 'wifi_offline'
    if not source_has_new:
        return 'no_source_messages'
    if count is not None and count >= history_threshold:
        return 'huge_history'
    if count_changed:
        return 'sync_active'
    if (stalled_s or 0) < STALL_CALL_S:
        return 'initial_sync'
    return 'sync_stalled'


# ------------------------------------------------- QR-SYNC hold/ETA/partial

def scan_hold_state(scan_at, now=None, min_hold_s=SYNC_MIN_HOLD_S):
    """Syncing-screen hold after a QR scan: visible ~10s minimum.

    scan_at: ISO-8601 / datetime / epoch of the scan (pairing) event,
    or None when no scan happened yet. Returns {'hold', 'elapsed_s',
    'remaining_s'} — hold is True only while the minimum display has
    not elapsed. None scan_at never holds.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    now = _parse_ts(now)
    scan = _parse_ts(scan_at)
    if scan is None or now is None:
        return {'hold': False, 'elapsed_s': 0, 'remaining_s': 0}
    elapsed = max(0.0, (now - scan).total_seconds())
    remaining = max(0.0, float(min_hold_s) - elapsed)
    return {'hold': remaining > 0, 'elapsed_s': elapsed,
            'remaining_s': remaining}


def sync_eta_estimate(samples, total=None, now=None):
    """Honest sync progress from (time, count) samples + known total.

    samples: [(t, count)] oldest-first; t any _parse_ts input.
    total: expected message/thread count, or None when unknown.
    Returns {'percent', 'eta_s', 'rate_per_s'} — percent/eta are None
    (never faked) when the total is unknown or no rate is measurable;
    eta_s is 0 only at 100%, never early on a stalled sync.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    now = _parse_ts(now)
    pts = []
    for t, count in samples or []:
        dt = _parse_ts(t)
        if dt is None:
            continue
        try:
            pts.append((dt, float(count)))
        except (TypeError, ValueError):
            continue
    pts.sort(key=lambda p: p[0])
    rate = 0.0
    if len(pts) >= 2:
        dt = (pts[-1][0] - pts[0][0]).total_seconds()
        if dt > 0:
            rate = max(0.0, (pts[-1][1] - pts[0][1]) / dt)
    latest = pts[-1][1] if pts else 0.0
    if not total or total <= 0:
        return {'percent': None, 'eta_s': None, 'rate_per_s': rate}
    percent = min(100.0, latest / float(total) * 100.0)
    if latest >= total:
        return {'percent': 100.0, 'eta_s': 0, 'rate_per_s': rate}
    if rate > 0:
        return {'percent': percent,
                'eta_s': max(0.0, (float(total) - latest) / rate),
                'rate_per_s': rate}
    return {'percent': percent, 'eta_s': None, 'rate_per_s': rate}


def partial_sections(payload):
    """Map a frontend_data snapshot to per-section ready/loading states.

    The shell is always usable (shell_ready True) so cached threads stay
    readable while the rest loads; empty sections report 'loading' and
    render skeleton shimmer instead of blank space.
    """
    payload = payload if isinstance(payload, dict) else {}
    tasks = payload.get('tasks')
    board_map = payload.get('map')
    bulletins = payload.get('bulletins')
    health = payload.get('health_wall')
    return {
        'shell_ready': True,
        'partial': bool(payload.get('partial', False)),
        'sections': {
            'threads': 'ready' if isinstance(tasks, list) and tasks
                       else 'loading',
            'map': 'ready' if isinstance(board_map, list) and board_map
                   else 'loading',
            'bulletins': 'ready' if isinstance(bulletins, list)
                         and bulletins else 'loading',
            'health': 'ready' if health else 'loading',
        },
    }


# ----------------------------------------------------------------- G5 ANALYZE

def validate_analysis_output(path, last_sync_change=None):
    """frontend_data.json exists + parses + tasks>0 + fresher than sync.

    Never raises: every failure mode returns ok False with cause + fix.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    except FileNotFoundError:
        return {'ok': False, 'reason': 'missing output file',
                'fix': 'run the analyze/build step to generate output'}
    except (OSError, ValueError) as exc:
        return {'ok': False, 'reason': f'corrupt output file: {exc}',
                'fix': 'delete the corrupt output and re-run the build'}
    tasks = payload.get('tasks') if isinstance(payload, dict) else None
    if not isinstance(tasks, list) or len(tasks) == 0:
        return {'ok': False, 'reason': 'empty tasks: no tasks extracted',
                'fix': 're-run the build after sync completes'}
    if last_sync_change is not None:
        exported = _parse_ts(payload.get('exported_at')) \
            if isinstance(payload, dict) else None
        sync_ts = _parse_ts(last_sync_change)
        if exported is None:
            return {'ok': False,
                    'reason': 'missing exported_at timestamp',
                    'fix': 're-run the build to stamp exported_at'}
        if sync_ts is not None and exported < sync_ts:
            return {'ok': False,
                    'reason': 'stale output: exported_at older than '
                              'last sync change',
                    'fix': 're-run the build to refresh output'}
    return {'ok': True, 'reason': 'ok', 'fix': ''}


# --------------------------------------------------------------- crash class

def open_messages_db(path):
    """Open messages.db read-only. Missing/corrupt -> (None, True) degraded."""
    try:
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        conn.execute(
            'SELECT name FROM sqlite_master LIMIT 1').fetchall()
        return conn, False
    except (sqlite3.Error, OSError, ValueError):
        return None, True


def load_themes_safe(path):
    """Load theme_groups.json. Corrupt/missing -> ({}, True) fallback."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}, True
        return data, False
    except (OSError, ValueError):
        return {}, True


# ----------------------------------------------------------------- G0/G1/G2

def pid_is_stale(pid):
    """True when no live process owns this pid (kill -0 probe)."""
    try:
        os.kill(int(pid), 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except (OSError, ValueError, OverflowError):
        return True


def health_summary(bridge_pid=None, api_pid=None, watch_paths=None, now=None):
    """G0 health from PID liveness plus file freshness.

    PID checks use signal 0 delivery; file checks use mtimes only.
    """
    if now is None:
        now = time.time()
    try:
        now_s = float(now) if not isinstance(now, (int, float)) else now
    except (TypeError, ValueError):
        now_s = time.time()
    bridge_alive = (not pid_is_stale(bridge_pid)
                    if bridge_pid is not None else None)
    api_alive = (not pid_is_stale(api_pid)
                 if api_pid is not None else None)
    files = {}
    for path in watch_paths or []:
        try:
            mtime = os.path.getmtime(path)
            files[path] = {'mtime': mtime, 'age_s': now_s - mtime}
        except OSError:
            files[path] = {'mtime': None, 'age_s': None}
    if bridge_pid is not None and api_pid is not None:
        degraded = not (bridge_alive and api_alive)
    elif bridge_pid is not None:
        degraded = not bridge_alive
    elif api_pid is not None:
        degraded = not api_alive
    else:
        degraded = any(v.get('mtime') is None for v in files.values())
    return {'bridge_alive': bridge_alive, 'api_alive': api_alive,
            'files': files, 'degraded': degraded}


def require_auth(cookies='', basic=''):
    """Session gate: 200 with a session/basic credential, else 401."""
    if cookies or basic:
        return 200
    return 401


def auth_state_public(cookies='', basic=''):
    """Auth-state probe is public so login can always load."""
    return {'public': True,
            'authenticated': bool(cookies or basic)}


def first_run_gate(setup_complete=False, cookies='', basic=''):
    """Setup-incomplete first run gates mutating calls with 403, not 401."""
    if not setup_complete:
        return 403
    return require_auth(cookies=cookies, basic=basic)


_KEY_PROBE_CHILD = (
    'import os, sys\n'
    'key = os.environ.get("MOMENTUM_GEMINI_KEY", "")\n'
    'if not key:\n'
    '    sys.exit(1)\n'
    'try:\n'
    '    import google.genai  # noqa: F401\n'
    'except Exception:\n'
    '    sys.exit(2)\n'
    'sys.exit(0)\n'
)


def validate_key_fresh(key, _runner=subprocess.run):
    """Validate the Gemini key in a FRESH interpreter (parent stays clean)."""
    env = dict(os.environ)
    env['MOMENTUM_GEMINI_KEY'] = key or ''
    try:
        proc = _runner([sys.executable, '-c', _KEY_PROBE_CHILD],
                       env=env, timeout=20, capture_output=True)
        return bool(getattr(proc, 'returncode', 1) == 0)
    except Exception:
        return False


def key_cache_set(key, value, ttl_s=60, now=None, path=None):
    """Cache a key verdict with a TTL timestamp (in-memory + optional file)."""
    if now is None:
        now = time.time()
    _KEY_CACHE[key] = (value, float(now), float(ttl_s))
    if path is not None:
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({k: [v, ts, ttl]
                           for k, (v, ts, ttl) in _KEY_CACHE.items()}, f)
        except OSError:
            pass
    return value


def key_cache_get(key, ttl_s=60, now=None, path=None):
    """Cached verdict or None once older than its TTL (never silent-stale)."""
    if now is None:
        now = time.time()
    entry = _KEY_CACHE.get(key)
    if entry is None and path is not None:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                disk = json.load(f)
            raw = disk.get(key)
            if raw is not None:
                entry = (raw[0], float(raw[1]), float(raw[2]))
                _KEY_CACHE[key] = entry
        except (OSError, ValueError, TypeError, IndexError):
            entry = None
    if entry is None:
        return None
    value, set_at, stored_ttl = entry
    ttl = float(ttl_s if ttl_s is not None else stored_ttl)
    if float(now) - float(set_at) > ttl:
        _KEY_CACHE.pop(key, None)
        return None
    return value


def key_file_mode_ok(path):
    """Persisted key file must be owner-only (no group/other bits)."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return (mode & 0o077) == 0


# ------------------------------------------------------------------ G6 BOARD

def describe_empty_board(ctx):
    """Explain an empty board with a one-click fix target.

    Returns (message, fix_target); fix_target is '' only when board is ready.
    """
    ctx = dict(ctx or {})
    groups_mapped = ctx.get('groups_mapped', 0)
    visible = ctx.get('visible_tasks', 0)
    total = ctx.get('total_tasks', 0)
    key_present = ctx.get('key_present', True)
    synced = ctx.get('synced', True)
    build_fresh = ctx.get('build_fresh', True)
    if not key_present:
        return ('No API key configured — add your Gemini key to enable '
                'analysis.', '/setup#key')
    if not synced:
        return ('Sync not complete — waiting for WhatsApp sync to finish.',
                '/journey#sync')
    if not build_fresh:
        return ('Board data is stale — rebuild to refresh tasks.',
                '/api/rebuild')
    if not groups_mapped:
        return ('No groups mapped — map at least one group to get started.',
                '/setup#groups')
    if total and not visible:
        return ('All tasks filtered out — loosen the filter to see tasks.',
                '/board#filters')
    if not total and not visible:
        return ('No tasks yet — sync or rebuild once messages arrive.',
                '/journey#sync')
    return ('Board ready.', '')
