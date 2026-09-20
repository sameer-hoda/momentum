"""Tests for the Nudge tab scan (Agent 4). Stdlib unittest, no network/LLM."""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from nudge_scan import scan_nudges

OWNER = 'Sameer'


def msg(sender, body, minutes_ago, now, group='Family', task_id=None):
    m = {'sender': sender, 'body': body,
         'time': (now - datetime.timedelta(minutes=minutes_ago)).isoformat(),
         'group': group}
    if task_id is not None:
        m['task_id'] = task_id
    return m


def task(tid, group='Family', waiting_on='', needs_my_action=False,
         nudges=0, last_ts_min_ago=2, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return {'id': tid, 'group': group, 'waiting_on': waiting_on,
            'needs_my_action': needs_my_action, 'nudges': nudges,
            'last_ts': (now - datetime.timedelta(minutes=last_ts_min_ago)).isoformat()}


class ScanNudgesTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime.now(datetime.timezone.utc)

    def test_direct_question_no_reply_emits_nudge_with_send_action(self):
        messages = [msg('Aisha', 'Hey Sameer, can you share the numbers?', 2, self.now)]
        nudges = scan_nudges([], messages, self.now, owner_name=OWNER)
        self.assertEqual(len(nudges), 1)
        n = nudges[0]
        for key in ('id', 'task_id', 'group', 'reason', 'why_now',
                    'suggested_move', 'quick_actions'):
            self.assertIn(key, n)
        kinds = [a['kind'] for a in n['quick_actions']]
        self.assertIn('send_nudge', kinds)
        self.assertEqual(n['reason'], 'direct_address')

    def test_user_replied_after_question_suppresses_nudge(self):
        messages = [
            msg('Aisha', 'Hey Sameer, can you share the numbers?', 4, self.now),
            msg('Sameer', 'Sending them over now', 1, self.now),
        ]
        nudges = scan_nudges([], messages, self.now, owner_name=OWNER)
        self.assertEqual(nudges, [])

    def test_banter_emits_nothing(self):
        messages = [msg('Aisha', 'lol nice', 1, self.now)]
        self.assertEqual(scan_nudges([], messages, self.now, owner_name=OWNER), [])

    def test_old_question_outside_window_emits_nothing(self):
        messages = [msg('Aisha', 'Hey Sameer, can you share the numbers?', 60, self.now)]
        self.assertEqual(
            scan_nudges([], messages, self.now, window_min=5, owner_name=OWNER), [])

    def test_fyi_link_emits_nothing(self):
        messages = [msg('Aisha', 'FYI: good read https://example.com/report', 1, self.now)]
        self.assertEqual(scan_nudges([], messages, self.now, owner_name=OWNER), [])

    def test_cap_limits_output_to_seven(self):
        messages = [msg(f'Person{i}', f'Hey Sameer, question {i}?', 2, self.now,
                        group=f'Group{i}') for i in range(10)]
        nudges = scan_nudges([], messages, self.now, owner_name=OWNER)
        self.assertEqual(len(nudges), 7)

    def test_empty_inputs_return_empty(self):
        self.assertEqual(scan_nudges([], [], self.now, owner_name=OWNER), [])

    def test_waiting_on_with_fresh_activity_emits_nudge(self):
        t = task('t1', waiting_on='Vendor', nudges=2, last_ts_min_ago=2, now=self.now)
        nudges = scan_nudges([t], [], self.now, owner_name=OWNER)
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0]['reason'], 'waiting_on')
        self.assertEqual(nudges[0]['task_id'], 't1')

    def test_ranking_direct_address_first(self):
        t = task('t1', group='Vendors', waiting_on='Vendor', nudges=2,
                 last_ts_min_ago=2, now=self.now)
        messages = [msg('Aisha', 'Hey Sameer, can you share the numbers?', 2, self.now)]
        nudges = scan_nudges([t], messages, self.now, owner_name=OWNER)
        reasons = [n['reason'] for n in nudges]
        self.assertIn('direct_address', reasons)
        self.assertIn('waiting_on', reasons)
        self.assertLess(reasons.index('direct_address'), reasons.index('waiting_on'))

    def test_overrides_hide_snoozed_and_closed(self):
        messages = [msg('Aisha', 'Hey Sameer, can you share the numbers?', 2, self.now,
                        group='Family', task_id='t9')]
        recent = (self.now - datetime.timedelta(hours=1)).isoformat()
        overrides = {'t9': {'status': 'snoozed', 'ts': recent}}
        self.assertEqual(
            scan_nudges([], messages, self.now, owner_name=OWNER, overrides=overrides), [])
        overrides = {'t9': {'status': 'closed', 'ts': recent}}
        self.assertEqual(
            scan_nudges([], messages, self.now, owner_name=OWNER, overrides=overrides), [])

    def test_needs_my_action_with_fresh_question_emits_nudge(self):
        t = task('t2', group='Family', needs_my_action=True, last_ts_min_ago=2, now=self.now)
        messages = [msg('Aisha', 'Can someone confirm the venue?', 2, self.now,
                        group='Family', task_id='t2')]
        nudges = scan_nudges([t], messages, self.now, owner_name=OWNER)
        self.assertTrue(any(n['task_id'] == 't2' and n['reason'] == 'needs_you'
                            for n in nudges))


class ScanNudgesAtlasContractTest(unittest.TestCase):
    """Pin engine behaviors the Atlas Nudges tab depends on."""

    def setUp(self):
        self.now = datetime.datetime.now(datetime.timezone.utc)

    def test_message_exactly_at_window_edge_emits(self):
        m = msg('Aisha', 'Hey Sameer, can you share the numbers?', 5, self.now)
        nudges = scan_nudges([], [m], self.now, window_min=5, owner_name=OWNER)
        self.assertEqual(len(nudges), 1)

    def test_message_just_outside_window_silent(self):
        m = {'sender': 'Aisha', 'body': 'Hey Sameer, can you share the numbers?',
             'time': (self.now - datetime.timedelta(minutes=5, seconds=1)).isoformat(),
             'group': 'Family'}
        self.assertEqual(
            scan_nudges([], [m], self.now, window_min=5, owner_name=OWNER), [])

    def test_expired_snooze_reappears(self):
        messages = [msg('Aisha', 'Hey Sameer, can you share the numbers?', 2, self.now,
                        group='Family', task_id='t9')]
        old = (self.now - datetime.timedelta(hours=73)).isoformat()
        overrides = {'t9': {'status': 'snoozed', 'ts': old}}
        nudges = scan_nudges([], messages, self.now, owner_name=OWNER,
                             overrides=overrides)
        self.assertEqual(len(nudges), 1)

    def test_closed_hides_despite_fresh_activity(self):
        messages = [msg('Aisha', 'Hey Sameer, can you share the numbers?', 1, self.now,
                        group='Family', task_id='t9')]
        recent = (self.now - datetime.timedelta(hours=1)).isoformat()
        overrides = {'t9': {'status': 'closed', 'ts': recent}}
        self.assertEqual(
            scan_nudges([], messages, self.now, owner_name=OWNER,
                        overrides=overrides), [])

    def test_three_way_ranking_order(self):
        t_wait = task('t1', group='Vendors', waiting_on='Vendor', nudges=2,
                      last_ts_min_ago=2, now=self.now)
        t_need = task('t2', group='Family', needs_my_action=True,
                      last_ts_min_ago=2, now=self.now)
        messages = [
            msg('Aisha', 'Hey Sameer, can you share the numbers?', 2, self.now),
            msg('Aisha', 'Can someone confirm the venue?', 2, self.now,
                group='Family', task_id='t2'),
        ]
        nudges = scan_nudges([t_wait, t_need], messages, self.now,
                             owner_name=OWNER)
        self.assertEqual(
            [n['reason'] for n in nudges],
            ['direct_address', 'waiting_on', 'needs_you'])

    def test_cross_branch_dedupe_prefers_direct(self):
        t = task('t2', group='Family', needs_my_action=True,
                 last_ts_min_ago=2, now=self.now)
        messages = [msg('Aisha', 'Hey Sameer, can you confirm the venue?', 2,
                        self.now, group='Family', task_id='t2')]
        nudges = scan_nudges([t], messages, self.now, owner_name=OWNER)
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0]['reason'], 'direct_address')


class NudgeOverrideStoreTest(unittest.TestCase):
    """Dismiss persistence: POST /api/nudges/dismiss writes STORE_DIR/nudge_overrides.json."""

    def test_dismiss_persists_and_scan_honors_it(self):
        import tempfile
        import momentum_api
        with tempfile.TemporaryDirectory() as tmp:
            old = momentum_api.STORE_DIR
            momentum_api.STORE_DIR = tmp
            try:
                out = momentum_api.save_nudge_override('t9', 'snoozed')
                self.assertTrue(out.get('ok'))
                overrides = momentum_api.load_nudge_overrides()
                self.assertEqual(overrides['t9']['status'], 'snoozed')
                now = datetime.datetime.now(datetime.timezone.utc)
                messages = [msg('Aisha', 'Hey Sameer, can you share the numbers?', 2,
                                now, group='Family', task_id='t9')]
                self.assertEqual(
                    scan_nudges([], messages, now, owner_name=OWNER,
                                overrides=overrides), [])
            finally:
                momentum_api.STORE_DIR = old


if __name__ == '__main__':
    unittest.main()
