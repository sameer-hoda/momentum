"""demo_seed.write smoke: schema tasks[].id/state/theme_label, map,
health_wall, bulletins present. Real code, no mocks."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from demo_seed import write


class TestDemoSmoke(unittest.TestCase):
    def test_demo_seed_schema(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'frontend_data.json')
            write(path)
            with open(path) as f:
                data = json.load(f)
        self.assertTrue(data['tasks'], 'no tasks generated')
        for t in data['tasks']:
            self.assertIn('id', t)
            self.assertIn('state', t)
            self.assertIn('theme_label', t)
        self.assertTrue(data.get('map'), 'missing map')
        self.assertIn('health_wall', data)
        self.assertTrue(data.get('bulletins'), 'missing bulletins')


if __name__ == '__main__':
    unittest.main()
