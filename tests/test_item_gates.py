"""FP gates: banter / FYI-link / past-tense / quoted-question rejection,
plus GOOD_ED pins. Real code, no mocks."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from build_data import item_ok, normalize_item


def _item(what, kind='ask', status='open'):
    return {'kind': kind, 'what': what, 'status': status}


class TestBanterRejected(unittest.TestCase):
    def test_r1_banter_thanks_is_none(self):
        self.assertIsNone(
            normalize_item(_item('Thanks for the update guys')))


class TestFYILinkRejected(unittest.TestCase):
    def test_r2_fyi_link_is_none(self):
        self.assertIsNone(
            normalize_item(_item('https://docs.google.com/q3-report numbers')))


class TestPastTenseRejected(unittest.TestCase):
    def test_r3_past_tense_is_none(self):
        self.assertIsNone(
            normalize_item(_item('Shared the deck yesterday')))


class TestQuotedQuestionRejected(unittest.TestCase):
    def test_r4_quoted_question_is_none(self):
        self.assertIsNone(
            normalize_item(_item('Rohan is coming tomorrow?')))


class TestGoodEdKept(unittest.TestCase):
    def test_need_stays_valid(self):
        self.assertTrue(item_ok('Need the numbers by EOD'))

    def test_proceed_stays_valid(self):
        self.assertTrue(item_ok('Proceed with rollout'))


class TestGreetingsRejected(unittest.TestCase):
    """Greetings/celebrations are never verb-first instructions."""

    def test_happy_birthday_is_none(self):
        self.assertIsNone(
            normalize_item(_item('Happy birthday Rohan')))

    def test_good_morning_is_none(self):
        self.assertIsNone(
            normalize_item(_item('Good morning team')))

    def test_congratulations_is_none(self):
        self.assertIsNone(
            normalize_item(_item('Congratulations on the launch')))


class TestWhQuestionsRejected(unittest.TestCase):
    """WH-questions are raw questions, not instructions (QUOTED_Q_RE only
    catches Name+auxiliary shapes, not what/how/why-led ones)."""

    def test_what_led_is_none(self):
        self.assertIsNone(
            normalize_item(_item('what changed in the deploy')))

    def test_how_led_is_none(self):
        self.assertIsNone(
            normalize_item(_item('how to proceed with rollout')))

    def test_why_led_is_none(self):
        self.assertIsNone(
            normalize_item(_item('why the delay on checkout')))


class TestAcksRejected(unittest.TestCase):
    """Acks/reactions must not become tasks."""

    def test_noted_is_none(self):
        self.assertIsNone(normalize_item(_item('Noted thanks')))

    def test_lol_nice_is_none(self):
        self.assertIsNone(normalize_item(_item('lol nice')))

    def test_sure_will_check_is_none(self):
        self.assertIsNone(normalize_item(_item('Sure will check')))


class TestIrregularPastTenseRejected(unittest.TestCase):
    """Irregular past tense is narration, not an instruction — the -ed
    suffix check misses sent/went/got/said/told/took."""

    def test_sent_is_none(self):
        self.assertIsNone(normalize_item(_item('Sent the deck yesterday')))

    def test_got_is_none(self):
        self.assertIsNone(normalize_item(_item('Got the numbers already')))

    def test_said_is_none(self):
        self.assertIsNone(normalize_item(_item('Said he would call back')))


class TestSingleWordRejected(unittest.TestCase):
    """A single token is never a verb-first instruction with an object."""

    def test_eta_question_is_not_ok(self):
        self.assertFalse(item_ok('ETA?'))

    def test_done_is_none(self):
        self.assertIsNone(normalize_item(_item('Done')))


class TestImperativeRecallKept(unittest.TestCase):
    """Precision tightening must not eat real imperatives — including
    present-tense twins of the irregular-past rejects."""

    def test_send_stays_valid(self):
        self.assertTrue(item_ok('Send the deck by EOD'))

    def test_make_stays_valid(self):
        self.assertTrue(item_ok('Make the checkout fast'))

    def test_take_stays_valid(self):
        self.assertTrue(item_ok('Take this forward with Rohan'))

    def test_go_live_stays_valid(self):
        self.assertTrue(item_ok('Go live next week'))


if __name__ == '__main__':
    unittest.main()
