"""Thread-level FP fixture pins (Agent 5 roster): banter, FYI-link,
past-tense recap, quoted question, bot digest — each must yield no ask
(or no task at all) through heuristic_extract, and the banter thread
must not even earn an LLM call. Real thread fixtures, no mocks."""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import build_data as bd
from build_data import BOT_RE, heuristic_extract, worth_llm


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


class TestBanterThreadExtractsNothing(unittest.TestCase):
    def test_f1_banter_thread_no_task_no_llm(self):
        t = _thread([
            _msg('Asha', 'Morning all, coffee run at eleven', 0),
            _msg('Dev', 'Haha count me in, see you there', 1),
            _msg('Asha', 'Great weather today, lovely morning', 2),
        ])
        self.assertFalse(t['from_me'] or t['q_to_me'] or t['nudges']
                         or t['has_ask'],
                         'fixture must carry no signal or it tests nothing')
        self.assertFalse(worth_llm(t))
        self.assertIsNone(heuristic_extract(t))


class TestFYILinkThreadExtractsNothing(unittest.TestCase):
    def test_f2_fyi_link_share_no_task(self):
        t = _thread([
            _msg('Asha', 'FYI team, Q3 numbers are out', 0),
            _msg('Dev', 'https://docs.google.com/q3-report', 1),
        ])
        got = heuristic_extract(t)
        self.assertTrue(got is None or
                        all(i['kind'] != 'ask' for i in got['items']),
                        f'FYI-link share became an ask: {got}')
        self.assertIsNone(got, f'FYI-link share became a task: {got}')


class TestPastTenseRecapExtractsNothing(unittest.TestCase):
    def test_f3_past_tense_recap_no_task(self):
        t = _thread([
            _msg('Asha', 'Shared the deck yesterday', 0),
            _msg('Dev', 'Got the numbers too', 1),
        ])
        self.assertIsNone(heuristic_extract(t))


class TestQuotedQuestionExtractsNothing(unittest.TestCase):
    def test_f4_quoted_question_no_task(self):
        t = _thread([_msg('Asha', 'Rohan is coming tomorrow?', 0)])
        got = heuristic_extract(t)
        self.assertTrue(got is None or
                        all(i['kind'] != 'ask' for i in got['items']),
                        f'quoted question became an ask: {got}')
        self.assertIsNone(got, f'quoted question became a task: {got}')


class TestBotDigestThreadExtractsNothing(unittest.TestCase):
    def test_f5_multi_bot_digest_no_task(self):
        bodies = [
            '\u26a1 *Pending Follow-Ups* open items: merchant offers any update?',
            '\U0001f4cb *Last 24 Hrs* 5 follow-ups pending, please review',
        ]
        for b in bodies:
            self.assertTrue(BOT_RE.search(b),
                            f'fixture must match BOT_RE or it tests nothing: {b!r}')
        t = _thread([_msg('task_dog', b, i) for i, b in enumerate(bodies)])
        got = heuristic_extract(t)
        self.assertTrue(got is None or
                        all(i['kind'] != 'ask' for i in got['items']),
                        f'bot digest became an ask: {got}')
        self.assertIsNone(got, f'bot digest became a task: {got}')


if __name__ == '__main__':
    unittest.main()
