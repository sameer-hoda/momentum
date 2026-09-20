"""Navigation scope pins: paused views stay unreachable, demo data never
reaches the live board.

- Nudges + Board tabs are parked: nav entries hidden and every entry point
  (sidebar, tabbar, keyboard, palette) funnels through switchView, which
  must bounce those views to Command Center.
- demo_seed.write must refuse the live board path unless DEMO_MODE=1, so a
  stray demo run can never again overwrite real analysis with dummy data.
"""
import os
import re
import sys
import unittest

REPO = os.path.join(os.path.dirname(__file__), '..')
FRONTEND = os.path.join(REPO, 'app', 'frontend', 'index.html')
LIVE_SNAPSHOT = os.path.join(REPO, 'app', 'frontend', 'frontend_data.json')

sys.path.insert(0, os.path.join(REPO, 'app'))


def _read():
    with open(FRONTEND, encoding='utf-8') as f:
        return f.read()


def _fn_body(src, name):
    m = re.search(r'function\s+' + re.escape(name) +
                  r'\s*\([^)]*\)\s*\{', src)
    assert m, f'{name} not found'
    i = m.end()
    depth = 1
    while depth:
        ch = src[i]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
        i += 1
    return src[m.end():i - 1]


class TestPausedViews(unittest.TestCase):
    def test_nudges_board_nav_hidden(self):
        src = _read()
        norm = re.sub(r'\s+', '', src)
        self.assertIn('[data-view="nudges"],[data-view="board"]{display:none!important}'.replace(' ', ''),
                      norm,
                      'paused views must be hidden in nav')

    def test_switchview_bounces_paused_views(self):
        body = _fn_body(_read(), 'switchView')
        self.assertRegex(body, r"view\s*=\s*['\"]command['\"]",
                         'paused views must be reassigned to command')

    def test_core_views_intact(self):
        src = _read()
        for view in ('home', 'command', 'pulse'):
            self.assertIn(f'data-view="{view}"', src,
                          f'{view} must stay reachable')


class TestFreshJourneyGates(unittest.TestCase):
    def test_key_gate_asks_first(self):
        with open(os.path.join(REPO, 'app', 'provision.py')) as f:
            src = f.read()
        self.assertIn('ASK_KEY_FIRST', src,
                      'fresh runs must stop at the key gate')
        self.assertIn('gemini.key', src)

    def test_static_served_fresh(self):
        with open(os.path.join(REPO, 'app', 'momentum_api.py')) as f:
            src = f.read()
        self.assertIn('no-store', src,
                      'static must be no-cache so reloads show truth')


class TestNoDummyData(unittest.TestCase):
    def test_demo_write_refuses_live_path(self):
        import demo_seed
        prev = os.environ.get('DEMO_MODE')
        os.environ.pop('DEMO_MODE', None)
        try:
            with self.assertRaises(RuntimeError):
                demo_seed.write(LIVE_SNAPSHOT)
        finally:
            if prev is not None:
                os.environ['DEMO_MODE'] = prev

    def test_demo_write_allowed_in_demo_mode(self):
        import demo_seed
        prev = os.environ.get('DEMO_MODE')
        os.environ['DEMO_MODE'] = '1'
        try:
            target = '/tmp/demo-interlock-probe.json'
            if os.path.exists(target):
                os.remove(target)
            demo_seed.write(target)
            self.assertTrue(os.path.exists(target))
            os.remove(target)
        finally:
            if prev is None:
                os.environ.pop('DEMO_MODE', None)
            else:
                os.environ['DEMO_MODE'] = prev


if __name__ == '__main__':
    unittest.main()
