from __future__ import annotations

import copy
import tempfile
import unittest
from unittest.mock import patch

from memsys.bench.provenance import model_details
from memsys.bench.runner import ConfigChanged, Runner, config_diff
from memsys.cli import _fake_models
from memsys.config import MemorySettings
from memsys.llm import ModelConfig


class ProvenanceTest(unittest.TestCase):
    def test_details_exclude_credentials_and_preserve_generation_settings(self):
        models = _fake_models()
        models.memory.config = ModelConfig(model="test", base_url="https://user:password@example.test/v1?token=private",
            api_key_env="KEY", extra_body={"seed": 42, "api_key": "hidden"})
        details = model_details(models.memory)
        self.assertEqual(details["base_url"], "https://example.test/v1")
        self.assertNotIn("password", str(details))
        self.assertNotIn("private", str(details))
        self.assertNotIn("hidden", str(details))
        self.assertIn("extra_body_sha256", details)
        self.assertEqual(details["generation_options"], {"seed": 42})

    def test_prompt_and_source_changes_prevent_mixed_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(_fake_models(), MemorySettings(), tmp)
            runner._write_config(1, False)
            with patch("memsys.prompts.ANSWER_RULES", "different self-authored rules"):
                with self.assertRaises(ConfigChanged):
                    Runner(_fake_models(), MemorySettings(), tmp)._write_config(1, True)
            changed = Runner(_fake_models(), MemorySettings(), tmp)
            changed.provenance["source_sha256"] = "changed"
            with self.assertRaises(ConfigChanged):
                changed._write_config(1, True)

    def test_price_and_commit_only_changes_do_not_invalidate_answers(self):
        old = {"model_settings": {"chat": {"model": "m", "pricing": {"input": 1}}},
               "provenance": {"git_commit": "old", "source_sha256": "same"}}
        new = copy.deepcopy(old)
        new["model_settings"]["chat"]["pricing"]["input"] = 2
        new["provenance"]["git_commit"] = "new"
        self.assertEqual(config_diff(old, new), [])
        new["model_settings"]["chat"]["model"] = "another"
        self.assertTrue(config_diff(old, new))

    def test_host_answer_rules_are_included_in_prompt_fingerprint(self):
        from memsys.bench.provenance import prompt_fingerprint
        before=prompt_fingerprint()
        with patch('memsys.webchat.SYSTEM','Changed direct-answer instructions.'):
            self.assertNotEqual(prompt_fingerprint(),before)

    def test_legacy_run_without_source_metadata_requires_separate_output(self):
        changes = config_diff({"mode": "memory"}, {"mode": "memory", "provenance": {"source_sha256": "new"}})
        self.assertIn("previous run lacks reproducibility metadata", changes[0])
