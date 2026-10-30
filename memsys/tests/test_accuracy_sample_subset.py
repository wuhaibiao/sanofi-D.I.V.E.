"""Prevent fixed-store subset controls from reusing changed dialogue inputs."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'cached_subset', Path(__file__).parents[1] / 'scripts/run_cached_read_iteration.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class SampleSubsetTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{'question_id': 'probe-%d' % index, 'question': 'Recall the recorded item.',
                      'question_date': '2025-04-30', 'haystack_sessions': [[
                          {'role': 'user', 'content': 'My workshop material is clay.'}]]}
                     for index in range(100)]

    def test_smaller_control_keeps_exact_original_inputs(self):
        subset = copy.deepcopy(self.rows[20:60])
        self.assertEqual(len(subset), 40)
        self.assertEqual(len(runner.validate_seed_records(subset, self.rows)), 100)

    def test_same_id_does_not_allow_different_history_or_date(self):
        for field, value in [('question_date', '2025-05-01'),
                             ('haystack_sessions', [[{'role': 'user', 'content': 'Changed source.'}]])]:
            subset = copy.deepcopy(self.rows[20:60])
            subset[0][field] = value
            with self.assertRaises(SystemExit):
                runner.validate_seed_records(subset, self.rows)

    def test_duplicate_or_unknown_id_is_rejected(self):
        for subset in [[self.rows[0], self.rows[0]], [dict(self.rows[0], question_id='unrecorded')]]:
            with self.assertRaises(SystemExit):
                runner.validate_seed_records(subset, self.rows)


if __name__ == '__main__':
    unittest.main()
