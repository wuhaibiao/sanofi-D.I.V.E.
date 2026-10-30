from __future__ import annotations

import unittest
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from memsys.bench.behavior import CASES, run_behavior
from memsys.fake import ScriptedChat
from memsys.llm import ModelError


class BehaviorTest(unittest.TestCase):
    def test_write_coverage_command_validates_input_without_a_provider(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as out:
            result = subprocess.run([sys.executable, str(root / "scripts/check_write_coverage.py"),
                                     "--data", str(root / "tests/fixtures/mini_oracle.json"), "--out", out],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((Path(out) / "summary.json").read_text())
            self.assertEqual((report["questions"], report["lossless_errors"], report["input_budget_errors"]), (4, 0, 0))
            self.assertTrue(report["embedding_lossless_checked"])
            self.assertEqual((report["embedding_lossless_errors"], report["embedding_budget_errors"]), (0, 0))
            result = subprocess.run([sys.executable, str(root / "scripts/check_write_coverage.py"),
                                     "--data", str(root / "tests/fixtures/mini_oracle.json"), "--prefix", "--out", out],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((Path(out) / "summary.json").read_text())
            self.assertFalse(report["lossless_checked"])
            self.assertIsNone(report["lossless_errors"])

    def test_requested_outputs_are_checked_and_expected_metadata_is_not_sent(self):
        replies = ["NOTES: verified.\nANSWER: " + c.expected for c in CASES]
        for index, case in enumerate(CASES):
            if case.name == 'booking_relative_to_today':
                quote = 'I reserved my cabin on November 10, 2024 for a trip on March 10, 2025.'
                spec = dict(operation='date_difference', unit='months',
                    start=dict(source='E1', quote=quote, date_text='November 10, 2024', date='2024-11-10'),
                    end=dict(source='TODAY', date=case.today), result='3')
                replies[index] = 'NOTES: quoted dates.\nCALCULATION: ' + json.dumps(spec) + '\nANSWER: 3'
        model = ScriptedChat({"answer": replies})
        report = run_behavior(model)
        self.assertEqual((report["status"], report["passed"], report["failed"], report["unstarted"]),
                         ("complete", len(CASES), 0, 0))
        for case, (_, messages) in zip(CASES, model.calls):
            self.assertIn(case.memory, messages[-1]["content"])
            self.assertIn(case.question, messages[-1]["content"])
            self.assertNotIn('"expected"', messages[-1]["content"])
        self.assertIn("not a benchmark score", report["mode"])

    def test_account_error_stops_without_scoring_unstarted_cases(self):
        def blocked(_messages):
            raise ModelError("HTTP 402: Insufficient Balance", status=402)
        model = ScriptedChat({"answer": blocked})
        report = run_behavior(model)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual((report["passed"], report["failed"], report["unstarted"]), (0, 1, len(CASES) - 1))
        self.assertEqual(len(model.calls), 1)
        self.assertEqual((report["answered"], report["incorrect"], report["errors"]), (0, 0, 1))
        self.assertIsNone(report["results"][0]["passed"])

    def test_rejected_answer_does_not_stop_other_cases(self):
        model = ScriptedChat({"answer": ["ANSWER: 999", "ANSWER: " + CASES[1].expected]})
        report = run_behavior(model, CASES[:2])
        self.assertEqual((report["passed"], report["failed"], report["unstarted"]), (1, 1, 0))


if __name__ == "__main__":
    unittest.main()
