"""Local HTTP routing/auth/remote rerank contract checks with synthetic providers."""
import json
import copy
import os
import threading
import urllib.error
import urllib.request
import unittest
from dataclasses import asdict
from unittest.mock import patch
from memsys.config import MemorySettings, Models
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.llm import ChatResult, ModelError
from memsys.core import MemorySystem
from memsys.serve import MemoryService, MemoryProxy
from memsys.store import Store
from memsys.remote_rerank import RemoteReranker
from memsys.reranking import RerankDocument, RerankFailure
from memsys.agent_adapter import AgentAdapter
from tests.qdrant_fixture import Engine
from tests.test_qdrant_storage import NOW


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        settings = MemorySettings(query_analysis=False, rerank_backend="fusion", worker_count=1)
        models = Models(None, None, HashEmbedder(), "hash-256")
        self.store_patch = patch("memsys.core.Qdrant", return_value=self.engine)
        self.store_patch.start()
        self.service = MemoryService(models, settings, "/tmp/memsys-http-functional", tick_s=3600, log=lambda text: None)
        self.env = patch.dict(os.environ, {"MEMSYS_API_TOKEN": "admin-secret", "MEMSYS_USER_TOKENS": '{"alice":"alice-secret","bob":"bob-secret"}'})
        self.env.start()
        self.server = MemoryProxy(("127.0.0.1", 0), self.service, None)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.service.shutdown(timeout=1)
        self.env.stop()
        self.store_patch.stop()

    def request(self, path, body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Memsys-Token"] = token
        req = urllib.request.Request(self.url + path, json.dumps(body).encode() if body is not None else None, headers)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_public_health_has_no_user_data_and_other_routes_require_token(self):
        self.assertEqual(self.request("/healthz"), (200, {"status": "ready"}))
        self.assertEqual(self.request("/memsys/status")[0], 401)
        self.assertEqual(self.request("/memsys/recall", {"user":"alice","query":"q"})[0], 401)

    def test_tenant_bound_token_rejects_spoofed_user(self):
        self.assertEqual(self.request("/memsys/recall", {"user":"bob","query":"q"}, "alice-secret")[0], 403)
        self.assertEqual(self.request("/memsys/recall", {"user":"alice","query":"q"}, "alice-secret")[0], 200)

    def test_explicit_session_record_and_retry(self):
        code, opened = self.request("/memsys/sessions", {"user":"alice","external_id":"agent-session"}, "alice-secret")
        self.assertEqual(code, 200)
        sid = opened["session_id"]
        body = {"user":"alice", "session_id":sid, "turns":[{"role":"user","content":"hello","idempotency_key":"evt"}]}
        self.assertEqual(self.request("/memsys/turns", body, "alice-secret")[0], 200)
        self.assertEqual(self.request("/memsys/turns", body, "alice-secret")[0], 200)
        self.assertEqual(len(self.service.req.store("alice").turns(sid)), 1)
        code, _ = self.request("/memsys/recall", {"user":"alice","query":"q"}, "alice-secret")
        self.assertEqual(code, 200)
        self.assertEqual(self.service.req.store("alice").session(sid)["status"], "open")

    def test_durable_close_job_completes(self):
        _, opened = self.request("/memsys/sessions", {"user":"alice"}, "alice-secret")
        sid = opened["session_id"]
        self.request("/memsys/turns", {"user":"alice","session_id":sid,"turns":[{"role":"user","content":"Remember I live in Oslo","idempotency_key":"e1"}]}, "alice-secret")
        code, _ = self.request("/memsys/end", {"user":"alice","session_id":sid}, "alice-secret")
        self.assertEqual(code, 200)
        self.assertTrue(self.service.drain(timeout=3))
        store = self.service.req.store("alice")
        self.assertEqual(store.session(sid)["status"], "closed")
        self.assertEqual(store.records("job")[0]["status"], "completed")

    def test_timestamp_rounding_does_not_strand_a_claimed_extraction(self):
        model = ScriptedChat({"extract": '{"facts": [{"text": "I live in Oslo.", "rounds": [1]}]}'})
        self.service.bg.models.memory = model
        upsert = self.engine.upsert
        rounded = []
        def server_rounding(collection, points, **kwargs):
            points = copy.deepcopy(points)
            for point in points:
                if point["payload"].get("kind") == "job":
                    data = point["payload"]["data"]
                    for key, value in data.items():
                        if isinstance(value, float):
                            result = float(format(value, ".12g"))
                            if result != value:
                                rounded.append(key)
                            data[key] = result
            return upsert(collection, points, **kwargs)
        with patch.object(self.engine, "upsert", side_effect=server_rounding):
            _, opened = self.request("/memsys/sessions", {"user": "alice"}, "alice-secret")
            sid = opened["session_id"]
            self.request("/memsys/turns", {"user": "alice", "session_id": sid,
                "turns": [{"role": "user", "content": "I live in Oslo."}]}, "alice-secret")
            self.assertEqual(self.request("/memsys/end", {"user": "alice", "session_id": sid}, "alice-secret")[0], 200)
            self.assertTrue(self.service.drain(timeout=3))
        store = self.service.req.store("alice")
        job = store.records("job")[0]
        self.assertIn("lease_until", rounded)
        self.assertEqual((job["status"], job["attempts"], job["not_before"]), ("completed", 0, 0))
        self.assertTrue(all(job.get(k) for k in ("queued_at", "started_at", "completed_at")))
        self.assertEqual(model.purposes(), ["extract"])
        self.assertEqual(store.session(sid)["status"], "closed")
        self.assertEqual(len(store.facts()), 1)

    def test_status_shows_explicit_sessions_and_only_bound_user(self):
        self.request("/memsys/sessions", {"user":"alice"}, "alice-secret")
        self.request("/memsys/sessions", {"user":"bob"}, "bob-secret")
        code, status = self.request("/memsys/status", token="alice-secret")
        self.assertEqual(code, 200)
        self.assertEqual(set(status["users"]), {"alice"})
        self.assertEqual(len(status["users"]["alice"]["sessions"]), 1)

    def test_end_request_closes_append_before_background_completion(self):
        _, opened = self.request("/memsys/sessions", {"user":"alice"}, "alice-secret")
        sid = opened["session_id"]
        self.service.req.store("alice").request_close(sid, NOW)
        code, _ = self.request("/memsys/turns", {"user":"alice", "session_id":sid,
                    "turns":[{"role":"user","content":"late","idempotency_key":"late"}]}, "alice-secret")
        self.assertEqual(code, 400)

    def test_restart_recovers_task_created_after_close_intent(self):
        from memsys.serve import Job
        store = self.service.req.store("alice")
        sid = store.create_session(NOW, external_id="recovery-case")
        store.append_turn(sid,"user","Remember I live in Oslo",NOW)
        # Simulate interruption before enqueue: only the durable close intent exists.
        store.request_close(sid,NOW)
        self.service._tick()
        self.assertTrue(self.service.drain(timeout=3))
        self.assertEqual(store.session(sid)["status"], "closed")
        self.assertEqual(store.records("job")[0]["status"], "completed")

    def test_example_agent_calls_final_generation_once(self):
        purposes = []
        class Chat:
            def chat(self, messages, **kwargs):
                purposes.append(kwargs["purpose"])
                return ChatResult("NOTES: none\nANSWER: hello", "synthetic", 0, 0, 0, 0.0, 0.0, "stop")
        reply = AgentAdapter(self.service.req, Chat()).respond("alice", "q", NOW)
        self.assertEqual(purposes, ["answer"])
        self.assertEqual(reply.text, "hello")

    def test_context_limit_failure_stops_until_explicit_retry(self):
        def rejected(_):
            raise ModelError("synthetic context length exceeded", status=400)
        chat = ScriptedChat({"extract": rejected})
        self.service.bg.models.memory = chat
        _, opened = self.request("/memsys/sessions", {"user": "alice"}, "alice-secret")
        sid = opened["session_id"]
        self.request("/memsys/turns", {"user": "alice", "session_id": sid,
            "turns": [{"role": "user", "content": "I live in Oslo. " * 1000, "idempotency_key": "long-batch"}]}, "alice-secret")
        self.assertEqual(self.request("/memsys/end", {"user": "alice", "session_id": sid}, "alice-secret")[0], 200)
        self.assertTrue(self.service.drain(timeout=3))
        store = self.service.req.store("alice")
        job = store.records("job")[0]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_type"], "ModelError")
        self.assertEqual(store.session(sid)["turns_done"], 0)
        self.assertEqual(len(store.turns(sid)), 1)
        self.assertEqual(store.facts(), [])
        self.service._tick()
        self.assertTrue(self.service.drain(timeout=3))
        self.assertEqual(chat.purposes(), ["extract"])
        _, status = self.request("/memsys/status", token="alice-secret")
        self.assertEqual((status["queued"], status["failed"]), (0, 1))
        self.assertEqual(self.request("/memsys/retry", {"user": "bob", "job_key": job["key"]}, "bob-secret")[0], 400)
        chat.handlers["extract"] = '{"facts": [{"text": "The user lives in Oslo.", "rounds": [1]}]}'
        self.assertEqual(self.request("/memsys/retry", {"user": "alice", "job_key": job["key"]}, "alice-secret")[0], 200)
        self.assertTrue(self.service.drain(timeout=3))
        self.assertEqual(chat.purposes(), ["extract", "extract"])
        self.assertEqual(store.record("job", job["key"])["status"], "completed")
        self.assertEqual(store.session(sid)["status"], "closed")
        self.assertEqual(len(store.facts()), 1)

    def test_truncated_response_is_a_failed_batch_without_partial_commit(self):
        truncated = ChatResult('{"facts": [', "synthetic", 0, 0, 0, 0, 0, "length")
        chat = ScriptedChat({"extract": lambda _: truncated})
        self.service.bg.models.memory = chat
        _, opened = self.request("/memsys/sessions", {"user": "alice"}, "alice-secret")
        sid = opened["session_id"]
        turns = [{"role": "user", "content": "I live in Oslo.", "idempotency_key": "first"},
                 {"role": "assistant", "content": "Noted.", "idempotency_key": "reply"},
                 {"role": "user", "content": "I like tea.", "idempotency_key": "second"}]
        self.request("/memsys/turns", {"user": "alice", "session_id": sid, "turns": turns}, "alice-secret")
        self.request("/memsys/end", {"user": "alice", "session_id": sid}, "alice-secret")
        self.assertTrue(self.service.drain(timeout=3))
        store = self.service.req.store("alice")
        self.assertEqual(store.records("job")[0]["status"], "failed")
        self.assertEqual((store.facts(), store.episodes(), store.session(sid)["turns_done"]), ([], [], 0))
        self.service._tick()
        self.assertTrue(self.service.drain(timeout=3))
        self.assertEqual(chat.purposes(), ["extract"])


if __name__ == "__main__":
    unittest.main()
