import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from helpers import settings
from memsys.aggregation import answer_with_validation, inspect_calculation
from memsys.dateproof import rounded_calendar_interval
from memsys.evidence_batch import paged_answer
from memsys.fake import ScriptedChat
from memsys.llm import ChatResult
from memsys.semantic_review import review_response


def output(spec, answer):
    return 'CALCULATION: ' + json.dumps(spec) + '\nANSWER: ' + answer


class BoundedRecoveryTest(unittest.TestCase):
    def test_truncated_answer_doubles_actual_allowance_and_discards_draft(self):
        class Model:
            config = SimpleNamespace(output_token_headroom=4096)

            def __init__(self):
                self.requests = []

            def chat(self, messages, **kwargs):
                self.requests.append((messages, kwargs['max_tokens']))
                text, reason = ('ANSWER: 999', 'length') if len(self.requests) == 1 else ('ANSWER: complete', 'stop')
                return ChatResult(text, 'test', 0, 0, 0, 0, 0, reason)
        model = Model()
        _, answer, audit = answer_with_validation(model, [], evidence={}, enabled=False, max_tokens=700)
        self.assertEqual(answer, 'complete')
        actual = [tokens + model.config.output_token_headroom for _, tokens in model.requests]
        self.assertEqual(actual, [4796, 9592])
        self.assertNotIn('999', json.dumps(model.requests[1][0]))
        self.assertEqual(len(audit['generation_attempts']), 2)

    def test_truncated_review_doubles_actual_allowance_without_accepting_verdict(self):
        class Model:
            config = SimpleNamespace(output_token_headroom=4096)

            def __init__(self):
                self.budgets = []

            def chat(self, messages, **kwargs):
                self.budgets.append(kwargs['max_tokens'])
                data = dict(responsive=True, supported=len(self.budgets) > 1)
                return ChatResult(json.dumps(data), 'test', 0, 0, 0, 0, 0,
                                  'length' if len(self.budgets) == 1 else 'stop')
        model = Model()
        review = review_response(model, 'Recall my item.', 'My item.', {}, 'lookup', (), None, None)
        self.assertTrue(review['supported'])
        self.assertEqual([b + 4096 for b in model.budgets], [4696, 9392])

    def test_truncated_evidence_page_retries_with_same_sources_and_more_room(self):
        class Model(ScriptedChat):
            config = SimpleNamespace(output_token_headroom=4096)

            def __init__(self, handlers):
                super().__init__(handlers)
                self.budgets = []

            def chat(self, messages, **kwargs):
                self.budgets.append((kwargs['purpose'], kwargs['max_tokens']))
                return super().chat(messages, **kwargs)
        text = 'The organizers I liked were document wallets and drawer trays.'
        page = {'E1': dict(text=text, rendered='[E1] 2025-04-02\n' + text, can_contribute=True)}
        valid = json.dumps(dict(reviewed=['E1'], items=[dict(source='E1', quote=text)]))
        model = Model({'evidence_page': lambda _: ChatResult('{}', 'test', 0, 0, 0, 0, 0, 'length'),
                       'evidence_page_repair': valid, 'answer': 'ANSWER: document wallets, drawer trays'})
        with patch('memsys.evidence_batch.aggregation_pages', return_value=[page]):
            _, answer, audit = paged_answer(model, 'List my preferred organizers.',
                SimpleNamespace(coverage={'search_complete': True}, items={}), {},
                settings(extract_max_tokens=800), date(2025, 4, 4))
        self.assertEqual(answer, 'document wallets, drawer trays')
        self.assertTrue(audit['pages_complete'])
        self.assertEqual(model.budgets[:2], [('evidence_page', 800), ('evidence_page_repair', 5696)])
        self.assertIn(text, json.dumps(model.calls[1][1]))

    def test_measurement_failure_can_repair_as_literal_category_counts(self):
        evidence = {'E1': 'Six training jackets arrived.', 'E2': '4 raincoats arrived.'}
        items = [dict(source=key, quote=quote, value=str(value), unit='outerwear')
                 for (key, quote), value in zip(evidence.items(), [6, 4])]
        invalid = dict(operation='sum', unit='outerwear', items=items, result='10')
        valid = dict(invalid, operation='count_groups', items=[
            dict(items[0], quantity_label='training jackets'), dict(items[1], quantity_label='raincoats')])
        model = ScriptedChat({'answer': [output(invalid, '10'), output(valid, '10')]})
        _, answer, audit = answer_with_validation(model, [], evidence=evidence,
            question='How many outerwear items arrived in total?', require_calculation=True)
        self.assertEqual(answer, '10')
        self.assertEqual(audit['status'], 'checks_passed')
        self.assertEqual(audit['entries'], 2)
        self.assertIn('quantity_label', json.dumps(model.calls[1][1]))

    def test_sum_of_literal_completed_ordinal_and_cardinal_counts(self):
        evidence = {'E1': 'I applied my fifth patch to the training kit, finishing that pack.',
                    'E2': 'I applied 7 patches from a different pack.'}
        items = [dict(source='E1', quote=evidence['E1'], value='5', unit='patches',
                      ordinal_progress=True, value_type='cumulative'),
                 dict(source='E2', quote=evidence['E2'], value='7', unit='patches')]
        spec = dict(operation='sum', unit='patches', items=items, result='12')
        audit = inspect_calculation(output(spec, '12'), evidence)[1]
        self.assertEqual((audit['status'], audit['computed']), ('checks_passed', '12'))
        for fields in [dict(ordinal_progress=False), dict(value_type='independent'), dict(value='6')]:
            changed = dict(spec, items=[dict(items[0], **fields), items[1]])
            self.assertEqual(inspect_calculation(output(changed, '12'), evidence)[1]['status'], 'invalid')
        without_claim = dict(items[0])
        without_claim.pop('ordinal_progress')
        self.assertEqual(inspect_calculation(output(dict(spec, items=[without_claim, items[1]]), '12'),
                                            evidence)[1]['status'], 'invalid')

    def test_ordinal_sum_cannot_bypass_measurement_or_conversion_proofs(self):
        for unit in ['USD', 'days', 'months', 'years']:
            quote = 'This is my fifth ' + unit + '.'
            item = dict(source='E1', quote=quote, value='5', unit=unit,
                        ordinal_progress=True, value_type='cumulative')
            spec = dict(operation='sum', unit=unit, items=[item], result='5')
            self.assertEqual(inspect_calculation(output(spec, '5'), {'E1': quote})[1]['status'], 'invalid')
            spec['operation'] = 'count_groups'
            item['quantity_label'] = unit
            self.assertEqual(inspect_calculation(output(spec, '5'), {'E1': quote})[1]['status'], 'invalid')

    def test_explicit_source_year_cues_keep_literal_quotes_and_leap_arithmetic(self):
        for start_text, end_text in [('February 28th this year', 'this year, March 2nd'),
                                     ('今年2月28日', '今年3月2日')]:
            evidence = {'E1': '[E1] 2024-03-07\nSent on ' + start_text + '.',
                        'E2': '[E2] 2024-03-09\nArrived on ' + end_text + '.'}
            spec = dict(operation='date_difference', unit='days', result='3',
                start=dict(source='E1', quote=evidence['E1'], date_text=start_text, anchor='recorded_at'),
                end=dict(source='E2', quote=evidence['E2'], date_text=end_text, anchor='recorded_at'))
            audit = inspect_calculation(output(spec, '3'), evidence, today='2031-04-01')[1]
            self.assertEqual((audit['status'], audit['computed']), ('checks_passed', '3'))
            wrong = dict(spec, start=dict(spec['start'], date='2031-02-28'))
            self.assertEqual(inspect_calculation(output(wrong, '3'), evidence)[1]['status'], 'invalid')

    def test_calendar_rounding_uses_anniversaries_and_explicit_approximation(self):
        evidence = {'E1': 'Started on April 10, 2025.', 'E2': 'Finished on August 7, 2025.'}
        spec = dict(operation='date_difference', unit='months', result='4', rounding='nearest',
            start=dict(source='E1', quote=evidence['E1'], date_text='April 10, 2025'),
            end=dict(source='E2', quote=evidence['E2'], date_text='August 7, 2025'))
        question = 'How many months passed between starting and finishing?'
        audit = inspect_calculation(output(spec, 'About 4 months.'), evidence, question=question)[1]
        self.assertEqual((audit['status'], audit['computed'], audit['complete_units'], audit['remainder_days']),
                         ('checks_passed', '4', 3, 28))
        for answer, q in [('4', question), ('About 4 months.', question.replace('months', 'whole months')),
                          ('About 4 months.', question.replace('months', 'calendar months'))]:
            self.assertEqual(inspect_calculation(output(spec, answer), evidence, question=q)[1]['status'], 'invalid')
        spec.update(rounding='complete', result='3')
        self.assertEqual(inspect_calculation(output(spec, '3 complete months.'), evidence, question=question)[1]['status'],
                         'checks_passed')
        self.assertEqual(rounded_calendar_interval(date(2024, 1, 31), date(2024, 3, 29), 'months')['computed'], '2')
        self.assertEqual(rounded_calendar_interval(date(2024, 3, 29), date(2024, 1, 31), 'months')['computed'], '-2')
        self.assertEqual(rounded_calendar_interval(date(2024, 2, 29), date(2025, 8, 30), 'years')['computed'], '2')


if __name__ == '__main__':
    unittest.main()
