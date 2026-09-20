"""API contract: unknown task / empty text / duplicate-guard errors;
compose_message attach_context includes title. load_data monkeypatched,
no network."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import momentum_api as api


class TestApiContract(unittest.TestCase):
    def setUp(self):
        self._orig_load = api.load_data
        self._orig_resolve = api.resolve_jid
        self.fixture = {
            'id': 'work::fix-checkout-dip',
            'title': 'Unblock the checkout success dip',
            'theme_label': 'Work', 'age': '2d',
            'groups': ['Work Group'], 'group': 'Work Group',
            'recap': 'Success rate dipped 2 points after release',
        }
        api.load_data = lambda: {'tasks': [self.fixture], 'group_jids': {
            'work group': '123@g.us'}}
        api.resolve_jid = lambda name: '123@g.us'
        api._last_send.clear()

    def tearDown(self):
        api.load_data = self._orig_load
        api.resolve_jid = self._orig_resolve
        api._last_send.clear()

    def test_unknown_task_error(self):
        got = api.do_send({'task_id': 'nope::missing', 'text': 'hi'})
        self.assertIn('error', got)
        self.assertIn('unknown task', got['error'])

    def test_empty_text_error(self):
        got = api.do_send({'task_id': self.fixture['id'], 'text': '   '})
        self.assertIn('error', got)
        self.assertIn('empty', got['error'])

    def test_duplicate_guard_on_second_identical_send(self):
        first = api.do_send({'task_id': self.fixture['id'], 'text': 'Ping?'})
        self.assertNotIn('error', first)
        second = api.do_send({'task_id': self.fixture['id'], 'text': 'Ping?'})
        self.assertIn('error', second)
        self.assertIn('duplicate', second['error'])

    def test_compose_attach_context_includes_title(self):
        out = api.compose_message(self.fixture, 'Ping?', True)
        self.assertIn(self.fixture['title'], out)


class TestPublicHealth(unittest.TestCase):
    """GET /api/health is public (Railway healthcheck + boot probe have no
    session): minimal liveness, no secrets, no owner identity."""

    def test_health_shape_minimal(self):
        got = api.public_health()
        self.assertTrue(got['ok'])
        self.assertIn('stage', got)

    def test_health_leaks_nothing(self):
        got = api.public_health()
        for key in ('owner', 'gemini', 'auth', 'messages', 'tasks',
                    'bridge_url', 'live_mode'):
            self.assertNotIn(key, got)


class TestBridgeOk(unittest.TestCase):
    """bridge_ok: an HTTP answer is decisive. Only 404 (Go default mux, no
    /api/health route on the bridge) counts as alive; any other HTTP error
    (500 from a broken bridge, 401, ...) is not alive. Live baseline:
    bridge GET /api/health -> 404."""

    def _http_error(self, code):
        from urllib.error import HTTPError
        return HTTPError('http://localhost:8080/api/health', code,
                         'err', {}, None)

    def test_bridge_404_counts_as_alive(self):
        orig = api.urlopen
        api.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            self._http_error(404))
        try:
            self.assertTrue(api.bridge_ok())
        finally:
            api.urlopen = orig

    def test_bridge_500_not_alive(self):
        orig = api.urlopen
        api.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            self._http_error(500))
        try:
            self.assertFalse(api.bridge_ok())
        finally:
            api.urlopen = orig

    def test_bridge_401_not_alive(self):
        orig = api.urlopen
        api.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            self._http_error(401))
        try:
            self.assertFalse(api.bridge_ok())
        finally:
            api.urlopen = orig


class TestProvisionPrecedence(unittest.TestCase):
    """/tmp/provision.json is machine-global; the per-stack STORE file must
    win so parallel stacks (live + scratch DEMO) cannot clobber each other."""

    def test_ports_collide_same_port(self):
        self.assertTrue(api.ports_collide(8080, 'http://localhost:8080'))

    def test_ports_no_collide(self):
        self.assertFalse(api.ports_collide(8099, 'http://localhost:8080'))

    def test_ports_collide_bare_host_defaults_to_bridge_8080(self):
        self.assertTrue(api.ports_collide(8080, 'http://localhost'))

    def test_qr_mid_write_tail_ignored(self):
        """Newest QR_BEGIN unterminated (mid-write art) -> previous
        complete block survives; the partial tail is never shown."""
        import tempfile
        log = ('noise\nQR_BEGIN\nGOOD1\nGOOD2\nGOOD3\nGOOD4\nGOOD5\n'
               'QR_END\nnoise\nQR_BEGIN\nPART1\nPART2\nPART3\nPART4\n'
               'PART5\nPART6\n')
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'bridge.log')
            with open(p, 'w') as f:
                f.write(log)
            orig = api.BRIDGE_LOG
            api.BRIDGE_LOG = p
            try:
                got = api.read_qr()
            finally:
                api.BRIDGE_LOG = orig
        self.assertIn('GOOD1', got['qr'])
        self.assertNotIn('PART1', got['qr'])

    def test_qr_bracketed_newest_trusted(self):
        """Bracketed blocks with QR_AT markers (G3 contract): newest code
        wins, trusted, with a real age."""
        import tempfile
        log = ('[QR_BEGIN QR_AT=2026-09-20T12:00:02Z]\nFAKE-QR-OLD-AAA\n'
               '[QR_END]\nnoise\n[QR_BEGIN QR_AT=2026-09-20T12:00:22Z]\n'
               'FAKE-QR-NEW-BBB\n[QR_END]\n')
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'bridge.log')
            with open(p, 'w') as f:
                f.write(log)
            orig = api.BRIDGE_LOG
            api.BRIDGE_LOG = p
            try:
                got = api.read_qr()
            finally:
                api.BRIDGE_LOG = orig
        self.assertIn('FAKE-QR-NEW-BBB', got['qr'])
        self.assertTrue(got['trusted'])
        self.assertIsNotNone(got['age_s'])

    def test_qr_legacy_complete_block_untrusted(self):
        """Pre-QR_AT bare blocks still resolve (live bridge today), but
        untrusted with file-mtime age."""
        import tempfile
        log = 'noise\nQR_BEGIN\nL1\nL2\nL3\nL4\nL5\nQR_END\n'
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'bridge.log')
            with open(p, 'w') as f:
                f.write(log)
            orig = api.BRIDGE_LOG
            api.BRIDGE_LOG = p
            try:
                got = api.read_qr()
            finally:
                api.BRIDGE_LOG = orig
        self.assertIn('L1', got['qr'])
        self.assertFalse(got['trusted'])

    def test_persistent_store_detection(self):
        """Persistence == mounted Volume (/data). Local dirs and missing
        mounts are NOT persistent (volume mis-mount must read False)."""
        import tempfile
        self.assertFalse(api.is_persistent_store(tempfile.mkdtemp()))
        self.assertFalse(api.is_persistent_store('/nonexistent-xyz/store'))
        self.assertFalse(api.is_persistent_store(''))

    def test_store_file_wins_over_tmp(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp_path = os.path.join(d, 'provision.json')
            store_dir = os.path.join(d, 'store')
            os.makedirs(store_dir)
            store_path = os.path.join(store_dir, 'provision.json')
            with open(tmp_path, 'w') as f:
                json.dump({'stage': 'ready'}, f)
            with open(store_path, 'w') as f:
                json.dump({'stage': 'syncing'}, f)
            orig_file, orig_store = api.PROVISION_FILE, api.STORE_DIR
            api.PROVISION_FILE, api.STORE_DIR = tmp_path, store_dir
            try:
                self.assertEqual(api.read_provision()['stage'], 'syncing')
            finally:
                api.PROVISION_FILE, api.STORE_DIR = orig_file, orig_store


if __name__ == '__main__':
    unittest.main()
