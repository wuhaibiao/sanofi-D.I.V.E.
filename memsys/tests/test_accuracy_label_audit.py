import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    'accuracy_analysis', Path(__file__).parents[1] / 'scripts/analyze_accuracy_iteration.py')
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


class AccuracyLabelAuditTest(unittest.TestCase):
    def row(self, reference, answer, correct=True):
        return dict(question_id='audit-example', question='Recall the total.',
                    reference=reference, answer=answer, correct=correct)

    def test_conflicting_boolean_label_is_flagged_without_rewriting_it(self):
        row = self.row('18 minutes', '17 minutes')
        self.assertEqual(len(analysis.numeric_label_anomalies([row])), 1)
        self.assertTrue(row['correct'])
        self.assertEqual(len(analysis.numeric_label_anomalies([self.row('18', '17.')])), 1)

    def test_grouped_numeric_literals_match_and_explicit_alternatives_are_preserved(self):
        rows = [self.row('2,400 people', '2400 people'),
                self.row('18 minutes', '18 minutes'),
                self.row('18 or 19 minutes are acceptable.', '19 minutes'),
                self.row('18 minutes', '17 minutes', correct=False)]
        self.assertEqual(analysis.numeric_label_anomalies(rows), [])


if __name__ == '__main__':
    unittest.main()
