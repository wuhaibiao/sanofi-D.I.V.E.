"""Long-term recall must work through extracted facts without original-text shortcuts."""
import dataclasses
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from memsys.budget import budget_counter
from memsys.config import MemorySettings, Models
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.qdrant import QdrantSettings
from memsys.read import Retriever, heuristic_plan, render_memory
from memsys.schema import FORGOTTEN
from memsys.store import Store
from memsys.webchat import WebChat
from memsys.write import Writer
from tests.qdrant_fixture import Engine


NOW = datetime(2026, 10, 13, 12)
RAW = "RAW_ONLY_SENTINEL: unextracted workout details."
FACT = "The user lost 10 pounds after going to the gym consistently."


class FactRecallTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.storage = QdrantSettings(namespace="fact-recall-tests")
        self.settings = MemorySettings(rerank_backend="fusion", max_history_share=0)
        self.memory_model = ScriptedChat({"extract": json.dumps({"facts": [
            {"text": FACT, "rounds": [1], "subject": "user", "attribute": "weight_loss", "value": "10 pounds"}]})})
        self.models = Models(None, self.memory_model, HashEmbedder(), "hash-256")
        self.system = MemorySystem(self.models, self.settings, storage=self.storage,
            store_factory=lambda **kw: Store(client=self.engine, **kw))
        self.store = self.system.store("alice")
        sid = self.store.create_session(NOW)
        self.store.append_turn(sid, "user", "I lost 10 pounds after going to the gym consistently. " + RAW, NOW)
        self.store.append_turn(sid, "assistant", "Congratulations! " + RAW, NOW)
        Writer(self.store, self.models, self.settings).consolidate(sid, closed_at=NOW)
        self.fact_id = self.store.facts()[0].id
        self.episode_id = self.store.episodes()[0].id
        self.plan = heuristic_plan("List all my weight loss records since going to the gym.")

    def retriever(self, **kw):
        return Retriever(self.store, self.models, dataclasses.replace(self.settings, **kw))

    def test_default_and_validation_and_fingerprints(self):
        self.assertEqual(MemorySettings.from_dict({}).recall_mode, "facts")
        mixed = dataclasses.replace(self.settings, recall_mode="facts_and_sources")
        mixed.validate()
        with self.assertRaises(ValueError):
            MemorySettings.from_dict({"recall_mode": "automatic"})
        self.assertEqual(self.settings.write_fingerprint(), mixed.write_fingerprint())
        self.assertEqual(self.settings.index_fingerprint("hash-256"), mixed.index_fingerprint("hash-256"))
        self.assertNotEqual(self.settings.read_fingerprint(), mixed.read_fingerprint())

    def test_default_search_does_not_query_or_expand_originals(self):
        self.plan.needs_all = False
        vector, lexical = self.store.vector_search, self.store.fts_search
        kinds = []
        def dense(kind, *args):
            kinds.append(kind)
            return vector(kind, *args)
        def sparse(kind, *args):
            kinds.append(kind)
            return lexical(kind, *args)
        with patch.object(self.store, "vector_search", side_effect=dense), \
                patch.object(self.store, "fts_search", side_effect=sparse), \
                patch.object(self.store, "episodes", side_effect=AssertionError("original retrieval")), \
                patch.object(self.store, "sources", side_effect=AssertionError("source expansion")):
            result = self.retriever().search(self.plan)
        self.assertTrue(result.ranked)
        self.assertEqual(set(kinds), {"fact"})
        self.assertTrue(all(c.kind == "fact" for c in result.ranked))

    def test_complete_search_counts_only_facts_and_never_enumerates_originals(self):
        for index in range(4):
            self.store.add_episode("extra", index, NOW, RAW, "", .8, "active")
        with patch.object(self.store, "episodes", side_effect=AssertionError("original enumeration")):
            result = self.retriever(aggregate_candidate_limit=1).search(self.plan)
        self.assertEqual(result.coverage["eligible_pool"], 1)
        self.assertTrue(result.coverage["search_complete"])
        self.assertNotIn("originals", result.coverage["scope"])

    def test_missing_fact_cannot_fall_back_to_originals(self):
        self.store.set_status("fact", [self.fact_id], FORGOTTEN)
        result = self.retriever().search(self.plan)
        self.assertEqual(result.ranked, [])
        recalled = self.system.recall("alice", self.plan.queries[0], NOW, save_trace=False)
        self.assertEqual(recalled.items, [])
        self.assertNotIn(RAW, recalled.context)

    def test_coverage_selector_cannot_reintroduce_sources_and_uses_fact_budget(self):
        result = self.retriever(recall_mode="facts_and_sources").search(self.plan)
        reader = self.retriever(selection_policy="coverage", fact_budget_share_all=.01)
        selected = reader.select(result, self.plan, budget=600, today=NOW.date())
        self.assertEqual([f.text for f in selected.facts], [FACT])
        self.assertEqual(selected.episodes, [])
        self.assertNotIn(RAW, render_memory(selected, NOW.date()))
        self.assertFalse(selected.coverage["context_complete"])

    def test_local_reranker_receives_only_facts_even_for_mixed_input(self):
        result = self.retriever(recall_mode="facts_and_sources").search(self.plan)
        captured = []
        class Ranker:
            def rank(self, question, documents):
                captured.extend(documents)
                return {doc.key: .9 for doc in documents}, {"backend": "test"}
        with patch("memsys.reranking.local_reranker", return_value=Ranker()):
            result = self.retriever(rerank_backend="bge").rerank(result, self.plan.queries[0], NOW, plan=self.plan)
        self.assertTrue(result.reranked)
        self.assertTrue(captured)
        self.assertTrue(all(doc.key.startswith("F") for doc in captured))
        self.assertNotIn(RAW, repr(captured))

    def test_core_returns_facts_with_source_ids_without_source_text(self):
        recalled = self.system.recall("alice", self.plan.queries[0], NOW, save_trace=False)
        self.assertEqual([item["kind"] for item in recalled.items], ["fact"])
        self.assertEqual(recalled.items[0]["sources"], ["E%d" % self.episode_id])
        self.assertIn(FACT, recalled.context)
        self.assertNotIn(RAW, recalled.context)
        self.assertNotIn("Past conversation excerpts", recalled.context)
        self.assertEqual(recalled.trace["selected"]["episodes"], [])
        self.assertEqual(recalled.trace["recall_mode"], "facts")
        self.assertTrue(recalled.trace["coverage"]["context_complete"])

    def test_pages_and_archive_respect_fact_scope(self):
        self.store.set_status("fact", [self.fact_id], "archived")
        pages = self.system.recall_pages("alice", self.plan.queries[0], NOW)
        self.assertTrue(pages["pages_complete"])
        self.assertEqual(pages["coverage"]["eligible_pool"], 1)
        self.assertTrue(all(item["kind"] == "fact" for page in pages["pages"] for item in page["items"]))
        self.assertNotIn(RAW, repr(pages))

    def test_large_pool_keeps_bounded_hits_without_claiming_complete_coverage(self):
        sid = self.store.create_session(NOW)
        self.store.add_fact(text='The user stores a red notebook.', kind='personal', subject='user',
            attribute='notebook', value='red', event=None, recorded_at=NOW,
            valid_from=NOW.date(), session_id=sid, importance=.5)
        original = self.store.facts
        def bounded(ids=None, *args, **kwargs):
            if ids is None:
                raise AssertionError('over-limit recall must not enumerate the whole fact store')
            return original(ids, *args, **kwargs)
        reader = self.retriever(aggregate_candidate_limit=1, relations_enabled=False)
        with patch.object(self.store, 'facts', side_effect=bounded), \
                patch.object(self.store, 'episodes', side_effect=AssertionError('original text read')):
            result = reader.search(self.plan)
        self.assertTrue(result.ranked)
        self.assertEqual(result.ranked[0].id, self.fact_id)
        self.assertLessEqual(len(result.ranked), 1)
        self.assertEqual(result.coverage['eligible_pool'], 2)
        self.assertFalse(result.coverage['search_complete'])
        self.assertNotIn(RAW, repr(result.items))

    def test_large_pool_partial_hits_reach_host_with_subset_warning(self):
        sid = self.store.create_session(NOW)
        self.store.add_fact(text='The user stores a red notebook.', kind='personal', subject='user',
            attribute='notebook', value='red', event=None, recorded_at=NOW,
            valid_from=NOW.date(), session_id=sid, importance=.5)
        self.settings.aggregate_candidate_limit = 1
        recall = self.system.recall('alice', self.plan.queries[0], NOW, save_trace=False,
                                    budget=4000, coverage_budget=12000)
        self.assertTrue(recall.items)
        self.assertIn('known subset', recall.memory)
        self.assertFalse(recall.trace['coverage']['context_complete'])
        self.assertEqual(recall.trace['selected']['episodes'], [])

    def test_sources_require_explicit_diagnostic_mode(self):
        self.system.settings = dataclasses.replace(self.settings, recall_mode="facts_and_sources")
        recalled = self.system.recall("alice", self.plan.queries[0], NOW, save_trace=False)
        self.assertIn("episode", [item["kind"] for item in recalled.items])
        self.assertIn(RAW, recalled.context)
        self.assertEqual(recalled.trace["recall_mode"], "facts_and_sources")

    def test_pending_originals_cannot_enter_default_prompt(self):
        web = WebChat(SimpleNamespace(req=self.system, models=self.models))
        pending = [{"recorded_at": NOW.isoformat(), "user": RAW, "assistant": "Noted."}]
        messages = [{"role": "system", "content": "Use remembered facts."},
                    {"role": "user", "content": "What do you remember?"}]
        supplied, refs = web.include_pending(messages, pending, budget_counter(self.settings))
        self.assertEqual(supplied, messages)
        self.assertEqual(refs, [])
        with patch.object(self.store, "records", side_effect=AssertionError("cross-session original scan")):
            self.assertEqual(web.pending_rounds(self.store, "current", include_current=True), [])


if __name__ == "__main__":
    unittest.main()
