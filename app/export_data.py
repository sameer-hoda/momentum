"""
Fast data export — no LLM calls.
Extracts messages from DB, groups by theme, outputs JSON for the frontend.
"""
import os, sys, re, json, sqlite3, datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STORE_DIR = os.environ.get('STORE_DIR', os.path.join(os.path.dirname(SCRIPT_DIR), 'store-fallback'))
GEMINI_KEY_FILE = os.path.join(STORE_DIR, 'gemini.key')


def get_gemini_key():
    """API key from env, else the file saved via the in-UI key step."""
    key = (os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY') or '').strip()
    if key:
        return key
    try:
        with open(GEMINI_KEY_FILE) as f:
            return f.read().strip()
    except Exception:
        return ''
MESSAGES_DB = os.path.join(STORE_DIR, 'messages.db')
WHATSAPP_DB = os.path.join(STORE_DIR, 'whatsapp.db')
THEME_FILE = os.path.join(SCRIPT_DIR, 'theme_groups.json')
EXCLUDE_FILE = os.path.join(SCRIPT_DIR, 'group_exclude_list.txt')
OUTPUT_FILE = os.path.join(SCRIPT_DIR, 'frontend_data.json')

def load_excluded():
    excluded = set()
    if os.path.exists(EXCLUDE_FILE):
        with open(EXCLUDE_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    excluded.add(line.lower())
    return excluded

THEME_LABELS = {
    'autopay': 'Autopay', 'upi': 'UPI', 'rupay': 'RuPay',
    'wallet_ppi': 'Wallet', 'bbps': 'Bills', 'ccbp': 'CCBP',
    'rewards': 'Rewards', 'branding': 'Brand', 'gtm_merchant': 'Merchants',
    'growth_mtu': 'Growth', 'comms': 'Comms', 'p2p': 'P2P',
    'revenue_subscription': 'Revenue', 'cred_pay': 'CRED Pay',
    'bio_auth': 'Verification', 'win': 'Win', 'dms': 'DMs', 'people': 'People',
    'ops': 'Ops', 'finance': 'Finance', 'chats': 'Chats', 'other': 'Other',
}

# Ordered first-match-wins. Plain tokens are substrings of the lowered
# group name; 're:' tokens are regexes for short ambiguous words.
THEME_RULES = [
    ('dms', ['re:^[\\d\\s+\\-()]+$']),
    ('win', ['re:\\bwin\\b']),
    ('autopay', ['autopay', 'e-mandate', 'emandate', 'nach', 'si hub']),
    ('upi', ['upi', 'token']),
    ('rupay', ['rupay']),
    ('wallet_ppi', ['wallet', 'ppi', 'gift card', 'giftcard']),
    ('bbps', ['bbps', 'bill payment', 'electricity', 'dth', 'recharge', 'fastag']),
    ('ccbp', ['ccbp', 'cbcc']),
    ('rewards', ['reward', 'cashback', 'coin', 'jackpot', 'scratch']),
    ('branding', ['brand', 're:\\bads\\b', 'advertisement', 'campaign', 'marketing', 'creative']),
    ('gtm_merchant', ['merchant', 'gtm', 'soundbox', 'enterprise']),
    ('growth_mtu', ['mtu', 'activation', 'retention', 'cross sell', 'cross-sell',
                    'winback', 'win back', 'referral', 'okr', 'north star',
                    're:\\b\\d{2}m\\b', 're:\\bcore\\b']),
    ('comms', ['comms', 'sms', 'push', 'notification', 'otp', 'email', 'whatsapp']),
    ('p2p', ['p2p']),
    ('revenue_subscription', ['revenue', 'subscription', 'monet', 'arpu']),
    ('cred_pay', ['cred pay', 'pay']),
    ('bio_auth', ['kyc', 'bio', 'biometric', 'digilocker', 'verification', 'face id']),
    ('people', ['hiring', 'interview', 'payroll', 'onboarding']),
    ('ops', ['ops', 'launch', 'release', 'deploy', 'incident', 'outage',
             'standup', 'stand-up', 'all hands', 'sprint', 'retro', 'action items',
             'go-live', 'go live']),
    ('finance', ['finance', 'invoice', 'legal', 're:\\bnda\\b', 'budget', 'procurement',
                 'vendor', 'payout', 'settlement']),
]


def classify_group_theme(name):
    """Keyword theme for a group name; 'other' when nothing matches."""
    low = (name or '').lower()
    for theme, tokens in THEME_RULES:
        for tok in tokens:
            if tok.startswith('re:'):
                if re.search(tok[3:], low):
                    return theme
            elif tok in low:
                return theme
    return 'other'


def is_auto_placeholder(data):
    """True when theme_groups.json is still the first-run auto-map: every
    group dumped into one 'chats' bucket. Classified properly instead."""
    if not isinstance(data, dict) or set(data.keys()) != {'chats'}:
        return False
    try:
        desc = (data['chats'].get('description') or '').lower()
    except Exception:
        return False
    return 'auto-mapped' in desc


def resolve_theme(gname, group_to_theme):
    """Explicit custom mapping wins; otherwise classify; never empty."""
    hit = (group_to_theme or {}).get((gname or '').strip().lower())
    if hit:
        return hit
    return classify_group_theme(gname)


def load_themes():
    with open(THEME_FILE) as f:
        data = json.load(f)
    # Canonical labels first so classified themes always have board metadata;
    # an explicit custom file overrides labels but never loses them.
    theme_meta = {k: {'label': v, 'owner': ''}
                  for k, v in THEME_LABELS.items()}
    if is_auto_placeholder(data):
        return {}, theme_meta
    # Build group_name -> theme_key mapping
    group_to_theme = {}
    for key, val in data.items():
        theme_meta[key] = {'label': val['label'], 'owner': val.get('owner', '')}
        for g in val['groups']:
            group_to_theme[g.strip().lower()] = key
    return group_to_theme, theme_meta

def extract_people(body):
    """@-mention extraction from message bodies (no hardcoded names)."""
    return sorted(set(re.findall(r'@(\w[\w.]{1,30})', body or '')))

def extract_status(body, msg_count):
    """Heuristic status detection (word-boundary matching — 'done' must
    not fire inside 'undone', 'live' not inside 'deliver')."""
    body = body or ''
    if re.search(r'\b(blocked|blocking|stuck|issues?|errors?|bugs?|broken)\b', body, re.I):
        return 'blocked'
    if re.search(r'\b(live|launched|went live|shipped|completed|done|achieved|closed)\b', body, re.I):
        return 'completed'
    if re.search(r'\b(testing|working on|in progress|investigating|discussing|finalizing)\b', body, re.I):
        return 'running'
    return 'running'

THEME_COLORS = {
    'cred_pay': 'bg-emerald-500', 'upi': 'bg-emerald-500', 'bio_auth': 'bg-emerald-500',
    'ccbp': 'bg-blue-500', 'win': 'bg-amber-500', 'rewards': 'bg-violet-500',
    'bbps': 'bg-blue-500', 'rupay': 'bg-emerald-500', 'wallet_ppi': 'bg-violet-500',
    'growth_mtu': 'bg-pink-500', 'branding': 'bg-cyan-500', 'gtm_merchant': 'bg-orange-500',
    'comms': 'bg-stone-500', 'p2p': 'bg-stone-500', 'autopay': 'bg-blue-500',
    'revenue_subscription': 'bg-stone-500',
}

# ---------------------------------------------------------------------------
# Contact resolution — per all_docs/whatsapp_database_master_guide.md §4
#
# senders come through whatsmeow_message_secrets.sender_jid and may be:
#   phone JID  : 15551234567@s.whatsapp.net
#   LID        : 100000000000001@lid     <- 24k+ msgs, never matches the
#                                          contacts JOIN directly
#   device JID : 15551234567:4@s.whatsapp.net
#   system     : 0@s.whatsapp.net
#
# Resolution path: LID -> whatsmeow_lid_map.pn -> {pn}@s.whatsapp.net
#                  -> whatsmeow_contacts (push_name, full_name, first_name, business_name)
# Mentions inside message bodies (@200471977451667) resolve the same way.
# ---------------------------------------------------------------------------

LID_TO_PN = {}     # lid -> pn
JID_TO_NAME = {}   # "pn@s.whatsapp.net" -> display name

def load_contact_index(conn):
    for row in conn.execute("SELECT lid, pn FROM wa.whatsmeow_lid_map"):
        LID_TO_PN[row['lid']] = row['pn']
    for row in conn.execute(
        "SELECT their_jid, push_name, full_name, first_name, business_name "
        "FROM wa.whatsmeow_contacts"
    ):
        name = row['push_name'] or row['full_name'] or row['first_name'] or row['business_name']
        if name:
            JID_TO_NAME[row['their_jid']] = name
    print(f"Loaded {len(LID_TO_PN)} lid mappings, {len(JID_TO_NAME)} contact names", flush=True)

def _fmt_phone(pn):
    """Format a phone number like 15551234567 -> +1 555 123 4567 (no name fallback)."""
    pn = pn.strip()
    if pn.startswith('91') and len(pn) == 12:
        return f'+91 {pn[2:7]} {pn[7:]}'
    if pn.startswith('0') and len(pn) == 11:
        return f'+{pn[1:5]} {pn[5:]}'
    if len(pn) == 10:
        return f'{pn[:5]} {pn[5:]}'
    return pn

def _jid_to_name(jid):
    """Resolve a full JID (phone/LID/device) to a display name."""
    if not jid:
        return None
    jid = jid.strip()
    # strip device suffix: 15551234567:4@s.whatsapp.net -> 15551234567@s.whatsapp.net
    jid = re.sub(r'^([^:@]+):\d+@', r'\1@', jid)
    pn = None
    if jid.endswith('@lid'):
        pn = LID_TO_PN.get(jid[:-len('@lid')])
        if pn:
            jid = f'{pn}@s.whatsapp.net'
    name = JID_TO_NAME.get(jid)
    if name:
        return name
    # no saved name but we know the phone -> readable fallback
    if jid.endswith('@s.whatsapp.net'):
        pn = jid.split('@')[0]
    if pn:
        return _fmt_phone(pn)
    return None

def resolve_id(raw):
    """Resolve a bare ID (lid or phone number) to a display name/phone, else None."""
    if not raw:
        return None
    raw = raw.strip()
    name = JID_TO_NAME.get(f'{raw}@s.whatsapp.net')
    if name:
        return name
    pn = LID_TO_PN.get(raw)
    if pn:
        return JID_TO_NAME.get(f'{pn}@s.whatsapp.net') or _fmt_phone(pn)
    return None

def resolve_mentions(text):
    """Replace @123456789012 mentions inside a message body with @Name."""
    if not text or '@' not in text:
        return text
    def repl(m):
        name = resolve_id(m.group(1))
        return f'@{name}' if name else m.group(0)
    return re.sub(r'@(\d{8,})', repl, text)

def main():
    excluded = load_excluded()
    group_to_theme, theme_meta = load_themes()
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff_7d = (now - datetime.timedelta(days=7)).isoformat()
    cutoff_15d = (now - datetime.timedelta(days=15)).isoformat()

    conn = sqlite3.connect(MESSAGES_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH DATABASE '{WHATSAPP_DB}' AS wa")

    load_contact_index(conn)

    # Find active non-archived groups
    active = conn.execute("""
        SELECT DISTINCT ch.name, COUNT(m.id) as msg_count
        FROM chats ch
        JOIN messages m ON m.chat_jid = ch.jid
        LEFT JOIN wa.whatsmeow_chat_settings cs ON ch.jid = cs.chat_jid
        WHERE m.timestamp >= ? AND m.content IS NOT NULL AND m.content != ''
          AND ch.jid != 'status@broadcast' AND (ch.jid LIKE '%@g.us')
          AND (cs.archived IS NULL OR cs.archived = 0)
        GROUP BY ch.name ORDER BY msg_count DESC
    """, (cutoff_7d,)).fetchall()

    group_names = [g['name'] for g in active if g['name'] and g['name'].lower() not in excluded]
    print(f"Found {len(group_names)} active groups", flush=True)

    # Fetch messages
    all_msgs = []
    for i in range(0, len(group_names), 100):
        batch = group_names[i:i+100]
        ph = ','.join('?' * len(batch))
        rows = conn.execute(f"""
            SELECT m.content, m.timestamp, m.is_from_me,
                ms.sender_jid as sender_jid,
                COALESCE(c.push_name, c.full_name, c.first_name, c.business_name,
                    SUBSTR(ms.sender_jid,1,INSTR(ms.sender_jid,'@')-1)) as sender,
                ch.name as chat_name
            FROM messages m
            JOIN chats ch ON m.chat_jid = ch.jid
            LEFT JOIN wa.whatsmeow_message_secrets ms ON m.id=ms.message_id AND m.chat_jid=ms.chat_jid
            LEFT JOIN wa.whatsmeow_contacts c ON ms.sender_jid=c.their_jid
            WHERE m.timestamp >= ? AND m.content IS NOT NULL AND m.content != ''
              AND TRIM(ch.name) IN ({ph})
            ORDER BY ch.name, m.timestamp ASC
        """, [cutoff_15d] + batch).fetchall()
        all_msgs.extend(rows)
    conn.close()
    print(f"Fetched {len(all_msgs)} messages", flush=True)

    # Group by theme
    theme_groups = {}
    for m in all_msgs:
        gname = (m['chat_name'] or '').strip()
        theme_key = resolve_theme(gname, group_to_theme)
        if theme_key not in theme_groups:
            theme_groups[theme_key] = []
        theme_groups[theme_key].append(m)

    # Build output
    tasks = []
    for theme_key, msgs in sorted(theme_groups.items()):
        if not msgs: continue
        meta = theme_meta.get(theme_key, {'label': theme_key, 'owner': ''})
        color = THEME_COLORS.get(theme_key, 'bg-stone-500')

        # Group messages into threads by topic keywords
        # For simplicity, create one task per group per theme
        group_msgs = {}
        for m in msgs:
            gn = m['chat_name'] or 'Unknown'
            if gn not in group_msgs:
                group_msgs[gn] = []
            group_msgs[gn].append(m)

        for gname, gmsgs in sorted(group_msgs.items()):
            bodies = [resolve_mentions(m['content'].strip()) for m in gmsgs if m['content']]
            if not bodies: continue
            combined = ' '.join(bodies)
            ts = gmsgs[-1]['timestamp'] or cutoff_15d
            try:
                age_days = (now - datetime.datetime.fromisoformat(ts.replace(' ', 'T'))).days
            except:
                age_days = 0

            status = extract_status(combined, len(gmsgs))
            people = extract_people(combined)

            tasks.append({
                'id': f"{theme_key}_{gname[:20]}_{ts[:10]}",
                'title': gname,
                'fact': combined[:400],
                'status': status,
                'age': f"{age_days}d",
                'theme': theme_key,
                'theme_label': meta['label'],
                'theme_dot': color,
                'group': gname,
                'people': people,
                'messages': [{
                    'sender': ('You' if m['is_from_me']
                               else (_jid_to_name(m['sender_jid']) or m['sender'] or 'Unknown')),
                    'body': resolve_mentions(m['content'].strip()),
                    'time': m['timestamp'],
                } for m in gmsgs[-10:]],  # last 10 messages, sender via LID resolver
            })

    output = {
        'exported_at': now.isoformat(),
        'total_groups': len(group_names),
        'total_messages': len(all_msgs),
        'total_tasks': len(tasks),
        'tasks': tasks,
    }

    with open(OUTPUT_FILE, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    print(f"Exported {len(tasks)} tasks to {OUTPUT_FILE}", flush=True)

if __name__ == '__main__':
    main()