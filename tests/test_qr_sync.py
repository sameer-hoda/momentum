"""QR-sync journey: scan -> 10s syncing hold -> partial portal + honest ETA.

TDD RED for the QR-SYNC mission. Pure functions live in
app/journey_gates.py; cadence constants live in app/provision.py.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

NOW = datetime.datetime(2026, 9, 20, 12, 1, 0,
                        tzinfo=datetime.timezone.utc)


def _ts(dt):
    return dt.isoformat()


class TestScanToSyncingFast(unittest.TestCase):
    def test_awaiting_qr_poll_cadence_under_2s(self):
        """Scan detection poll must fire within 2s (scan-to-syncing < 2s)."""
        import provision
        self.assertLessEqual(provision.AWAITING_QR_POLL_S, 2)

    def test_sync_min_hold_constant_about_10s(self):
        from journey_gates import SYNC_MIN_HOLD_S
        self.assertGreaterEqual(SYNC_MIN_HOLD_S, 9)
        self.assertLessEqual(SYNC_MIN_HOLD_S, 12)


class TestSyncHold(unittest.TestCase):
    def test_hold_active_right_after_scan(self):
        from journey_gates import scan_hold_state
        scan_at = _ts(NOW)
        got = scan_hold_state(scan_at, NOW)
        self.assertTrue(got['hold'])
        self.assertGreater(got['remaining_s'], 0)

    def test_hold_releases_after_minimum(self):
        from journey_gates import scan_hold_state, SYNC_MIN_HOLD_S
        scan_at = _ts(NOW)
        later = NOW + datetime.timedelta(seconds=SYNC_MIN_HOLD_S + 1)
        got = scan_hold_state(scan_at, later)
        self.assertFalse(got['hold'])
        self.assertEqual(got['remaining_s'], 0)

    def test_hold_remaining_counts_down(self):
        from journey_gates import scan_hold_state, SYNC_MIN_HOLD_S
        scan_at = _ts(NOW)
        mid = NOW + datetime.timedelta(seconds=4)
        got = scan_hold_state(scan_at, mid)
        self.assertTrue(got['hold'])
        self.assertAlmostEqual(got['remaining_s'], SYNC_MIN_HOLD_S - 4,
                               delta=1)

    def test_hold_elapsed_reported(self):
        from journey_gates import scan_hold_state
        scan_at = _ts(NOW)
        mid = NOW + datetime.timedelta(seconds=3)
        got = scan_hold_state(scan_at, mid)
        self.assertAlmostEqual(got['elapsed_s'], 3, delta=1)

    def test_no_scan_no_hold(self):
        from journey_gates import scan_hold_state
        got = scan_hold_state(None, NOW)
        self.assertFalse(got['hold'])
        self.assertEqual(got['remaining_s'], 0)


class TestPartialPortal(unittest.TestCase):
    def test_empty_payload_all_loading_but_shell_usable(self):
        """Zero full-sync data: every section loading, shell still renders."""
        from journey_gates import partial_sections
        got = partial_sections({})
        self.assertTrue(got['shell_ready'])
        for section in ('threads', 'map', 'bulletins', 'health'):
            self.assertEqual(got['sections'][section], 'loading')

    def test_partial_snapshot_threads_ready_rest_loading(self):
        """publish_partial shape: cached threads readable, rest shimmers."""
        from journey_gates import partial_sections
        payload = {'partial': True,
                   'progress': {'done': 120, 'total': 983},
                   'tasks': [{'id': 'a'}],
                   'map': [{'group': 'g'}],
                   'bulletins': [], 'health_wall': None}
        got = partial_sections(payload)
        self.assertTrue(got['shell_ready'])
        self.assertEqual(got['sections']['threads'], 'ready')
        self.assertEqual(got['sections']['map'], 'ready')
        self.assertEqual(got['sections']['bulletins'], 'loading')
        self.assertEqual(got['sections']['health'], 'loading')

    def test_full_payload_all_ready(self):
        from journey_gates import partial_sections
        payload = {'tasks': [{'id': 'a'}], 'map': [{'group': 'g'}],
                   'bulletins': [{'id': 'b'}],
                   'health_wall': {'cells': []}}
        got = partial_sections(payload)
        self.assertTrue(got['shell_ready'])
        for section in ('threads', 'map', 'bulletins', 'health'):
            self.assertEqual(got['sections'][section], 'ready')


class TestSyncEta(unittest.TestCase):
    def _samples(self, total=1000, rate=100, n=5, gap_s=2):
        t0 = NOW
        return [(t0 + datetime.timedelta(seconds=i * gap_s),
                 min(total, (i + 1) * rate * gap_s)) for i in range(n)]

    def test_eta_within_20_percent_on_steady_fixture(self):
        """Constant-rate fixture: ETA within 20% of actual remaining."""
        from journey_gates import sync_eta_estimate
        samples = self._samples(total=1000, rate=100, n=5, gap_s=2)
        # last sample: t=8s, count=1000? (5*200=1000) -> use n=4: last=800@6s
        samples = self._samples(total=1000, rate=100, n=4, gap_s=2)
        got = sync_eta_estimate(samples, total=1000,
                                now=NOW + datetime.timedelta(seconds=6))
        # actual remaining: 200 msgs @100/s = 2.0s
        self.assertIsNotNone(got['eta_s'])
        self.assertAlmostEqual(got['eta_s'], 2.0, delta=0.4)
        self.assertAlmostEqual(got['percent'], 80.0, delta=1)

    def test_slow_sync_extends_never_zero_early(self):
        """Stalled counts: ETA indeterminate/extended, never 0 while open."""
        from journey_gates import sync_eta_estimate
        t0 = NOW
        samples = [(t0, 100), (t0 + datetime.timedelta(seconds=30), 100),
                   (t0 + datetime.timedelta(seconds=60), 100)]
        got = sync_eta_estimate(
            samples, total=1000,
            now=t0 + datetime.timedelta(seconds=60))
        self.assertTrue(got['eta_s'] is None or got['eta_s'] > 0)
        self.assertLess(got['percent'], 100)

    def test_fast_sync_complete_reads_zero(self):
        from journey_gates import sync_eta_estimate
        t0 = NOW
        samples = [(t0, 500), (t0 + datetime.timedelta(seconds=5), 1000)]
        got = sync_eta_estimate(
            samples, total=1000,
            now=t0 + datetime.timedelta(seconds=5))
        self.assertEqual(got['percent'], 100)
        self.assertEqual(got['eta_s'], 0)

    def test_unknown_total_is_indeterminate(self):
        """Syncing with no known total: honest indeterminate, never fake 0."""
        from journey_gates import sync_eta_estimate
        samples = self._samples()
        got = sync_eta_estimate(samples, total=None,
                                now=NOW + datetime.timedelta(seconds=8))
        self.assertIsNone(got['eta_s'])
        self.assertIsNone(got['percent'])
        self.assertGreater(got['rate_per_s'], 0)


if __name__ == '__main__':
    unittest.main()
