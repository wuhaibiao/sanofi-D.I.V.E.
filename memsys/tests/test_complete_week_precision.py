import json
import unittest

from memsys.aggregation import _inspect_read_answer, answer_with_validation
from memsys.fake import ScriptedChat


class CompleteWeekPrecisionTest(unittest.TestCase):
    def setUp(self):
        self.evidence = {'E1': 'User: I surveyed the north trail on March 3, 2025.',
                         'E2': 'User: I flew the survey drone on March 26, 2025.'}
        self.spec = dict(operation='date_difference', unit='weeks', result=3,
            start=dict(source='E1', quote=self.evidence['E1'], date_text='March 3, 2025'),
            end=dict(source='E2', quote=self.evidence['E2'], date_text='March 26, 2025'))
        self.question = 'How many weeks passed between surveying the north trail and flying the survey drone?'

    def response(self, answer='3 weeks.', spec=None):
        return 'CALCULATION: ' + json.dumps(spec or self.spec) + '\nANSWER: ' + answer

    def inspect(self, answer='3 weeks.', spec=None, question=None):
        return _inspect_read_answer(self.response(answer, spec), self.evidence,
            today='2025-03-30', question=question or self.question)

    def test_partial_week_preserves_exact_days_and_qualifies_only_bare_result(self):
        stripped, audit = self.inspect()
        self.assertEqual(audit['status'], 'checks_passed')
        self.assertEqual(audit['calculation'], self.spec)
        self.assertEqual((audit['computed'], audit['elapsed_days'], audit['remainder_days']), ('3', 23, 2))
        self.assertEqual(audit['rounding'], 'complete')
        self.assertIn('ANSWER: 3 complete weeks.', stripped)
        self.assertEqual(audit['presentation_repair']['original_answer'], '3 weeks.')

    def test_negative_interval_has_signed_remainder_without_changing_value(self):
        spec = dict(self.spec, start=self.spec['end'], end=self.spec['start'], result=-3)
        stripped, audit = self.inspect('-3 weeks.', spec)
        self.assertEqual(audit['status'], 'checks_passed')
        self.assertEqual((audit['complete_units'], audit['elapsed_days'], audit['remainder_days']), (-3, -23, -2))
        self.assertIn('ANSWER: -3 complete weeks.', stripped)

    def test_wrong_value_or_source_never_receives_a_qualification(self):
        for spec, answer in [(dict(self.spec, result=4), '4 weeks.'),
            (dict(self.spec, start=dict(self.spec['start'], date_text='March 2, 2025')), '3 weeks.'),
            (self.spec, '2 weeks.')]:
            _, audit = self.inspect(answer, spec)
            self.assertEqual(audit['status'], 'invalid')
            self.assertNotIn('presentation_repair', audit)

    def test_substantive_answer_and_number_only_request_are_preserved(self):
        for answer, question in [('3 weeks and 2 days.', self.question),
            ('Exactly 3 weeks.', self.question), ('3', self.question+' Reply with only a number.')]:
            stripped, audit = self.inspect(answer, question=question)
            self.assertNotIn('presentation_repair', audit)
            self.assertIn('ANSWER: '+answer, stripped)

    def test_exact_week_multiple_needs_no_presentation_repair(self):
        self.evidence['E2'] = 'User: I flew the survey drone on March 24, 2025.'
        spec = dict(self.spec, end=dict(source='E2', quote=self.evidence['E2'], date_text='March 24, 2025'))
        _, audit = self.inspect(spec=spec)
        self.assertEqual(audit['status'], 'checks_passed')
        self.assertEqual((audit['elapsed_days'], audit['remainder_days']), (21, 0))
        self.assertNotIn('presentation_repair', audit)

    def test_semantic_review_receives_rounding_days_and_qualified_answer(self):
        observed = []
        def review(messages):
            observed.append(messages[-1]['content'])
            return json.dumps(dict(responsive=True, supported=True))
        model = ScriptedChat({'answer': self.response(), 'answer_review': review})
        _, answer, audit = answer_with_validation(model, [], evidence=self.evidence,
            question=self.question, today='2025-03-30', operation='date_difference', semantic_enabled=True)
        self.assertEqual(answer, '3 complete weeks.')
        self.assertEqual(audit['status'], 'checks_passed')
        self.assertEqual(model.purposes(), ['answer', 'answer_review'])
        self.assertIn('"elapsed_days": 23', observed[0])
        self.assertIn('"remainder_days": 2', observed[0])
        self.assertIn('"rounding": "complete"', observed[0])
        self.assertIn('Proposed answer:\n3 complete weeks.', observed[0])

    def test_semantic_rejection_of_event_identity_still_withholds_answer(self):
        model = ScriptedChat({'answer': self.response(), 'answer_review': json.dumps(dict(
            responsive=False, supported=False, reason='The sources refer to different requested events.'))})
        _, answer, audit = answer_with_validation(model, [], evidence=self.evidence,
            question=self.question, today='2025-03-30', operation='date_difference', semantic_enabled=True)
        self.assertEqual(audit['status'], 'incomplete')
        self.assertNotIn('3 complete weeks', answer)


if __name__ == '__main__':
    unittest.main()
