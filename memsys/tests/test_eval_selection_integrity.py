"""Self-authored metadata verifies configurable counts and exposed-run labeling."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_relational_memory_eval as evaluation


class SelectionIntegrityTests(unittest.TestCase):
    def test_exposed_questions_require_explicit_regression_and_record_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            counts = {category: 1 for category in evaluation.COUNTS}
            sample = [dict(question_id='authored_' + category, question_type=category,
                question='What color is the fixture?', question_date='2026-01-03', answer='blue',
                haystack_dates=['2026-01-02'], haystack_session_ids=['authored_source_' + category],
                haystack_sessions=[[dict(role='user', content='The fixture is blue.')]]) for category in counts]
            data = root / 'authored.json'
            data.write_text(json.dumps(sample))
            ids = {row['question_id'] for row in sample}
            args = SimpleNamespace(out=root / 'selected', data=data, counts=counts,
                seed='self-authored-selection', allow_exposed=False)
            with patch.object(evaluation, 'exclusions', return_value=(ids, [])):
                with self.assertRaisesRegex(ValueError, 'Not enough unused'):
                    evaluation.select(args)
                self.assertFalse(args.out.exists())
                args.allow_exposed = True
                evaluation.select(args)
            frozen = evaluation.read(args.out / 'selection-freeze.json')
            self.assertEqual(frozen['counts'], counts)
            self.assertEqual(frozen['selected_previous_overlap'], len(sample))
            self.assertTrue(frozen['development_regression'])
            self.assertIn('Development regression', frozen['exposure_limit'])
            self.assertEqual(len(evaluation.read(args.out / 'read-inputs.json')), len(sample))

    def test_nonpositive_count_rejected_before_reading_data(self):
        with tempfile.TemporaryDirectory() as directory:
            counts = dict(evaluation.COUNTS)
            counts[next(iter(counts))] = 0
            with self.assertRaisesRegex(ValueError, 'positive integer'):
                evaluation.select(SimpleNamespace(out=Path(directory) / 'new', counts=counts))


if __name__ == '__main__':
    unittest.main()
