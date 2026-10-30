import json
import unittest
from datetime import date

from memsys.aggregation import answer_with_validation, inspect_calculation, is_temporal_interval, wants_calculation
from memsys.dateproof import interval, literal_day
from memsys.fake import ScriptedChat


class DateProofTest(unittest.TestCase):
    def setUp(self):
        self.quote = 'I reserved the cabin on November 10, 2024 for a trip on March 10, 2025.'
        self.evidence = {'E1': self.quote}
        self.spec = dict(operation='date_difference', unit='months',
            start=dict(source='E1', quote=self.quote, date_text='November 10, 2024', date='2024-11-10'),
            end=dict(source='TODAY', date='2025-02-10'), result='3')
        self.question = 'How many calendar months ago did I reserve the cabin?'

    def output(self, spec=None, answer='3'):
        return 'NOTES: date evidence.\nCALCULATION: ' + json.dumps(spec or self.spec) + '\nANSWER: ' + answer

    def inspect(self, spec=None, answer='3'):
        return inspect_calculation(self.output(spec, answer), self.evidence,
                                   today='2025-02-10', question=self.question)[1]

    def test_literal_dates_and_trusted_today_produce_calendar_interval(self):
        audit = self.inspect()
        self.assertEqual(audit['status'], 'checks_passed')
        self.assertEqual(audit['computed'], '3')
        self.assertEqual(audit['endpoints'], ['2024-11-10', '2025-02-10'])

    def test_conversation_date_cannot_replace_caller_today(self):
        wrong = dict(self.spec, end=dict(source='TODAY', date='2025-01-10'), result='2')
        self.assertEqual(self.inspect(wrong, '2')['status'], 'invalid')
        self.evidence['E2'] = 'The conversation was on January 10, 2025.'
        wrong['end'] = dict(source='E2', quote=self.evidence['E2'], date_text='January 10, 2025', date='2025-01-10')
        self.assertIn('ago/since', self.inspect(wrong, '2')['errors'][0])

    def test_wrong_interval_result_or_answer_fails(self):
        self.assertEqual(self.inspect(dict(self.spec, result='2'), '2')['status'], 'invalid')
        self.assertEqual(self.inspect(answer='2')['status'], 'invalid')

    def test_vague_or_invented_day_cannot_establish_exact_interval(self):
        for literal, declared in [('November 2024', '2024-11-01'), ('2024-11', '2024-11-10')]:
            self.evidence['E1'] = 'The reservation was in ' + literal + '.'
            spec = dict(self.spec, start=dict(source='E1', quote=self.evidence['E1'], date_text=literal, date=declared))
            self.assertEqual(self.inspect(spec)['status'], 'invalid')
        self.evidence['E1'] = 'Invalid date: 2024-11-100.'
        spec = dict(self.spec, start=dict(source='E1', quote=self.evidence['E1'], date_text='2024-11-10', date='2024-11-10'))
        self.assertEqual(self.inspect(spec)['status'], 'invalid')

    def test_complete_units_handle_leap_days_month_ends_and_sign(self):
        self.assertEqual(interval(date(2024, 2, 28), date(2024, 3, 1), 'days'), 2)
        self.assertEqual(interval(date(2025, 1, 31), date(2025, 2, 28), 'months'), 1)
        self.assertEqual(interval(date(2025, 2, 28), date(2025, 1, 31), 'months'), -1)
        self.assertEqual(interval(date(2024, 2, 29), date(2025, 2, 28), 'years'), 1)
        self.assertEqual(literal_day('2025年2月10日'), date(2025, 2, 10))

    def test_repair_uses_validated_clock_and_numeric_without_proof_is_rejected(self):
        model = ScriptedChat({'answer': ['ANSWER: 2', self.output()]})
        _, answer, audit = answer_with_validation(model, [{'role': 'user', 'content': 'Today: 2025-02-10'}],
            evidence=self.evidence, question=self.question, today='2025-02-10')
        self.assertEqual(answer, '3')
        self.assertEqual(audit['status'], 'checks_passed')
        model = ScriptedChat({'answer': 'ANSWER: 2'})
        _, answer, audit = answer_with_validation(model, [], evidence=self.evidence,
            question=self.question, today='2025-02-10')
        self.assertIn('couldn\'t verify this date interval', answer)
        self.assertEqual(audit['status'], 'invalid')

    def test_interval_grammar_does_not_enter_quantity_aggregation(self):
        for question in (
            'How many days before the observatory inspection did I calibrate the telescope?',
            'How many days had elapsed between the telescope calibration and the dome inspection?',
            'How many weeks had I been enrolled when the observatory orientation began?',
            'How many days did it take to finish the telescope calibration?',
            '望远镜校准和穹顶检查相隔多少天？',
        ):
            self.assertTrue(is_temporal_interval(question), question)
            self.assertFalse(wants_calculation(question), question)
        for question in ('How many meteorite samples did I catalog?',
                         'How many days did I exercise across all recorded months?'):
            self.assertFalse(is_temporal_interval(question), question)
            self.assertTrue(wants_calculation(question), question)

    def test_recorded_activity_duration_is_not_a_calendar_difference(self):
        for question in ('How long did it take to calibrate the spectrometer?',
                         'How long have I been running this exposure?',
                         '校准光谱仪用了多久？'):
            self.assertFalse(is_temporal_interval(question), question)
            model = ScriptedChat({'answer': 'NOTES: E1 states the duration.\nANSWER: 3 hours.'})
            _, answer, audit = answer_with_validation(model, [],
                evidence={'E1': 'I spent 3 hours calibrating the spectrometer.'},
                question=question, today='2025-02-10')
            self.assertEqual(answer, '3 hours.')
            self.assertEqual(audit['status'], 'not_provided')
            self.assertEqual(len(model.calls), 1)

    def test_date_repair_retains_sources_without_replaying_oversized_draft(self):
        from memsys.budget import TokenCounter
        counter = TokenCounter('utf8')
        original = [{'role': 'user', 'content': 'Today: 2025-02-10\n' + self.quote + '\n' + 'Reference notes. ' * 170}]
        draft = 'NOTES: ' + 'Rejected lengthy reasoning. ' * 220 + '\nANSWER: 2'
        model = ScriptedChat({'answer': [draft, self.output()]})
        _, answer, audit = answer_with_validation(model, original, evidence=self.evidence,
            question=self.question, today='2025-02-10', counter=counter, input_limit=6000)
        self.assertEqual(answer, '3')
        self.assertEqual(audit['status'], 'checks_passed')
        repaired = model.calls[1][1]
        self.assertEqual(repaired[0], original[0])
        self.assertFalse(any(message['role'] == 'assistant' for message in repaired))
        self.assertLessEqual(counter.messages(repaired), 6000)
        self.assertGreater(counter.messages(original + [{'role': 'assistant', 'content': draft}]), 6000)

    def test_nearly_full_prompt_repair_preserves_all_evidence_and_stays_within_limit(self):
        from memsys.budget import TokenCounter
        counter = TokenCounter('utf8')
        self.evidence['E2'] = 'The spectrometer inspection was logged on January 3, 2025.'
        sources = '\n'.join(self.evidence.values())
        original = [{'role': 'user', 'content': sources + '\n' + 'Generic framing. ' * 260}]
        model = ScriptedChat({'answer': ['ANSWER: 2', self.output()]})
        _, answer, audit = answer_with_validation(model, original, evidence=self.evidence,
            question=self.question, today='2025-02-10', counter=counter, input_limit=5000)
        self.assertEqual(answer, '3')
        self.assertEqual(audit['status'], 'checks_passed')
        self.assertEqual(audit['repair_context'], 'source_evidence')
        self.assertLessEqual(audit['repair_prompt_budget_count'], 5000)
        repaired_text = '\n'.join(message['content'] for message in model.calls[1][1])
        for source in self.evidence.values():
            self.assertIn(source, repaired_text)
        self.assertIn('Today: 2025-02-10', repaired_text)
        self.assertIn(self.question, repaired_text)

    def test_historical_since_when_uses_two_sources_and_requested_units(self):
        self.evidence['E2'] = 'The dome inspection happened on February 10, 2025.'
        spec = dict(self.spec, end=dict(source='E2', quote=self.evidence['E2'],
                                      date_text='February 10, 2025', date='2025-02-10'))
        question = 'How many months had passed since the telescope calibration when the dome inspection happened?'
        audit = inspect_calculation(self.output(spec), self.evidence, today='2025-03-01', question=question)[1]
        self.assertEqual(audit['status'], 'checks_passed')
        audit = inspect_calculation(self.output(spec), self.evidence, today='2025-03-01',
                                    question=question.replace('months', 'days'))[1]
        self.assertEqual(audit['status'], 'invalid')
        self.assertIn('requested unit', audit['errors'][0])

    def test_actual_displayed_source_dates_are_available_to_validation(self):
        import re
        from helpers import dt, models, settings
        from memsys.core import MemorySystem
        def answer(messages):
            blocks = re.findall(r'(\[(E\d+)\] conversation on (\d{4}-\d{2}-\d{2})[^\n]*\nUser: Today[^\n]+)',
                                messages[-1]['content'])
            self.assertEqual(len(blocks), 2)
            points = [dict(source=source, quote=block, date_text=day, date=day)
                      for block, source, day in blocks]
            spec = dict(operation='date_difference', unit='days', start=points[0], end=points[1], result='15')
            return self.output(spec, '15')
        system = MemorySystem(models(chat={'answer': answer}), settings(query_analysis=False,
                              min_similarity=0, max_history_share=0, context_budget_tokens=1800))
        self.addCleanup(system.close)
        store = system.store('observatory')
        with store.transaction():
            for n, (day, text) in enumerate((('2025-01-03', 'Today I calibrated the telescope.'),
                                             ('2025-01-18', 'Today I inspected the dome.'))):
                at = dt(day+'T10:00')
                sid = store.create_session(at)
                store.add_episode(sid, n, at, text, '', .8, 'active')
        reply = system.respond('observatory', 'How many days had elapsed between calibration and inspection?',
                               dt('2025-02-05T10:00'))
        self.assertEqual(reply.text, '15')
        self.assertEqual(reply.trace['aggregation']['status'], 'checks_passed')
