"""Significance gate pins (SCRUB): tiny low-signal threads must not reach
the default board. Hard DROP (extraction -> None) for obvious noise;
DEMOTE (assess_significance -> significant False, recoverable in
demoted_tasks) for close calls. Recall-first: anything addressed to the
user always keeps. Real fixtures, no mocks."""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import build_data as bd
from build_data import heuristic_extract


def _msg(sender, body, hour, group='Work Group', from_me=False, fresh=False):
    base = (datetime.datetime.now(datetime.timezone.utc)
            if fresh else
            datetime.datetime(2026, 9, 10, 12, 0,
                              tzinfo=datetime.timezone.utc))
    return {'group': group, 'theme': 'work', 'sender': sender, 'body': body,
            'ts': base + datetime.timedelta(hours=hour), 'from_me': from_me}


def _thread(msgs):
    t = {'key': 'k', 'group': 'Work Group', 'theme': 'work', 'msgs': msgs,
         'start': msgs[0]['ts'], 'end': msgs[-1]['ts']}
    bd.enrich_thread_signals(t)
    return t


def _task(**kw):
    t = {'id': 'work::x', 'theme': 'work', 'title': 'Share the numbers',
         'one_liner': 'Please share the numbers', 'kind': 'ask',
         'status': 'running', 'owner': '', 'requester': '',
         'due': '', 'needs_my_action': False, 'waiting_on': '',
         'stale': False, 'nudges': 0, 'age_days': 0, 'msg_count': 3,
         'messages': [{'sender': 'Asha', 'body': 'Please share the numbers'}],
         'last_ts': datetime.datetime.now(
             datetime.timezone.utc).isoformat()}
    t.update(kw)
    return t


class TestReactionDrop(unittest.TestCase):
    def test_short_signal_free_reaction_emits_nothing(self):
        t = _thread([_msg('Asha', 'Weekend trek photos soon', 0),
                     _msg('Dev', 'Looks great', 1)])
        self.assertFalse(t['has_ask'] or t['nudges'] or t['q_to_me'],
                         'fixture must be signal-free or it tests nothing')
        self.assertIsNone(heuristic_extract(t))

    def test_one_word_reply_emits_nothing(self):
        t = _thread([_msg('Asha', 'Deck is shared', 0),
                     _msg('Dev', 'Noted', 1)])
        self.assertIsNone(heuristic_extract(t))


class TestEmojiDrop(unittest.TestCase):
    def test_emoji_only_reaction_emits_nothing(self):
        body = '\U0001f44d \U0001f64f \U0001f44f'  # 5 chars: clears the len<4 floor
        self.assertGreaterEqual(len(body), 4,
                                'fixture must clear len<4 or it tests nothing')
        t = _thread([_msg('Asha', 'Nice photos from the offsite', 0),
                     _msg('Dev', body, 1)])
        self.assertIsNone(heuristic_extract(t))


class TestFYIForwardDemoteNotDrop(unittest.TestCase):
    def test_fyi_forward_stays_update_for_demote(self):
        t = _thread([_msg('Asha', 'Fwd: Q3 all-hands recording', 0),
                     _msg('Dev', 'Recording attached for reference', 1)])
        got = heuristic_extract(t)
        self.assertIsNotNone(got)
        self.assertEqual(got['items'][0]['kind'], 'update')
        task = _task(title='Fwd: Q3 all-hands recording',
                     one_liner='Recording attached for reference',
                     kind='update', msg_count=2)
        sig, reason = bd.assess_significance(task)
        self.assertFalse(sig)
        self.assertEqual(reason, 'fyi-forward')


class TestBotSignalsIgnored(unittest.TestCase):
    def _mixed(self):
        return _thread([
            _msg('task_dog',
                 '\u26a1 *Pending Follow-Ups* merchant offers any update?',
                 0),
            _msg('Dev', 'Checking now', 1),
        ])

    def test_bot_text_contributes_no_nudge_or_ask(self):
        t = self._mixed()
        self.assertEqual(t['nudges'], 0)
        self.assertFalse(t['has_ask'])

    def test_mixed_bot_thread_never_an_ask(self):
        got = heuristic_extract(self._mixed())
        self.assertIsNotNone(got)
        self.assertNotEqual(got['items'][0]['kind'], 'ask')

    def test_bot_context_update_demoted(self):
        task = _task(
            kind='update', msg_count=5,
            messages=[
                {'sender': 'task_dog',
                 'body': '\u26a1 *Pending Follow-Ups* merchant offers any update?'},
                {'sender': 'Dev', 'body': 'Checking now'},
            ])
        sig, reason = bd.assess_significance(task)
        self.assertFalse(sig)
        self.assertEqual(reason, 'bot-context')


class TestStaleCarryover(unittest.TestCase):
    def test_20_day_quiet_carryover_demoted(self):
        task = _task(age_days=20, stale=True)
        sig, reason = bd.assess_significance(task)
        self.assertFalse(sig)
        self.assertEqual(reason, 'stale-quiet')

    def test_stale_addressed_to_user_kept(self):
        task = _task(age_days=20, stale=True, needs_my_action=True)
        sig, _ = bd.assess_significance(task)
        self.assertTrue(sig)

    def test_old_waiting_on_kept(self):
        task = _task(age_days=30, stale=True, waiting_on='Rohan',
                     owner='Rohan', requester='You')
        sig, _ = bd.assess_significance(task)
        self.assertTrue(sig)


class TestKeepRules(unittest.TestCase):
    def test_blocked_old_kept(self):
        sig, reason = bd.assess_significance(
            _task(status='blocked', age_days=30))
        self.assertTrue(sig)
        self.assertEqual(reason, 'stuck-needs-attention')

    def test_completed_demoted(self):
        sig, reason = bd.assess_significance(
            _task(status='completed', age_days=2))
        self.assertFalse(sig)
        self.assertEqual(reason, 'done-history')

    def test_tiny_update_demoted(self):
        sig, reason = bd.assess_significance(
            _task(kind='update', msg_count=1))
        self.assertFalse(sig)
        self.assertEqual(reason, 'tiny-no-signal')

    def test_chased_fresh_kept(self):
        sig, reason = bd.assess_significance(
            _task(kind='ask', nudges=2, age_days=3))
        self.assertTrue(sig)
        self.assertEqual(reason, 'being-chased')

    def test_fresh_ask_kept(self):
        sig, reason = bd.assess_significance(
            _task(kind='ask', msg_count=1, age_days=0))
        self.assertTrue(sig)
        self.assertEqual(reason, 'new-and-unjudged')

    def test_quiet_old_ask_demoted(self):
        sig, reason = bd.assess_significance(
            _task(kind='ask', age_days=10))
        self.assertFalse(sig)
        self.assertEqual(reason, 'quiet-old')


class TestTrueAskBatteryEndToEnd(unittest.TestCase):
    """Fresh genuine asks survive extraction AND the gate, kind by kind."""

    def _run(self, bodies):
        t = _thread([_msg(s, b, i, fresh=True)
                     for i, (s, b) in enumerate(bodies)])
        got = heuristic_extract(t)
        self.assertIsNotNone(got, f'true ask lost at extraction: {bodies!r}')
        cand = {'theme': 'work', 'thread': t, 'items': got['items']}
        task = bd.build_task('work', [cand])
        sig, reason = bd.assess_significance(task)
        self.assertTrue(sig, f'true ask demoted ({reason}): {bodies!r}')
        return task

    def test_explicit_ask_kept(self):
        self._run([('Asha', 'Please share the numbers by EOD')])

    def test_nudge_followup_kept(self):
        self._run([('Dev', 'any update on the merchant offers?')])

    def test_send_me_ask_kept(self):
        self._run([('Asha', 'We need the invoice'),
                   ('Dev', 'Send me the invoice today')])

    def test_blocker_kept(self):
        self._run([('Dev', 'Unblock the checkout dip, stuck on API key')])

    def test_share_the_ask_kept(self):
        self._run([('Asha', 'Can you share the deck?'),
                   ('Dev', 'Share the deck with Rohan')])


if __name__ == '__main__':
    unittest.main()
