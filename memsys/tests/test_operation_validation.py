"""General cardinality grammar must not confuse scoped events with durations."""
import json
import unittest

from helpers import dt, models, settings
from memsys.operation_validation import entity_cardinality, validate_operation
from memsys.queryops import operation_for
from memsys.read import plan_query


class OperationValidationTest(unittest.TestCase):
    def test_time_scope_does_not_change_the_object_being_counted(self):
        for question in (
            'How many amber scanners did we inspect in the past month?',
            'How many pottery workshops have I attended this year?',
            'How many paintings from last year did we exhibit?',
            'How many device repairs were completed during the last two weeks?',
        ):
            with self.subTest(question=question):
                self.assertTrue(entity_cardinality(question))
                self.assertEqual(operation_for(question),'count_entities')

    def test_elapsed_units_and_currency_are_not_object_cardinality(self):
        for question in (
            'How many days have passed since the amber scanner was repaired?',
            'How many complete weeks has the inspection taken?',
            'How many calendar years were between the two inspections?',
            'How many dollars did we spend on the repair?',
            'How many of my devices are available?',
            'Explain this sentence: how many amber scanners did we inspect in the past month?',
        ):
            with self.subTest(question=question):
                self.assertFalse(entity_cardinality(question))
        self.assertEqual(operation_for('How many days have passed since the repair?'),'date_difference')

    def test_progress_and_computed_operations_keep_their_existing_priority(self):
        self.assertEqual(operation_for('How many amber scanners have we inspected so far?'),'latest_state')
        self.assertEqual(operation_for('How many more amber scanners did we inspect than before?'),'difference')
        for declared in ('sum_events','difference','ratio','count_entities'):
            selected, trace = validate_operation('How many pottery workshops have I attended this year?',
                                                  declared,'count_entities')
            self.assertEqual(selected,declared)
            self.assertEqual(trace,{})

    def test_planner_state_error_corrected_without_altering_queries_or_source_clock(self):
        question='How many amber scanners did we inspect in the past month?'
        payload=dict(queries=['amber scanner inspections'],keywords=['scanner'],operation='latest_state',
            needs_all=False,focus='past',time_range={'start':'2025-04-01','end':'2025-04-30'})
        model=models(memory={'query':json.dumps(payload)})
        plan=plan_query(model,question,dt('2025-05-01T10:00'),
                        settings(query_policy='always',semantic_validation=True))
        self.assertEqual(plan.operation,'count_entities')
        self.assertTrue(plan.needs_all)
        self.assertEqual(plan.queries,[question,'amber scanner inspections'])
        self.assertEqual(plan.time_range.start.isoformat(),'2025-04-01')
        self.assertEqual(plan.operation_validation['declared'],'latest_state')
        self.assertEqual(plan.to_dict()['operation_validation']['selected'],'count_entities')
        self.assertEqual(len(model.memory.calls),1)

    def test_normalizer_leaves_ambiguous_operations_and_legacy_mode_alone(self):
        question='How many amber scanners did we inspect in the past month?'
        model=models(memory={'query':json.dumps({'operation':'latest_state'})})
        plan=plan_query(model,question,dt('2025-05-01T10:00'),
                        settings(query_policy='always',semantic_validation=False))
        self.assertEqual(plan.operation,'latest_state')
        self.assertEqual(plan.operation_validation,{})
        selected, trace=validate_operation('How long was the repair?','lookup','lookup')
        self.assertEqual((selected,trace),('lookup',{}))


if __name__=='__main__':
    unittest.main()
