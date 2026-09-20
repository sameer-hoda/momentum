"""Pipeline FP pins: bot-digest threads must not become asks; stale
reconcile without shared keywords must stay open. Real code, no mocks."""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import build_data as bd
from build_data import BOT_RE, heuristic_extract, reconcile_stale, worth_llm


def _msg(sender, body, hour, group='Work Group', from_me=False):
    base = datetime.datetime(2026, 9, 10, 12, 0,
                             tzinfo=datetime.timezone.utc)
    return {'group': group, 'theme': 'work', 'sender': sender, 'body': body,
            'ts': base + datetime.timedelta(hours=hour), 'from_me': from_me}


def _thread(msgs):
    t = {'key': 'k', 'group': 'Work Group', 'theme': 'work', 'msgs': msgs,
         'start': msgs[0]['ts'], 'end': msgs[-1]['ts']}
    bd.enrich_thread_signals(t)
    return t


class TestBotDigestThread(unittest.TestCase):
    def test_single_bot_message_returns_none_or_non_ask(self):
        body = ('\u26a1 *Pending Follow-Ups* team please review open items '
                'any update?')
        self.assertTrue(BOT_RE.search(body),
                        'fixture must match BOT_RE or it tests nothing')
        t = _thread([_msg('task_dog', body, 0)])
        got = heuristic_extract(t)
        self.assertTrue(got is None or
                        all(i['kind'] != 'ask' for i in got['items']),
                        f'bot digest became an ask: {got}')


class TestReconcileStaleNoSharedKeywords(unittest.TestCase):
    def test_stays_open_without_shared_keywords(self):
        last = datetime.datetime(2026, 8, 1, 12, 0,
                                 tzinfo=datetime.timezone.utc)
        tasks = [{
            'id': 'work::fix-checkout-dip', 'theme': 'work',
            'title': 'Unblock the checkout success dip',
            'owner': 'Rohan', 'status': 'running', 'age_days': 20,
            'groups': ['Work Group'], 'group': 'Work Group',
            'last_ts': last.isoformat(), 'stale': False,
        }]
        msgs = [_msg('Priya', 'Weekend trek was amazing, photos soon', 500,
                     from_me=False)]
        # later message, no resolution keyword, no shared keywords
        self.assertGreater(msgs[-1]['ts'], last)
        kept, dropped = reconcile_stale(tasks, msgs, {})
        live = [t for t in kept if t['id'] == 'work::fix-checkout-dip']
        self.assertEqual(len(live), 1)
        self.assertNotEqual(live[0]['status'], 'completed')
        self.assertEqual(dropped, [])


class TestReconcileSameMessageRequirement(unittest.TestCase):
    def _task(self, tid='work::fix-checkout-dip'):
        last = datetime.datetime(2026, 8, 1, 12, 0,
                                 tzinfo=datetime.timezone.utc)
        return {
            'id': tid, 'theme': 'work',
            'title': 'Unblock the checkout success dip',
            'owner': 'Rohan', 'status': 'running', 'age_days': 20,
            'groups': ['Work Group'], 'group': 'Work Group',
            'last_ts': last.isoformat(), 'stale': False,
        }, last

    def test_resolution_plus_keywords_same_message_completes(self):
        t, last = self._task()
        msgs = [_msg('Dev', 'Checkout success dip is done, shipped today',
                     720)]
        self.assertGreater(msgs[-1]['ts'], last)
        kept, _ = reconcile_stale([t], msgs, {})
        self.assertEqual(kept[0]['status'], 'completed')

    def test_resolution_split_across_messages_stays_open(self):
        t, last = self._task('work::split-case')
        msgs = [_msg('Dev', 'All done with random chores', 720),
                _msg('Dev', 'Checkout success dip discussed at length', 744)]
        kept, dropped = reconcile_stale([t], msgs, {})
        self.assertNotEqual(kept[0]['status'], 'completed')
        self.assertEqual(dropped, [])


class TestWorthLlmSignalGate(unittest.TestCase):
    def test_big_thread_without_signal_not_worth_llm(self):
        t = _thread([
            _msg('Asha', 'Morning all, coffee run at eleven', 0),
            _msg('Dev', 'Haha count me in, see you there', 1),
            _msg('Asha', 'Great weather today, lovely morning', 2),
        ])
        self.assertFalse(t['from_me'] or t['q_to_me'] or t['nudges']
                         or t['has_ask'],
                         'fixture must carry no signal or it tests nothing')
        self.assertFalse(worth_llm(t))

    def test_big_thread_with_signal_still_worth_llm(self):
        t = _thread([
            _msg('Asha', 'Morning all', 0),
            _msg('Dev', 'Can you share the numbers please?', 1),
            _msg('Asha', 'Will do', 2),
        ])
        self.assertTrue(worth_llm(t))


class TestHeuristicFromMePrecision(unittest.TestCase):
    """Heuristic path cannot attribute direction, so a thread where the
    user merely spoke must not claim sameer_involved — banter with an
    own-message must emit nothing, and an own ask must not self-flag."""

    def test_from_me_banter_emits_nothing(self):
        t = _thread([
            _msg('You', 'lol nice', 0, from_me=True),
            _msg('Asha', 'haha great', 1),
        ])
        self.assertIsNone(heuristic_extract(t))

    def test_from_me_ask_does_not_self_flag(self):
        t = _thread([
            _msg('You', 'Please share the numbers by EOD', 0, from_me=True),
        ])
        got = heuristic_extract(t)
        self.assertIsNotNone(got)
        self.assertEqual(got['items'][0]['kind'], 'ask')
        self.assertFalse(got['items'][0]['sameer_involved'])


class TestHeuristicQToMeRecall(unittest.TestCase):
    """The one signal that IS definitionally aimed at the user — an
    unanswered question mentioning them — must keep sameer_involved=True."""

    def setUp(self):
        self._orig = bd.ME

    def tearDown(self):
        bd.ME = self._orig

    def test_unanswered_question_to_me_flags_involvement(self):
        bd.ME = 'sameer'
        base = datetime.datetime(2026, 9, 10, 12, 0,
                                 tzinfo=datetime.timezone.utc)
        msgs = [{'group': 'Work Group', 'theme': 'work', 'sender': 'Asha',
                 'body': 'Sameer, please share the numbers?',
                 'ts': base, 'from_me': False}]
        bd._mark_unanswered_questions(msgs)
        self.assertTrue(msgs[0].get('_q_to_me'))
        got = heuristic_extract(_thread(msgs))
        self.assertIsNotNone(got)
        self.assertTrue(got['items'][0]['sameer_involved'])


class TestMentionsMeWholeWord(unittest.TestCase):
    """Health-wall friction and the q_to_me flagger must agree: first-name
    whole-word match only — 'sam' in 'samaritan' is never a mention."""

    def setUp(self):
        self._orig = bd.ME

    def tearDown(self):
        bd.ME = self._orig

    def test_direct_mention_true(self):
        bd.ME = 'sam'
        self.assertTrue(bd._mentions_me('Hey Sam, ETA?'))

    def test_substring_mention_false(self):
        bd.ME = 'sam'
        self.assertFalse(bd._mentions_me('Samaritan work is going well?'))

    def test_no_owner_disables(self):
        bd.ME = ''
        self.assertFalse(bd._mentions_me('Hey Sam, ETA?'))


class TestHeuristicAskNeedsSignal(unittest.TestCase):
    def test_bare_question_mark_is_not_an_ask(self):
        t = _thread([_msg('Asha', 'Rohan is coming tomorrow?', 0)])
        got = heuristic_extract(t)
        self.assertTrue(got is None or
                        all(i['kind'] != 'ask' for i in got['items']),
                        f'signal-free thread became an ask: {got}')

    def test_ask_re_signal_still_an_ask(self):
        t = _thread([_msg('Asha', 'Please share the numbers by EOD', 0)])
        got = heuristic_extract(t)
        self.assertIsNotNone(got)
        self.assertEqual(got['items'][0]['kind'], 'ask')


if __name__ == '__main__':
    unittest.main()
