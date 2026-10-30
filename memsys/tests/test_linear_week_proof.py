"""Equivalent week/day presentation never excuses bad dates or arithmetic."""
import json
import unittest

from memsys.aggregation import _inspect_read_answer


class LinearWeekProofTest(unittest.TestCase):
    def setUp(self):
        self.evidence={'E1':'User: I calibrated the spectrometer on May 4, 2025.',
                       'E2':'User: I inspected the dome on May 21, 2025.'}
        self.spec=dict(operation='date_difference',unit='days',result=17,
            start=dict(source='E1',quote=self.evidence['E1'],date_text='May 4, 2025'),
            end=dict(source='E2',quote=self.evidence['E2'],date_text='May 21, 2025'))
        self.question='How many weeks passed between the spectrometer calibration and the dome inspection?'

    def inspect(self,answer,spec=None,question=None):
        output='CALCULATION: '+json.dumps(spec or self.spec)+'\nANSWER: '+answer
        return _inspect_read_answer(output,self.evidence,today='2025-05-30',
                                    question=question or self.question)

    def test_days_proof_supports_exact_weeks_and_remainder_without_rewriting_proof(self):
        for answer in ('2 weeks and 3 days.','two weeks and three days.'):
            _,audit=self.inspect(answer)
            self.assertEqual(audit['status'],'checks_passed')
            self.assertEqual(audit['calculation'],self.spec)
            self.assertEqual((audit['computed'],audit['complete_units'],audit['remainder_days']),('17',2,3))
            self.assertEqual(audit['requested_unit'],'weeks')

    def test_bare_converted_weeks_receive_complete_unit_qualification(self):
        stripped,audit=self.inspect('2 weeks.')
        self.assertEqual(audit['status'],'checks_passed')
        self.assertIn('ANSWER: 2 complete weeks.',stripped)
        self.assertEqual(audit['calculation']['result'],17)

    def test_bad_result_bad_dates_and_wrong_remainder_remain_invalid(self):
        for spec,answer in ((dict(self.spec,result=18),'2 weeks and 3 days.'),
            (dict(self.spec,start=dict(self.spec['start'],date_text='May 3, 2025')),'2 weeks and 3 days.'),
            (self.spec,'2 weeks and 4 days.'),(self.spec,'3 weeks and 3 days.'),
            (self.spec,'2 weeks and 10 days.'),(self.spec,'17 days.')):
            _,audit=self.inspect(answer,spec)
            self.assertEqual(audit['status'],'invalid')

    def test_original_week_proof_checks_remainder_too(self):
        spec=dict(self.spec,unit='weeks',result=2)
        self.assertEqual(self.inspect('2 weeks and 3 days.',spec)[1]['status'],'checks_passed')
        self.assertEqual(self.inspect('2 weeks and 4 days.',spec)[1]['status'],'invalid')

    def test_calendar_months_years_and_nearest_days_cannot_use_linear_conversion(self):
        for spec,question in ((self.spec,self.question.replace('weeks','months')),
                             (dict(self.spec,rounding='nearest'),self.question),
                             (dict(self.spec,precision='year'),self.question)):
            self.assertEqual(self.inspect('2',spec,question)[1]['status'],'invalid')

    def test_signed_compound_interval_preserves_direction(self):
        spec=dict(self.spec,start=self.spec['end'],end=self.spec['start'],result=-17)
        self.assertEqual(self.inspect('-2 weeks and -3 days.',spec)[1]['status'],'checks_passed')
        self.assertEqual(self.inspect('-2 weeks and 3 days.',spec)[1]['status'],'invalid')

    def test_whole_day_equivalence_is_not_mistaken_for_remainder(self):
        for answer in ('2 complete weeks (17 days).','2 weeks (17 days, or 2 complete weeks).',
                       '2 weeks and 3 days (17 days in total).','2 weeks, equivalent to 17 days.',
                       '2 个完整周（共 17 天）。'):
            with self.subTest(answer=answer):
                self.assertEqual(self.inspect(answer)[1]['status'],'checks_passed')
        for answer in ('2 weeks and 17 days.','2 complete weeks (18 days).',
                       '2 weeks (17 days, or 3 complete weeks).','2 complete weeks (3 days).'):
            with self.subTest(answer=answer):
                self.assertEqual(self.inspect(answer)[1]['status'],'invalid')


if __name__=='__main__':
    unittest.main()
