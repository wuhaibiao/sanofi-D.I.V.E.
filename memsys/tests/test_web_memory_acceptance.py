"""Acceptance grading/freeze checks; no network or real model calls."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("web_acceptance", ROOT / "scripts/check_webchat_reliability.py")
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


class AcceptanceTests(unittest.TestCase):
    def test_old_state_and_ambiguous_numbers_are_not_counted_as_correct(self):
        rubric = {"contains": ["新城"], "forbidden": ["旧城"]}
        self.assertFalse(acceptance.evaluate("旧城", rubric)["passed"])
        self.assertFalse(acceptance.evaluate("新城或旧城", rubric)["passed"])
        self.assertTrue(acceptance.evaluate("新城", rubric)["passed"])
        self.assertTrue(acceptance.evaluate("54页", {"number": 54})["passed"])
        self.assertFalse(acceptance.evaluate("100减28，剩72页", {"number": 54})["passed"])

    def test_unknown_cannot_hide_an_invented_exact_date(self):
        rubric = {"unknown": True, "forbidden_regex": [r"[0-9]+\s*[日号]"]}
        self.assertTrue(acceptance.evaluate("不知道具体哪天。", rubric)["passed"])
        self.assertFalse(acceptance.evaluate("不知道，不过我猜是6月15日。", rubric)["passed"])
        self.assertFalse(acceptance.evaluate("6月15日。", rubric)["passed"])

    def test_config_fixture_runner_and_ui_are_frozen_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text("{}")
            args = SimpleNamespace(config=config, suite=ROOT / "tests/fixtures/web_memory_reliability.json")
            first = acceptance.inputs(args)
            self.assertIn(str(args.suite.resolve()), first)
            self.assertIn(str(ROOT / "src/memsys/web/chat.js"), first)
            config.write_text('{"memory": {"context_budget_tokens": 1000}}')
            self.assertNotEqual(first, acceptance.inputs(args))

    def test_fixtures_have_explicit_expectations_separate_from_source_turns(self):
        fixture = json.loads((ROOT / "tests/fixtures/web_memory_reliability.json").read_text())
        for case in fixture["cases"]:
            for session in case["sessions"]:
                self.assertTrue(all(set(t) == {"role", "content"} for t in session["turns"]))
            for query in case["queries"]:
                self.assertTrue(set(query) & {"contains", "number", "unknown"})


if __name__ == "__main__":
    unittest.main()
