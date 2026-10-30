import json
import unittest
from datetime import datetime, timezone

from memsys.aggregation import answer_with_validation, inspect_calculation
from memsys.dateproof import source_quote
from memsys.fake import ScriptedChat
from memsys.queryops import is_duration_total, operation_for


class DurationAndDateCitationTests(unittest.TestCase):
    def output(self, spec, answer):
        return 'NOTES: source-backed calculation.\nCALCULATION: ' + json.dumps(spec) + '\nANSWER: ' + answer

    def test_fixed_duration_sum_uses_literal_half_and_rejects_integer_component(self):
        question = 'How many weeks did it take me to complete both the ceramics and the weaving courses?'
        evidence = {'E1': 'The ceramics course took three weeks.',
                    'E2': 'The weaving course took a week and a half.'}
        spec = dict(operation='sum', unit='weeks', items=[
            dict(source='E1', quote=evidence['E1'], value='3', unit='weeks'),
            dict(source='E2', quote=evidence['E2'], value='1.5', unit='weeks')], result='4.5')
        self.assertEqual(operation_for(question), 'sum_events')
        self.assertEqual(inspect_calculation(self.output(spec, '4.5'), evidence, question=question)[1]['status'], 'checks_passed')
        spec['items'][1]['value'] = '1'
        spec['result'] = '4'
        self.assertEqual(inspect_calculation(self.output(spec, '4'), evidence, question=question)[1]['status'], 'invalid')

    def test_calendar_interval_is_not_reclassified_by_two_event_names(self):
        question = 'How many days passed between the delivery and the inspection?'
        self.assertFalse(is_duration_total(question))
        self.assertEqual(operation_for(question), 'date_difference')


    def test_duration_total_overrides_calendar_planner_and_keeps_all_sources(self):
        from helpers import models, settings
        from memsys.read import plan_query
        question = 'How many weeks did it take me to complete the ceramics and weaving courses?'
        model = ScriptedChat({'query':[json.dumps(dict(queries=[question], keywords=['courses'],
                                                       operation='date_difference', needs_all=False))]})
        bundle = models()
        bundle.memory = model
        plan = plan_query(bundle, question, datetime(2025, 7, 1, tzinfo=timezone.utc), settings(semantic_validation=True))
        self.assertEqual(plan.operation, 'sum_events')
        self.assertTrue(plan.needs_all)

    def test_duration_without_calculation_evidence_cannot_pass(self):
        question = 'How many weeks did it take me to finish the ceramics and weaving courses?'
        model = ScriptedChat({'answer': ['ANSWER: 4.5', 'ANSWER: unknown']})
        _, answer, audit = answer_with_validation(model, [], evidence={}, question=question,
                                                  semantic_enabled=False)
        self.assertEqual(answer, 'unknown')
        self.assertEqual(audit['attempts'][0]['status'], 'invalid')

    def test_program_copies_source_and_retains_literal_date_and_clock_guards(self):
        text = '[E1] conversation on 2025-06-04\nUser: I received the kiln controller today.'
        evidence = {'E1': text}
        point = dict(source='E1', full_source=True, event_text='received the kiln controller', date_text='2025-06-04')
        self.assertEqual(source_quote(point, evidence), text)
        spec = dict(operation='date_difference', unit='days', start=point,
                    end=dict(source='TODAY'), result='3')
        _, audit = inspect_calculation(self.output(spec, '3'), evidence,
                                       today='2025-06-07', question='How many days ago did I receive the kiln controller?')
        self.assertEqual(audit['status'], 'checks_passed')
        point['date_text'] = '2025-06-03'
        self.assertEqual(inspect_calculation(self.output(spec, '3'), evidence, today='2025-06-07')[1]['status'], 'invalid')

    def test_full_source_requires_an_event_in_the_body_and_a_boolean_flag(self):
        text = '[E1] conversation on 2025-06-04\nUser: I received the kiln controller today.'
        spec = dict(operation='date_difference', unit='days',
                    start=dict(source='E1', full_source=True, event_text='2025-06-04', date_text='2025-06-04'),
                    end=dict(source='TODAY'), result='3')
        self.assertEqual(inspect_calculation(self.output(spec, '3'), {'E1':text}, today='2025-06-07')[1]['status'], 'invalid')
        with self.assertRaises(ValueError):
            source_quote(dict(source='E1', full_source='true'), {'E1':text})


if __name__ == '__main__':
    unittest.main()
