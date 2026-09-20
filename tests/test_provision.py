"""Provisioner key gate: env-key check must run out-of-process.

The old in-process genai ping hit a client-reuse bug (RuntimeError) and
rejected valid keys, wedging fresh boots in awaiting_key (observed in
local-store/provision_run.log). The G2 contract
(journey_gates.validate_key_fresh) checks in a fresh interpreter:
a present key is accepted here, the parent stays free of genai
imports, and true liveness stays with the UI-time live check
(momentum_api.validate_gemini_key).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import provision


class TestProvisionKeyFresh(unittest.TestCase):
    def test_env_key_check_runs_out_of_process(self):
        os.environ['GEMINI_API_KEY'] = 'BOGUS-KEY-FOR-TEST'
        provision._key_ok.clear()
        try:
            self.assertTrue(provision.has_key())
        finally:
            del os.environ['GEMINI_API_KEY']
            provision._key_ok.clear()
        self.assertNotIn('google.genai', sys.modules)
        self.assertNotIn('genai', sys.modules)


if __name__ == '__main__':
    unittest.main()
