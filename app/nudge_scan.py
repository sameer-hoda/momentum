"""Nudge scan — pure functions, stdlib only, no LLM/network.

HIGH BAR: emit a nudge only if
  (a) a message explicitly addresses the user (OWNER_NAME word-boundary,
      or 'you' + a question mark) AND there is no user reply after it in
      the same thread/group, OR
  (b) task.waiting_on is set AND there is fresh activity (last_ts within
      the window) AND nudges >= 1, OR
  (c) task.needs_my_action AND there is a fresh question in the window.

Output is capped at 7 nudges, ranked: direct_address > waiting_on > needs_you.
Overrides ({task_id: {status, ts}}) hide snoozed items for 72h and closed
items permanently; lookup tries both the nudge id and the task id.
"""
import datetime
import re

MAX_NUDGES = 7
SNOOZE_HOURS = 72

_RANK = {'direct_address': 0, 'waiting_on': 1, 'needs_you': 2}

_REQUEST_RE = re.compile(
    r'\b(can|could|would|will|please|share|send|confirm|update|approve|review|'
    r'check|decide|need|needs|chase|ping|remind|asap|let me know|follow up)\b',
    re.IGNORECASE)

_QUICK_ACTIONS = [
    ('send_nudge', 'Send nudge \u2713'),
    ('soften', 'Soften'),
    ('open_thread', 'Open thread \u2192'),
    ('mark_closed', 'Mark closed'),
    ('snooze', 'Snooze 3d'),
]


def _parse_ts(value, now):
    if isinstance(value, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip().replace('Z', '+00:00')
    try:
        ts = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts


def _norm_now(now):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    return now


def _is_owner(sender, owner_name):
    ol = (owner_name or '').strip().lower()
    sl = (sender or '').strip().lower()
    if not ol or not sl:
        return False
    return ol == sl or ol in sl or sl in ol


def _msg_body(m):
    return m.get('body') if m.get('body') is not None else m.get('text', '')


def _thread_key(m):
    if m.get('task_id'):
        return ('task', str(m['task_id']))
    return ('group', str(m.get('group') or ''))


def _addresses_user(body, owner_name):
    """True when the message explicitly addresses the user AND asks something."""
    words = body.split()
    if len(words) < 3:
        return False
    if body.strip().lower().startswith('fyi'):
        return False
    owner = (owner_name or '').strip()
    name_hit = bool(owner) and re.search(
        r'\b' + re.escape(owner) + r'\b', body, re.IGNORECASE)
    you_hit = re.search(r'\byou\b', body, re.IGNORECASE)
    question = '?' in body
    if name_hit:
        return bool(question or _REQUEST_RE.search(body))
    if you_hit:
        return bool(question)
    return False


def _age_str(ts, now):
    mins = max(0, int((now - ts).total_seconds() // 60))
    if mins < 1:
        return 'just now'
    if mins < 60:
        return f'{mins} min ago'
    hours = mins // 60
    if hours < 24:
        return f'{hours} h ago'
    return f'{hours // 24} d ago'


def _make_nudge(reason, task_id, group, why_now, suggested_move, epoch):
    slug = re.sub(r'[^a-z0-9]+', '-', (task_id or group or 'thread').lower()).strip('-')
    nid = f'n-{reason}-{slug or "thread"}-{int(epoch)}'
    return {
        'id': nid,
        'task_id': task_id or '',
        'group': group or '',
        'reason': reason,
        'why_now': why_now,
        'suggested_move': suggested_move,
        'quick_actions': [{'id': f'{nid}:{kind}', 'label': label, 'kind': kind}
                          for kind, label in _QUICK_ACTIONS],
    }


def _hidden_by_override(nudge, overrides, now):
    if not overrides:
        return False
    for key in (nudge['id'], nudge['task_id']):
        if not key:
            continue
        entry = overrides.get(key)
        if not isinstance(entry, dict):
            continue
        status = entry.get('status')
        if status == 'closed':
            return True
        if status == 'snoozed':
            ts = _parse_ts(entry.get('ts'), now)
            if ts is None:
                return True
            if (now - ts).total_seconds() < SNOOZE_HOURS * 3600:
                return True
    return False


def scan_nudges(tasks, messages, now=None, window_min=5, owner_name='',
                overrides=None):
    """Full scan of messages in the last window_min minutes. Returns ≤7 nudges."""
    now = _norm_now(now)
    tasks = tasks or []
    messages = messages or []
    cutoff = now - datetime.timedelta(minutes=window_min)
    future_tol = now + datetime.timedelta(seconds=60)

    parsed = []
    for m in messages:
        ts = _parse_ts(m.get('time', m.get('ts')), now)
        if ts is None:
            continue
        parsed.append((ts, m))
    in_window = [(ts, m) for ts, m in parsed if cutoff <= ts <= future_tol]

    by_thread = {}
    for ts, m in parsed:
        by_thread.setdefault(_thread_key(m), []).append((ts, m))

    def owner_replied_after(thread_key, ts):
        for ots, om in by_thread.get(thread_key, []):
            if ots > ts and _is_owner(om.get('sender', om.get('from', '')), owner_name):
                return True
        return False

    candidates = []  # (rank, epoch, nudge)
    claimed = set()  # id() of messages already claimed by branch (a)

    # Branch (a): direct address, unanswered.
    for ts, m in in_window:
        sender = m.get('sender', m.get('from', ''))
        if _is_owner(sender, owner_name):
            continue
        body = _msg_body(m) or ''
        if not _addresses_user(body, owner_name):
            continue
        if owner_replied_after(_thread_key(m), ts):
            continue
        claimed.add(id(m))
        group = m.get('group') or ''
        task_id = m.get('task_id') or ''
        who = sender or 'Someone'
        why = (f'{who} asked you {_age_str(ts, now)} in {group or "a group"} '
               '— no reply from you yet')
        move = 'Send a short reply, or fire the nudge draft below.'
        candidates.append(
            (0, ts.timestamp(),
             _make_nudge('direct_address', task_id, group, why, move,
                         ts.timestamp())))

    # Branch (b): waiting_on with fresh activity and prior nudges.
    for t in tasks:
        if not t.get('waiting_on'):
            continue
        try:
            ncount = int(t.get('nudges') or 0)
        except (TypeError, ValueError):
            ncount = 0
        if ncount < 1:
            continue
        last = _parse_ts(t.get('last_ts'), now)
        if last is None or not (cutoff <= last <= future_tol):
            continue
        who = t.get('waiting_on')
        group = t.get('group') or ''
        why = (f'Waiting on {who}; fresh activity {_age_str(last, now)} in '
               f'{group or "the thread"} (nudge #{ncount})')
        move = f'Chase {who} with a short nudge.'
        candidates.append(
            (1, last.timestamp(),
             _make_nudge('waiting_on', t.get('id', ''), group, why, move,
                         last.timestamp())))

    # Branch (c): needs_my_action with a fresh question in the window.
    for t in tasks:
        if not t.get('needs_my_action'):
            continue
        tid = str(t.get('id', ''))
        tgroup = t.get('group') or ''
        best = None
        for ts, m in in_window:
            if id(m) in claimed:
                continue
            sender = m.get('sender', m.get('from', ''))
            if _is_owner(sender, owner_name):
                continue
            body = _msg_body(m) or ''
            if '?' not in body:
                continue
            mid = str(m.get('task_id') or '')
            if mid == tid or (not mid and (m.get('group') or '') == tgroup):
                if best is None or ts > best[0]:
                    best = (ts, m)
        if best is None:
            continue
        ts, m = best
        sender = m.get('sender', m.get('from', '')) or 'Someone'
        why = (f'Fresh question from {sender} {_age_str(ts, now)} in '
               f'{tgroup or "the thread"} on something you own')
        move = 'Answer it, or send a holding reply until you can.'
        candidates.append(
            (2, ts.timestamp(),
             _make_nudge('needs_you', tid, tgroup, why, move, ts.timestamp())))

    # Dedupe by task_id keeping the best rank, then rank + recency sort.
    best_by_task = {}
    free = []
    for rank, epoch, n in candidates:
        key = n['task_id']
        if not key:
            free.append((rank, epoch, n))
            continue
        prev = best_by_task.get(key)
        if prev is None or (rank, -epoch) < (prev[0], -prev[1]):
            best_by_task[key] = (rank, epoch, n)
    ranked = sorted(list(best_by_task.values()) + free,
                    key=lambda c: (c[0], -c[1]))

    out = []
    for _, _, n in ranked:
        if _hidden_by_override(n, overrides, now):
            continue
        out.append(n)
        if len(out) >= MAX_NUDGES:
            break
    return out
