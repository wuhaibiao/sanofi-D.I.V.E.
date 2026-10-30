import json
import unittest

from helpers import dt, models, settings
from memsys.aggregation import answer_with_validation, inspect_calculation, is_temporal_interval
from memsys.fake import ScriptedChat
from memsys.queryops import is_calendar_span, operation_for
from memsys.read import plan_query


def output(spec, answer):
    return 'CALCULATION: ' + json.dumps(spec) + '\nANSWER: ' + answer


class CalendarYearPrecisionTest(unittest.TestCase):
    def setUp(self):
        self.evidence = {'E1': '[E1] 2027-04-02\nUser: The theater survey was completed in 2018.',
                         'E2': '[E2] 2027-04-03\nUser: The restored theater reopened in 2024.'}
        self.spec = dict(operation='date_difference', unit='years', precision='year', result='6',
            start=dict(source='E1', full_source=True, event_text='The theater survey was completed', year_text='2018'),
            end=dict(source='E2', full_source=True, event_text='The restored theater reopened', year_text='2024'))
        self.question = 'How many years in total passed from the theater survey to its reopening?'

    def check(self, spec=None, answer='About 6 years.', question=None, evidence=None):
        return inspect_calculation(output(spec or self.spec, answer), evidence or self.evidence,
                                   today='2029-04-01', question=question or self.question)[1]

    def test_body_years_support_approximate_span_without_inventing_days(self):
        audit = self.check()
        self.assertEqual((audit['status'], audit['computed'], audit['precision'], audit['years']),
                         ('checks_passed', '6', 'year', [2018, 2024]))
        self.assertNotIn('endpoints', audit)
        self.assertEqual(audit['basis'], 'quoted_calendar_years')

    def test_years_cannot_prove_exact_units_or_finer_units(self):
        self.assertEqual(self.check(answer='6 years.')['status'], 'invalid')
        for unit in ['days', 'weeks', 'months']:
            self.assertEqual(self.check(dict(self.spec, unit=unit))['status'], 'invalid')
        for modifier in ['whole', 'complete', 'exact', 'calendar']:
            question = self.question.replace('years', modifier + ' years')
            self.assertEqual(self.check(question=question)['status'], 'invalid')
        self.assertEqual(self.check(dict(self.spec, rounding='nearest'))['status'], 'invalid')

    def test_recording_year_cannot_date_an_undated_event(self):
        evidence = dict(self.evidence, E1='[E1] 2018-04-02\nUser: The theater survey was completed.')
        self.assertEqual(self.check(evidence=evidence)['status'], 'invalid')
        for fields in [dict(year_text='2017'), dict(date='2018-01-01'),
                       dict(date_text='2018'), dict(event_text='A different event')]:
            spec = dict(self.spec, start=dict(self.spec['start'], **fields))
            self.assertEqual(self.check(spec)['status'], 'invalid')

    def test_year_numbers_in_prices_or_identifiers_are_not_literal_years(self):
        for text in ['The theater survey cost $2018.', 'The theater survey used model X2018.']:
            evidence = dict(self.evidence, E1='[E1] 2027-04-02\nUser: ' + text)
            spec = dict(self.spec, start=dict(self.spec['start'], event_text='The theater survey'))
            self.assertEqual(self.check(spec, evidence=evidence)['status'], 'invalid')

    def test_year_ago_uses_caller_clock_and_same_year_does_not_prove_zero(self):
        spec = dict(self.spec, end=dict(source='TODAY'), result='11')
        audit = self.check(spec, 'About 11 years.', 'How many years ago was the theater survey completed?')
        self.assertEqual((audit['status'], audit['computed']), ('checks_passed', '11'))
        self.assertEqual(self.check(question='How many years ago was the theater survey completed?')['status'], 'invalid')
        self.assertEqual(self.check(dict(spec, end=dict(source='TODAY', year_text='2029')), 'About 11 years.')['status'], 'invalid')
        same = dict(self.spec, end=dict(self.spec['start']), result='0')
        self.assertEqual(self.check(same, 'About 0 years.')['status'], 'invalid')

    def test_completed_action_is_not_a_request_for_complete_year_units(self):
        question = 'How many years from when I completed the theater survey to the reopening?'
        self.assertEqual(self.check(question=question)['status'], 'checks_passed')

    def test_from_to_span_overrides_sum_plan_but_separate_activities_do_not(self):
        self.assertTrue(is_calendar_span(self.question))
        self.assertTrue(is_temporal_interval(self.question))
        self.assertEqual(operation_for(self.question), 'date_difference')
        planner = models(memory={'query': json.dumps(dict(operation='sum_events', needs_all=True))})
        plan = plan_query(planner, self.question, dt('2029-04-01T10:00'), settings())
        self.assertEqual(plan.operation, 'date_difference')
        question = 'How many years in total did I spend on both theater and library restoration?'
        self.assertFalse(is_calendar_span(question))
        self.assertFalse(is_temporal_interval(question))
        self.assertEqual(operation_for(question), 'sum_events')


class ApproximationRepairAuditTest(unittest.TestCase):
    def test_invalid_revised_numeric_field_is_recorded_and_not_published(self):
        evidence = {'E1': 'I made about 2 visits to the workshop.', 'E2': 'I made 3 visits to the studio.'}
        spec = dict(operation='sum', unit='visits', result='5', items=[
            dict(source=key, quote=quote, value=str(value), unit='visits')
            for (key, quote), value in zip(evidence.items(), [2, 3])])
        invalid = dict(spec, result='about 5')
        model = ScriptedChat({'answer': [output(spec, '5 visits.'), output(invalid, 'About 5 visits.')],
            'answer_review': json.dumps(dict(responsive=True, supported=False, reason='The estimate must remain approximate.'))})
        _, answer, audit = answer_with_validation(model, [], evidence=evidence,
            question='How many visits did I make in total?', semantic_enabled=True, require_calculation=True)
        self.assertEqual(audit['status'], 'incomplete')
        self.assertIn("don't have enough verified", answer)
        revised = audit['semantic_repair_calculation']
        self.assertEqual(revised['status'], 'invalid')
        self.assertEqual(revised['calculation']['result'], 'about 5')
        self.assertTrue(revised['errors'])


if __name__ == '__main__':
    unittest.main()
