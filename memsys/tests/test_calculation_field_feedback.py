import json
import unittest

from memsys.aggregation import answer_with_validation, inspect_calculation
from memsys.fake import ScriptedChat


class CalculationFieldFeedbackTest(unittest.TestCase):
    def test_conversion_feedback_identifies_the_source_value_and_keeps_validation(self):
        evidence = {'E1': 'User: I rehearsed for four days.'}
        item = dict(source='E1', quote=evidence['E1'], value='96', unit='hours',
                    source_value='four days', source_unit='days')
        def output(point):
            return 'CALCULATION: '+json.dumps(dict(operation='sum', unit='hours',
                items=[point], result='96'))+'\nANSWER: 96 hours'
        audit = inspect_calculation(output(item), evidence)[1]
        self.assertEqual(audit['status'], 'invalid')
        self.assertIn('source_value:', audit['errors'][0])
        self.assertEqual(inspect_calculation(output(dict(item, source_value='4')), evidence)[1]['status'], 'checks_passed')
        self.assertEqual(inspect_calculation(output(dict(item, source_value='5')), evidence)[1]['status'], 'invalid')

    def test_relative_day_repair_keeps_source_words_instead_of_computed_date_text(self):
        evidence = {'E1': '[E1] 2025-06-01\nUser: I assembled a prop today.',
                    'E2': '[E2] 2025-06-09\nUser: I dismantled the prop today.'}
        start = dict(source='E1', quote='I assembled a prop today.', date_text='2025-06-01')
        end = dict(source='E2', quote='I dismantled the prop today.', date_text='2025-06-09')
        def output(a, b):
            return 'CALCULATION: '+json.dumps(dict(operation='date_difference', unit='days',
                start=a, end=b, result='8'))+'\nANSWER: 8 days'
        invalid = output(start, end)
        question = 'How many days passed between assembling and dismantling the prop?'
        audit = inspect_calculation(invalid, evidence, question=question, today='2030-01-01')[1]
        self.assertEqual(audit['status'], 'invalid')
        self.assertIn('date_text', audit['errors'][0])
        self.assertIn('relative_text', audit['errors'][0])
        valid = output(dict(source='E1', quote=start['quote'], event_text='I assembled a prop',
                            relative_text='today', anchor='recorded_at'),
                       dict(source='E2', quote=end['quote'], event_text='I dismantled the prop',
                            relative_text='today', anchor='recorded_at'))
        model = ScriptedChat({'answer': [invalid, valid]})
        _, answer, audit = answer_with_validation(model, [], evidence=evidence,
            question=question, today='2030-01-01')
        self.assertEqual((answer, audit['status']), ('8 days', 'checks_passed'))
        self.assertEqual(audit['attempts'][0]['status'], 'invalid')


if __name__ == '__main__':
    unittest.main()
