"""Theme classification pins: every group lands in a real theme.

The auto-generated theme_groups.json dumps all groups into one 'chats'
bucket, which collapses the Atlas into a single territory. These pins cover
the replacement contract in app/export_data.py:

- classify_group_theme(): keyword rules over real group names.
- Auto-placeholder files are ignored (all groups classified).
- Explicit custom mappings always win over the classifier.
- Unclassifiable groups land in an honest 'other', never silently dropped.
- Every classified theme has a label for the board.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import export_data as ed


class TestClassifyGroupTheme(unittest.TestCase):
    def test_brand(self):
        self.assertEqual(ed.classify_group_theme('Harbor ads <> bluefin'),
                         'branding')

    def test_growth_code(self):
        self.assertEqual(ed.classify_group_theme('Wave 42m cohort'),
                         'growth_mtu')

    def test_win_beats_growth(self):
        self.assertEqual(
            ed.classify_group_theme('Harbor 88m : home, win and cross sell'),
            'win')

    def test_ccbp(self):
        self.assertEqual(ed.classify_group_theme('North CCBP rollout'),
                         'ccbp')

    def test_hiring_is_people(self):
        self.assertEqual(
            ed.classify_group_theme('(Temp) L3 Hiring North & Partnerships'),
            'people')

    def test_revenue_not_brand(self):
        self.assertEqual(ed.classify_group_theme('Ad-tech revenue from Northbank'),
                         'revenue_subscription')

    def test_ops(self):
        self.assertEqual(
            ed.classify_group_theme("Action items from today's meeting"),
            'ops')

    def test_autopay(self):
        self.assertEqual(ed.classify_group_theme('Autopay port'), 'autopay')

    def test_dm_number(self):
        self.assertEqual(ed.classify_group_theme('23949492670633'), 'dms')
        self.assertEqual(ed.classify_group_theme('+91 98999 76847'), 'dms')

    def test_bio_auth(self):
        self.assertEqual(ed.classify_group_theme('Federated rails pilot - bio auth'),
                         'bio_auth')

    def test_okr_is_growth(self):
        self.assertEqual(ed.classify_group_theme('Burn OKR'), 'growth_mtu')

    def test_golive_is_ops(self):
        self.assertEqual(ed.classify_group_theme('District North Go-Live'),
                         'ops')

    def test_personal_stays_other(self):
        self.assertEqual(ed.classify_group_theme('Running Buddies - 2'), 'other')
        self.assertEqual(ed.classify_group_theme('The Family'), 'other')

    def test_unclassifiable_is_other(self):
        self.assertEqual(ed.classify_group_theme('Sunday hiking club'),
                         'other')
        self.assertEqual(ed.classify_group_theme('Jordan + Riverside Reads'),
                         'other')


class TestPlaceholderAndPrecedence(unittest.TestCase):
    def test_placeholder_detected(self):
        data = {'chats': {'label': 'Chats', 'owner': '',
                          'description': 'auto-mapped on first run — edit me',
                          'groups': ['A', 'B']}}
        self.assertTrue(ed.is_auto_placeholder(data))

    def test_custom_file_not_placeholder(self):
        data = {'rewards': {'label': 'Rewards', 'groups': ['90 day rewards']},
                'chats': {'label': 'Chats', 'groups': ['Random']}}
        self.assertFalse(ed.is_auto_placeholder(data))

    def test_resolve_prefers_custom_map(self):
        self.assertEqual(ed.resolve_theme('90 day rewards',
                                          {'90 day rewards': 'rewards'}),
                         'rewards')

    def test_resolve_classifies_unmapped(self):
        self.assertEqual(ed.resolve_theme('Harbor ads <> bluefin', {}),
                         'branding')

    def test_every_theme_has_label(self):
        for theme in {ed.classify_group_theme(g) for g in
                      ['Harbor ads x', 'Wave help', 'L3 Hiring', 'Autopay y',
                       'QCBP z', 'Random personal chat']}:
            self.assertIn(theme, ed.THEME_LABELS)


if __name__ == '__main__':
    unittest.main()
