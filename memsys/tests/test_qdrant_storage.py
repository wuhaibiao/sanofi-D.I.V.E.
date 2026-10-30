"""Functional contract tests; never run benchmarks, judges or real models."""
import json
import copy
import tempfile
import threading
import unittest
from datetime import datetime
from unittest.mock import patch
from memsys.config import AppConfig, MemorySettings, Models
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder
from memsys.llm import ChatResult
from memsys.qdrant import QdrantSettings
from memsys.schema import FORGOTTEN, EPISODE_WITHHELD
from memsys.store import Store, WriteConflict, EmbeddingMismatch
from memsys.read import Retriever, heuristic_plan
from memsys.write import Writer
from memsys.maintenance import cleanup_plan, apply_cleanup, integrity
from tests.qdrant_fixture import Engine

NOW = datetime(2026, 10, 12, 12)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.settings = QdrantSettings(namespace="functional-tests")
        self.a = self.store("alice")
        self.b = self.store("bob")
        self.session = self.a.create_session(NOW)

    def store(self, user):
        return Store(user_id=user, settings=self.settings, client=self.engine)

    def fact(self, store=None, text="Alice likes tea", value="tea", episode_ids=()):
        store = store or self.a
        return store.add_fact(text=text, kind="preference", subject="Alice", attribute="drink", value=value,
            event=None, recorded_at=NOW, valid_from=NOW.date(), session_id=self.session, importance=.8,
            episode_ids=episode_ids)

    def test_tenant_isolation_and_vectorless_raw_log(self):
        self.a.append_turn(self.session, "user", "raw secret", NOW)
        fid = self.fact()
        self.assertEqual(self.b.facts(), [])
        self.assertEqual(self.b.turns(self.session), [])
        with self.assertRaises(KeyError):
            self.b.fact(fid)
        points = [p for p in self.engine.collections[self.a.collection].values() if p["payload"].get("kind") == "turn"]
        self.assertTrue(points)
        self.assertTrue(all(p["vector"] == {} for p in points))

    def test_idempotent_turn_and_different_payload_rejected(self):
        first = self.a.append_turn(self.session, "user", "hello", NOW, idempotency_key="event-1")
        self.assertEqual(first, self.a.append_turn(self.session, "user", "hello", NOW, idempotency_key="event-1"))
        with self.assertRaises(ValueError):
            self.a.append_turn(self.session, "user", "different", NOW, idempotency_key="event-1")
        self.assertEqual(len(self.a.turns(self.session)), 1)

    def test_nested_failure_leaves_no_visible_fact(self):
        with self.assertRaises(ValueError):
            with self.a.transaction():
                self.fact()
                raise ValueError("abort")
        self.assertEqual(self.a.facts(), [])

    def test_partial_stage_failure_is_invisible(self):
        self.engine.fail_stage = True
        with self.assertRaises(Exception):
            self.fact()
        self.engine.fail_stage = False
        self.assertEqual(self.a.facts(), [])
        self.fact(text="safe recovery")
        self.assertEqual([f.text for f in self.a.facts()], ["safe recovery"])

    def test_published_operation_recovers_after_finalize_failure(self):
        self.engine.fail_finalize = True
        with self.assertRaises(Exception):
            self.fact()
        self.engine.fail_finalize = False
        self.assertEqual(len(self.a.facts()), 1)
        self.fact(text="next operation")
        self.assertEqual(len(self.store("alice").facts()), 2)

    def test_competing_publication_does_not_overwrite(self):
        second = self.store("alice")
        self.engine.before_publish = lambda: second.set_meta("winner", "yes")
        with self.assertRaises(WriteConflict):
            self.fact(text="unpublished")
        self.assertEqual(self.a.facts(), [])
        self.assertEqual(self.a.get_meta("winner"), "yes")

    def test_job_cas_verification_accepts_server_timestamp_rounding(self):
        upsert = self.engine.upsert
        def round_timestamps(collection, points, **kwargs):
            points = copy.deepcopy(points)
            for point in points:
                if point["payload"].get("kind") == "job":
                    data = point["payload"]["data"]
                    for key, value in data.items():
                        if isinstance(value, float):
                            data[key] = float(format(value, ".16g"))
            return upsert(collection, points, **kwargs)
        timestamp = 1791885353.0289001
        with patch.object(self.engine, "upsert", side_effect=round_timestamps):
            self.a.put_record("job", "rounding", dict(status="queued", lease_until=0, queued_at=timestamp))
            row = self.a.record("job", "rounding")
            row.update(status="running", owner="worker", lease_until=timestamp, started_at=timestamp)
            self.a.put_record("job", "rounding", row)
        stored = self.a.record("job", "rounding")
        self.assertNotEqual(stored["lease_until"], timestamp)
        self.assertEqual((stored["status"], stored["owner"], stored["_version"]), ("running", "worker", 1))

    def test_job_cas_still_rejects_an_actual_competing_claim(self):
        self.a.put_record("job", "competing", dict(status="queued", lease_until=0))
        stale = self.a.record("job", "competing")
        other = self.store("alice")
        winner = other.record("job", "competing")
        winner.update(status="running", owner="winner", lease_until=1791885353.0289001)
        other.put_record("job", "competing", winner)
        stale.update(status="running", owner="loser", lease_until=1791885353.0289001)
        with self.assertRaises(WriteConflict):
            self.a.put_record("job", "competing", stale)
        self.assertEqual(self.a.record("job", "competing")["owner"], "winner")

    def test_read_snapshot_remains_stable(self):
        fid = self.fact()
        other = self.store("alice")
        with self.a.read_snapshot():
            other.set_status("fact", [fid], FORGOTTEN)
            self.assertEqual(self.a.fact(fid).status, "current")
        self.assertEqual(self.a.fact(fid).status, FORGOTTEN)

    def test_versions_and_sources_publish_together(self):
        with self.a.transaction():
            eid = self.a.add_episode(self.session, 0, NOW, "tea", "okay", .8, "active")
            old = self.fact(episode_ids=[eid])
        with self.a.transaction():
            new = self.fact(text="Alice likes coffee", value="coffee", episode_ids=[eid])
            self.a.supersede(old, new, NOW.date())
        self.assertEqual([f.id for f in self.a.chain(old)], [old, new])
        self.assertEqual(self.a.sources([new])[new], [eid])
        self.assertEqual(set(self.a.facts_of_episodes([eid])[eid]), {old, new})
        self.assertEqual([f.id for f in self.a.slot_facts("Alice", "drink")], [new])

    def test_old_index_revision_never_resurrects_forgotten_fact(self):
        fid = self.fact()
        self.a.put_vectors("fact", [(fid, [1, 0])])
        self.a.set_status("fact", [fid], FORGOTTEN)
        self.assertEqual(self.a.vector_search("fact", [1, 0], ["current"], 10), [])
        self.assertEqual(self.a.fts_search("fact", "tea", ["current"], 10), [])

    def test_filter_changed_field_does_not_return_old_record(self):
        fid = self.fact()
        self.a._patch("fact", fid, subject="", attribute="", status=FORGOTTEN)
        self.assertEqual(self.a.records("fact", subject="Alice"), [])

    def test_access_count_does_not_duplicate_dense_index(self):
        fid = self.fact()
        self.a.put_vectors("fact", [(fid, [1, 0])])
        before = len(self.engine.collections["memsys_dense_2"])
        self.a.touch("fact", [fid], NOW)
        self.assertEqual(self.a.fact(fid).access_count, 1)
        self.assertEqual(len(self.engine.collections["memsys_dense_2"]), before)
        self.assertEqual(self.a.vector_search("fact", [1, 0], ["current"], 1)[0][0], fid)

    def test_cursor_conflict_rolls_back_staged_episode(self):
        self.a.append_turn(self.session, "user", "x", NOW)
        self.a.mark_consolidated(self.session, 1)
        with self.assertRaises(WriteConflict):
            with self.a.transaction():
                self.a.add_episode(self.session, 0, NOW, "x", "", .8, "active")
                self.a.mark_consolidated(self.session, 1, expected_start=0)
        self.assertEqual(self.a.episodes(), [])

    def test_forget_withholds_shared_source_and_blocks_backfill(self):
        with self.a.transaction():
            self.a.append_turn(self.session, "user", "tea and chess", NOW)
            eid = self.a.add_episode(self.session, 0, NOW, "tea and chess", "", .8, "active")
            f1 = self.fact(episode_ids=[eid])
            f2 = self.fact(text="Alice plays chess", value="chess", episode_ids=[eid])
            self.a.add_backlog([eid])
            self.a.mark_consolidated(self.session, 1)
        self.a.forget("fact", f1)
        self.assertEqual(self.a.fact(f2).status, "current")
        self.assertEqual(self.a.episodes([eid])[0].status, EPISODE_WITHHELD)
        self.assertEqual(self.a.backlog(), [])
        self.assertEqual(self.a.conversation_turns(self.session), [])

    def test_selective_forget_redacts_verified_literal_span(self):
        with self.a.transaction():
            self.a.append_turn(self.session, "user", "My city is Paris. I like chess.", NOW)
            eid = self.a.add_episode(self.session, 0, NOW, "My city is Paris. I like chess.", "", .8, "active")
            fid = self.fact(text="Alice lives in Paris", value="Paris", episode_ids=[eid])
            self.fact(text="Alice likes chess", value="chess", episode_ids=[eid])
            self.a.record_span(fid, eid, "user", "My city is Paris.")
            self.a.mark_consolidated(self.session, 1)
        self.a.forget("fact", fid)
        self.assertNotIn("Paris", self.a.episodes([eid])[0].user_text)
        self.assertNotIn("Paris", self.a.conversation_turns(self.session)[0]["content"])
        self.assertEqual(self.a.episodes([eid])[0].status, "active")

    def test_parallel_append_preserves_distinct_indices(self):
        errors = []
        def append(number):
            try:
                self.store("alice").append_turn(self.session, "user", str(number), NOW, idempotency_key="parallel-%d" % number)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=append, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual([t["idx"] for t in self.a.turns(self.session)], list(range(6)))

    def test_cleanup_requires_offline_and_preserves_version_tombstone(self):
        fid = self.fact()
        self.a.forget("fact", fid)
        plan = cleanup_plan(self.a)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                apply_cleanup(self.a, plan, directory + "/backup.json")
            result = apply_cleanup(self.a, plan, directory + "/backup.json", offline=True)
            self.assertTrue(all(result["integrity"].values()))
            self.assertEqual(self.a.fact(fid).text, "")
            self.assertTrue(all(integrity(self.a).values()))

    def test_normal_retrieval_never_exports_all_vectors(self):
        fid = self.fact()
        self.a.put_vectors("fact", [(fid, [1, 0])])
        self.a.set_meta("embedding_model", "synthetic")
        class Embed:
            def embed(self, texts, **kwargs):
                return [[1, 0] for _ in texts]
        models = Models(None, None, Embed(), "synthetic")
        settings = MemorySettings(query_analysis=False, rerank_backend="fusion", fallback_min_candidates=0)
        with patch.object(self.a, "vectors", side_effect=AssertionError("full vector export")):
            result = Retriever(self.a, models, settings).search(heuristic_plan("tea"))
        self.assertIn("F%d" % fid, result.items)
        self.assertTrue(self.engine.queries)
        self.assertTrue(all(any(c.get("key") == "tenant" for c in q["filter"]["must"]) for q in self.engine.queries))

    def test_core_recall_does_not_generate_or_review_answer(self):
        embed = HashEmbedder()
        class ForbiddenChat:
            def chat(self, *args, **kwargs):
                raise AssertionError("unexpected model call")
        chat = ForbiddenChat()
        models = Models(chat, chat, embed, "hash-256", by_purpose={"answer_review": chat})
        system = MemorySystem(models, MemorySettings(query_analysis=False, rerank_backend="fusion"), storage=self.settings,
                              store_factory=lambda **kwargs: Store(client=self.engine, **kwargs))
        rec = system.recall("bob", "query", NOW, save_trace=False)
        self.assertEqual(rec.items, [])
        self.assertEqual(rec.trace["mode"], "recall")
        system.close()

    def test_writer_raw_only_without_model_consolidates_once(self):
        models = Models(None, None, HashEmbedder(), "hash-256")
        self.a.append_turn(self.session, "user", "Remember my city is Oslo", NOW)
        self.a.append_turn(self.session, "assistant", "Recorded", NOW)
        writer = Writer(self.a, models, MemorySettings(rerank_backend="fusion"))
        writer.consolidate(self.session, closed_at=NOW)
        count = len(self.a.episodes())
        writer.consolidate(self.session, closed_at=NOW)
        self.assertEqual(len(self.a.episodes()), count)
        self.assertEqual(self.a.session(self.session)["turns_done"], 2)

    def test_storage_quota_does_not_block_forgetting_or_access_bookkeeping(self):
        fid = self.fact()
        self.a.max_bytes = 1
        self.a.touch("fact", [fid], NOW)
        self.a.forget("fact", fid)
        self.assertEqual(self.a.fact(fid).status, "forgotten")
        from memsys.store import StorageLimitExceeded
        with self.assertRaises(StorageLimitExceeded):
            self.fact(text="new active fact")

    def test_index_generation_rollback_restores_vectors_only(self):
        fid = self.fact()
        source = self.a.index_source_fingerprint()
        self.a.replace_vectors("synthetic-one", {"fact": [(fid, [1,0])]}, expected_source=source)
        self.a.replace_vectors("synthetic-two", {"fact": [(fid, [0,1])]}, expected_source=source)
        self.a.rollback_index()
        self.assertEqual(self.a.get_meta("embedding_model"), "synthetic-one")
        self.assertEqual(self.a.vectors("fact", ["current"])[1][0], [1.,0.])
        self.a.forget("fact",fid)
        with self.assertRaises(EmbeddingMismatch):
            self.a.rollback_index()



if __name__ == "__main__":
    unittest.main()
