"""Synthetic demo workspace — same schema as build_data.py output, zero PII."""
import datetime, json, random

PEOPLE = ['Alex Morgan', 'Priya Nair', 'Rohan Mehta', 'Sara Iqbal',
          'Dev Patel', 'Neha Rao', 'Kiran Das', 'Anika Shah']
THEMES = [
    ('work', 'Work', ['planning', 'launches', 'reviews', 'hiring', 'ops']),
    ('product', 'Product', ['roadmap', 'bugs', 'design', 'analytics', 'mobile']),
    ('growth', 'Growth', ['campaigns', 'onboarding', 'referrals', 'pricing', 'email']),
    ('personal', 'Personal', ['travel', 'finance', 'health', 'events', 'home']),
    ('chats', 'Chats', ['general', 'announcements', 'intros', 'events', 'random']),
]
# tasks per sub-index within a theme + their states (heat concentrated up front)
SUB_PLAN = [
    (3, ['needs_you', 'needs_you', 'blocked']),
    (2, ['blocked', 'waiting_other']),
    (2, ['waiting_other', 'active']),
    (2, ['active', 'stale']),
    (1, ['done']),
]
STATUS_OF = {'needs_you': 'running', 'blocked': 'blocked', 'waiting_other': 'running',
             'active': 'running', 'stale': 'running', 'done': 'completed'}
TITLES = [
    ('Decide launch date for v2 rollout', 'Launch checklist is 90% done, need a final call on the date'),
    ('Unblock the checkout success dip', 'Success rate dipped 2 points after the latest release'),
    ('Review the onboarding experiment', 'Variant B is winning by a clear margin this week'),
    ('Confirm copy for the festive campaign', 'Legal approved, need brand sign-off by Friday'),
    ('Fix the invite deep-link on Android', 'Repro documented, fix is in review'),
    ('Chase vendor invoice approval', 'Finance needs the approved quote re-sent'),
    ('Prep the monthly business review', 'Draft is ready, metrics need one refresh'),
    ('Align on Q4 hiring plan', 'Two roles approved, two still under discussion'),
    ('Follow up on the partner API keys', 'Staging keys issued, production pending infosec'),
    ('Close the refund policy thread', 'Policy agreed, needs a closing message in group'),
    ('Nudge design for empty-state assets', 'Specs shared last week, no update since'),
    ('Verify analytics event naming', 'Three events renamed, dashboard filters updated'),
    ('Scope the referral leaderboard', 'Top 3 designs shortlisted, need a tie-break vote'),
    ('Debug push notification delays', 'P99 latency doubled on the new provider'),
    ('Approve the pricing page rewrite', 'New tiers explained, FAQ still missing'),
    ('Plan the offsite agenda', 'Venue booked, sessions need owners'),
    ('Review crash reports from Friday', 'One new signature in the top 5 crashes'),
    ('Localize the paywall for Spanish', 'Strings frozen, translators booked'),
    ('Audit notification preferences', 'Opt-out rate crept up after the last blast'),
    ('Ship the dark-mode toggle', 'Behind a flag, rolling to 10% today'),
    ('Reconcile the ad spend report', 'Two invoices mismatch the dashboard totals'),
    ('Interview loop for iOS engineer', 'Three screens done, onsite pending'),
    ('Migrate the email templates', 'New builder ready, 12 templates to port'),
    ('Settle the data retention policy', 'Legal wants 90 days, eng prefers 30'),
    ('Prototype the widget gallery', 'Three concepts, testing one with users'),
    ('Renew the vendor contracts', 'Two renewals due end of quarter'),
    ('Write the launch retrospective', 'Survey out, synthesis starts Monday'),
    ('Triage the support backlog', 'Oldest ticket is 11 days old, needs an owner'),
    ('Refresh the status page copy', 'Incident wording approved by support'),
    ('Evaluate the new search vendor', 'POC results beat the baseline by 18%'),
    ('Book flights for the team offsite', 'Three itineraries on the table, need a pick'),
    ('Confirm the dinner reservation', 'Friday 8pm holds 12, need a final headcount'),
    ('Split the grocery bill fairly', 'Receipt posted, two items disputed'),
    ('Pick a weekend trek route', 'Two moderate trails shortlisted near town'),
    ('Renew the apartment lease', 'Landlord offered 5% hike, counter with 3%'),
    ('Schedule the dentist appointment', 'Two slots open next week'),
    ('Organize the photo album', 'Beach trip folder needs sorting'),
    ('Vote on the movie night pick', 'Comedy leads by two votes'),
    ('Plan the anniversary surprise', 'Venue shortlisted, guest list pending'),
    ('Welcome the new joiners', 'Intros posted, buddy pairs assigned'),
]
MSGS = [
    'Draft is ready for review, please check the latest build.',
    'Blocked on confirmation, ETA awaited by EOD.',
    'Can you share the updated numbers for this week?',
    'Fix is in QA, will confirm rollout timing shortly.',
    'Any update on the pending approval?',
    'Metrics look healthy, WoW uptick holding steady.',
    'Flagging a dip in success rate, investigating now.',
    'Approved to proceed, tracking conversion closely.',
    'Reminder: pending RCA from yesterday, please post it here.',
    'New experiment variant is live for 10% traffic.',
]


def write(path):
    rng = random.Random(7)
    now = datetime.datetime.now(datetime.timezone.utc)
    tasks, groups_seen, ti = [], set(), 0
    for theme, tlabel, subs in THEMES:
        for si, sub in enumerate(subs):
            count, states = SUB_PLAN[si % len(SUB_PLAN)]
            for j in range(count):
                title, one = TITLES[ti % len(TITLES)]
                state = states[j % len(states)]
                gname = f'Demo Group {(ti % 6) + 1}'
                groups_seen.add(gname)
                age_d = (ti * 3) % 11
                last = now - datetime.timedelta(days=age_d, hours=(ti * 5) % 20)
                senders = rng.sample(PEOPLE, 3)
                msgs = [{'sender': senders[(ti + m) % 3], 'body': MSGS[(ti + m) % len(MSGS)],
                         'time': (last - datetime.timedelta(hours=m * 5)).isoformat()}
                        for m in range(6)]
                tasks.append({
                    'id': f'demo::{theme}::{ti}', 'theme': theme, 'sub_theme': sub,
                    'topics': [sub], 'title': title, 'one_liner': one, 'kind': 'ask',
                    'status': STATUS_OF[state],
                    'owner': 'You' if state == 'needs_you' else senders[0],
                    'requester': senders[1] if state != 'waiting_other' else 'You',
                    'due': '', 'needs_my_action': state == 'needs_you',
                    'waiting_on': senders[2] if state == 'waiting_other' else '',
                    'stale': state == 'stale', 'nudges': ti % 3,
                    'age': 'today' if age_d == 0 else f'{age_d}d',
                    'age_days': age_d, 'msg_count': 6, 'recap': one,
                    'people': senders, 'participants': senders,
                    'groups': [gname], 'group': gname, 'last_ts': last.isoformat(),
                    'messages': msgs, 'state': state, 'theme_label': tlabel,
                })
                ti += 1
    atlas = []
    for theme, tlabel, subs in THEMES:
        for sub in subs:
            sts = [t for t in tasks if t['theme'] == theme and t['sub_theme'] == sub]
            if not sts:
                continue
            states = {s: 0 for s in ('needs_you', 'blocked', 'waiting_other', 'active', 'stale', 'done')}
            for t in sts:
                states[t['state']] += 1
            attn = states['needs_you'] * 3 + states['blocked'] * 2 + states['waiting_other']
            atlas.append({'theme': theme, 'theme_label': tlabel, 'sub': sub,
                          'label': sub.replace('-', ' '), 'tasks': len(sts),
                          'msgs': sum(t['msg_count'] for t in sts),
                          'states': states, 'attention': attn})
    days = []
    for i in range(90, 0, -1):
        d = (now - datetime.timedelta(days=i)).date().isoformat()
        if i > 14:
            days.append({'date': d, 'score': None, 'msgs': 0, 'nudges': 0, 'resolutions': 0, 'qs': 0})
        else:
            m = 20 + (i * 7) % 40
            days.append({'date': d, 'score': 55 + (i * 13) % 35, 'msgs': m,
                         'nudges': m // 12, 'resolutions': m // 20, 'qs': 0})
    scored = [d['score'] for d in days if d['score'] is not None]
    bulletins = []
    for h in (0, 3, 6, 9, 12, 15, 18, 21):
        start = (now - datetime.timedelta(hours=h)).replace(minute=0, second=0, microsecond=0)
        devs = [{'text': TITLES[(h + j) % len(TITLES)][1],
                 'group': f'Demo Group {(h + j) % 6 + 1}',
                 'theme': THEMES[(h + j) % len(THEMES)][0], 'msg_count': 6 - j % 4}
                for j in range(3)]
        bulletins.append({'start': start.isoformat(), 'developments': devs})
    out = {
        'exported_at': now.isoformat(), 'window_days': 30,
        'total_groups': len(groups_seen), 'total_messages': len(tasks) * 6,
        'total_tasks': len(tasks), 'tasks': tasks, 'group_jids': {},
        'map': [{'theme': t[0], 'theme_label': t[1],
                 'tasks': sum(1 for x in tasks if x['theme'] == t[0]),
                 'msgs': sum(x['msg_count'] for x in tasks if x['theme'] == t[0]),
                 'attention': sum(a['attention'] for a in atlas if a['theme'] == t[0]),
                 'subs': [a for a in atlas if a['theme'] == t[0]]} for t in THEMES],
        'health_wall': {'days': days, 'score': 78, 'prev_score': 71,
                        'best': max(scored) if scored else 0},
        'bulletins': bulletins, 'days': days[-7:],
        'week': {'intro': 'Demo week: launches on track, a few threads need you.'},
    }
    with open(path, 'w') as f:
        json.dump(out, f, indent=1)
    return f"{len(tasks)} demo tasks"


if __name__ == '__main__':
    import sys
    print(write(sys.argv[1] if len(sys.argv) > 1 else 'frontend_data.json'))
