import json
import unittest

from memsys.aggregation import answer_with_validation, inspect_calculation
from memsys.fake import ScriptedChat
from memsys.llm import ChatResult, IncompleteJSONError, ModelError
from memsys.queryops import operation_for
from memsys.semantic_review import review_response


def calculation(spec):
    return 'CALCULATION: ' + json.dumps(spec) + '\nANSWER: ' + spec['result']


class GroundedQuantityRecoveryTest(unittest.TestCase):
    def test_separate_group_labels_and_completed_ordinal_snapshot(self):
        evidence = {'E1': 'This is my fourth completed circuit of the trail.',
                    'E2': 'I completed 7 loops of the riverside route.'}
        spec = dict(operation='count_groups', unit='circuits', result='11', items=[
            dict(source='E1', quote=evidence['E1'], value='4', unit='circuits',
                 quantity_label='completed circuit', ordinal_progress=True, value_type='cumulative'),
            dict(source='E2', quote=evidence['E2'], value='7', unit='circuits', quantity_label='loops')])
        audit = inspect_calculation(calculation(spec), evidence)[1]
        self.assertEqual((audit['status'], audit['computed']), ('checks_passed', '11'))
        for fields in [dict(ordinal_progress=False), dict(value_type='independent'),
                       dict(value='5'), dict(quantity_label='circuit')]:
            changed = dict(spec, items=[dict(spec['items'][0], **fields)])
            self.assertEqual(inspect_calculation(calculation(changed), evidence)[1]['status'], 'invalid')

    def test_ordinals_are_opt_in_and_cannot_bypass_measurement_validation(self):
        quote = 'This is my 21st completed circuit.'
        item = dict(source='E1', quote=quote, quantity_label='completed circuit', value='21', unit='circuits')
        spec = dict(operation='count_groups', unit='circuits', result='21', items=[item])
        self.assertEqual(inspect_calculation(calculation(spec), {'E1': quote})[1]['status'], 'invalid')
        item.update(ordinal_progress=True, value_type='cumulative')
        self.assertEqual(inspect_calculation(calculation(spec), {'E1': quote})[1]['status'], 'checks_passed')
        for unit in ['USD', 'days']:
            changed = dict(spec, unit=unit, items=[dict(item, unit=unit)])
            self.assertEqual(inspect_calculation(calculation(changed), {'E1': quote})[1]['status'], 'invalid')

    def test_source_year_anchor_handles_leap_day_without_using_caller_year(self):
        evidence = {'E1': '[E1] 2024-03-07\nUser: I posted the parcel on February 28th.',
                    'E2': '[E2] 2024-03-09\nUser: It arrived on March 2nd.'}
        spec = dict(operation='date_difference', unit='days', result='3',
            start=dict(source='E1', quote='I posted the parcel on February 28th.',
                       date_text='February 28th', anchor='recorded_at'),
            end=dict(source='E2', quote='It arrived on March 2nd.',
                     date_text='March 2nd', anchor='recorded_at'))
        audit = inspect_calculation(calculation(spec), evidence, today='2030-03-09')[1]
        self.assertEqual((audit['status'], audit['computed']), ('checks_passed', '3'))
        self.assertEqual(audit['endpoints'], ['2024-02-28', '2024-03-02'])
        for fields in [dict(anchor=None), dict(date='2030-02-28'), dict(date_text='February')]:
            changed = dict(spec, start=dict(spec['start'], **fields))
            self.assertEqual(inspect_calculation(calculation(changed), evidence)[1]['status'], 'invalid')
        missing_header = dict(evidence, E1=evidence['E1'].split('\n', 1)[1])
        self.assertEqual(inspect_calculation(calculation(spec), missing_header)[1]['status'], 'invalid')

    def test_explicit_other_year_cannot_use_source_year_anchor(self):
        evidence = {'E1': '[E1] 2025-05-09\nUser: I posted it last year on May 3rd.'}
        spec = dict(operation='date_difference', unit='days', result='6',
            start=dict(source='E1', full_source=True, event_text='I posted it',
                       date_text='May 3rd', anchor='recorded_at'),
            end=dict(source='TODAY'))
        self.assertEqual(inspect_calculation(calculation(spec), evidence, today='2025-05-09')[1]['status'], 'invalid')

    def test_shortfall_operation_is_distinct_from_total_requirement(self):
        for question in ['How many credits do I need to earn for the next reward?',
                         'How much do I have left to save?', '还需要积累多少积分？']:
            self.assertEqual(operation_for(question), 'difference')
        self.assertNotEqual(operation_for('What is the total credit requirement for this reward?'), 'difference')

    def test_completion_retry_increases_budget_only_after_truncation(self):
        class CompletionModel:
            def __init__(self):
                self.budgets = []

            def chat(self, messages, **kwargs):
                self.budgets.append(kwargs['max_tokens'])
                text, reason = ('ANSWER: unfinished', 'length') if len(self.budgets) == 1 else ('ANSWER: complete', 'stop')
                return ChatResult(text, 'test', 0, 0, 0, 0, 0, reason)
        model = CompletionModel()
        _, answer, audit = answer_with_validation(model, [], evidence={}, enabled=False, max_tokens=700)
        self.assertEqual(answer, 'complete')
        self.assertEqual(model.budgets, [700, 1400])
        self.assertEqual([a['max_tokens'] for a in audit['generation_attempts']], model.budgets)

    def test_review_discards_partial_verdict_and_retries_once(self):
        class ReviewModel:
            def __init__(self):
                self.budgets = []

            def chat(self, messages, **kwargs):
                self.budgets.append(kwargs['max_tokens'])
                text, reason = ('{"responsive": true, "supported": false}', 'length') if len(self.budgets) == 1 else (
                    '{"responsive": true, "supported": true}', 'stop')
                return ChatResult(text, 'test', 0, 0, 0, 0, 0, reason)
        model = ReviewModel()
        review = review_response(model, 'Recall my item.', 'An item.', {'E1': 'An item.'}, 'lookup', (), None, None)
        self.assertTrue(review['supported'])
        self.assertEqual(model.budgets, [600, 1200])
        def truncated(messages):
            return ChatResult('{}', 'test', 0, 0, 0, 0, 0, 'length')
        model = ScriptedChat({'answer_review': truncated})
        with self.assertRaises(IncompleteJSONError):
            review_response(model, 'Recall my item.', 'An item.', {}, 'lookup', (), None, None)
        self.assertEqual(len(model.calls), 2)


if __name__ == '__main__':
    unittest.main()
