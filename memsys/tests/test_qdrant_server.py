"""Optional real-server functional integration, isolated to a disposable local Qdrant.

QDRANT_TEST_URL must be a loopback URL. No model or timing/accuracy evaluation is run.
"""
import os
import tempfile
import threading
import unittest
import uuid
from datetime import datetime
from urllib.parse import urlsplit
from memsys.qdrant import Qdrant, QdrantSettings
from memsys.store import Store, WriteConflict
from memsys.schema import FORGOTTEN
from memsys.maintenance import cleanup_plan, apply_cleanup

URL = os.environ.get("QDRANT_TEST_URL")
NOW = datetime(2026, 10, 12)


@unittest.skipUnless(URL, "disposable Qdrant server not configured")
class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if urlsplit(URL).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("integration tests require a disposable loopback server")
        cls.prefix = "memsys_test_" + uuid.uuid4().hex[:12]
        cls.settings = QdrantSettings(url=URL, collection_prefix=cls.prefix, namespace="functional-test")
        cls.client = Qdrant(cls.settings)

    @classmethod
    def tearDownClass(cls):
        collections = cls.client.request("GET", "/collections")["collections"]
        for item in collections:
            if item["name"].startswith(cls.prefix + "_"):
                cls.client.request("DELETE", "/collections/" + item["name"])

    def setUp(self):
        self.user = "user-" + uuid.uuid4().hex
        self.store = Store(user_id=self.user, settings=self.settings, client=self.client)
        self.sid = self.store.create_session(NOW)

    def add_fact(self, text="The preferred city is Oslo"):
        return self.store.add_fact(text=text, kind="personal", subject="user", attribute="city", value="Oslo",
                    event=None, recorded_at=NOW, valid_from=NOW.date(), session_id=self.sid, importance=.8)

    def test_raw_vectorless_messages_idempotence_and_isolation(self):
        for _ in range(2):
            self.store.append_turn(self.sid, "user", "原始消息", NOW, idempotency_key="evt-1")
        self.assertEqual(len(self.store.turns(self.sid)), 1)
        other = Store(user_id="other-" + self.user, settings=self.settings, client=self.client)
        self.assertEqual(other.turns(self.sid), [])
        with self.assertRaises(KeyError):
            other.session(self.sid)

    def test_additive_hash_lookup_and_legacy_text_filter(self):
        from memsys.config import MemorySettings, Models
        from memsys.fake import HashEmbedder, ScriptedChat
        from memsys.write import Writer
        import json
        chat = ScriptedChat({"extract": json.dumps({"facts": [{"text": "The user lives in Oslo.",
             "rounds": [1], "kind": "personal", "subject": "user", "attribute": "home_city"}]})})
        writer = Writer(self.store, Models(None, chat, HashEmbedder(), "hash-256"),
                        MemorySettings(rerank_backend="fusion"))
        self.store.append_turn(self.sid, "user", "I live in Oslo.", NOW)
        writer.consolidate(self.sid, closed_at=NOW)
        fid = self.store.facts()[0].id
        self.assertEqual(self.store.exact_fact("The user lives in Oslo.").id, fid)
        row = self.store.record("fact", fid)
        row.pop("content_hash")
        self.store.put_record("fact", fid, row)
        self.assertEqual(self.store.exact_fact("The user lives in Oslo.").id, fid)
        again = self.store.create_session(NOW)
        self.store.append_turn(again, "user", "I still live in Oslo.", NOW)
        report = writer.consolidate(again, closed_at=NOW)
        self.assertEqual((report.duplicates, len(self.store.facts())), (1, 1))
        self.assertEqual(chat.purposes(), ["extract", "extract"])
        self.store.forget("fact", fid)
        self.assertIsNone(self.store.exact_fact("The user lives in Oslo."))

    def test_dense_sparse_and_status_filter(self):
        fid = self.add_fact()
        self.store.put_vectors("fact", [(fid, [1., 0., 0.])])
        self.assertEqual(self.store.vector_search("fact", [1., 0., 0.], ["current"], 3)[0][0], fid)
        self.assertEqual(self.store.fts_search("fact", "Oslo", ["current"], 3)[0][0], fid)
        self.store.forget("fact", fid)
        self.assertEqual(self.store.vector_search("fact", [1., 0., 0.], ["current"], 3), [])
        self.assertEqual(self.store.fts_search("fact", "Oslo", ["current"], 3), [])

    def test_conditional_publication_rejects_stale_writer(self):
        other = Store(user_id=self.user, settings=self.settings, client=self.client)
        with self.assertRaises(WriteConflict):
            with self.store.transaction():
                self.add_fact("unpublished")
                other.set_meta("winner", "published")
        self.assertEqual(self.store.facts(), [])
        self.assertEqual(self.store.get_meta("winner"), "published")

    def test_native_compare_and_set_filter(self):
        # Native conditional filter must be enforced even when the point already exists.
        head = self.store._head()
        wrong = dict(head, revision=head["revision"] + 99)
        self.client.upsert(self.store.collection, [self.store._point(self.store._head_id, wrong)],
            update_filter=self.store._where({"key": "revision", "match": {"value": -1}}), update_mode="update_only")
        self.assertEqual(self.store._head()["revision"], head["revision"])
        same = Store(user_id=self.user, settings=self.settings, client=self.client)
        self.assertEqual(same.get_meta("revision"), str(head["revision"]))

    def test_job_lease_cas_is_independent_of_memory_publication(self):
        revision = self.store.get_meta("revision")
        job = dict(key="close", kind="close", status="queued", owner=None)
        self.store.put_record("job", "close", job)
        stale = self.store.record("job", "close")
        current = dict(stale, status="running", owner="worker-a")
        self.store.put_record("job", "close", current)
        with self.assertRaises(WriteConflict):
            self.store.put_record("job", "close", dict(stale, status="running", owner="worker-b"))
        self.assertEqual(self.store.record("job", "close")["owner"], "worker-a")
        self.assertEqual(self.store.get_meta("revision"), revision)

    def test_real_server_parallel_raw_append(self):
        errors = []
        def append(index):
            try:
                store = Store(user_id=self.user, settings=self.settings, client=self.client)
                store.append_turn(self.sid, "user", str(index), NOW, idempotency_key="parallel-%d" % index)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=append, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual([r["idx"] for r in self.store.turns(self.sid)], [0,1,2,3])

    def test_real_server_compaction_preserves_latest_state(self):
        fid = self.add_fact()
        self.store.put_vectors("fact", [(fid, [1., 0.])])
        self.store.forget("fact", fid)
        plan = cleanup_plan(self.store)
        with tempfile.TemporaryDirectory() as directory:
            apply_cleanup(self.store, plan, directory + "/backup.json", offline=True)
        self.assertEqual(self.store.fact(fid).status, FORGOTTEN)
        self.assertEqual(self.store.fact(fid).text, "")
        self.assertEqual(self.store.vector_search("fact", [1., 0.], ["current"], 3), [])


if __name__ == "__main__":
    unittest.main()
