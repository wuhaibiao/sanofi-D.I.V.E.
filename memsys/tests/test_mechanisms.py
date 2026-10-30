from __future__ import annotations

import tempfile
import unittest
import json
import subprocess
import sys
from pathlib import Path

from helpers import fact, facts_json, models, settings
from memsys.bench.mechanisms import MechanismCase, run_mechanisms, session
from memsys.fake import ScriptedChat
from memsys.llm import ModelError


class MechanismRegressionTest(unittest.TestCase):
    def test_pressure_command_checks_capacity_archive_recall_and_budget(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as out:
            completed = subprocess.run([sys.executable, str(root / "scripts/check_lifecycle_pressure.py"),
                                        "--sizes", "100", "--out", out],
                                       capture_output=True, text=True, timeout=15)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads((Path(out) / "report.json").read_text())
            self.assertTrue(report["passed"])
            self.assertEqual(len(report["rows"][0]["checks"]), 6)

    def test_raw_sessions_are_ingested_and_expected_answer_never_enters_model_requests(self):
        case = MechanismCase("color", (session("2025-04-01", "I own a teal kayak."),),
                             "What color is my kayak?", "private verification label")
        model_set = models(memory={"extract": facts_json(fact("The user owns a teal kayak.", [1], "kayak"))},
                           chat={"answer": "ANSWER: teal"})
        with tempfile.TemporaryDirectory() as out:
            report = run_mechanisms(model_set, settings(query_analysis=False), out, [case])
        self.assertEqual((report["answered"], report["incorrect"], report["errors"]), (1, 1, 0))
        row = report["results"][0]
        self.assertEqual(row["write_stats"]["counts"]["fact"], {"current": 1})
        self.assertIn("teal", row["trace"]["memory"])
        for _, messages in model_set.memory.calls + model_set.chat.calls:
            self.assertNotIn(case.expected, str(messages))

    def test_account_failure_records_write_stage_and_leaves_rest_unstarted(self):
        def blocked(_messages):
            raise ModelError("HTTP 402: Insufficient Balance", status=402)
        model_set = models(memory={"extract": blocked})
        case = MechanismCase("color", (session("2025-04-01", "I own a teal kayak."),), "Color?", "teal")
        with tempfile.TemporaryDirectory() as out:
            report = run_mechanisms(model_set, settings(), out, [case, case])
        self.assertEqual((report["status"], report["answered"], report["errors"], report["unstarted"]),
                         ("blocked", 0, 1, 1))
        self.assertEqual(report["results"][0]["stage"], "write")
        self.assertIsNone(report["results"][0]["passed"])

    def test_incomplete_extraction_is_a_failure_even_when_answer_matches(self):
        case = MechanismCase("color", (session("2025-04-01", "I own a teal kayak."),), "Color?", "teal")
        model_set = models(memory={"extract": "invalid"}, chat={"answer": "ANSWER: teal"})
        with tempfile.TemporaryDirectory() as out:
            report = run_mechanisms(model_set, settings(query_analysis=False), out, [case])
        self.assertEqual(report["incorrect"], 1)
        self.assertEqual(report["results"][0]["pending_extraction"], 1)


if __name__ == "__main__":
    unittest.main()
