import json
import unittest

from memsys.aggregation import _inspect_read_answer, answer_with_validation
from memsys.fake import ScriptedChat


def response(spec,answer):
    return 'CALCULATION: '+json.dumps(spec)+'\nANSWER: '+answer


class VerifiedDateFormatTest(unittest.TestCase):
    def setUp(self):
        self.evidence={'E1':'[E1] 2027-04-02\nUser: The theater survey was completed in 2018.',
                       'E2':'[E2] 2027-04-03\nUser: The restored theater reopened in 2024.'}
        self.spec=dict(operation='date_difference',unit='years',precision='year',result=6,
            start=dict(source='E1',full_source=True,event_text='The theater survey was completed',year_text='2018'),
            end=dict(source='E2',full_source=True,event_text='The restored theater reopened',year_text='2024'))
        self.question='How many years passed from the theater survey to its reopening?'

    def inspect(self,spec=None,answer='6 years.',question=None,evidence=None):
        return _inspect_read_answer(response(spec or self.spec,answer),evidence or self.evidence,
            today='2029-04-01',question=question or self.question)

    def test_bare_year_result_is_qualified_without_changing_proof_or_value(self):
        stripped,audit=self.inspect()
        self.assertEqual(audit['status'],'checks_passed')
        self.assertEqual(audit['calculation'],self.spec)
        self.assertEqual(audit['presentation_repair']['original_answer'],'6 years.')
        self.assertIn('ANSWER: About 6 years.',stripped)
        self.assertEqual(audit['computed'],'6')

    def test_invalid_quote_or_numeric_value_is_never_reformatted(self):
        specs=[dict(self.spec,result=5),dict(self.spec,start=dict(self.spec['start'],year_text='2017'))]
        for spec in specs:
            _,audit=self.inspect(spec)
            self.assertEqual(audit['status'],'invalid')
            self.assertNotIn('presentation_repair',audit)
        _,audit=self.inspect(answer='5 years.')
        self.assertEqual(audit['status'],'invalid')
        self.assertNotIn('presentation_repair',audit)

    def test_substantive_text_wrong_unit_and_exact_request_are_not_rewritten(self):
        for answer in ['Exactly 6 years.','6 years in continuous work.','6 days.']:
            _,audit=self.inspect(answer=answer)
            self.assertEqual(audit['status'],'invalid')
            self.assertNotIn('presentation_repair',audit)
        _,audit=self.inspect(question=self.question.replace('years','complete years'))
        self.assertEqual(audit['status'],'invalid')
        self.assertNotIn('presentation_repair',audit)

    def test_number_only_formats_are_not_silently_changed(self):
        for directive in ['Reply with only a number.','Return just one integer.','Use number-only output.']:
            _,audit=self.inspect(answer='6',question=self.question+' '+directive)
            self.assertEqual(audit['status'],'invalid')
            self.assertNotIn('presentation_repair',audit)

    def test_rounded_month_qualification_uses_checked_calendar_value(self):
        evidence={'E1':'User: I started on February 1, 2025.','E2':'User: I finished on February 25, 2025.'}
        spec=dict(operation='date_difference',unit='months',rounding='nearest',result=1,
            start=dict(source='E1',quote=evidence['E1'],date_text='February 1, 2025'),
            end=dict(source='E2',quote=evidence['E2'],date_text='February 25, 2025'))
        stripped,audit=self.inspect(spec,answer='1 month.',evidence=evidence,
            question='How many months passed between the start and finish?')
        self.assertEqual(audit['status'],'checks_passed')
        self.assertIn('ANSWER: About 1 month.',stripped)
        self.assertEqual(audit['computed'],'1')

    def test_bounded_result_is_never_presented_as_an_exact_event_date(self):
        evidence={'E1':'[E1] 2025-04-02\nUser: I returned the rented lantern.',
                  'E2':'[E2] 2025-04-23\nUser: I collected the telescope today.'}
        spec=dict(operation='date_difference',unit='days',bound='at_least',result=21,
            start=dict(source='E1',full_source=True,event_text='I returned the rented lantern',completed_by_recorded_date=True),
            end=dict(source='E2',full_source=True,event_text='I collected the telescope',relative_text='today',anchor='recorded_at'))
        stripped,audit=self.inspect(spec,answer='21 days.',evidence=evidence,
            question='How many days passed, at minimum, between returning the lantern and collecting the telescope?')
        self.assertEqual(audit['status'],'checks_passed')
        self.assertIn('At least 21 days; the exact interval was not recorded.',stripped)
        self.assertEqual(audit['bound'],'at_least')

    def test_semantic_review_remains_required_after_formatting(self):
        observed=[]
        def review(messages):
            observed.append(messages[-1]['content'])
            return json.dumps(dict(responsive=True,supported=True))
        model=ScriptedChat({'answer':response(self.spec,'6 years.'),'answer_review':review})
        _,answer,audit=answer_with_validation(model,[],evidence=self.evidence,question=self.question,
            today='2029-04-01',semantic_enabled=True,operation='date_difference')
        self.assertEqual(answer,'About 6 years.')
        self.assertEqual(audit['status'],'checks_passed')
        self.assertEqual(model.purposes(),['answer','answer_review'])
        self.assertIn('Proposed answer:\nAbout 6 years.',observed[0])
        rejecting=ScriptedChat({'answer':response(self.spec,'6 years.'),'answer_review':json.dumps(
            dict(responsive=False,supported=False,reason='The selected events are unrelated to the question.'))})
        _,answer,audit=answer_with_validation(rejecting,[],evidence=self.evidence,question=self.question,
            today='2029-04-01',semantic_enabled=True,operation='date_difference')
        self.assertEqual(audit['status'],'incomplete')
        self.assertNotIn('About 6',answer)


if __name__=='__main__':unittest.main()
