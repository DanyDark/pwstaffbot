"""Focused OCR-confusable nickname checks."""
import unittest

import test_flows as flows


class OcrConfusableTests(unittest.IsolatedAsyncioTestCase):
    setUp = flows.BotFlows.setUp
    tearDown = flows.BotFlows.tearDown

    def test_uppercase_i_is_accepted_for_unique_lowercase_l(self):
        state = {}
        result = self.ns['accumulate_screenshot'](state, ['BartIbi'], ['Bartlbi'])
        self.assertEqual(result, (['Bartlbi'], [], 'Bartlbi'))
        self.assertEqual(state['activity_nicks'], ['Bartlbi'])

    def test_ambiguous_ocr_fallback_stays_unknown(self):
        state = {}
        result = self.ns['accumulate_screenshot'](state, ['Bart1bi'], ['Bartlbi', 'Bart|bi'])
        self.assertEqual(result, ([], ['Bart1bi'], None))
        self.assertEqual(state['activity_nicks'], [])

    def test_exact_spelling_has_priority_over_ocr_fallback(self):
        state = {}
        result = self.ns['accumulate_screenshot'](state, ['BartIbi'], ['BartIbi', 'Bartlbi'])
        self.assertEqual(result, (['BartIbi'], [], 'BartIbi'))


if __name__ == '__main__':
    unittest.main()
