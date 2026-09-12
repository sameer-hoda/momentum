"""
Momentum data pipeline v3 — atomic action-item extraction + follow-up tracking.

v2 extracted ONE summary per conversation thread, which systematically missed
real work: multiple asks inside one thread collapsed into a single vague topic
("Optimizing X"), 1-2 message asks were dropped by the min-message filter, and
multi-day ask -> nudge -> resolution sequences lost their state.

What v3 does differently (tuned by mining 120d of actual chat history — 582
follow-up nudges, 1177 explicit asks, 315 commitments):

  1. FETCH    — 30d window (open items must outlive the 10d churn)
  2. THREAD   — time-gap disentanglement, but every message is annotated with
                deterministic signals: follow-up nudges ("any update", "ETA"),
                asks ("please/can you"), and *unanswered questions aimed at
                you* (checked at group level across thread boundaries)
  3. EXTRACT  — per-thread LLM call returns MULTIPLE atomic items
                (ask / commitment / blocker / decision), each with owner,
                requester, due-date and open/done/blocked status
  4. MERGE    — embedding dedupe within theme -> MECE tasks; status now comes
                from the MOST RECENT evidence, not the worst fragment
  5. SURFACE  — tasks carry needs_my_action (you owe action) /
                waiting_on (you are chasing someone) / owner / due; stale
                completed items are pruned

Bulletins / day digests / week digest unchanged from v2.

Run:
    bash momentum_update/sync_dbs.sh
    python3 momentum_update/build_data.py        # full/incremental auto
    cd momentum_update && python3 -m http.server 8081   # -> http://localhost:8081/

Fallback: with no GEMINI_API_KEY (or on LLM errors) it degrades to heuristic
extraction so the front end always gets data.
"""

import os, re, json, math, sqlite3, hashlib, datetime
from concurrent.futures import ThreadPoolExecutor

import export_data as ed  # reuse contact resolution + theme/exclude loaders

from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '..', 'wa_productivity', '.env'))
API_KEY = os.environ.get('GEMINI_API_KEY', os.environ.get('GOOGLE_API_KEY', ''))
ME = os.environ.get('OWNER_NAME', '').strip().lower()
def is_me(name):
    return bool(ME) and ME in str(name or '').lower()

STATE_FILE   = os.path.join(SCRIPT_DIR, 'pipeline_state.json')
OUTPUT_FILE  = os.path.join(SCRIPT_DIR, 'frontend', 'frontend_data.json')

TASK_WINDOW_DAYS   = 30     # open items must survive longer than a sprint
BULLETIN_HOURS     = 72     # timeline bulletin lookback
THREAD_GAP_MIN     = 90     # conversation disentanglement gap
MERGE_COSINE       = 0.87   # embedding dedupe threshold (same theme)
MAX_TASK_MSGS      = 14     # messages kept per task for the detail pane
MAX_PARALLEL       = 8
PRUNE_COMPLETED_DAYS = 6    # completed tasks older than this leave the board
UNANSWERED_HOURS     = 48   # question to you unanswered for this long => owes action
BODY_CAP             = 9000 # chars of thread body sent to the LLM
STALE_DAYS           = 14   # open task silent this long => stale (re-verify or close)
HEALTH_DAYS          = 90   # unblock-score wall lookback
RECONCILE_BATCH      = 8    # stale tasks per reconcile LLM call

EXTRACT_MODEL = 'gemini-3-flash-preview'
EMBED_MODEL   = 'gemini-embedding-001'

# ---------------------------------------------------------------------------
# Deterministic follow-up signal patterns (mined from real chat history)
# ---------------------------------------------------------------------------
NUDGE_RE = re.compile(
    r'\b(any update|update on|update\?|follow(ed)?[ -]?up|following up|gentle reminder|reminder|'
    r'still waiting|waiting on|waiting for|awaiting|pending on|pending from|by when|when can|'
    r'eta\b|no response|not responded|chase|nudge|status\?|ho gaya|kiya\?)', re.I)
ASK_RE = re.compile(
    r'\b(please|pls|plz|kindly|can you|could you|need you to|request you|req:|action item|'
    r'take this (fwd|forward)|pursue|share the|share it|send me|let me know)\b', re.I)
RESOLUTION_RE = re.compile(
    r'\b(done|shipped|closed|resolved|fixed|went live|is live|launched|rolled out|completed|sorted|'
    r'pulled back|deprioriti[sz]ed|dropped)\b', re.I)

# automated digests posted into groups by your own bots — never human asks
BOT_RE = re.compile(
    r'(task_dog|\*?SCOREBOARD\*?|\*Last 24 Hrs\*|⚡ \*Pending Follow-Ups\*|'
    r'###\s+\*\*PROJECT:|📋 \*(?:📋 )?Last 24)', re.I)

# ---------------------------------------------------------------------------
# LLM client (lazy)
# ---------------------------------------------------------------------------
_client = None
def client():
    global _client
    if _client is None and API_KEY:
        from google import genai
        _client = genai.Client(api_key=API_KEY)
    return _client

LLM_TIMEOUT = 90  # seconds — genai can hang forever on network issues

def ask(prompt, model=EXTRACT_MODEL):
    """generate_content with a hard timeout; raises on failure/hang."""
    import concurrent.futures as cf
    def _call():
        return client().models.generate_content(model=model, contents=prompt)
    with cf.ThreadPoolExecutor(1) as ex:
        return ex.submit(_call).result(timeout=LLM_TIMEOUT)

def log(m): print(m, flush=True)

def parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace(' ', 'T'))
    except Exception:
        return None

def slug(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')[:48]

# ---------------------------------------------------------------------------
# 1. FETCH — non-archived groups, theme-mapped only (drops 'other' noise)
# ---------------------------------------------------------------------------
def fetch_messages(days):
    excluded = ed.load_excluded()
    group_to_theme, theme_meta = ed.load_themes()
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = (now - datetime.timedelta(days=days)).isoformat()

    conn = sqlite3.connect(ed.MESSAGES_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH DATABASE '{ed.WHATSAPP_DB}' AS wa")
    ed.load_contact_index(conn)

    rows = conn.execute("""
        SELECT m.content, m.timestamp, m.is_from_me,
               ms.sender_jid as sender_jid, ch.name as chat_name
        FROM messages m
        JOIN chats ch ON m.chat_jid = ch.jid
        LEFT JOIN wa.whatsmeow_message_secrets ms ON m.id = ms.message_id AND m.chat_jid = ms.chat_jid
        LEFT JOIN wa.whatsmeow_chat_settings cs ON m.chat_jid = cs.chat_jid
        WHERE m.timestamp >= ? AND m.content IS NOT NULL AND m.content != ''
          AND ch.jid != 'status@broadcast' AND ch.jid LIKE '%@g.us'
          AND (cs.archived IS NULL OR cs.archived = 0)
        ORDER BY ch.name, m.timestamp ASC
    """, (cutoff,)).fetchall()
    conn.close()

    msgs = []
    for r in rows:
        gname = (r['chat_name'] or '').strip()
        if not gname or gname.lower() in excluded:
            continue
        theme = group_to_theme.get(gname.lower())
        if not theme:            # unmapped group -> not a key work group
            continue
        sender = ('You' if r['is_from_me']
                  else (ed._jid_to_name(r['sender_jid']) or 'Unknown'))
        body = ed.resolve_mentions(r['content'].strip())
        if BOT_RE.search(body):   # bot digest, not a human message
            continue
        ts = parse_ts(r['timestamp'])
        if not ts:
            continue
        msgs.append({'group': gname, 'theme': theme, 'sender': sender,
                     'body': body, 'ts': ts, 'from_me': bool(r['is_from_me'])})

    _mark_unanswered_questions(msgs)
    log(f"Fetched {len(msgs)} messages from theme-mapped groups (last {days}d)")
    return msgs, theme_meta

def _mark_unanswered_questions(msgs):
    """Flag questions aimed at you that got no reply from you within
    UNANSWERED_HOURS. Checked at GROUP level (replies often land in a later
    thread after the 90-min gap would have split it)."""
    by_group = {}
    for m in msgs:
        by_group.setdefault(m['group'], []).append(m)
    flagged = 0
    for group, ms in by_group.items():
        ms.sort(key=lambda x: x['ts'])
        mine_ts = [m['ts'] for m in ms if m['from_me']]
        for i, m in enumerate(ms):
            if m['from_me'] or '?' not in m['body']:
                continue
            low = m['body'].lower()
            aimed = (bool(ME) and (ME in low or f'@{ME}' in low))
            if not aimed:
                continue
            answered = any(t > m['ts'] and t - m['ts'] <= datetime.timedelta(hours=UNANSWERED_HOURS)
                           for t in mine_ts)
            if not answered:
                m['_q_to_me'] = True
                flagged += 1
    if flagged:
        log(f"Flagged {flagged} unanswered question(s) aimed at you")

# ---------------------------------------------------------------------------
# 2. THREAD — time-gap conversation disentanglement + per-thread signals
# ---------------------------------------------------------------------------
def cluster_threads(msgs):
    by_group = {}
    for m in msgs:
        by_group.setdefault(m['group'], []).append(m)
    threads = []
    gap = THREAD_GAP_MIN * 60
    for group, ms in by_group.items():
        ms.sort(key=lambda x: x['ts'])
        cur = []
        for m in ms:
            if cur and (m['ts'] - cur[-1]['ts']).total_seconds() > gap:
                threads.append(make_thread(group, cur))
                cur = []
            cur.append(m)
        if cur:
            threads.append(make_thread(group, cur))
    for t in threads:
        enrich_thread_signals(t)
    log(f"Disentangled into {len(threads)} conversation threads (gap {THREAD_GAP_MIN}m)")
    return threads

def make_thread(group, ms):
    key = hashlib.sha1(f"{group}|{ms[0]['ts'].isoformat()}".encode()).hexdigest()[:12]
    return {'key': key, 'group': group, 'theme': ms[0]['theme'],
            'msgs': ms, 'start': ms[0]['ts'], 'end': ms[-1]['ts']}

def enrich_thread_signals(t):
    t['nudges'] = sum(1 for m in t['msgs'] if NUDGE_RE.search(m['body']))
    t['has_ask'] = any(ASK_RE.search(m['body']) or '?' in m['body'] for m in t['msgs'])
    t['q_to_me'] = any(m.get('_q_to_me') for m in t['msgs'])
    t['from_me'] = any(m['from_me'] for m in t['msgs'])

def worth_llm(t):
    """Gate for spending an LLM call. Threads >=3 msgs always processed;
    tiny threads only when they carry a real-work signal."""
    if len(t['msgs']) >= 3:
        return True
    return t['from_me'] or t['q_to_me'] or t['nudges'] > 0 or t['has_ask']

def sample_body(msgs, cap=BODY_CAP):
    lines = [f"{m['ts'].strftime('%m-%d %H:%M')} {m['sender']}: {m['body']}" for m in msgs]
    body = '\n'.join(lines)
    if len(body) <= cap:
        return body
    third = cap // 3
    return body[:third] + '\n…\n' + body[len(body)//2 - third//2 : len(body)//2 + third//2] \
           + '\n…\n' + body[-third:]

# ---------------------------------------------------------------------------
# 3. EXTRACT — one call per thread returns MULTIPLE atomic action items
# ---------------------------------------------------------------------------
EXTRACT_PROMPT = """You extract ACTION ITEMS from a WhatsApp work-group chat for the user's personal tracker.

Group: {group} (theme: {theme})
{n} messages, {start} to {end}.{nudge_note}
---
{body}
---

Return ONLY JSON: {{"items": [...]}} — one object per DISTINCT action item (max 4), most important first. Empty list [] if the thread is pure banter, greetings, FYI links, or logistics with no ask.

Item kinds:
- "ask": someone asked another person to do/answer something. EVERY unanswered question directed at a person counts — "@X can you share?", "ETA?", "any update?", "by when?".
- "commitment": someone promised to do something ("will share by EOD", "on it", "checking").
- "blocker": work explicitly stuck or waiting on someone/something.
- "decision": a decision that closes an item or requires follow-through.

Each item:
{{"kind": "ask|commitment|blocker|decision",
  "what": "<the action needed, max 9 words, verb-first>",
  "context": "<single most important fact/number/ask, max 18 words>",
  "owner": "<first name of who must act, '' if genuinely unclear>",
  "requester": "<first name of who asked/waited, '' if none>",
  "due": "<deadline exactly as written, e.g. 'EOD today', '' if none>",
  "status": "open|done|blocked",
  "sameer_involved": true/false,
  "topic": "<2-3 word workstream tag, lowercase, e.g. 'merchant offers', 'zombie retention', 'ios rollout'>"}}

Rules:
- "what" must be a clean verb-first instruction ("Share HTML with Miten"), NEVER a raw quote, never an @mention.
- "done" ONLY if the chat explicitly confirms completion/shipping. Otherwise "open". "blocked" if explicitly stuck.
- A nudge ("any update?", "following up") means an earlier item is STILL OPEN — capture the original item as open.
- owner must be a person who appears in the chat. Never invent names or actions.
- sameer_involved=true ONLY when the user must personally act or answer. If they merely asked someone else to do something, sameer_involved=false and owner=that person.
- topic must be a REUSABLE bucket name for the workstream (others in this theme should map to the same tag) — not a one-off description."""

KINDS = ('ask', 'commitment', 'blocker', 'decision', 'update')
STATUSES = ('open', 'done', 'blocked')

# "what" must read like a verb-first instruction — reject raw quotes,
# URLs, questions and past-tense narration that flash sometimes emits
JUNK_URL_RE = re.compile(r'(https?://|\bslack\.com\b)', re.I)
# leading Name + auxiliary verb => a quoted question, not an instruction
QUOTED_Q_RE = re.compile(
    r"^[A-Z][A-Za-z]+['\"]? (is|are|was|were|can|could|did|does|do|has|have|will|would|should)\b", re.I)
BAD_FIRST_WORDS = {
    'we', 'i', 'their', 'they', 'them', 'this', 'that', 'these', 'those',
    'it', 'its', 'he', 'she', 'his', 'her', 'also', 'so', 'yes', 'no', 'ok',
    'okay', 'thanks', 'thank', 'the', 'have', 'has', 'had', 'been', 'being',
    'am', 'is', 'are', 'was', 'were', 'will', 'would', 'should', 'could',
    'can', 'may', 'might', 'must', 'do', 'does', 'did', 'a', 'an', 'not',
}
GOOD_ED = {'need', 'feed', 'embed', 'proceed', 'succeed', 'exceed'}

def item_ok(what):
    first = what.split()[0].lower().rstrip('.,!?;:\'"')
    if JUNK_URL_RE.search(what) or QUOTED_Q_RE.match(what):
        return False
    if first in BAD_FIRST_WORDS:
        return False
    if first.endswith('ed') and first not in GOOD_ED:   # past-tense narration
        return False
    return True

def normalize_item(i):
    kind = str(i.get('kind') or 'ask').lower()
    what = re.sub(r'^@\S+,?\s+', '', str(i.get('what') or '').strip())
    what = re.sub(r'^(please|kindly)\s+', '', what, flags=re.I).strip()[:90]
    if not what or len(what) < 4 or not item_ok(what):
        return None
    owner = str(i.get('owner') or '').strip().split()[0][:24] if str(i.get('owner') or '').strip() else ''
    requester = str(i.get('requester') or '').strip().split()[0][:24] if str(i.get('requester') or '').strip() else ''
    topic = slug(str(i.get('topic') or ''))[:32] or 'general'
    return {
        'kind': kind if kind in KINDS else 'ask',
        'what': what,
        'context': str(i.get('context') or '')[:200],
        'owner': owner,
        'requester': requester,
        'due': str(i.get('due') or '')[:40],
        'status': str(i.get('status') or 'open').lower() if str(i.get('status') or '').lower() in STATUSES else 'open',
        'sameer_involved': bool(i.get('sameer_involved')),
        'topic': topic,
    }

def extract_thread(t, theme_label):
    nudge_note = ''
    if t['nudges']:
        nudge_note = f"\nNOTE: {t['nudges']} follow-up nudge(s) in this thread (\"any update\" / ETA) — something here is likely STILL OPEN."
    prompt = EXTRACT_PROMPT.format(
        group=t['group'], theme=theme_label, n=len(t['msgs']),
        start=t['start'].strftime('%b %d %H:%M'), end=t['end'].strftime('%b %d %H:%M'),
        nudge_note=nudge_note, body=sample_body(t['msgs']))
    try:
        resp = ask(prompt)
        txt = resp.text.strip()
        m = re.search(r'\{.*\}', txt, re.S)
        raw = json.loads(m.group(0)).get('items') or []
        items = [x for x in (normalize_item(i) for i in raw[:4]) if x]
        if not items:
            return None
        return {'items': items}
    except Exception:
        return heuristic_extract(t)

def heuristic_extract(t):
    bodies = ' '.join(m['body'] for m in t['msgs'])
    last = t['msgs'][-1]
    status = ed.extract_status(bodies, len(t['msgs']))
    st = {'blocked': 'blocked', 'completed': 'done'}.get(status, 'open')
    item = {
        'kind': 'ask' if (t['has_ask'] or t['nudges']) else 'update',
        'what': last['body'][:60] or t['group'],
        'context': last['body'][:140],
        'owner': '', 'requester': '', 'due': '',
        'status': st, 'sameer_involved': bool(t['q_to_me'] or t['from_me']),
        'topic': 'general',
    }
    norm = normalize_item(item)
    if not norm:
        return None
    return {'items': [norm]}

# ---------------------------------------------------------------------------
# 4. MERGE — embedding dedupe within theme -> MECE tasks
# ---------------------------------------------------------------------------
def embed_texts(texts):
    if not texts:
        return []
    try:
        def _call():
            return client().models.embed_content(model=EMBED_MODEL, contents=texts)
        ex = ThreadPoolExecutor(1)
        try:
            resp = ex.submit(_call).result(timeout=LLM_TIMEOUT)
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        return [e.values for e in resp.embeddings]
    except Exception as e:
        log(f"  embed failed ({type(e).__name__}), skipping merge")
        return None

def cosine(a, b):
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a)); nb = math.sqrt(sum(x*x for x in b))
    return dot / (na * nb) if na and nb else 0.0

def merge_candidates(cands):
    """cands: list of dicts with items + thread refs. Returns merged tasks."""
    by_theme = {}
    for c in cands:
        by_theme.setdefault(c['theme'], []).append(c)
    merged = []
    for theme, items in by_theme.items():
        vecs = embed_texts([_embed_text(c) for c in items]) if client() else None
        parent = list(range(len(items)))
        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]; i = parent[i]
            return i
        if vecs:
            for i in range(len(items)):
                for j in range(i+1, len(items)):
                    if cosine(vecs[i], vecs[j]) >= MERGE_COSINE:
                        pi, pj = find(i), find(j)
                        if pi != pj:
                            parent[pj] = pi
        clusters = {}
        for i, c in enumerate(items):
            clusters.setdefault(find(i), []).append(c)
        for cl in clusters.values():
            merged.append(build_task(theme, cl))
    return merged

def _embed_text(c):
    p = c['items'][0]
    return f"{c['thread']['group']}: {p['what']}: {p['context']}"

STATUS_MAP = {'open': 'running', 'done': 'completed', 'blocked': 'blocked'}
STATUS_RANK = {'blocked': 0, 'running': 1, 'completed': 2}

def build_task(theme, cl):
    all_msgs, seen = [], set()
    people, groups = [], []
    for c in cl:
        for m in c['thread']['msgs']:
            k = (m['ts'].isoformat(), m['sender'], m['body'][:40])
            if k not in seen:
                seen.add(k); all_msgs.append(m)
        if c['thread']['group'] not in groups: groups.append(c['thread']['group'])
        for p in c.get('people', []):
            if p not in people: people.append(p)
    all_msgs.sort(key=lambda x: x['ts'])

    # flatten items newest-thread first; primary = newest still-open item
    flat = []
    for c in sorted(cl, key=lambda c: c['thread']['end'], reverse=True):
        for it in c['items']:
            flat.append({'item': it, 'cand': c})
    open_flat = [f for f in flat if f['item']['status'] != 'done']
    primary = (open_flat or flat)[0]
    it = primary['item']

    # You owe action when the PRIMARY open item is owned by you, aimed
    # at him (unanswered question in its own thread), or unowned but flagged
    # as involving him. Being merely the requester is waiting_on, not you-owe.
    owner_is_me = is_me(it['owner'])
    needs_my_action = it['status'] != 'done' and (
        owner_is_me
        or (it.get('sameer_involved') and not it['owner'])
        or primary['cand']['thread'].get('q_to_me')
    )
    waiting_on = ''
    if it['status'] == 'open' and is_me(it['requester']) \
            and it['owner'] and not is_me(it['owner']):
        waiting_on = it['owner']

    counts = {}
    for m in all_msgs:
        counts[m['sender']] = counts.get(m['sender'], 0) + 1
    participants = [s for s, _ in sorted(counts.items(), key=lambda kv: -kv[1])]

    nudges = sum(c['thread'].get('nudges', 0) for c in cl)
    last = all_msgs[-1]['ts']
    age_days = max(0, (datetime.datetime.now(datetime.timezone.utc) - last).days)
    title = re.sub(r'^@\S+,?\s+', '', it['what']).strip() or it['context'][:60]
    topics = []
    for f in flat:
        tp = f['item'].get('topic')
        if tp and tp not in topics:
            topics.append(tp)
    return {
        'id': f"{theme}::{slug(title)}",
        'theme': theme,
        'sub_theme': it.get('topic') or 'general',
        'topics': topics[:4],
        'title': title,
        'one_liner': it['context'],
        'kind': it['kind'],
        'status': STATUS_MAP[it['status']],
        'owner': it['owner'],
        'requester': it['requester'],
        'due': it['due'] if it['status'] == 'open' else '',
        'needs_my_action': bool(needs_my_action),
        'waiting_on': waiting_on,
        'stale': bool(it['status'] != 'done' and age_days >= STALE_DAYS),
        'nudges': nudges,
        'age': 'today' if age_days == 0 else f"{age_days}d",
        'age_days': age_days,
        'msg_count': len(all_msgs),
        'recap': ' · '.join(filter(None, [
            f"{f['item']['what']} ({f['item']['status']}, {f['item']['owner'] or 'owner?'})"
            for f in flat[:3]])),
        'people': people or participants[:5],
        'participants': participants,
        'groups': groups,
        'group': groups[0],
        'last_ts': last.isoformat(),
        'messages': [{'sender': m['sender'], 'body': m['body'], 'time': m['ts'].isoformat()}
                     for m in all_msgs[-MAX_TASK_MSGS:]],
    }

# ---------------------------------------------------------------------------
# 5. BULLETINS — 3-hour windows: top developments, each tagged with its group
# ---------------------------------------------------------------------------
BULLETIN_PROMPT = """WhatsApp work-group activity between {start} and {end}.
From ALL groups below, pick the TOP 3-5 most important developments in this window, ranked by business importance — decisions, numbers, launches, blockers. Each development must say WHAT moved, not which group. Skip banter/logistics.

{blocks}

Return ONLY JSON: {{"developments": [{{"text": "max 22 words, concrete", "group": "<exact group name it happened in>"}}]}}"""

def _window_of(ts, hours):
    ws = ts.replace(minute=0, second=0, microsecond=0)
    return ws - datetime.timedelta(hours=ws.hour % hours)

def build_bulletins(msgs, state):
    if not msgs:
        return []
    # anchor to the latest available message, not wall-clock — the DB may be
    # a few days stale between syncs, and bulletins should cover the most
    # recent activity we actually have
    anchor = max(m['ts'] for m in msgs)
    since = anchor - datetime.timedelta(hours=BULLETIN_HOURS)
    recent = [m for m in msgs if m['ts'] >= since]
    windows = {}
    for m in recent:
        windows.setdefault(_window_of(m['ts'], 3), []).append(m)
    cache = state.setdefault('bulletins_v2', {})
    out = []
    for ws in sorted(windows, reverse=True):
        wmsgs = windows[ws]
        wkey = ws.isoformat()
        max_ts = max(m['ts'] for m in wmsgs).isoformat()
        if wkey in cache and cache[wkey].get('max_ts') == max_ts:
            out.append(cache[wkey]['entry']); continue
        by_group = {}
        for m in wmsgs:
            by_group.setdefault(m['group'], []).append(m)
        blocks = []
        for g, ms in sorted(by_group.items()):
            txt = '\n'.join(f"{x['sender']}: {x['body']}" for x in ms)[:1500]
            blocks.append(f"### {g} ({len(ms)} msgs)\n{txt}")
        entry = None
        if client():
            try:
                resp = ask(BULLETIN_PROMPT.format(
                    start=ws.strftime('%a %H:%M'),
                    end=(ws + datetime.timedelta(hours=3)).strftime('%H:%M'),
                    blocks='\n\n'.join(blocks)))
                mtxt = re.search(r'\{.*\}', resp.text.strip(), re.S)
                devs = json.loads(mtxt.group(0)).get('developments', [])
                entry = {'start': wkey, 'developments': [
                    {'text': str(i.get('text') or '')[:180], 'group': i['group'],
                     'theme': by_group[i['group']][0]['theme'],
                     'msg_count': len(by_group[i['group']])}
                    for i in devs if i.get('group') in by_group and i.get('text')][:5]}
            except Exception as e:
                log(f"  bulletin {wkey} failed: {type(e).__name__}")
        if entry is None:  # heuristic fallback — NOT cached, so next run retries the LLM
            top = sorted(by_group.items(), key=lambda kv: -len(kv[1]))[:4]
            entry = {'start': wkey, 'developments': [
                {'text': ms[-1]['body'][:140], 'group': g,
                 'theme': ms[0]['theme'], 'msg_count': len(ms)}
                for g, ms in top]}
        else:
            cache[wkey] = {'max_ts': max_ts, 'entry': entry}
        out.append(entry)
    log(f"Built {len(out)} bulletin windows (last {BULLETIN_HOURS}h)")
    return out

# ---------------------------------------------------------------------------
# 5b. DAY DIGESTS — top developments per day, last 7 days
# ---------------------------------------------------------------------------
DAY_PROMPT = """WhatsApp work-group activity on {day}.
From ALL groups below, pick the TOP 5-6 most important developments of the day, ranked by business importance — decisions, numbers, launches, blockers. Each development must say WHAT moved, not which group. Skip banter/logistics. Do not repeat the same development twice.

{blocks}

Return ONLY JSON: {{"developments": [{{"text": "max 22 words, concrete", "group": "<exact group name it happened in>"}}]}}"""

DAY_WINDOW_DAYS = 7

def _group_theme(msgs):
    gt = {}
    for m in msgs:
        gt.setdefault(m['group'], m['theme'])
    return gt

def build_days(msgs, state, theme_meta):
    if not msgs:
        return []
    anchor = max(m['ts'] for m in msgs)
    since = (anchor - datetime.timedelta(days=DAY_WINDOW_DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    recent = [m for m in msgs if m['ts'] >= since]
    by_day = {}
    for m in recent:
        by_day.setdefault(m['ts'].date().isoformat(), []).append(m)
    gt = _group_theme(msgs)
    cache = state.setdefault('days', {})
    out = []
    for day in sorted(by_day, reverse=True):
        dmsgs = by_day[day]
        max_ts = max(m['ts'] for m in dmsgs).isoformat()
        if day in cache and cache[day].get('max_ts') == max_ts:
            out.append(cache[day]['entry']); continue
        by_group = {}
        for m in dmsgs:
            by_group.setdefault(m['group'], []).append(m)
        blocks = []
        for g, ms in sorted(by_group.items(), key=lambda kv: -len(kv[1])):
            txt = '\n'.join(f"{x['sender']}: {x['body']}" for x in ms)[:1800]
            blocks.append(f"### {g} ({len(ms)} msgs)\n{txt}")
        dt = datetime.date.fromisoformat(day)
        entry = None
        if client():
            try:
                resp = ask(DAY_PROMPT.format(
                    day=dt.strftime('%A, %b %d'),
                    blocks='\n\n'.join(blocks)))
                mtxt = re.search(r'\{.*\}', resp.text.strip(), re.S)
                devs = json.loads(mtxt.group(0)).get('developments', [])
                entry = {'date': dt.strftime('%a, %b %d'), 'iso': day,
                         'msg_count': len(dmsgs), 'groups': len(by_group),
                         'developments': [
                             {'text': str(i.get('text') or '')[:180], 'group': i['group'],
                              'theme': gt.get(i['group'], 'other')}
                             for i in devs if i.get('group') in by_group and i.get('text')][:6]}
            except Exception as e:
                log(f"  day digest {day} failed: {type(e).__name__}")
        if entry is None:  # heuristic fallback — NOT cached
            top = sorted(by_group.items(), key=lambda kv: -len(kv[1]))[:5]
            entry = {'date': dt.strftime('%a, %b %d'), 'iso': day,
                     'msg_count': len(dmsgs), 'groups': len(by_group),
                     'developments': [
                         {'text': ms[-1]['body'][:140], 'group': g,
                          'theme': gt.get(g, 'other')}
                         for g, ms in top]}
        else:
            cache[day] = {'max_ts': max_ts, 'entry': entry}
        out.append(entry)
    log(f"Built {len(out)} day digests (last {DAY_WINDOW_DAYS}d)")
    return out

# ---------------------------------------------------------------------------
# 5c. WEEK DIGEST — one view of the last 7 days
# ---------------------------------------------------------------------------
WEEK_PROMPT = """Daily digests of work-group activity over the last 7 days:
{blocks}

Write the week's executive view. Return ONLY JSON:
{{"intro": "one sentence, max 24 words: the week's single biggest storyline",
  "developments": [{{"text": "max 22 words, concrete", "group": "<exact group name>", "day": "<e.g. Mon, Aug 10>"}}]}}
Pick the TOP 8 developments across the whole week, ranked by importance. No duplicates. Each says WHAT moved."""

def build_week(days, msgs, state, theme_meta):
    if not days or not msgs:
        return None
    anchor = max(m['ts'] for m in msgs)
    since = (anchor - datetime.timedelta(days=DAY_WINDOW_DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    recent = [m for m in msgs if m['ts'] >= since]
    gt = _group_theme(msgs)
    theme_counts, groups = {}, set()
    for m in recent:
        theme_counts[m['theme']] = theme_counts.get(m['theme'], 0) + 1
        groups.add(m['group'])
    theme_activity = sorted(
        [{'theme': k, 'label': theme_meta.get(k, {}).get('label', k), 'count': v}
         for k, v in theme_counts.items()],
        key=lambda x: -x['count'])
    latest = max(m['ts'] for m in recent).isoformat() if recent else ''
    cache = state.setdefault('week', {})
    if cache.get('max_ts') == latest and cache.get('entry'):
        e = dict(cache['entry'])
        e['theme_activity'] = theme_activity
        e['msg_count'] = len(recent); e['groups'] = len(groups)
        return e
    blocks = []
    for d in days:
        devs = '\n'.join(f"- [{i['group']}] {i['text']}" for i in d['developments'])
        blocks.append(f"### {d['date']}\n{devs}")
    entry = None
    if client():
        try:
            resp = ask(WEEK_PROMPT.format(blocks='\n\n'.join(blocks)))
            mtxt = re.search(r'\{.*\}', resp.text.strip(), re.S)
            data = json.loads(mtxt.group(0))
            entry = {
                'intro': str(data.get('intro') or '')[:220],
                'developments': [
                    {'text': str(i.get('text') or '')[:180], 'group': str(i.get('group') or ''),
                     'day': str(i.get('day') or ''),
                     'theme': gt.get(i.get('group'), 'other')}
                    for i in (data.get('developments') or []) if i.get('text')][:8],
            }
        except Exception as e:
            log(f"  week digest failed: {type(e).__name__}")
    if entry is None:  # heuristic fallback — NOT cached
        devs = []
        for d in days[:3]:
            for i in d['developments'][:3]:
                devs.append({'text': i['text'], 'group': i['group'], 'day': d['date'],
                             'theme': i.get('theme', 'other')})
        entry = {'intro': '', 'developments': devs[:8]}
    else:
        cache['max_ts'] = latest; cache['entry'] = entry
    entry = dict(entry)
    entry['theme_activity'] = theme_activity
    entry['msg_count'] = len(recent); entry['groups'] = len(groups)
    log("Built week digest")
    return entry

# ---------------------------------------------------------------------------
# 6. RECONCILE — stale open tasks checked against later group chatter.
#    Completions often land in a DIFFERENT thread (or even a different group)
#    than the ask; this pass catches those so the board stops showing work
#    that already happened.
# ---------------------------------------------------------------------------
RECONCILE_PROMPT = """Below are stale open tasks from a Chief-of-Staff tracker, each followed by newer WhatsApp chatter from the SAME work groups (newest last). Chatter may complete a task in different words than the task title.

{tasks}

Follow-up chatter:
{context}

Return ONLY JSON: {{"verdicts":[{{"id":"<task id>","verdict":"done|open|obsolete|unclear","evidence":"<=15 words quoting the chat"}}]}}
- "done": chatter confirms the task completed / shipped / resolved / answered
- "obsolete": superseded or no longer relevant
- "open": still pending, or people are still actively working on it
- "unclear": not enough evidence (default to this over guessing)"""

def _post_thread_context(t, by_group, cap=900):
    """Messages in the task's groups AFTER its last activity."""
    lines = []
    last_ts = parse_ts(t['last_ts'])
    for g in t.get('groups', [])[:2]:
        for m in by_group.get(g, []):
            if m['ts'] > last_ts:
                lines.append(f"{m['ts'].strftime('%m-%d %H:%M')} {m['sender']}: {m['body'][:120]}")
    txt = '\n'.join(lines)
    return txt[-cap:] if len(txt) > cap else txt

def _reconcile_sig(t, ctx):
    return f"{t['id']}|{t['last_ts']}|{len(ctx)}"

def reconcile_stale(tasks, msgs, state):
    """Mark stale tasks done/obsolete using post-thread evidence. Returns (tasks, dropped_ids)."""
    by_group = {}
    for m in msgs:
        by_group.setdefault(m['group'], []).append(m)
    stale_open = [t for t in tasks if t['status'] != 'completed' and t['age_days'] >= STALE_DAYS]
    if not stale_open:
        log("Reconcile: nothing stale to check")
        return tasks, []

    cache = state.setdefault('reconcile', {})
    verdicts, need_llm = {}, []
    for t in stale_open:
        ctx = _post_thread_context(t, by_group)
        sig = _reconcile_sig(t, ctx)
        c = cache.get(t['id'])
        if c and c.get('sig') == sig:
            verdicts[t['id']] = (c['verdict'], c['evidence'], '')
        else:
            need_llm.append((t, ctx, sig))

    # deterministic fallback for anything without an LLM verdict: resolution
    # keyword sharing >=2 title keywords with the task, after its last activity
    def heuristic_verdict(t):
        kws = [w for w in slug(t['title']).split('-') if len(w) > 3][:4]
        last_ts = parse_ts(t['last_ts'])
        for g in t.get('groups', []):
            for m in by_group.get(g, []):
                if m['ts'] <= last_ts:
                    continue
                if RESOLUTION_RE.search(m['body']):
                    mk = slug(m['body']).split('-')
                    if sum(1 for w in kws if w in mk) >= max(1, min(2, len(kws))):
                        return ('done', m['body'][:80], '')
        return ('open', '', '')

    if client() and need_llm:
        for i in range(0, len(need_llm), RECONCILE_BATCH):
            batch = need_llm[i:i+RECONCILE_BATCH]
            tlist = '\n'.join(
                f"id={t['id']} | {t['title']} | owner={t['owner'] or '?'} | last activity {t['age_days']}d ago"
                for t, _, _ in batch)
            context = '\n\n'.join(
                f"### for id={t['id']} ({t['group']})\n{ctx or '(no later messages found)'}"
                for t, ctx, _ in batch)
            try:
                resp = ask(RECONCILE_PROMPT.format(tasks=tlist, context=context[:11000]))
                data = json.loads(re.search(r'\{.*\}', resp.text.strip(), re.S).group(0))
                got = {v.get('id'): (str(v.get('verdict') or 'unclear').lower(),
                                     str(v.get('evidence') or '')[:100])
                       for v in data.get('verdicts', [])}
            except Exception as e:
                log(f"  reconcile batch failed: {type(e).__name__}")
                got = {}
            for t, ctx, sig in batch:
                v, ev = got.get(t['id'], ('unclear', ''))
                if v not in ('done', 'open', 'obsolete'):
                    v, ev = heuristic_verdict(t)[0], ''
                verdicts[t['id']] = (v, ev, sig)
                cache[t['id']] = {'sig': sig, 'verdict': v, 'evidence': ev}
    else:
        for t, _, _ in need_llm:
            v, ev, sig = heuristic_verdict(t)
            verdicts[t['id']] = (v, ev, sig)
            cache[t['id']] = {'sig': sig, 'verdict': v, 'evidence': ev}

    kept, dropped = [], []
    done_cnt = obs_cnt = 0
    for t in tasks:
        v = verdicts.get(t['id'])
        if not v:
            kept.append(t); continue
        verd, ev, _ = v
        if verd == 'done':
            t['status'] = 'completed'
            t['resolved_evidence'] = ev
            done_cnt += 1
            kept.append(t)
        elif verd == 'obsolete':
            dropped.append(t['id']); obs_cnt += 1
        else:
            t['stale'] = True
            kept.append(t)
    log(f"Reconcile: {len(stale_open)} stale checked -> {done_cnt} actually done, "
        f"{obs_cnt} obsolete/dropped, {len(stale_open)-done_cnt-obs_cnt} still open")
    return kept, dropped

# ---------------------------------------------------------------------------
# 7. THREAD HEALTH — one MECE state per task that says exactly where it stands
# ---------------------------------------------------------------------------
STATE_RANK = {'needs_you': 0, 'blocked': 1, 'waiting_other': 2, 'active': 3, 'stale': 4, 'done': 5}
STATE_LABELS = {
    'needs_you': 'Needs you',
    'blocked': 'Blocked',
    'waiting_other': 'Waiting on other',
    'active': 'Active',
    'stale': 'Stale',
    'done': 'Done',
}

def derive_state(t):
    """Exactly one primary state per task, priority-ordered:
    your move > stuck > their move > moving > silent > finished."""
    if t['status'] == 'completed':
        return 'done'
    if t['needs_my_action']:
        return 'needs_you'
    if t['status'] == 'blocked':
        return 'blocked'
    if t['waiting_on']:
        return 'waiting_other'
    if t.get('stale'):
        return 'stale'
    return 'active'

# ---------------------------------------------------------------------------
# 7b. TOPIC TAGS — consistent sub-theme vocabulary per theme (batched, cheap;
#     avoids re-extracting every thread when the topic field is new)
# ---------------------------------------------------------------------------
TOPIC_PROMPT = """You are organizing a Chief-of-Staff tracker. Below are tasks from the theme "{label}". Assign EACH task a reusable workstream tag: 2-3 words, lowercase (e.g. "merchant offers", "zombie retention", "ios rollout", "mbr cadence").

{tasks}

Return ONLY JSON: {{"tags":[{{"id":"<task id>","topic":"<2-3 word lowercase tag>"}}]}}
Rules:
- REUSE the same tag for related tasks — aim for 4-12 DISTINCT tags across the whole set, not one per task.
- Tag the underlying workstream (payments infra, growth experiments, partnerships…), never a person or a one-off event."""

def tag_topics(tasks, theme_meta, state):
    """Fill missing/inconsistent sub_theme tags with a batched LLM pass."""
    cache = state.setdefault('topic_tags', {})
    need = [t for t in tasks
            if (t.get('sub_theme') in (None, '', 'general'))
            or cache.get(t['id'], {}).get('sig') != t['title']]
    # group by theme so the model builds a consistent vocabulary per theme
    by_theme = {}
    for t in need:
        by_theme.setdefault(t['theme'], []).append(t)
    for theme, items in by_theme.items():
        label = theme_meta.get(theme, {}).get('label', theme)
        for i in range(0, len(items), 30):
            batch = items[i:i+30]
            tlist = '\n'.join(f"{t['id']} | {t['title']} | {t['one_liner'][:80]}"
                              for t in batch)
            try:
                resp = ask(TOPIC_PROMPT.format(label=label, tasks=tlist))
                data = json.loads(re.search(r'\{.*\}', resp.text.strip(), re.S).group(0))
                tags = {x.get('id'): str(x.get('topic') or '') for x in data.get('tags', [])}
            except Exception as e:
                log(f"  topic batch ({theme}) failed: {type(e).__name__}")
                continue
            for t in batch:
                tp = slug(tags.get(t['id'], ''))[:32]
                if not tp:
                    continue
                t['sub_theme'] = tp
                cache[t['id']] = {'sig': t['title'], 'topic': tp}
    # apply cached tags to everything else whose stored sub_theme is general
    for t in tasks:
        c = cache.get(t['id'])
        if c and (t.get('sub_theme') in (None, '', 'general')):
            t['sub_theme'] = c['topic']
    tagged = sum(1 for t in tasks if t.get('sub_theme') not in (None, '', 'general'))
    log(f"Topic tags: {tagged}/{len(tasks)} tasks have sub-themes")
    return tasks

# ---------------------------------------------------------------------------
# 8. MAP — theme x sub-theme attention grid for the front page
# ---------------------------------------------------------------------------
def build_map(tasks, theme_meta):
    nodes = {}
    for t in tasks:
        st = t.get('state', 'active')
        key = (t['theme'], t.get('sub_theme') or 'general')
        n = nodes.setdefault(key, {
            'theme': t['theme'],
            'theme_label': t.get('theme_label', t['theme']),
            'sub': key[1],
            'label': (key[1] or 'general').replace('-', ' '),
            'tasks': 0, 'msgs': 0,
            'states': {s: 0 for s in STATE_RANK},
        })
        n['tasks'] += 1
        n['msgs'] += t.get('msg_count', 0)
        n['states'][st] += 1
    out = sorted(nodes.values(), key=lambda n: (
        -sum(n['states'][s] * w for s, w in
             (('needs_you', 3), ('blocked', 2), ('waiting_other', 1), ('stale', 1))),
        -n['msgs']))
    themes = []
    for n in out:
        th = next((x for x in themes if x['theme'] == n['theme']), None)
        if th is None:
            th = {'theme': n['theme'], 'theme_label': n['theme_label'],
                  'subs': [], 'tasks': 0, 'msgs': 0,
                  'attention': 0}
            themes.append(th)
        attn = sum(n['states'][s] * w for s, w in
                   (('needs_you', 3), ('blocked', 2), ('waiting_other', 1), ('stale', 1)))
        th['subs'].append({**n, 'attention': attn})
        th['tasks'] += n['tasks']; th['msgs'] += n['msgs']; th['attention'] += attn
    themes.sort(key=lambda x: -x['attention'])
    log(f"Built map: {len(themes)} themes, {len(out)} sub-theme nodes")
    return themes

# ---------------------------------------------------------------------------
# 9. UNBLOCK SCORE — GitHub-style 90-day wall of how fast work moved.
#    Deterministic: daily message volume (activity), nudge density +
#    unanswered questions to you (friction), resolution-speak (progress).
# ---------------------------------------------------------------------------
def build_health_wall():
    excluded = ed.load_excluded()
    group_to_theme, _ = ed.load_themes()
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = (now - datetime.timedelta(days=HEALTH_DAYS)).isoformat()

    conn = sqlite3.connect(ed.MESSAGES_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH DATABASE '{ed.WHATSAPP_DB}' AS wa")

    rows = conn.execute("""
        SELECT m.content, m.timestamp, m.is_from_me,
               ms.sender_jid as sender_jid, ch.name as chat_name
        FROM messages m
        JOIN chats ch ON m.chat_jid = ch.jid
        LEFT JOIN wa.whatsmeow_message_secrets ms ON m.id = ms.message_id AND m.chat_jid = ms.chat_jid
        LEFT JOIN wa.whatsmeow_chat_settings cs ON m.chat_jid = cs.chat_jid
        WHERE m.timestamp >= ? AND m.content IS NOT NULL AND m.content != ''
          AND ch.jid != 'status@broadcast' AND ch.jid LIKE '%@g.us'
          AND (cs.archived IS NULL OR cs.archived = 0)
        ORDER BY m.timestamp ASC
    """, (cutoff,)).fetchall()
    conn.close()

    days = {}
    q_flagged = {}   # date -> count of unanswered questions aimed at you
    pending_qs = []  # (ts, is aimed at the user)
    mine_ts = []
    parsed = []
    for r in rows:
        gname = (r['chat_name'] or '').strip()
        if not gname or gname.lower() in excluded or not group_to_theme.get(gname.lower()):
            continue
        body = ed.resolve_mentions(r['content'].strip())
        if BOT_RE.search(body):
            continue
        ts = parse_ts(r['timestamp'])
        if not ts:
            continue
        parsed.append({'ts': ts, 'body': body, 'from_me': bool(r['is_from_me'])})
        if r['is_from_me']:
            mine_ts.append(ts)

    for m in parsed:
        d = m['ts'].date().isoformat()
        day = days.setdefault(d, {'msgs': 0, 'nudges': 0, 'resolutions': 0})
        day['msgs'] += 1
        if NUDGE_RE.search(m['body']):
            day['nudges'] += 1
        if RESOLUTION_RE.search(m['body']):
            day['resolutions'] += 1
        if not m['from_me'] and '?' in m['body'] and ME and ME in m['body'].lower():
            pending_qs.append(m)

    # a question counts as unanswered if no message from you within 48h after
    for m in pending_qs:
        answered = any(t > m['ts'] and t - m['ts'] <= datetime.timedelta(hours=UNANSWERED_HOURS)
                       for t in mine_ts)
        if not answered:
            d = m['ts'].date().isoformat()
            q_flagged[d] = q_flagged.get(d, 0) + 1

    out_days = []
    for i in range(HEALTH_DAYS - 1, -1, -1):
        d = (now.date() - datetime.timedelta(days=i)).isoformat()
        v = days.get(d)
        if not v:
            out_days.append({'date': d, 'score': None, 'msgs': 0,
                             'nudges': 0, 'resolutions': 0, 'qs': 0})
            continue
        friction = v['nudges'] + 3 * q_flagged.get(d, 0)
        activity = min(v['msgs'] / 25.0, 1.0)
        friction_ratio = min(friction / max(v['msgs'], 1) * 6.0, 1.0)
        resolution_ratio = min(v['resolutions'] / max(v['msgs'], 1) * 8.0, 1.0)
        score = round(100 * (0.45 * activity + 0.35 * (1 - friction_ratio) + 0.20 * resolution_ratio))
        out_days.append({'date': d, 'score': score, 'msgs': v['msgs'],
                         'nudges': v['nudges'], 'resolutions': v['resolutions'],
                         'qs': q_flagged.get(d, 0)})

    scored = [d['score'] for d in out_days if d['score'] is not None]
    recent = [d['score'] for d in out_days[-14:] if d['score'] is not None]
    prev = [d['score'] for d in out_days[-28:-14] if d['score'] is not None]
    score = round(sum(recent) / len(recent)) if recent else 0
    prev_score = round(sum(prev) / len(prev)) if prev else score
    log(f"Health wall: {HEALTH_DAYS}d built, unblock score {score} (prev 14d: {prev_score})")
    return {'days': out_days, 'score': score, 'prev_score': prev_score,
            'best': max(scored) if scored else 0}

# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {'threads_v3': {}, 'bulletins_v2': {}, 'days': {}, 'week': {}, 'reconcile': {}}

def save_state(s):
    json.dump(s, open(STATE_FILE, 'w'), indent=1, default=str)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def build_group_jid_map():
    try:
        conn = sqlite3.connect(ed.MESSAGES_DB)
        conn.row_factory = sqlite3.Row
        m = {}
        for r in conn.execute("SELECT name, jid FROM chats WHERE jid LIKE '%@g.us' AND name IS NOT NULL"):
            m.setdefault(r['name'].strip().lower(), r['jid'])
        conn.close()
        log(f"Built group_jids map: {len(m)} groups")
        return m
    except Exception as e:
        log(f"group_jids map failed: {e}")
        return {}

def main():
    state = load_state()
    state.pop('threads', None)          # drop legacy v2 cache
    msgs, theme_meta = fetch_messages(TASK_WINDOW_DAYS)
    threads = cluster_threads(msgs)

    # incremental: reuse cached ITEMS when thread end unchanged (signals are
    # deterministic — recomputed fresh every run from the messages themselves)
    cache = state.setdefault('threads_v3', {})
    todo, extracted = [], {}
    for t in threads:
        c = cache.get(t['key'])
        if c and c.get('end') == t['end'].isoformat():
            # re-normalize cached items (quality gates evolve between runs)
            items = [x for x in (normalize_item(i) for i in c['items']) if x]
            if items:
                extracted[t['key']] = {'items': items}
        elif worth_llm(t):
            todo.append(t)
    gated_off = sum(1 for t in threads if t['key'] not in extracted and t not in todo)
    log(f"Threads: {len(threads)} total, {len(todo)} new/changed to extract, {gated_off} tiny no-signal skipped")

    if todo and client():
        done = 0
        with ThreadPoolExecutor(MAX_PARALLEL) as ex:
            futs = {ex.submit(extract_thread, t, theme_meta.get(t['theme'], {}).get('label', t['theme'])): t
                    for t in todo}
            for fut in futs:
                t = futs[fut]
                data = fut.result()
                done += 1
                if done % 25 == 0:
                    log(f"  extracted {done}/{len(todo)}")
                if data:
                    extracted[t['key']] = data
                    cache[t['key']] = {'end': t['end'].isoformat(), 'items': data['items']}
    elif todo:  # no API key -> heuristic
        for t in todo:
            data = heuristic_extract(t)
            if not data:
                continue
            extracted[t['key']] = data
            cache[t['key']] = {'end': t['end'].isoformat(), 'items': data['items']}

    cands = []
    for t in threads:
        d = extracted.get(t['key'])
        if d and d.get('items'):
            cands.append({'theme': t['theme'], 'thread': t, **d})
    total_items = sum(len(c['items']) for c in cands)
    log(f"Candidates with actionable items: {len(cands)} ({total_items} atomic items)")

    tasks = merge_candidates(cands)
    before = len(tasks)
    tasks = [t for t in tasks
             if not (t['status'] == 'completed' and t['age_days'] >= PRUNE_COMPLETED_DAYS)]

    # cross-chat completion check for stale open items (>= STALE_DAYS silent):
    # completions often land in another thread/group than the original ask
    tasks, dropped_ids = reconcile_stale(tasks, msgs, state)
    if dropped_ids:
        log(f"  dropped obsolete: {[d.split('::')[-1][:36] for d in dropped_ids[:8]]}")

    # MECE thread-health state + ordering
    tasks = tag_topics(tasks, theme_meta, state)
    for t in tasks:
        t['state'] = derive_state(t)
        t['theme_label'] = theme_meta.get(t['theme'], {}).get('label', t['theme'])
    tasks.sort(key=lambda t: (STATE_RANK[t['state']], t['age_days']))
    state_counts = {}
    for t in tasks:
        state_counts[t['state']] = state_counts.get(t['state'], 0) + 1
    log(f"Tasks: {before} -> {len(tasks)} | states: {state_counts}")

    atlas = build_map(tasks, theme_meta)
    wall = build_health_wall()

    bulletins = build_bulletins(msgs, state)
    days = build_days(msgs, state, theme_meta)
    week = build_week(days, msgs, state, theme_meta)

    out = {
        'exported_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'window_days': TASK_WINDOW_DAYS,
        'total_groups': len({m['group'] for m in msgs}),
        'total_messages': len(msgs),
        'total_tasks': len(tasks),
        'tasks': tasks,
        'group_jids': build_group_jid_map(),
        'map': atlas,
        'health_wall': wall,
        'bulletins': bulletins,
        'days': days,
        'week': week,
    }
    json.dump(out, open(OUTPUT_FILE, 'w'), indent=1, default=str)
    save_state(state)
    log(f"Wrote {OUTPUT_FILE} — {len(tasks)} tasks, {len(atlas)} map nodes, unblock score {wall['score']}")

if __name__ == '__main__':
    main()
