"""Status + owner attribution pins: extract_status word-boundary
matching ('done' must not fire inside 'undone'); is_me exact-ish
(first-name word-boundary, not substring). Real code, no mocks."""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import build_data as bd
from export_data import extract_status


class TestExtractStatusWordBoundary(unittest.TestCase):
    def test_undone_is_not_completed(self):
        self.assertNotEqual(
            extract_status('The undone tasks are piling up', 3), 'completed')

    def test_deliver_is_not_completed(self):
        self.assertNotEqual(
            extract_status('We will deliver the build tomorrow', 2),
            'completed')

    def test_explicit_done_still_completed(self):
        self.assertEqual(
            extract_status('Task is done, shipped yesterday', 2), 'completed')

    def test_explicit_blocked_still_blocked(self):
        self.assertEqual(
            extract_status('Build is blocked on the API key', 2), 'blocked')

    def test_plural_issues_still_blocked(self):
        self.assertEqual(
            extract_status('Facing some issues with the API', 2), 'blocked')

    def test_plural_bugs_still_blocked(self):
        self.assertEqual(
            extract_status('Two bugs filed against checkout', 2), 'blocked')


class TestIsMeExactish(unittest.TestCase):
    def setUp(self):
        self._orig = bd.ME

    def tearDown(self):
        bd.ME = self._orig

    def test_exact_first_name_matches(self):
        bd.ME = 'sam'
        self.assertTrue(bd.is_me('Sam'))

    def test_full_name_matches_first_name_owner(self):
        bd.ME = 'sameer'
        self.assertTrue(bd.is_me('Sameer Hoda'))

    def test_substring_inside_longer_name_rejected(self):
        bd.ME = 'sam'
        self.assertFalse(bd.is_me('Samaritan'))

    def test_empty_owner_disables(self):
        bd.ME = ''
        self.assertFalse(bd.is_me('Sam'))


class TestQToMeAiming(unittest.TestCase):
    def setUp(self):
        self._orig = bd.ME

    def tearDown(self):
        bd.ME = self._orig

    def _msgs(self, body):
        base = datetime.datetime(2026, 9, 10, 12, 0,
                                 tzinfo=datetime.timezone.utc)
        return [{'group': 'G', 'theme': 'w', 'sender': 'Asha', 'body': body,
                 'ts': base, 'from_me': False}]

    def test_direct_address_flagged(self):
        bd.ME = 'sameer'
        ms = self._msgs('Hey Sameer, can you share the numbers?')
        bd._mark_unanswered_questions(ms)
        self.assertTrue(ms[0].get('_q_to_me'))

    def test_substring_name_not_flagged(self):
        bd.ME = 'sam'
        ms = self._msgs('Samaritan work is going well, ETA soon?')
        bd._mark_unanswered_questions(ms)
        self.assertFalse(ms[0].get('_q_to_me'))


if __name__ == '__main__':
    unittest.main()
