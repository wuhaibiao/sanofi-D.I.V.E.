"""失败恢复、信息遗忘与生命周期的端到端回归，全部使用自拟数据。"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from helpers import dt, fact, facts_json, models, settings
from memsys.core import MemorySystem
from memsys.fake import ScriptedChat
from memsys.llm import ModelError
from memsys.config import MemorySettings
from memsys.lifecycle import run_lifecycle
from memsys.read import QueryPlan, Retriever
from memsys.store import Store
from memsys.schema import ARCHIVED, EPISODE_WITHHELD, FACT_CURRENT, FORGOTTEN
from memsys.write import Writer

AT = dt("2025-05-01T10:00")


def save(system, text, close=True):
    handle = system.open_session("u", AT)
    system.add_turn(handle, "user", text, AT)
    report, _ = system.close_session(handle, AT) if close else (
        Writer(system.store("u"), system.models, system.settings).consolidate(handle.session_id), None)
    return handle, report


class ExtractionRecoveryTest(unittest.TestCase):
    def test_closed_failure_survives_restart_and_recovery_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            system = MemorySystem(models(memory={"extract": "invalid JSON"}), settings(), data_dir=directory)
            _, report = save(system, "I collect amber fossils.")
            self.assertEqual(report.retry_rounds, [1])
            self.assertEqual(system.stats("u")["backlog"], 1)
            self.assertEqual(system.store("u").open_sessions(), [])
            system.close()
            system = MemorySystem(models(memory={"extract": facts_json(
                fact("The user collects amber fossils.", [1], "hobby"))}), settings(), data_dir=directory)
            self.addCleanup(system.close)
            self.assertEqual(len(system.recover("u")), 1)
            self.assertEqual(system.stats("u")["backlog"], 0)
            self.assertEqual(len(system.facts("u")), 1)
            self.assertEqual(system.recover("u"), [])
            self.assertEqual(system.rebuild("u"), [])
            self.assertEqual(system.facts("u")[0].mentions, 1)

    def test_invalid_schema_and_repeated_failure_keep_queue_but_valid_empty_completes(self):
        for output in ('{}', '{"facts": "wrong type"}', '{"facts": [null]}',
                       '{"facts": [{"text": ""}]}', '"wrong root"', 'invalid'):
            with self.subTest(output=output):
                system = MemorySystem(models(memory={"extract": output}), settings())
                try:
                    save(system, "I collect amber fossils.")
                    system.rebuild("u")
                    self.assertEqual(system.stats("u")["backlog"], 1)
                    self.assertEqual(system.facts("u"), [])
                    system.models = models(memory={"extract": facts_json()})
                    system.rebuild("u")
                    self.assertEqual(system.stats("u")["backlog"], 0)
                finally:
                    system.close()

    def test_mixed_valid_and_invalid_entries_retry_the_entire_round(self):
        valid = fact("The user collects amber fossils.", [1], "hobby")
        for invalid in (None, "malformed", {"text": ["not a string"], "rounds": [1]}):
            with self.subTest(invalid=invalid):
                system = MemorySystem(models(memory={"extract": facts_json(valid, invalid)}), settings())
                try:
                    _, report = save(system, "I collect amber fossils and build clay sculptures.")
                    self.assertEqual(report.retry_rounds, [1])
                    self.assertEqual(system.facts("u"), [])
                    system.models = models(memory={"extract": facts_json(valid)})
                    system.rebuild("u")
                    self.assertEqual(len(system.facts("u")), 1)
                    self.assertEqual(system.stats("u")["backlog"], 0)
                finally:
                    system.close()

    def test_one_failed_part_does_not_commit_a_partial_round(self):
        def extract(messages):
            if "the final sentence" in messages[-1]["content"]:
                return "invalid"
            return facts_json(fact("The assistant suggested a compass.", [1], "equipment", kind="assistant"))
        system = MemorySystem(models(memory={"extract": extract}), settings(extract_chunk_tokens=90))
        self.addCleanup(system.close)
        handle = system.open_session("u", AT)
        system.add_turn(handle, "user", "Please prepare my equipment list.", AT)
        system.add_turn(handle, "assistant", "A compass helps navigation. " * 100 + "the final sentence", AT)
        report, _ = system.close_session(handle, AT)
        self.assertGreater(report.extract_parts, 1)
        self.assertEqual(system.facts("u"), [])
        self.assertEqual(system.stats("u")["backlog"], 1)
        system.models = models(memory={"extract": facts_json(fact(
            "The assistant suggested a compass.", [1], "equipment", kind="assistant"))})
        system.rebuild("u")
        self.assertEqual(len(system.facts("u")), 1)
        self.assertEqual(system.stats("u")["backlog"], 0)

    def test_retry_error_rolls_back_and_preserves_backlog(self):
        system = MemorySystem(models(memory={"extract": "invalid"}), settings())
        self.addCleanup(system.close)
        save(system, "I collect amber fossils.")
        def unavailable(_messages):
            raise ModelError("service unavailable")
        system.models = models(memory={"extract": unavailable})
        with self.assertRaises(ModelError):
            system.rebuild("u")
        self.assertEqual(system.stats("u")["backlog"], 1)
        self.assertEqual(system.facts("u"), [])

    def test_successful_round_completes_while_failed_round_stays_pending(self):
        system = MemorySystem(models(memory={"extract": "invalid"}), settings())
        self.addCleanup(system.close)
        handle = system.open_session("u", AT)
        for role, text in [("user", "I collect amber fossils."), ("assistant", "Okay."),
                           ("user", "I also build clay sculptures.")]:
            system.add_turn(handle, role, text, AT)
        system.close_session(handle, AT)
        def extract(messages):
            text = messages[-1]["content"]
            return "invalid" if "sculptures" in text else facts_json(fact("The user collects amber fossils.", [1], "hobby"))
        system.models = models(memory={"extract": extract})
        system.rebuild("u")
        self.assertEqual(len(system.facts("u")), 1)
        self.assertEqual([e.round_idx for e in system.store("u").backlog()], [1])


class ForgettingReliabilityTest(unittest.TestCase):
    def make_system(self, directory=None):
        system = MemorySystem(models(memory={"extract": facts_json(
            fact("The user lives in Quito.", [1], "home_city"),
            fact("The user collects fossils.", [1], "hobby"))}, chat={"answer": "ANSWER: fossils"}),
            settings(query_analysis=False), data_dir=directory)
        self.addCleanup(system.close)
        return system

    def test_shared_source_cannot_reintroduce_forgotten_fact_after_restart_or_reindex(self):
        with tempfile.TemporaryDirectory() as directory:
            system = self.make_system(directory)
            save(system, "I live in Quito and collect fossils.")
            system.forget("u", "F1")
            self.assertEqual(system.store("u").episodes()[0].status, EPISODE_WITHHELD)
            self.assertEqual(system.store("u").vectors("episode_chunk", ("active", ARCHIVED))[0], [])
            self.assertEqual(system.store("u").fts_search("episode", '"quito"', ("active", ARCHIVED), 10), [])
            self.assertIn("fossils", system.recall("u", "What do I collect?", AT).memory)
            self.assertNotIn("Quito", system.recall("u", "Quito", AT).memory)
            self.assertEqual(system.reindex("u"), 1)
            system.close()
            system = self.make_system(directory)
            self.assertNotIn("Quito", system.recall("u", "Quito", AT).memory)
            self.assertEqual(system.store("u").fact(1).status, FORGOTTEN)
            self.assertEqual(system.store("u").fact(2).status, FACT_CURRENT)

    def test_recent_conversation_is_excluded_but_new_turns_and_audit_log_remain(self):
        system = self.make_system()
        handle, _ = save(system, "I live in Quito and collect fossils.", close=False)
        system.forget("u", "F1")
        system.respond("u", "What do I collect?", AT, session=handle)
        messages = system.models.chat.calls[-1][1]
        self.assertNotIn("Quito", str(messages))
        self.assertIn("What do I collect?", str(messages))
        self.assertIn("Quito", system.store("u").turns(handle.session_id)[0]["content"])

    def test_forget_source_prevents_backfill_and_retains_unrelated_fact(self):
        system = self.make_system()
        save(system, "I live in Quito and collect fossils.")
        with system.store("u").transaction():
            system.store("u").add_backlog([1])
        system.forget("u", "F1")
        self.assertEqual(system.rebuild("u"), [])
        self.assertEqual(system.stats("u")["backlog"], 0)
        self.assertEqual([f.key for f in system.facts("u")], ["F2"])
        system.forget("u", "F2")
        self.assertEqual(system.store("u").episodes()[0].status, FORGOTTEN)

    def test_forgotten_conflict_cannot_be_confirmed(self):
        system = self.make_system()
        save(system, "I live in Quito and collect fossils.")
        with system.store("u").transaction():
            pid = system.store("u").add_pending(1, 2, AT, "conflict")
        system.forget("u", "F1")
        self.assertEqual(system.pending("u"), [])
        with self.assertRaises(KeyError):
            system.confirm("u", pid, True)


class LifecycleReliabilityTest(unittest.TestCase):
    def test_default_capacity_is_bounded_and_explicit_unbounded_is_allowed(self):
        self.assertEqual((MemorySettings().max_active_facts, MemorySettings().max_active_episodes), (5000, 2000))
        MemorySettings.from_dict({"max_active_facts": None, "max_active_episodes": None})

    def test_capacity_retains_valuable_facts_and_is_idempotent(self):
        store = Store()
        self.addCleanup(store.close)
        with store.transaction():
            for i in range(20):
                store.add_fact(text="The user owns specimen%d." % i, kind="personal", subject="user",
                               attribute="specimen", value=str(i), event=None, recorded_at=AT,
                               valid_from=None, session_id="s1", importance=1.0 if i >= 17 else 0.1)
        config = settings(max_active_facts=3)
        report = run_lifecycle(store, AT, config)
        self.assertEqual(len(report.archived_facts), 17)
        self.assertEqual([f.id for f in store.facts(statuses=(FACT_CURRENT,))], [18, 19, 20])
        self.assertEqual(run_lifecycle(store, AT, config).archived_facts, [])

    def test_aggregate_and_past_queries_include_archives_despite_enough_active_candidates(self):
        store = Store()
        self.addCleanup(store.close)
        with store.transaction():
            for i in range(5):
                store.add_fact(text="The user attended pottery workshop %d." % i, kind="event", subject="user",
                               attribute="workshop", value=str(i), event=None, recorded_at=AT,
                               valid_from=None, session_id="s1", importance=0.5)
            store.set_status("fact", [1], ARCHIVED)
        config = settings(query_analysis=False, min_similarity=1.0)
        model_set = models()
        with store.transaction():
            store.put_vectors("fact", list(zip(range(1, 6), model_set.embedder.embed(
                [f.text for f in store.facts()]))))
        retrieval = Retriever(store, model_set, config)
        for needs_all, focus in [(True, "any"), (False, "past")]:
            plan = QueryPlan(queries=["pottery workshop"], keywords=["pottery"], time_range=None,
                             needs_all=needs_all, focus=focus, source="test")
            with patch.object(model_set.embedder, "embed", wraps=model_set.embedder.embed) as encode:
                result = retrieval.search(plan)
                self.assertEqual(encode.call_count, 1)
            self.assertTrue(result.used_archive)
            self.assertIn("F1", result.items)
            self.assertEqual(len(result.ranked), 5)


if __name__ == "__main__":
    unittest.main()
