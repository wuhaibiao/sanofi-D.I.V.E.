import json
import unittest

from memsys.aggregation import answer_with_validation, inspect_calculation
from memsys.fake import ScriptedChat


def response(spec, answer):
    return 'CALCULATION: '+json.dumps(spec)+'\nANSWER: '+answer


class CompletedEventBoundsTest(unittest.TestCase):
    def setUp(self):
        self.evidence={
            'E1':'[E1] 2025-04-02 speaker=mixed\nUser: I am glad I returned the rented lantern.\nAssistant: Try another lantern tomorrow.',
            'E2':'[E2] 2025-04-23 speaker=user\nUser: I collected the new telescope today.'}
        self.spec=dict(operation='date_difference',unit='days',bound='at_least',result=21,
            start=dict(source='E1',full_source=True,event_text='I returned the rented lantern',completed_by_recorded_date=True),
            end=dict(source='E2',quote='I collected the new telescope today.',event_text='I collected the new telescope',
                     relative_text='today',anchor='recorded_at'))
        self.question='How many days passed, at minimum, between returning the lantern and collecting the telescope?'

    def check(self,spec=None,answer='At least 21 days; the exact return date was not recorded.',evidence=None,question=None,today='2025-04-24'):
        return inspect_calculation(response(spec or self.spec,answer),evidence or self.evidence,
            question=question or self.question,today=today)[1]

    def test_recording_day_proves_only_a_lower_bound(self):
        audit=self.check()
        self.assertEqual((audit['status'],audit['computed'],audit['bound']),('checks_passed','21','at_least'))
        self.assertEqual(audit['endpoint_roles'],['latest_possible_start','exact_end'])
        self.assertEqual(audit['basis'],'completed_by_recorded_date')

    def test_ordinary_intervals_and_coarse_units_cannot_use_recording_bounds(self):
        ordinary=self.question.replace(', at minimum,', '')
        self.assertEqual(self.check(question=ordinary)['status'],'invalid')
        for unit in ['months','years']:
            spec=dict(self.spec,unit=unit,result=0)
            self.assertEqual(self.check(spec,answer='At least 0 '+unit+'.',question=self.question.replace('days',unit))['status'],'invalid')

    def test_answer_cannot_turn_bound_into_exact_or_approximate_interval(self):
        for answer in ['21 days.','About 21 days.']:
            audit=self.check(answer=answer)
            self.assertEqual(audit['status'],'invalid')
            self.assertIn('at-least qualification',' '.join(audit['errors']))
        self.assertEqual(self.check(answer='至少 21 天；确切日期未记录。')['status'],'checks_passed')
        self.assertEqual(self.check(answer='At least 22 days.')['status'],'invalid')

    def test_explicit_opt_in_cannot_assert_exact_dates(self):
        for extra in [dict(completed_by_recorded_date=False),dict(date='2025-04-02'),
                      dict(date_text='2025-04-02'),dict(anchor='recorded_at'),dict(year_text='2025')]:
            spec=dict(self.spec,start=dict(self.spec['start'],**extra))
            self.assertEqual(self.check(spec)['status'],'invalid')
        for extra in [dict(bound='at_most'),dict(bound=None),dict(precision='year'),dict(rounding='nearest')]:
            self.assertEqual(self.check(dict(self.spec,**extra))['status'],'invalid')
        spec={k:v for k,v in self.spec.items() if k!='bound'}
        self.assertEqual(self.check(spec)['status'],'invalid')

    def test_two_bounded_events_and_unknown_source_do_not_prove_an_interval(self):
        end=dict(self.spec['start'],source='E2',event_text='I collected the new telescope')
        self.assertEqual(self.check(dict(self.spec,end=end))['status'],'invalid')
        self.assertEqual(self.check(dict(self.spec,start=dict(self.spec['start'],source='E9')))['status'],'invalid')
        evidence=dict(self.evidence,F1=self.evidence['E1'].replace('[E1]','[F1]'))
        self.assertEqual(self.check(dict(self.spec,start=dict(self.spec['start'],source='F1')),evidence=evidence)['status'],'invalid')

    def test_event_must_be_in_original_user_turn_not_header_or_assistant_advice(self):
        for event in ['2025-04-02','Try another lantern tomorrow','I returned a different item']:
            spec=dict(self.spec,start=dict(self.spec['start'],event_text=event))
            self.assertEqual(self.check(spec)['status'],'invalid')
        for body in ['Assistant: I returned the rented lantern.','I returned the rented lantern.']:
            evidence=dict(self.evidence,E1='[E1] 2025-04-02\n'+body)
            self.assertEqual(self.check(evidence=evidence)['status'],'invalid')

    def test_ago_bound_uses_caller_clock_and_earlier_end_remains_unresolved(self):
        spec=dict(self.spec,end=dict(source='TODAY'),result=22)
        audit=self.check(spec,answer='At least 22 days; exact duration unknown.',question='How many days ago, at minimum, did I return the lantern?')
        self.assertEqual((audit['status'],audit['computed']),('checks_passed','22'))
        self.assertEqual(self.check(question='How many days ago, at minimum, did I return the lantern?')['status'],'invalid')
        self.assertEqual(self.check(spec,answer='At least -1 days.',today='2025-04-01')['status'],'invalid')

    def test_unit_matching_and_complete_weeks_remain_checked(self):
        spec=dict(self.spec,unit='weeks',result=3)
        audit=self.check(spec,answer='At least 3 weeks; exact duration unknown.',question=self.question.replace('days','weeks'))
        self.assertEqual((audit['status'],audit['computed']),('checks_passed','3'))
        self.assertEqual(self.check(spec,answer='At least 3 weeks.')['status'],'invalid')

    def test_semantic_review_still_rejects_uncompleted_action_despite_numeric_proof(self):
        evidence=dict(self.evidence,E1='[E1] 2025-04-02\nUser: I might return the rented lantern.')
        spec=dict(self.spec,start=dict(self.spec['start'],event_text='return the rented lantern'))
        output=response(spec,'At least 21 days; exact duration unknown.')
        model=ScriptedChat({'answer':output,'answer_review':json.dumps(dict(responsive=False,supported=False,reason='The user only planned the return.'))})
        _,answer,audit=answer_with_validation(model,[],evidence=evidence,question=self.question,
            today='2025-04-24',operation='date_difference',semantic_enabled=True)
        self.assertEqual(audit['status'],'incomplete')
        self.assertNotIn('At least 21',answer)
        self.assertEqual(audit['semantic_reviews'][0]['supported'],False)


if __name__=='__main__':unittest.main()
