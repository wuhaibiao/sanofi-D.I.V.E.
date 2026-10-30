"""Additive write contracts, using only synthetic replies and an in-memory protocol double."""
import json
import unittest
import warnings
from datetime import datetime
from unittest.mock import patch

from memsys.config import AppConfig, MemorySettings, Models
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.llm import BadJSONError, ChatResult, IncompleteJSONError, ModelError
from memsys.qdrant import QdrantSettings
from memsys.store import Store, WriteConflict
from memsys.write import Writer
from tests.qdrant_fixture import Engine

NOW = datetime(2026, 10, 12, 12)


def response(text, rounds=(1,)):
    return json.dumps({"facts": [dict(text=text, rounds=list(rounds), kind="personal",
        subject="user", attribute="home_city", value=text, certainty="firm")]})


class AdditiveWriteTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.storage = QdrantSettings(namespace="additive-functional")
        self.store = Store(user_id="alice", client=self.engine, settings=self.storage)
        self.settings = MemorySettings(rerank_backend="fusion")
        self.model = ScriptedChat({})
        self.models = Models(None, self.model, HashEmbedder(), "hash-256")
        self.writer = Writer(self.store, self.models, self.settings)

    def write(self, raw, reply):
        sid = self.store.create_session(NOW)
        self.store.append_turn(sid, "user", raw, NOW)
        self.model.handlers["extract"] = reply
        return self.writer.consolidate(sid, closed_at=NOW)

    def test_changed_value_is_added_with_one_call_and_old_memory_context(self):
        self.write("I live in Hangzhou.", response("The user lives in Hangzhou."))
        old = self.store.facts()[0]
        self.model.calls.clear()
        report = self.write("I have moved to Shanghai.", response("The user lives in Shanghai."))
        self.assertEqual(self.model.purposes(), ["extract"])
        self.assertEqual(len(report.added), 1)
        self.assertEqual(report.updates, [])
        self.assertEqual({f.text for f in self.store.facts()},
                         {"The user lives in Hangzhou.", "The user lives in Shanghai."})
        self.assertTrue(all(f.status == "current" and f.superseded_by is None for f in self.store.facts()))
        self.assertEqual(self.store.fact(old.id).text, old.text)
        prompt = self.model.calls[0][1][-1]["content"]
        self.assertIn(old.text, prompt)
        self.assertIn("never output update/delete actions", prompt)
        self.assertEqual(self.store.pending(), [])

    def test_exact_duplicate_links_sources_without_ann_or_confidence_changes(self):
        self.write("I live in Hangzhou.", response("The user lives in Hangzhou."))
        old = self.store.facts()[0]
        self.store.set_confidence(old.id, .5)
        self.model.calls.clear()
        with patch.object(self.store, "vector_search", return_value=[]):
            report = self.write("Again, I live in Hangzhou.", response("The user lives in Hangzhou."))
        self.assertEqual(self.model.purposes(), ["extract"])
        self.assertEqual((len(self.store.facts()), report.duplicates, report.added), (1, 1, []))
        self.assertEqual(self.store.fact(old.id).confidence, .5)
        self.assertEqual(len(self.store.sources([old.id])[old.id]), 2)

    def test_same_batch_duplicate_retains_both_sources(self):
        sid = self.store.create_session(NOW)
        self.store.append_turn(sid, "user", "I live in Hangzhou.", NOW)
        self.store.append_turn(sid, "assistant", "Noted.", NOW)
        self.store.append_turn(sid, "user", "I still live in Hangzhou.", NOW)
        items = json.loads(response("The user lives in Hangzhou.", (1,)))["facts"]
        items += json.loads(response("The user lives in Hangzhou.", (2,)))["facts"]
        self.model.handlers["extract"] = json.dumps({"facts": items})
        report = self.writer.consolidate(sid, closed_at=NOW)
        self.assertEqual((len(report.added), self.model.purposes()), (1, ["extract"]))
        self.assertEqual(len(self.store.sources(report.added)[report.added[0]]), 2)

    def test_empty_extraction_does_not_reinsert_existing_context(self):
        self.write("I live in Hangzhou.", response("The user lives in Hangzhou."))
        fid = self.store.facts()[0].id
        sources = self.store.sources([fid])
        report = self.write("Tell me about my city.", '{"facts": []}')
        self.assertEqual(report.added, [])
        self.assertEqual(self.store.sources([fid]), sources)
        self.assertEqual(len(self.store.episodes()), 2)

    def test_backfill_is_additive_and_has_no_second_model_step(self):
        self.write("I live in Hangzhou.", response("The user lives in Hangzhou."))
        sid = self.store.create_session(NOW)
        self.store.append_turn(sid, "user", "I have moved to Shanghai.", NOW)
        raw_only = Models(None, None, HashEmbedder(), "hash-256")
        Writer(self.store, raw_only, self.settings).consolidate(sid, closed_at=NOW)
        self.assertEqual(self.store.backlog_count(), 1)
        self.model.calls.clear()
        self.model.handlers["extract"] = response("The user lives in Shanghai.")
        reports = self.writer.backfill()
        self.assertEqual(self.model.purposes(), ["extract"])
        self.assertEqual((len(self.store.facts()), self.store.backlog_count()), (2, 0))
        self.assertEqual(reports[0].updates, [])

    def test_incomplete_extraction_fails_once_and_preserves_unprocessed_source(self):
        truncated = ChatResult(text='{"facts": [', model="synthetic", prompt_tokens=0,
            completion_tokens=0, cached_tokens=0, cost=0, latency_s=0, finish_reason="length")
        sid = self.store.create_session(NOW)
        self.store.append_turn(sid, "user", "I live in Hangzhou.", NOW)
        self.store.append_turn(sid, "assistant", "Noted.", NOW)
        self.store.append_turn(sid, "user", "I moved to Shanghai.", NOW)
        self.model.handlers["extract"] = lambda _: truncated
        with self.assertRaises(IncompleteJSONError):
            self.writer.consolidate(sid, closed_at=NOW)
        self.assertEqual(len(self.store.facts()), 0)
        self.assertEqual(len(self.store.turns(sid)), 3)
        self.assertEqual(self.store.session(sid)["turns_done"], 0)
        self.assertEqual(self.model.purposes(), ["extract"])
        self.model.handlers["extract"] = response("The user lives in Hangzhou.")
        self.writer.consolidate(sid, closed_at=NOW)
        self.assertEqual(len(self.store.facts()), 1)
        self.assertEqual(self.model.purposes(), ["extract", "extract"])

    def test_legacy_record_dedup_validates_latest_status(self):
        self.write("I live in Hangzhou.", response("The user lives in Hangzhou."))
        fid = self.store.facts()[0].id
        row = self.store.record("fact", fid)
        row.pop("content_hash")
        self.store.put_record("fact", fid, row)
        self.assertEqual(self.store.exact_fact(row["text"]).id, fid)
        self.store.forget("fact", fid)
        self.assertIsNone(self.store.exact_fact(row["text"]))

    def test_dedup_is_tenant_scoped(self):
        self.write("I live in Hangzhou.", response("The user lives in Hangzhou."))
        other = Store(user_id="bob", client=self.engine, settings=self.storage)
        self.assertIsNone(other.exact_fact("The user lives in Hangzhou."))

    def test_old_memory_context_is_not_limited_by_the_previous_byte_budget(self):
        old_text = "The user lives in Hangzhou. " + "旧信息" * 2000 + "CONTEXT_END"
        self.write("Remember my long background.", response(old_text))
        self.settings.memory_input_budget_tokens = 1  # Optional read budget must not constrain writes.
        self.model.calls.clear()
        self.write("I have moved to Shanghai.", response("The user lives in Shanghai."))
        prompt = self.model.calls[0][1][-1]["content"]
        self.assertIn(old_text, prompt)
        self.assertEqual(self.model.purposes(), ["extract"])

    def test_large_multiround_input_is_one_full_request(self):
        sid = self.store.create_session(NOW)
        user = "I need you to remember " + "完整消息" * 2000 + " USER_END"
        assistant = "完整回复" * 2000 + " ASSISTANT_END"
        self.store.append_turn(sid, "user", user, NOW)
        self.store.append_turn(sid, "assistant", assistant, NOW)
        self.store.append_turn(sid, "user", "The next round must stay together. ROUND_TWO_END", NOW)
        self.model.handlers["extract"] = response("A synthetic memory.", (1, 2))
        report = self.writer.consolidate(sid, closed_at=NOW)
        self.assertEqual((report.extract_calls, report.extract_split_rounds, report.extract_truncated_rounds), (1, 0, 0))
        prompt = self.model.calls[0][1][-1]["content"]
        self.assertIn(user, prompt)
        self.assertIn(assistant, prompt)
        self.assertIn("ROUND_TWO_END", prompt)
        self.assertNotIn("[Part ", prompt)
        self.assertEqual(self.model.purposes(), ["extract"])

    def test_recent_context_uses_only_last_ten_processed_messages(self):
        sid = self.store.create_session(NOW)
        for i in range(12):
            self.store.append_turn(sid, "user" if i % 2 == 0 else "assistant", "message-%d" % i, NOW)
        self.model.handlers["extract"] = '{"facts": []}'
        self.writer.consolidate(sid)
        self.store.append_turn(sid, "user", "I moved to Shanghai.", NOW)
        self.model.handlers["extract"] = response("The user lives in Shanghai.")
        self.writer.consolidate(sid, closed_at=NOW)
        prompt = self.model.calls[-1][1][-1]["content"]
        self.assertNotIn('"content": "message-0"', prompt)
        self.assertNotIn('"content": "message-1"', prompt)
        for i in range(2, 12):
            self.assertIn('"content": "message-%d"' % i, prompt)

    def test_bad_json_is_not_split_or_partially_committed(self):
        sid = self.store.create_session(NOW)
        self.store.append_turn(sid, "user", "I live in Oslo.", NOW)
        self.store.append_turn(sid, "assistant", "Noted.", NOW)
        self.store.append_turn(sid, "user", "I like tea.", NOW)
        self.model.handlers["extract"] = 'not-json'
        with self.assertRaises(BadJSONError):
            self.writer.consolidate(sid, closed_at=NOW)
        self.assertEqual(self.model.purposes(), ["extract"])
        self.assertEqual(self.store.facts(), [])
        self.assertEqual(self.store.episodes(), [])
        self.assertEqual(self.store.session(sid)["turns_done"], 0)

    def test_provider_input_limit_error_propagates_without_splitting(self):
        sid = self.store.create_session(NOW)
        self.store.append_turn(sid, "user", "I have a long conversation. " * 1000, NOW)
        def too_large(_):
            raise ModelError("synthetic context length exceeded", status=400)
        self.model.handlers["extract"] = too_large
        with self.assertRaises(ModelError) as raised:
            self.writer.consolidate(sid, closed_at=NOW)
        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(self.model.purposes(), ["extract"])
        self.assertEqual(self.store.session(sid)["turns_done"], 0)
        self.assertEqual(len(self.store.turns(sid)), 1)

    def test_legacy_split_and_truncation_settings_are_retired(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            settings = MemorySettings.from_dict(dict(extract_chunk_tokens=1, extract_full_rounds=False,
                extract_assistant_chars=1, extract_context_tokens=1))
        self.assertTrue(any("retired extraction limits" in str(w.message) for w in caught))
        self.assertTrue(all(not hasattr(settings, name) for name in MemorySettings.RETIRED_EXTRACT_FIELDS))

    def test_default_recall_calls_no_chat_model_even_for_complex_question(self):
        system = MemorySystem(self.models, self.settings, storage=self.storage,
            store_factory=lambda **kw: Store(client=self.engine, **kw))
        system.recall("alice", "List all my past cities and compare the current one.", NOW, save_trace=False)
        self.assertEqual(self.model.purposes(), [])
        system.close()

    def test_old_resolve_role_is_ignored_without_loading_it(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            config = AppConfig.from_dict({"models": {}, "roles": {"resolve": "retired-provider"}})
        self.assertNotIn("resolve", config.roles)
        self.assertTrue(any("roles.resolve" in str(w.message) for w in caught))

    def test_concurrent_fact_change_rolls_back_extraction_commit(self):
        sid = self.store.create_session(NOW)
        self.store.append_turn(sid, "user", "I live in Hangzhou.", NOW)
        def reply(_):
            other = Store(user_id="alice", client=self.engine, settings=self.storage)
            other.add_fact(text="A concurrent memory", kind="personal", subject="user", attribute="",
                value="", event=None, recorded_at=NOW, valid_from=NOW.date(), session_id=sid, importance=.5)
            return response("The user lives in Hangzhou.")
        self.model.handlers["extract"] = reply
        with self.assertRaises(WriteConflict):
            self.writer.consolidate(sid, closed_at=NOW)
        self.assertEqual([f.text for f in self.store.facts()], ["A concurrent memory"])
        self.assertEqual(self.store.episodes(), [])
        self.assertEqual(self.store.session(sid)["turns_done"], 0)

    def test_chat_publication_conflict_reuses_extraction_and_vectors(self):
        other = Store(user_id="alice", client=self.engine, settings=self.storage)
        other_sid = other.create_session(NOW)
        embed = self.models.embedder.embed
        def inject_conflict(*args, **kwargs):
            result = embed(*args, **kwargs)
            self.engine.before_publish = lambda: other.append_turn(other_sid, "assistant", "Concurrent new-chat reply.", NOW)
            return result
        with patch.object(self.models.embedder, "embed", side_effect=inject_conflict):
            report = self.write("I live in Hangzhou.", response("The user lives in Hangzhou."))
        self.assertEqual(self.model.purposes(), ["extract"])
        self.assertEqual(self.models.embedder.calls, 1)
        self.assertEqual(report.commit_retries, 1)
        self.assertEqual(report.extract_calls, 1)
        self.assertEqual((len(report.added), report.duplicates), (1, 0))
        self.assertEqual(report.added, [f.id for f in self.store.facts()])
        self.assertEqual(len(self.store.episodes()), 1)
        self.assertEqual(other.turns(other_sid)[0]["content"], "Concurrent new-chat reply.")
        sid = self.store.facts()[0].session_id
        self.assertEqual((self.store.session(sid)["status"], self.store.session(sid)["turns_done"]), ("closed", 1))

    def test_fact_change_during_publication_requires_fresh_extraction(self):
        other = Store(user_id="alice", client=self.engine, settings=self.storage)
        other_sid = other.create_session(NOW)
        embed = self.models.embedder.embed
        def inject_change(*args, **kwargs):
            result = embed(*args, **kwargs)
            self.engine.before_publish = lambda: other.add_fact(text="Changed memory context", kind="personal",
                subject="user", attribute="", value="", event=None, recorded_at=NOW,
                valid_from=NOW.date(), session_id=other_sid, importance=.5)
            return result
        with patch.object(self.models.embedder, "embed", side_effect=inject_change):
            with self.assertRaises(WriteConflict):
                self.write("I live in Hangzhou.", response("The user lives in Hangzhou."))
        self.assertEqual(self.model.purposes(), ["extract"])
        self.assertEqual([f.text for f in self.store.facts()], ["Changed memory context"])
        self.assertEqual(self.store.episodes(), [])

    def test_other_worker_advancing_source_cursor_does_not_duplicate_rounds(self):
        sid = self.store.create_session(NOW)
        self.store.append_turn(sid, "user", "I live in Hangzhou.", NOW)
        other = Store(user_id="alice", client=self.engine, settings=self.storage)
        embed = self.models.embedder.embed
        self.model.handlers["extract"] = response("The user lives in Hangzhou.")
        def inject_cursor_change(*args, **kwargs):
            result = embed(*args, **kwargs)
            self.engine.before_publish = lambda: other.mark_consolidated(sid, 1, closed_at=NOW)
            return result
        with patch.object(self.models.embedder, "embed", side_effect=inject_cursor_change):
            with self.assertRaises(WriteConflict):
                self.writer.consolidate(sid, closed_at=NOW)
        self.assertEqual(self.model.purposes(), ["extract"])
        self.assertEqual(self.store.facts(), [])
        self.assertEqual(self.store.episodes(), [])
        self.assertEqual(self.store.session(sid)["turns_done"], 1)

    def test_explicit_recall_budget_preserves_changed_state_in_short_history(self):
        original_share = self.settings.max_history_share
        self.write("I live in Larchport.", response("The user lives in Larchport."))
        self.write("I have moved to Reedhaven and live there now.",
                   response("The user has moved from Larchport to Reedhaven and now lives in Reedhaven."))
        with patch("memsys.core.Qdrant", return_value=self.engine):
            system = MemorySystem(self.models, self.settings, storage=self.storage)
        try:
            recalled = system.recall("alice", "Where do I currently live?", NOW, budget=2048)
            self.assertEqual(recalled.trace["effective_budget"], 2048)
            self.assertIn("Reedhaven", recalled.memory)
            self.assertIn("Larchport", recalled.memory)
            self.assertEqual(recalled.trace["omitted"]["facts"], [])
            self.assertEqual(self.settings.max_history_share, original_share)
        finally:
            system.close()
