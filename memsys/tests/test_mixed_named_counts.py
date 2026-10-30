import json
import unittest

from memsys.aggregation import answer_with_validation, inspect_calculation
from memsys.fake import ScriptedChat


class MixedNamedCountsTest(unittest.TestCase):
    def setUp(self):
        self.evidence = {'E1': 'User: I have 6 doves in my aviary.',
                         'E2': 'User: My parrot Rowan lives in the other aviary.'}
        self.items = [dict(source='E1', quote=self.evidence['E1'], quantity_label='doves', value=6, unit='birds'),
                      dict(source='E2', quote=self.evidence['E2'], entity='Rowan', value=1, unit='birds')]

    def output(self, items=None, result=7, unit='birds'):
        return 'CALCULATION: '+json.dumps(dict(operation='count_groups', unit=unit,
            items=items or self.items, result=result))+'\nANSWER: '+str(result)+' '+unit

    def test_group_sizes_and_named_singleton_can_contribute_together(self):
        audit = inspect_calculation(self.output(), self.evidence)[1]
        self.assertEqual((audit['status'], audit['computed']), ('checks_passed', '7'))

    def test_named_entity_needs_one_literal_whole_name_without_a_group_claim(self):
        for change in [dict(value=2), dict(entity='Row'), dict(entity='Birch'),
                       dict(entity=True), dict(quantity_label='parrot'), dict(ordinal_progress=True)]:
            audit = inspect_calculation(self.output([self.items[0], dict(self.items[1], **change)]), self.evidence)[1]
            self.assertEqual(audit['status'], 'invalid', change)

    def test_repeated_named_identity_is_counted_once_across_quotes(self):
        self.evidence['E3'] = 'User: Rowan is still my parrot.'
        repeated = dict(self.items[1], source='E3', quote=self.evidence['E3'])
        audit = inspect_calculation(self.output(self.items+[repeated]), self.evidence)[1]
        self.assertEqual((audit['status'], audit['computed']), ('checks_passed', '7'))

    def test_two_named_individuals_may_share_a_source_excerpt(self):
        self.evidence['E2'] = 'User: My parrots Rowan and Birch live together.'
        first = dict(self.items[1], quote=self.evidence['E2'])
        second = dict(first, entity='Birch')
        audit = inspect_calculation(self.output([self.items[0], first, second], result=8), self.evidence)[1]
        self.assertEqual((audit['status'], audit['computed']), ('checks_passed', '8'))

    def test_singleton_cannot_supply_money_time_or_a_mixed_unit(self):
        for unit in ['days', 'USD', 'hours']:
            items = [dict(item, unit=unit) for item in self.items]
            self.assertEqual(inspect_calculation(self.output(items, unit=unit), self.evidence)[1]['status'], 'invalid')
        items = [self.items[0], dict(self.items[1], unit='parrots')]
        self.assertEqual(inspect_calculation(self.output(items), self.evidence)[1]['status'], 'invalid')

    def test_category_or_ownership_still_needs_semantic_support(self):
        model = ScriptedChat({'answer': self.output(), 'answer_review': json.dumps(dict(
            responsive=True, supported=False, reason='The named bird belongs to someone else.'))})
        _, answer, audit = answer_with_validation(model, [], evidence=self.evidence,
            question='How many birds do I own in total?', operation='sum_events', semantic_enabled=True)
        self.assertEqual(audit['status'], 'incomplete')
        self.assertNotEqual(answer, '7 birds')


if __name__ == '__main__':
    unittest.main()
