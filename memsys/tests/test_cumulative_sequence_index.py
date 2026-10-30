import json
import unittest

from memsys.aggregation import answer_with_validation, inspect_calculation
from memsys.fake import ScriptedChat


class CumulativeSequenceIndexTest(unittest.TestCase):
    def setUp(self):
        self.evidence = {'E1': 'User: I read around 4 chapters of the Harbor Atlas.',
                         'E2': 'User: I just finished chapter 8 of the Woodland Journal.'}
        self.items = [dict(source='E1', quote=self.evidence['E1'], value=4, unit='chapters'),
                      dict(source='E2', quote=self.evidence['E2'], value=8, unit='chapters',
                           ordinal_progress=True, value_type='cumulative')]
        self.question = 'What is the total number of chapters I read from the Harbor Atlas and the Woodland Journal?'

    def response(self, items=None, operation='sum', result=12):
        return 'CALCULATION: ' + json.dumps(dict(operation=operation, unit='chapters',
            items=items or self.items, result=result)) + '\nANSWER: About '+str(result)+' chapters.'

    def inspect(self, items=None, operation='sum', result=12):
        return inspect_calculation(self.response(items, operation, result), self.evidence,
                                   question=self.question)[1]

    def test_typed_completed_index_supports_literal_sum_and_group_count(self):
        self.assertEqual(self.inspect()['status'], 'checks_passed')
        groups = [dict(self.items[0], quantity_label='chapters'),
                  dict(self.items[1], quantity_label='chapter')]
        audit = self.inspect(groups, 'count_groups')
        self.assertEqual(audit['status'], 'checks_passed')
        self.assertEqual(audit['computed'], '12')

    def test_cardinal_word_index_retains_the_original_value(self):
        self.evidence['E2'] = self.evidence['E2'].replace('chapter 8', 'chapter eight')
        items = [self.items[0], dict(self.items[1], quote=self.evidence['E2'])]
        self.assertEqual(self.inspect(items)['status'], 'checks_passed')

    def test_progress_flag_and_cumulative_claim_are_required_for_indexed_group(self):
        for change in [dict(ordinal_progress=False), dict(value_type='independent'),
                       dict(ordinal_progress=None)]:
            groups = [dict(self.items[0], quantity_label='chapters'),
                      dict(self.items[1], quantity_label='chapter', **change)]
            self.assertEqual(self.inspect(groups, 'count_groups')['status'], 'invalid')
        groups = [dict(self.items[0], quantity_label='chapters'),
                  {k:v for k,v in dict(self.items[1], quantity_label='chapter').items()
                   if k != 'ordinal_progress'}]
        self.assertEqual(self.inspect(groups, 'count_groups')['status'], 'invalid')

    def test_fraction_substring_or_wrong_quantity_cannot_match_the_index(self):
        for literal in ['chapter 8.5', 'chapter 80', 'chapter 8a', 'otherchapter 8']:
            self.evidence['E2'] = 'User: I just finished '+literal+' of the Woodland Journal.'
            items = [self.items[0], dict(self.items[1], quote=self.evidence['E2'])]
            self.assertEqual(self.inspect(items)['status'], 'invalid', literal)

    def test_indexed_measurement_cannot_bypass_unit_validation(self):
        for unit in ['months', 'usd']:
            evidence = {'E1': 'User: I reached '+unit+' 8.'}
            item = dict(source='E1', quote=evidence['E1'], value=8, unit=unit,
                        ordinal_progress=True, value_type='cumulative')
            output = 'CALCULATION: '+json.dumps(dict(operation='sum', unit=unit,
                items=[item], result=8))+'\nANSWER: 8'
            self.assertEqual(inspect_calculation(output, evidence)[1]['status'], 'invalid')

    def test_rank_or_plan_still_fails_semantic_review(self):
        self.evidence['E2'] = 'User: My favorite part is chapter 8 of the Woodland Journal.'
        items = [self.items[0], dict(self.items[1], quote=self.evidence['E2'])]
        model = ScriptedChat({'answer': self.response(items), 'answer_review': json.dumps(dict(
            responsive=False, supported=False, reason='A preferred chapter does not establish completed cumulative reading.'))})
        _, answer, audit = answer_with_validation(model, [], evidence=self.evidence,
            question=self.question, operation='sum_events', semantic_enabled=True)
        self.assertEqual(audit['status'], 'incomplete')
        self.assertNotIn('12 chapters', answer)


if __name__ == '__main__':
    unittest.main()
