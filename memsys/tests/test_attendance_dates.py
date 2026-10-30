import json
import unittest
from datetime import datetime

from helpers import settings
from memsys.aggregation import answer_with_validation, inspect_calculation
from memsys.config import Models
from memsys.core import MemorySystem
from memsys.daycount import attendance_dates
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.queryops import is_attendance_day_count


class AttendanceDatesTest(unittest.TestCase):
    def setUp(self):
        self.question = 'How many days did I spend attending seminars and exhibits in June?'
        self.evidence = {'E1': '[E1] 2024-06-12\nUser: I attended seminars on the 6th and 7th of June.',
                         'E2': '[E2] 2024-06-15\nUser: I attended an exhibit on June 7th.',
                         'E3': '[E3] 2024-06-20\nUser: I attended a talk on June 19th.'}
        self.items = [dict(source='E1', full_source=True, event_text='I attended seminars',
                           date_text='6th and 7th of June', anchor='recorded_at'),
                      dict(source='E2', full_source=True, event_text='I attended an exhibit',
                           date_text='June 7th', anchor='recorded_at'),
                      dict(source='E3', full_source=True, event_text='I attended a talk',
                           date_text='June 19th', anchor='recorded_at')]

    def output(self, items=None, result=3, unit='days'):
        return 'CALCULATION: '+json.dumps(dict(operation='count_dates', unit=unit,
            items=items or self.items, result=result))+'\nANSWER: '+str(result)+' '+unit

    def inspect(self, items=None, result=3, unit='days', question=None):
        return inspect_calculation(self.output(items, result, unit), self.evidence,
                                   question=question or self.question, today='2030-12-01')[1]

    def test_calendar_union_deduplicates_same_day_across_events(self):
        audit = self.inspect()
        self.assertEqual((audit['status'], audit['computed']), ('checks_passed', '3'))
        self.assertEqual(audit['dates'], ['2024-06-06', '2024-06-07', '2024-06-19'])
        self.assertEqual(audit['duplicates_removed'], 1)

    def test_complete_body_date_needs_no_recorded_year(self):
        evidence = {'E1': 'User: I attended a talk on 2022-07-06.'}
        point = dict(source='E1', quote=evidence['E1'], event_text='I attended a talk', date_text='2022-07-06')
        self.assertEqual(attendance_dates(point, evidence), ['2022-07-06'])

    def test_explicit_year_date_lists_and_comma_lists(self):
        for phrase, expected in [('6th and 7th of June 2022', ['2022-06-06', '2022-06-07']),
                                 ('the 6th and 7th of June 2022', ['2022-06-06', '2022-06-07']),
                                 ('June 6th, 7th and 9th, 2022', ['2022-06-06', '2022-06-07', '2022-06-09'])]:
            evidence = {'E1': 'User: I attended seminars on '+phrase+'.'}
            point = dict(source='E1', quote=evidence['E1'], event_text='I attended seminars', date_text=phrase)
            self.assertEqual(attendance_dates(point, evidence), expected)

    def test_missing_year_anchor_or_header_cannot_use_caller_year(self):
        for change in [dict(anchor=None), dict(anchor='today')]:
            self.assertEqual(self.inspect([dict(self.items[0], **change)], result=2)['status'], 'invalid')
        self.evidence['E1'] = self.evidence['E1'].split('\n', 1)[1]
        self.assertEqual(self.inspect([self.items[0]], result=2)['status'], 'invalid')

    def test_completed_attendance_can_anchor_to_previous_calendar_year(self):
        for when, phrase, expected in [('2025-01-12', 'December 8th and 9th', ['2024-12-08','2024-12-09']),
                                       ('2025-06-12', 'June 6th', ['2025-06-06'])]:
            evidence = {'E1': '[E1] '+when+'\nUser: I attended seminars on '+phrase+'.'}
            point = dict(source='E1', full_source=True, event_text='I attended seminars',
                         date_text=phrase, anchor='recorded_at_past')
            self.assertEqual(attendance_dates(point, evidence), expected)

    def test_past_year_anchor_cannot_use_assistant_facts_or_omit_stated_year(self):
        for key, text, phrase, event in [
            ('F1', '[F1] noted 2025-01-12\nI attended seminars on December 8th.', 'December 8th', 'I attended seminars'),
            ('E1', '[E1] 2025-01-12\nAssistant: I attended seminars on December 8th.', 'December 8th', 'I attended seminars'),
            ('E1', '[E1] 2025-01-12\nUser: I attended seminars on December 8th, 2020.', 'December 8th', 'I attended seminars'),
            ('E1', '[E1] 2025-01-12\nUser: I attended seminars on December 8th last year.', 'December 8th', 'I attended seminars')]:
            point = dict(source=key, full_source=True, event_text=event, date_text=phrase, anchor='recorded_at_past')
            with self.assertRaises(ValueError):
                attendance_dates(point, {key:text})

    def test_header_date_bare_day_and_other_year_cannot_supply_attendance_date(self):
        for change in [dict(date_text='2024-06-12'), dict(date_text='6th'),
                       dict(event_text='missing event'), dict(date='2030-06-06')]:
            self.assertEqual(self.inspect([dict(self.items[0], **change)], result=2)['status'], 'invalid')
        self.evidence['E1'] = self.evidence['E1'].replace('on the', 'last year on the')
        self.assertEqual(self.inspect([self.items[0]], result=2)['status'], 'invalid')

    def test_wrong_result_unit_or_elapsed_duration_cannot_use_calendar_count(self):
        self.assertEqual(self.inspect(result=4)['status'], 'invalid')
        self.assertEqual(self.inspect(unit='hours')['status'], 'invalid')
        for question in ['How many hours did I spend attending seminars and exhibits?',
                         'How many elapsed days did I spend attending seminars and exhibits?',
                         'How many full days did I spend attending seminars and exhibits?',
                         'How many days passed between my attending a seminar and attending an exhibit?']:
            self.assertFalse(is_attendance_day_count(question))
            self.assertEqual(self.inspect(question=question)['status'], 'invalid')

    def test_actual_attendance_still_needs_semantic_review(self):
        model = ScriptedChat({'answer': self.output(), 'answer_review': json.dumps(dict(
            responsive=True, supported=False, reason='These events were planned rather than attended.'))})
        _, answer, audit = answer_with_validation(model, [], evidence=self.evidence,
            question=self.question, operation='sum_events', require_calculation=True, semantic_enabled=True)
        self.assertEqual(audit['status'], 'incomplete')
        self.assertNotEqual(answer, '3 days')

    def test_verified_calendar_metadata_reaches_the_support_review(self):
        model = ScriptedChat({'answer': self.output(), 'answer_review': json.dumps(dict(responsive=True, supported=True))})
        _, answer, audit = answer_with_validation(model, [], evidence=self.evidence,
            question=self.question, operation='sum_events', require_calculation=True, semantic_enabled=True)
        self.assertEqual((answer, audit['status']), ('3 days', 'checks_passed'))
        review = next(messages for purpose, messages in model.calls if purpose == 'answer_review')
        self.assertIn('2024-06-19', review[-1]['content'])
        self.assertIn('not elapsed 24-hour periods', review[-1]['content'])

    def test_paged_recovery_collects_dated_events_instead_of_only_numeric_durations(self):
        def page_reply(messages):
            import re
            sources = re.findall(r'\[(E\d+)\][^\n]*\n(L1:[^\n]*)', messages[-1]['content'])
            return json.dumps(dict(reviewed=[key for key, _ in sources], items=[
                dict(source=key, line_start=1, line_end=1) for key, _ in sources]))
        items = [dict(source='E1', full_source=True, event_text='I attended a seminar',
                      date_text='June 6th', anchor='recorded_at'),
                 dict(source='E2', full_source=True, event_text='I attended an exhibit',
                      date_text='June 7th', anchor='recorded_at')]
        model = ScriptedChat({'evidence_page': page_reply, 'answer': self.output(items, result=2),
            'answer_review': json.dumps(dict(responsive=True, supported=True)),
            'evidence_select': json.dumps(dict(keep=['C1','C2'], insufficient=False))})
        system = MemorySystem(Models(chat=model, memory=None, embedder=HashEmbedder(), embedding_name='hash'),
            settings(query_analysis=False, semantic_validation=True, budget_mode='utf8',
                     context_budget_tokens=700, answer_input_budget_tokens=14000,
                     complete_episode_context=False, max_episodes=1, max_history_share=0))
        self.addCleanup(system.close)
        for when, text in [('2024-06-08T10:00', 'I attended a seminar on June 6th.'),
                           ('2024-06-09T10:00', 'I attended an exhibit on June 7th.')]:
            at = datetime.fromisoformat(when)
            handle = system.open_session('u', at)
            system.add_turn(handle, 'user', text, at)
            system.close_session(handle, at)
        from unittest.mock import patch
        from memsys.read import QueryPlan
        plan = QueryPlan([self.question], [], None, True, 'past', 'llm', operation='latest_state')
        with patch('memsys.core.plan_query', return_value=plan):
            result = system.respond('u', self.question, datetime(2024,6,20,10))
        self.assertEqual(result.text, '2 days')
        self.assertIn('evidence_page', model.purposes())
        self.assertNotIn('aggregate_page', model.purposes())
        self.assertEqual(result.trace['aggregation']['answer_validation']['operation'], 'count_dates')


if __name__ == '__main__':
    unittest.main()
