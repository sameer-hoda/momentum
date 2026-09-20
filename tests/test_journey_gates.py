"""Journey V2 gate-contract tests (TDD RED phase).

G0..G6 gate cases per JOURNEY_V2.md gate contracts. Planned pure functions
live in app/journey_gates.py, which DOES NOT EXIST yet — every gate test
imports it inside the test body, so each RED test errors with
ModuleNotFoundError (the RED signal). Passing pins at the bottom cover
helpers that already exist (is_me, extract_status) against real code.

G3 fixtures: tests/fixtures/g3/*.log (fake bridge logs with QR blocks,
QR_AT markers, mid-write truncation, logout tails). Message-count fixtures
use tmp SQLite DBs built with stdlib sqlite3.
"""
import datetime
import inspect
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

FIX_G3 = os.path.join(os.path.dirname(__file__), 'fixtures', 'g3')


def _read_fixture(name):
    with open(os.path.join(FIX_G3, name), errors='ignore') as f:
        return f.read()


def _make_msg_db(path, n):
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)')
    conn.executemany('INSERT INTO messages (body) VALUES (?)',
                     [(f'msg {i}',) for i in range(n)])
    conn.commit()
    conn.close()


NOW = datetime.datetime(2026, 9, 20, 12, 1, 0,
                        tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------- G3 PAIRED
class TestG3Paired(unittest.TestCase):
    def test_paired_via_messages(self):
        """count>0 passes even with no log hint."""
        from journey_gates import gate_g3
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, 'messages.db')
            _make_msg_db(db, 5)
            res = gate_g3(message_count=5, log_text='', now=NOW)
        self.assertTrue(res['pass'])

    def test_paired_via_log_hint(self):
        """count==0 but log shows connected/logged-in -> pass."""
        from journey_gates import gate_g3
        res = gate_g3(message_count=0,
                      log_text=_read_fixture('connected.log'), now=NOW)
        self.assertTrue(res['pass'])

    def test_fail_shows_qr(self):
        """G3 fail always carries a QR code to show (never a bare spinner)."""
        from journey_gates import gate_g3
        res = gate_g3(message_count=0,
                      log_text=_read_fixture('qr_only.log'), now=NOW)
        self.assertFalse(res['pass'])
        self.assertIn('FAKE-QR-SHOW-ME', res['qr_code'])

    def test_newest_block_wins(self):
        """Two QR blocks -> cache holds the newest code."""
        from journey_gates import parse_qr_cache
        cache = parse_qr_cache(_read_fixture('qr_refresh.log'))
        self.assertEqual(cache['code'].strip(), 'FAKE-QR-NEW-BBB')

    def test_mid_write_keeps_previous(self):
        """Unterminated (mid-write) newest block -> keep previous complete QR."""
        from journey_gates import parse_qr_cache
        cache = parse_qr_cache(_read_fixture('truncated.log'))
        self.assertEqual(cache['code'].strip(), 'FAKE-QR-GOOD-AAA')

    def test_age_from_qr_at(self):
        """QR age derives from the QR_AT marker, not file mtime."""
        from journey_gates import parse_qr_cache, qr_age_seconds
        cache = parse_qr_cache(_read_fixture('qr_refresh.log'))
        age = qr_age_seconds(cache, NOW)
        self.assertEqual(age, 38)  # 12:01:00 - 12:00:22

    def test_stale_flag(self):
        """QR older than the 20s refresh window is flagged stale (but shown)."""
        from journey_gates import gate_g3
        old = NOW - datetime.timedelta(seconds=60)
        res = gate_g3(message_count=0,
                      log_text=_read_fixture('qr_only.log'), now=old)
        _ = res
        res2 = gate_g3(message_count=0,
                       log_text=_read_fixture('qr_only.log'), now=NOW)
        self.assertTrue(res2['qr_stale'])

    def test_no_qr_at_untrusted(self):
        """QR block without QR_AT marker is untrusted (no silent stale age)."""
        from journey_gates import parse_qr_cache
        cache = parse_qr_cache(_read_fixture('no_qr_at.log'))
        self.assertFalse(cache['trusted'])

    def test_logout_reopens(self):
        """'logged out' tail after a connect re-opens G3 (fail + QR)."""
        from journey_gates import gate_g3
        res = gate_g3(message_count=0,
                      log_text=_read_fixture('logout.log'), now=NOW)
        self.assertFalse(res['pass'])
        self.assertIn('FAKE-QR-AAA', res['qr_code'])

    def test_qr_etag_304(self):
        """Matching etag -> 304 function result (no HTTP involved)."""
        from journey_gates import qr_etag, qr_cached_response
        tag = qr_etag('FAKE-QR-SHOW-ME', '2026-09-20T12:00:02Z')
        status, _ = qr_cached_response(tag, 'FAKE-QR-SHOW-ME',
                                       '2026-09-20T12:00:02Z')
        self.assertEqual(status, 304)


# ---------------------------------------------------------------- G4 SYNCED
class TestG4Synced(unittest.TestCase):
    def test_stable_sequence_passes(self):
        """3 consecutive stable 15s polls + count>0 + bridge alive -> pass."""
        from journey_gates import record_sync_poll, sync_passes
        st = None
        t = NOW
        for _ in range(3):
            st = record_sync_poll(st, count=1200, bridge_alive=True, now=t)
            t += datetime.timedelta(seconds=15)
        self.assertTrue(sync_passes(st))

    def test_reset_on_change(self):
        """Count changing between polls resets the stability streak."""
        from journey_gates import record_sync_poll, sync_passes
        st = None
        t = NOW
        st = record_sync_poll(st, count=100, bridge_alive=True, now=t)
        t += datetime.timedelta(seconds=15)
        st = record_sync_poll(st, count=100, bridge_alive=True, now=t)
        t += datetime.timedelta(seconds=15)
        st = record_sync_poll(st, count=250, bridge_alive=True, now=t)
        self.assertFalse(sync_passes(st))

    def test_zero_never_passes(self):
        """Stable streak at count==0 never passes (needs messages)."""
        from journey_gates import record_sync_poll, sync_passes
        st = None
        t = NOW
        for _ in range(5):
            st = record_sync_poll(st, count=0, bridge_alive=True, now=t)
            t += datetime.timedelta(seconds=15)
        self.assertFalse(sync_passes(st))

    def test_bridge_down_pauses(self):
        """Polls while bridge is down neither pass nor advance the streak."""
        from journey_gates import record_sync_poll, sync_passes
        st = None
        t = NOW
        st = record_sync_poll(st, count=50, bridge_alive=True, now=t)
        t += datetime.timedelta(seconds=15)
        st = record_sync_poll(st, count=50, bridge_alive=False, now=t)
        self.assertFalse(sync_passes(st))

    def test_stall_cause_phone_asleep(self):
        from journey_gates import stall_cause
        self.assertEqual(
            stall_cause(bridge_alive=True, count_changed=False,
                        phone_awake=False, net_ok=True, source_has_new=True,
                        stalled_s=90),
            'phone_asleep')

    def test_stall_cause_bridge_down(self):
        from journey_gates import stall_cause
        self.assertEqual(
            stall_cause(bridge_alive=False, count_changed=False,
                        phone_awake=True, net_ok=True, source_has_new=True,
                        stalled_s=90),
            'bridge_down')

    def test_stall_cause_wifi(self):
        from journey_gates import stall_cause
        self.assertEqual(
            stall_cause(bridge_alive=True, count_changed=False,
                        phone_awake=True, net_ok=False, source_has_new=True,
                        stalled_s=90),
            'wifi_offline')

    def test_stall_cause_no_source(self):
        from journey_gates import stall_cause
        self.assertEqual(
            stall_cause(bridge_alive=True, count_changed=False,
                        phone_awake=True, net_ok=True, source_has_new=False,
                        stalled_s=90),
            'no_source_messages')

    def test_stall_cause_initial(self):
        from journey_gates import stall_cause
        self.assertEqual(
            stall_cause(bridge_alive=True, count_changed=False,
                        phone_awake=True, net_ok=True, source_has_new=True,
                        stalled_s=10),
            'initial_sync')


# ------------------------------------------------------------- G5 ANALYZED
class TestG5Analyzed(unittest.TestCase):
    def _write_output(self, d, payload):
        p = os.path.join(d, 'frontend_data.json')
        with open(p, 'w') as f:
            f.write(payload)
        return p

    def test_valid_output_passes(self):
        from journey_gates import validate_analysis_output
        with tempfile.TemporaryDirectory() as d:
            p = self._write_output(
                d, '{"tasks": [{"id": 1}], "exported_at": "2026-09-20T12:00:00Z"}')
            res = validate_analysis_output(
                p, last_sync_change='2026-09-20T11:00:00Z')
        self.assertTrue(res['ok'])

    def test_stale_output_fails(self):
        from journey_gates import validate_analysis_output
        with tempfile.TemporaryDirectory() as d:
            p = self._write_output(
                d, '{"tasks": [{"id": 1}], "exported_at": "2026-09-20T10:00:00Z"}')
            res = validate_analysis_output(
                p, last_sync_change='2026-09-20T11:00:00Z')
        self.assertFalse(res['ok'])
        self.assertIn('stale', res['reason'])

    def test_empty_tasks_fails(self):
        from journey_gates import validate_analysis_output
        with tempfile.TemporaryDirectory() as d:
            p = self._write_output(
                d, '{"tasks": [], "exported_at": "2026-09-20T12:00:00Z"}')
            res = validate_analysis_output(
                p, last_sync_change='2026-09-20T11:00:00Z')
        self.assertFalse(res['ok'])

    def test_corrupt_output_fails(self):
        from journey_gates import validate_analysis_output
        with tempfile.TemporaryDirectory() as d:
            p = self._write_output(d, '{"tasks": [NOT JSON')
            res = validate_analysis_output(
                p, last_sync_change='2026-09-20T11:00:00Z')
        self.assertFalse(res['ok'])


# --------------------------------------------------------------- crash class
class TestCrashClass(unittest.TestCase):
    def test_missing_db_degraded(self):
        """Missing messages.db -> degraded (not a crash)."""
        from journey_gates import open_messages_db
        conn, degraded = open_messages_db('/nonexistent/messages.db')
        self.assertTrue(degraded)
        self.assertIsNone(conn)

    def test_corrupt_themes_fallback(self):
        """Corrupt theme_groups.json -> empty-map fallback (not a crash)."""
        from journey_gates import load_themes_safe
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'theme_groups.json')
            open(p, 'w').write('{corrupt!!')
            mapping, fallback = load_themes_safe(p)
        self.assertTrue(fallback)
        self.assertEqual(mapping, {})


# ------------------------------------------------------------------ G0/G1/G2
class TestG0Services(unittest.TestCase):
    def test_stale_pid_reap(self):
        """Dead PID in pidfile is reaped (reports stale) instead of trusted."""
        from journey_gates import pid_is_stale
        self.assertTrue(pid_is_stale(2 ** 30))  # no such process

    def test_health_path_has_no_tcp_probe(self):
        """Gate health evaluation never uses socket.create_connection."""
        import journey_gates as jg
        src = inspect.getsource(jg.health_summary)
        self.assertNotIn('create_connection', src)


class TestG1Auth(unittest.TestCase):
    def test_unauth_401(self):
        from journey_gates import require_auth
        self.assertEqual(require_auth(cookies='', basic=''), 401)

    def test_auth_state_ungated(self):
        """Auth-state probe itself needs no session (else login can never load)."""
        from journey_gates import auth_state_public
        self.assertTrue(auth_state_public(cookies='', basic='')['public'])

    def test_first_run_403(self):
        """Setup-incomplete first run gates mutating calls with 403, not 401."""
        from journey_gates import first_run_gate
        self.assertEqual(first_run_gate(setup_complete=False,
                                        cookies='s=abc', basic=''), 403)


class TestG2Key(unittest.TestCase):
    def test_subprocess_validation_keeps_parent_clean(self):
        """Fresh-process key ping; parent never imports genai."""
        from journey_gates import validate_key_fresh
        validate_key_fresh('DUMMY-KEY-FOR-TEST')
        self.assertNotIn('genai', sys.modules)
        self.assertNotIn('google.genai', sys.modules)

    def test_key_cache_ttl(self):
        """Cached verdicts expire after their TTL (no silent-stale green)."""
        from journey_gates import key_cache_get, key_cache_set
        key_cache_set('k1', True, ttl_s=60, now=1000.0)
        self.assertTrue(key_cache_get('k1', ttl_s=60, now=1059.0))
        self.assertIsNone(key_cache_get('k1', ttl_s=60, now=1061.0))

    def test_key_file_0600(self):
        """Persisted key file must be owner-only."""
        from journey_gates import key_file_mode_ok
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'gemini.key')
            open(p, 'w').write('x')
            os.chmod(p, 0o644)
            self.assertFalse(key_file_mode_ok(p))


# ------------------------------------------------------------------ G6 BOARD
class TestG6Board(unittest.TestCase):
    def _ctx(self, **kw):
        base = dict(groups_mapped=3, visible_tasks=5, total_tasks=5,
                    key_present=True, synced=True, build_fresh=True)
        base.update(kw)
        return base

    def test_empty_no_groups_mapped(self):
        from journey_gates import describe_empty_board
        msg, fix = describe_empty_board(self._ctx(groups_mapped=0,
                                                  visible_tasks=0,
                                                  total_tasks=0))
        self.assertIn('group', msg.lower())
        self.assertTrue(fix)

    def test_empty_all_filtered(self):
        from journey_gates import describe_empty_board
        msg, fix = describe_empty_board(self._ctx(visible_tasks=0,
                                                  total_tasks=9))
        self.assertIn('filter', msg.lower())
        self.assertTrue(fix)

    def test_empty_key_missing(self):
        from journey_gates import describe_empty_board
        msg, fix = describe_empty_board(self._ctx(key_present=False,
                                                  visible_tasks=0,
                                                  total_tasks=0))
        self.assertIn('key', msg.lower())
        self.assertTrue(fix)

    def test_empty_sync_empty(self):
        from journey_gates import describe_empty_board
        msg, fix = describe_empty_board(self._ctx(synced=False,
                                                  visible_tasks=0,
                                                  total_tasks=0))
        self.assertIn('sync', msg.lower())
        self.assertTrue(fix)

    def test_empty_build_stale(self):
        from journey_gates import describe_empty_board
        msg, fix = describe_empty_board(self._ctx(build_fresh=False,
                                                  visible_tasks=0,
                                                  total_tasks=0))
        self.assertIn('stale', msg.lower())
        self.assertTrue(fix)


# ------------------------------------------------- pins (helpers that exist)
class TestOwnerStatusPins(unittest.TestCase):
    def setUp(self):
        import build_data as bd
        self._bd = bd
        self._old = bd.ME

    def tearDown(self):
        self._bd.ME = self._old

    def test_owner_empty_safe(self):
        self._bd.ME = ''
        self.assertFalse(self._bd.is_me('Sameer'))

    def test_owner_whitespace_safe(self):
        self._bd.ME = '   '
        self.assertFalse(self._bd.is_me('Sameer'))

    def test_owner_unset_safe(self):
        self._bd.ME = os.environ.get('OWNER_NAME_UNSET_XYZ', '')
        self.assertEqual(self._bd.ME, '')
        self.assertFalse(self._bd.is_me('Sameer'))

    def test_status_undone_not_completed(self):
        from export_data import extract_status
        self.assertNotEqual(
            extract_status('The undone tasks are piling up', 3), 'completed')

    def test_status_done_completed(self):
        from export_data import extract_status
        self.assertEqual(
            extract_status('Task is done, shipped yesterday', 2), 'completed')


if __name__ == '__main__':
    unittest.main()
