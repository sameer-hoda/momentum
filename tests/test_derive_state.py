"""Pin derive_state priority chain: done > needs_you > blocked >
waiting_other > stale > active. Real code, no mocks."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from build_data import derive_state


def _task(**kw):
    t = {'status': 'running', 'needs_my_action': False,
         'waiting_on': '', 'stale': False}
    t.update(kw)
    return t


class TestDeriveStateChain(unittest.TestCase):
    def test_done_wins_over_everything(self):
        self.assertEqual(
            derive_state(_task(status='completed', needs_my_action=True,
                               waiting_on='Rohan', stale=True)),
            'done')

    def test_needs_you_beats_blocked(self):
        self.assertEqual(
            derive_state(_task(status='blocked', needs_my_action=True)),
            'needs_you')

    def test_blocked_beats_waiting(self):
        self.assertEqual(
            derive_state(_task(status='blocked', waiting_on='Rohan')),
            'blocked')

    def test_waiting_other(self):
        self.assertEqual(
            derive_state(_task(status='running', needs_my_action=False,
                               waiting_on='Rohan', stale=False)),
            'waiting_other')

    def test_stale_when_only_stale(self):
        self.assertEqual(derive_state(_task(stale=True)), 'stale')

    def test_active_by_default(self):
        self.assertEqual(derive_state(_task()), 'active')


if __name__ == '__main__':
    unittest.main()
