"""Live-refresh view preservation pins (Home yank + thread clobber regressions).

The board re-renders on every new data export while analysis runs. These pins
read the served frontend and assert the two invariants that keep that refresh
from destroying what the user is looking at:

1. initUI() defaults to Home on first paint only, never on refinements.
2. The live-refine tick defers the re-render while a thread overlay is open
   (and stays quiet in background tabs).
"""
import os
import re
import unittest

FRONTEND = os.path.join(os.path.dirname(__file__), '..', 'app',
                        'frontend', 'index.html')


def _read():
    with open(FRONTEND, encoding='utf-8') as f:
        return f.read()


def _fn_body(src, name):
    """Extract the brace-balanced body of `function name(...) { ... }`."""
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


class TestInitUIDefaultsHomeOnce(unittest.TestCase):
    def test_home_forced_only_on_first_paint(self):
        body = _fn_body(_read(), 'initUI')
        self.assertIn("switchView('home')", body,
                      'first paint must still land on Home')
        self.assertNotRegex(
            body, r"(?m)^\s*switchView\('home'\);\s*$",
            'bare switchView(home) re-runs on every refinement and '
            'yanks the user out of their current view')


class TestRefineDefersOnOverlay(unittest.TestCase):
    def test_refine_skips_hidden_tabs(self):
        src = _read()
        m = re.search(r'LIVE_TIMER\s*=\s*setInterval\(async\s*\(\)\s*=>\s*\{',
                      src)
        self.assertIsNotNone(m, 'LIVE_TIMER refine loop not found')
        body = src[m.end():m.end() + 2000]
        self.assertIn('document.hidden', body,
                      'refine must stay quiet in background tabs')

    def test_refine_defers_while_thread_open(self):
        src = _read()
        m = re.search(r'LIVE_TIMER\s*=\s*setInterval\(async\s*\(\)\s*=>\s*\{',
                      src)
        body = src[m.end():m.end() + 2000]
        self.assertIn('detailPanel', body,
                      'refine must check the thread overlay before re-rendering')
        self.assertIn('LIVE_EXPORTED = d.exported_at', body)
        defer_pos = body.find('return;')
        stamp_pos = body.find('LIVE_EXPORTED = d.exported_at')
        self.assertTrue(0 <= defer_pos < stamp_pos,
                        'overlay-open tick must return BEFORE stamping the '
                        'export id, or the update is lost instead of deferred')


class TestQuietRefreshCadence(unittest.TestCase):
    def test_refine_ticks_every_two_minutes(self):
        src = _read()
        m = re.search(r'LIVE_TIMER\s*=\s*setInterval\(async\s*\(\)\s*=>\s*\{',
                      src)
        self.assertIsNotNone(m, 'LIVE_TIMER refine loop not found')
        body = src[m.end():m.end() + 2500]
        self.assertIn('}, 120000);', body,
                      'live refresh must tick every 2 minutes, not constantly')
        self.assertNotIn('}, 8000);', body,
                         '8s refresh cadence must be gone')

    def test_ready_watch_retires_pill_promptly(self):
        src = _read()
        self.assertIn('LIVE_READY_TIMER', src,
                      'a lightweight ready-watch must retire the pill')
        m = re.search(r'LIVE_READY_TIMER\s*=\s*setInterval\(async\s*\(\)\s*=>\s*\{',
                      src)
        self.assertIsNotNone(m, 'ready-watch timer not found')
        body = src[m.end():m.end() + 2500]
        self.assertIn('}, 5000);', body,
                      'ready-watch must tick every 5s')
        self.assertIn("s.stage !== 'ready'", body,
                      'ready-watch must only act on the ready stage')


class TestSyncToast(unittest.TestCase):
    def test_pill_anchored_bottom_right(self):
        body = _fn_body(_read(), 'renderLivePill')
        self.assertIn('live-pill', body)
        self.assertRegex(body, r'right:\s*18px',
                         'progress toast must sit bottom-right, out of the way')

    def test_pill_has_progress_bar(self):
        body = _fn_body(_read(), 'renderLivePill')
        self.assertIn('sync-bar', body,
                      'progress toast must show a thin progress bar')

    def test_pill_respects_reduced_motion(self):
        src = _read()
        self.assertIn('prefers-reduced-motion', src,
                      'pulse animation must switch off under reduced motion')


class TestNoDanglingRenderCalls(unittest.TestCase):
    def test_updateArchBadge_defined(self):
        src = _read()
        self.assertRegex(src, r'function\s+updateArchBadge\s*\(',
                         'initUI/saveArchived call updateArchBadge: a missing '
                         'definition throws on every render and the boot loop '
                         'retries forever')


class TestSignalFilter(unittest.TestCase):
    def test_list_gated_on_signal(self):
        body = _fn_body(_read(), 'liveTasks')
        self.assertIn('SIG_ONLY', body,
                      'thread list must honor the signal-only filter')
        self.assertIn('needs_my_action', body,
                      'signal filter must keep user-owed threads')

    def test_toggle_exists_and_rerenders(self):
        src = _read()
        self.assertIn('function toggleSignalOnly', src)
        m = re.search(r'function\s+toggleSignalOnly\s*\([^)]*\)\s*\{',
                      src)
        body = _fn_body(src, 'toggleSignalOnly')
        self.assertIn('renderTasks()', body,
                      'toggling the filter must re-render the list')


if __name__ == '__main__':
    unittest.main()
