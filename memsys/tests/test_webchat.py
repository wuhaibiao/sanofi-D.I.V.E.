"""Browser/host-Agent contracts with synthetic Qdrant, embedding and answer providers."""
import http.cookiejar
import hashlib
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

from memsys.config import MemorySettings, Models
from memsys.budget import budget_counter
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.llm import ModelError, ChatResult
from memsys.serve import MemoryProxy, MemoryService
from memsys.webchat import WebChat, WebError
from memsys.write import Writer
from tests.qdrant_fixture import Engine


class WebChatTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.store_patch = patch("memsys.core.Qdrant", return_value=self.engine)
        self.store_patch.start()
        self.env = patch.dict(os.environ, {"MEMSYS_API_TOKEN": "synthetic-admin",
            "MEMSYS_USER_TOKENS": '{"alice":"synthetic-alice","bob":"synthetic-bob"}'})
        self.env.start()
        self.chat = ScriptedChat({"answer": "A synthetic reply.\n\n```python\nprint('hello')\n```"})
        models = Models(self.chat, None, HashEmbedder(), "hash-256")
        self.service = MemoryService(models, MemorySettings(query_analysis=False, query_policy="off",
            rerank_backend="fusion", worker_count=1), "/tmp/memsys-web-functional", tick_s=3600, log=lambda _: None)
        self.server = MemoryProxy(("127.0.0.1", 0), self.service, None, web_local_user="alice")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                         urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def test_truncated_answer_is_failed_and_never_saved_as_assistant_history(self):
        web = WebChat(self.service)
        row = web.create('alice')
        self.chat.handlers['answer'] = lambda _: ChatResult('Unfinished fixture answer', 'fake', 0, 0, 0, 0, 0, 'length')
        with self.assertRaisesRegex(ModelError, 'answer did not finish'):
            web.send('alice', row['id'], 'A synthetic request.', 'fixture-truncated')
        store = self.service.req.store('alice')
        sid = store.record('web_conversation', row['id'])['session_ids'][0]
        self.assertEqual([t['role'] for t in store.turns(sid)], ['user'])
        self.assertEqual(store.record('web_request', 'fixture-truncated')['status'], 'failed')

    def test_answer_is_saved_directly_without_audit_fields_or_resampling(self):
        web=WebChat(self.service);row=web.create('alice')
        item=dict(key='F1',kind='fact',memory_kind='assistant',text='Use a cork mat.')
        memory='[F1] Use a cork mat.'
        recall=SimpleNamespace(items=[item],memory=memory,context=memory,trace={})
        self.chat.handlers['answer']='Use a cork mat.'
        with patch.object(self.service,'recall',return_value=recall):
            result=web.send('alice',row['id'],'Give me a mat recommendation.','fixture-direct')
        self.assertEqual(result['content'],'Use a cork mat.')
        stored=self.service.req.store('alice').record('web_request','fixture-direct')
        self.assertEqual(stored['status'],'completed')
        self.assertNotIn('answer_evidence',stored)
        self.assertEqual(len(stored['generation_attempts']),1)
        prompt=self.chat.calls[0][1][0]['content']
        for field in ('memory_answer','advice_task','answer_span','calculations:'):
            self.assertNotIn(field,prompt)
        self.assertEqual(self.chat.purposes(),['answer'])

    def test_both_recall_modes_generate_direct_text_without_json_mode(self):
        for include_sources in (False,True):
            with self.subTest(include_sources=include_sources):
                self.service.req.settings.recall_mode='facts_and_sources' if include_sources else 'facts'
                self.assertEqual(self.service.req.settings.include_sources,include_sources)
                web=WebChat(self.service)
                messages=[dict(role='system',content=web.system_prompt),dict(role='user',content='A fixture request.')]
                with patch.object(self.chat,'chat',wraps=self.chat.chat) as call:
                    response=web.answer(messages)
                self.assertEqual(response.text,self.chat.handlers['answer'])
                self.assertFalse(call.call_args.kwargs['json_mode'])
                self.assertEqual(len(response.generation_attempts),1)

    def test_output_growth_saves_one_complete_answer_and_preserves_request_idempotence(self):
        web=WebChat(self.service);row=web.create('alice')
        replies=iter([ChatResult('clipped','fake',0,0,0,0,0,'length'),
                      ChatResult('Complete fixture answer.','fake',0,0,0,0,0,'stop')])
        self.chat.handlers['answer']=lambda _:next(replies)
        result=web.send('alice',row['id'],'A fixture request.','fixture-growth')
        self.assertEqual(result['content'],'Complete fixture answer.')
        store=self.service.req.store('alice')
        sid=store.record('web_conversation',row['id'])['session_ids'][0]
        self.assertEqual([(t['role'],t['content']) for t in store.turns(sid)],
                         [('user','A fixture request.'),('assistant','Complete fixture answer.')])
        again=web.send('alice',row['id'],'A fixture request.','fixture-growth')
        self.assertEqual(again,result)
        self.assertEqual(self.chat.purposes(),['answer','answer'])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.service.shutdown(timeout=1)
        self.env.stop()
        self.store_patch.stop()

    def request(self, path, body=None, origin=None, raw=False, host=None):
        headers = {}
        if body is not None:
            headers = {"Content-Type": "application/json", "Origin": origin or self.url}
        if host:
            headers["Host"] = host
        request = urllib.request.Request(self.url + path, json.dumps(body).encode() if body is not None else None, headers)
        try:
            with self.opener.open(request, timeout=5) as response:
                data = response.read().decode()
                return response.status, data if raw else json.loads(data)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def connect(self):
        self.assertEqual(self.request("/chat-api/connect", {})[0], 200)

    def create(self):
        code, row = self.request("/chat-api/conversations", {})
        self.assertEqual(code, 200)
        return row["id"]

    def body(self, key, request_id="synthetic-message-1", message="hello", stream=False):
        return dict(conversation_id=key, request_id=request_id, message=message, stream=stream)

    def test_cold_ui_and_authenticated_metadata_do_not_call_models(self):
        code, html = self.request("/", raw=True)
        self.assertEqual(code, 200)
        self.assertIn("今天，想聊点什么", html)
        self.assertNotIn('id="use-memory"', html)
        self.assertNotIn("synthetic-alice", html)
        self.assertEqual(self.request("/assets/chat.js", raw=True)[0], 200)
        self.assertEqual(self.request("/chat-api/me")[0], 401)
        self.connect()
        self.assertEqual(self.request("/chat-api/me")[1]["user"], "alice")
        self.assertEqual(self.request("/chat-api/conversations")[1], {"conversations": []})
        self.assertEqual(self.request("/memsys/status")[0], 401)  # Browser cookie grants no admin API access.
        self.assertEqual(self.chat.purposes(), [])

    def test_cross_origin_and_rebinding_hosts_cannot_connect_or_write(self):
        self.assertEqual(self.request("/chat-api/connect", {}, origin="https://example.invalid")[0], 403)
        self.assertEqual(self.request("/chat-api/connect", {}, host="rebound.invalid")[0], 403)
        self.connect()
        self.assertEqual(self.request("/chat-api/conversations", {}, origin="https://example.invalid")[0], 403)
        self.assertEqual(self.chat.purposes(), [])

    def test_cookie_account_cannot_read_another_users_conversation(self):
        self.connect()
        key = self.create()
        self.assertEqual(self.request("/chat-api/connect", {"token": "synthetic-bob", "user": "alice"})[0], 200)
        self.assertEqual(self.request("/chat-api/connect", {})[0], 200)  # Page refresh preserves the connected account.
        self.assertEqual(self.request("/chat-api/me")[1]["user"], "bob")
        self.assertEqual(self.request("/chat-api/messages?id=" + key)[0], 404)
        self.assertEqual(self.request("/chat-api/send", self.body(key))[0], 404)
        self.assertEqual(self.request("/chat-api/connect", {"token": "无效令牌"})[0], 401)

    def test_reply_is_saved_and_retry_does_not_generate_again(self):
        self.connect()
        key = self.create()
        body = self.body(key)
        code, reply = self.request("/chat-api/send", body)
        self.assertEqual(code, 200)
        self.assertIn("synthetic reply", reply["content"])
        self.assertEqual(self.request("/chat-api/send", body), (200, reply))
        self.assertEqual(self.chat.purposes(), ["answer"])
        restored = WebChat(self.service).messages("alice", key)
        self.assertEqual([r["role"] for r in restored["messages"]], ["user", "assistant"])
        self.assertIsNone(restored["retry"])
        self.assertEqual(self.request("/chat-api/send", self.body(key, message="changed"))[0], 400)
        self.assertEqual(len(restored["messages"]), 2)

    def test_stream_reports_progress_and_persists_one_final_answer(self):
        self.connect()
        key = self.create()
        code, events = self.request("/chat-api/send", self.body(key, stream=True), raw=True)
        self.assertEqual(code, 200)
        self.assertIn("event: progress", events)
        self.assertIn("event: done", events)
        self.assertNotIn("event: error", events)
        self.assertEqual(self.chat.purposes(), ["answer"])

    def test_each_new_answer_retrieves_memory_even_if_old_client_sends_false(self):
        self.connect()
        key = self.create()
        with patch.object(self.service, "recall", wraps=self.service.recall) as recall:
            self.assertEqual(self.request("/chat-api/send", dict(self.body(key), use_memory=False))[0], 200)
            self.assertEqual(self.request("/chat-api/send", self.body(key, "synthetic-message-2", "another message"))[0], 200)
        self.assertEqual(recall.call_count, 2)
        self.assertEqual(self.chat.purposes(), ["answer", "answer"])

    def test_retrieval_failure_does_not_generate_without_memory(self):
        self.connect()
        key = self.create()
        with patch.object(self.service, "recall", side_effect=RuntimeError("synthetic retrieval unavailable")):
            self.assertEqual(self.request("/chat-api/send", self.body(key))[0], 503)
        self.assertEqual(self.chat.purposes(), [])
        self.assertEqual(self.request("/chat-api/messages?id=" + key)[1]["messages"][0]["content"], "hello")

    def test_legacy_failed_request_retries_with_memory_and_preserves_user_message(self):
        self.connect()
        key = self.create()
        store = self.service.req.store("alice")
        sid = store.record("web_conversation", key)["session_ids"][-1]
        request_id = "synthetic-legacy-message"
        store.append_turn(sid, "user", "hello", self.service.clock(), idempotency_key="web:" + request_id + ":user")
        store.put_record("web_request", request_id, dict(conversation_id=key, request_id=request_id,
            digest=hashlib.sha256(b"helloFalse").hexdigest(), status="failed", use_memory=False, user_message_id=sid + ":0"))
        retry = self.request("/chat-api/messages?id=" + key)[1]["retry"]
        self.assertNotIn("use_memory", retry)
        with patch.object(self.service, "recall", wraps=self.service.recall) as recall:
            self.assertEqual(self.request("/chat-api/send", retry)[0], 200)
        self.assertEqual(recall.call_count, 1)
        self.assertEqual([r["role"] for r in store.turns(sid)], ["user", "assistant"])

    def test_default_answer_prompt_uses_facts_without_other_conversation_originals(self):
        self.connect()
        first = self.create()
        sentinel = "RAW_ONLY_CROSS_CONVERSATION_SENTINEL"
        self.request("/chat-api/send", self.body(first, "fact-source-001", "I lost 10 pounds. " + sentinel))
        store = self.service.req.store("alice")
        sid = store.record("web_conversation", first)["session_ids"][-1]
        extractor = ScriptedChat({"extract": '{"facts": [{"text": "The user lost 10 pounds.", "rounds": [1]}]}'})
        Writer(store, Models(None, extractor, HashEmbedder(), "hash-256"), self.service.req.settings).consolidate(sid)
        second = self.create()
        code, reply = self.request("/chat-api/send", self.body(second, "fact-answer-001", "List all my weight loss records."))
        self.assertEqual(code, 200)
        messages = [messages for purpose, messages in self.chat.calls if purpose == "answer"][-1]
        self.assertIn("The user lost 10 pounds.", messages[0]["content"])
        self.assertNotIn(sentinel, repr(messages))
        self.assertNotIn("recent_conversation_evidence", messages[0]["content"])
        self.assertNotIn("Past conversation excerpts", messages[0]["content"])
        self.assertEqual([item["text"] for item in reply["memory_items"]], ["The user lost 10 pounds."])

    def test_default_answer_does_not_use_unprocessed_other_conversation(self):
        self.connect()
        first = self.create()
        sentinel = "UNPROCESSED_RAW_ONLY_SENTINEL"
        self.request("/chat-api/send", self.body(first, "pending-source-001", sentinel))
        second = self.create()
        code, reply = self.request("/chat-api/send", self.body(second, "pending-answer-001", "What do you remember?"))
        self.assertEqual(code, 200)
        messages = [messages for purpose, messages in self.chat.calls if purpose == "answer"][-1]
        self.assertNotIn(sentinel, repr(messages))
        self.assertEqual(reply["memory_items"], [])

    def test_failed_answer_preserves_message_and_allows_explicit_retry(self):
        def failure(_):
            raise ModelError("synthetic provider failure")
        self.chat.handlers["answer"] = failure
        self.connect()
        key = self.create()
        body = self.body(key)
        self.assertEqual(self.request("/chat-api/send", body)[0], 503)
        data = self.request("/chat-api/messages?id=" + key)[1]
        self.assertEqual([r["role"] for r in data["messages"]], ["user"])
        self.assertEqual(data["retry"]["request_id"], body["request_id"])
        self.chat.handlers["answer"] = "Recovered reply."
        self.assertEqual(self.request("/chat-api/send", body)[0], 200)
        self.assertEqual(len(self.request("/chat-api/messages?id=" + key)[1]["messages"]), 2)
        self.assertEqual(self.chat.purposes(), ["answer", "answer"])

    def test_closed_memory_segment_can_continue_as_the_same_web_conversation(self):
        self.connect()
        key = self.create()
        self.assertEqual(self.request("/chat-api/send", self.body(key))[0], 200)
        self.assertEqual(self.request("/chat-api/close", {"conversation_id": key})[0], 200)
        self.assertTrue(self.service.drain(timeout=3))
        self.assertEqual(self.request("/chat-api/send", self.body(key, "synthetic-message-2", "continue"))[0], 200)
        self.assertEqual(len(self.request("/chat-api/messages?id=" + key)[1]["messages"]), 4)
        row = self.service.req.store("alice").record("web_conversation", key)
        self.assertEqual(len(row["session_ids"]), 2)
        self.assertEqual(self.chat.purposes(), ["answer", "answer"])

    def test_new_conversation_reads_saved_rounds_while_extraction_is_running(self):
        self.service.req.settings.recall_mode = "facts_and_sources"
        entered, release = threading.Event(), threading.Event()
        def extract(_):
            entered.set()
            if not release.wait(10):
                raise AssertionError("test did not release extraction")
            return '{"facts": []}'
        self.chat.handlers["extract"] = extract
        self.service.bg.models.memory = self.chat
        self.connect()
        first = self.create()
        self.request("/chat-api/send", self.body(first, "residence-past", "我三年前住在成都"))
        self.request("/chat-api/send", self.body(first, "residence-now", "现在住在北京"))
        self.request("/chat-api/close", {"conversation_id": first})
        try:
            self.assertTrue(entered.wait(3))
            second = self.create()
            for request_id, question in [("ask-residence-past", "我三年前住在哪里"),
                                         ("ask-residence-now", "现在住在哪里")]:
                code, reply = self.request("/chat-api/send", self.body(second, request_id, question))
                self.assertEqual(code, 200)
                answer_calls = [messages for purpose, messages in self.chat.calls if purpose == "answer"]
                prompt = answer_calls[-1][0]["content"]
                self.assertIn("我三年前住在成都", prompt)
                self.assertIn("现在住在北京", prompt)
                self.assertIn("recent_conversation_evidence", prompt)
                self.assertEqual(answer_calls[-1][-1]["content"], question)
                self.assertEqual(len(reply["memory_items"]), 2)
            # Originals and references survive a WebChat/page restart, including a completed retry.
            restored = WebChat(self.service)
            self.assertEqual(len(restored.messages("alice", second)["messages"][-1]["memory_items"]), 2)
            self.assertEqual(restored.send("alice", second, "现在住在哪里", "ask-residence-now"), reply)
            self.assertEqual(self.chat.purposes().count("extract"), 1)
            self.assertEqual(self.chat.purposes().count("answer"), 4)
        finally:
            release.set()
            self.assertTrue(self.service.drain(timeout=3))

    def test_pending_capture_precedes_recall_publication_and_indexed_rounds_drop_out(self):
        self.service.req.settings.recall_mode = "facts_and_sources"
        self.connect()
        first, second = self.create(), self.create()
        store = self.service.req.store("alice")
        sid = store.record("web_conversation", first)["session_ids"][-1]
        store.append_turn(sid, "user", "I live in Oslo.", self.service.clock())
        empty = self.service.recall("alice", "Where do I live?", self.service.clock())
        self.chat.handlers["extract"] = '{"facts": [{"text": "I live in Oslo.", "rounds": [1]}]}'
        self.service.bg.models.memory = self.chat
        def publish_during_recall(*_, **kwargs):
            Writer(store, self.service.bg.models, self.service.bg.settings).consolidate(
                sid, closed_at=self.service.clock())
            return empty  # Simulate recall's earlier empty snapshot.
        with patch.object(self.service, "recall", side_effect=publish_during_recall):
            code, reply = self.request("/chat-api/send", self.body(second, "publication-race", "Where do I live?"))
        self.assertEqual(code, 200)
        self.assertIn("I live in Oslo.", self.chat.calls[-1][1][0]["content"])
        self.assertEqual(reply["memory_items"][0]["text"], "User: I live in Oslo.")
        self.assertEqual(self.server.web_chat.pending_rounds(store, second), [])
        self.assertEqual(self.request("/chat-api/send", self.body(second, "after-publication", "Where do I live?"))[0], 200)
        prompt = self.chat.calls[-1][1][0]["content"]
        self.assertIn("I live in Oslo.", prompt)
        self.assertNotIn("recent_conversation_evidence", prompt)

    def test_pending_originals_respect_account_exclusions_and_safe_content(self):
        self.service.req.settings.recall_mode = "facts_and_sources"
        web = self.server.web_chat
        for user, texts in [("alice", ["excluded statement", "private original", "pending safe statement"]),
                            ("bob", ["Bob's private statement"])]:
            first = web.create(user)["id"]
            store = self.service.req.store(user)
            sid = store.record("web_conversation", first)["session_ids"][-1]
            for text in texts:
                store.append_turn(sid, "user", text, self.service.clock())
            if user == "alice":
                turn = store.turns(sid)[1]
                turn["safe_content"] = "sanitized statement"
                store.put_record("turn", sid + ":1", turn)
                store.put_record("conversation_exclusion", sid, {"upto_turn": 1})
        current = web.create("alice")["id"]
        web = WebChat(self.service)  # Evidence comes from persistent state, not an in-memory cache.
        self.assertEqual(web.send("alice", current, "What did I say?", "sanitized-recall")["content"],
                         self.chat.handlers["answer"])
        prompt = self.chat.calls[-1][1][0]["content"]
        self.assertIn("sanitized statement", prompt)
        self.assertIn("pending safe statement", prompt)
        self.assertNotIn("excluded statement", prompt)
        self.assertNotIn("private original", prompt)
        self.assertNotIn("Bob's private statement", prompt)

    def test_recent_evidence_keeps_whole_rounds_within_input_budget(self):
        self.service.req.settings.recall_mode = "facts_and_sources"
        web = self.server.web_chat
        self.service.req.settings.context_budget_tokens = 1000
        self.service.req.settings.answer_input_budget_tokens = 1100
        counter = budget_counter(self.service.req.settings)
        original = [dict(role="system", content="system"), dict(role="user", content="question")]
        rounds = [dict(recorded_at="2026-10-13T10:00:00", user="oversized " * 1000, assistant="reply"),
                  dict(recorded_at="2026-10-13T09:00:00", user="I live in Oslo.", assistant="Noted.")]
        messages, references = web.include_pending(original, rounds, counter)
        self.assertLessEqual(counter.messages(messages), 1100)
        self.assertIn("I live in Oslo.", messages[0]["content"])
        self.assertNotIn("oversized", messages[0]["content"])
        self.assertEqual(references, [dict(text="User: I live in Oslo.\nAssistant: Noted.", date="2026-10-13")])
        self.assertEqual(messages[-1], original[-1])

    def test_failed_extraction_keeps_originals_readable_without_model_retry(self):
        self.service.req.settings.recall_mode = "facts_and_sources"
        def rejected(_):
            raise ModelError("synthetic extraction rejected", status=400)
        self.service.bg.models.memory = self.chat
        self.chat.handlers["extract"] = rejected
        self.connect()
        first = self.create()
        self.assertEqual(self.request("/chat-api/send", self.body(first, "failed-extract-source", "I live in Oslo."))[0], 200)
        self.request("/chat-api/close", {"conversation_id": first})
        self.assertTrue(self.service.drain(timeout=3))
        self.assertEqual(self.service.req.store("alice").records("job")[0]["status"], "failed")
        second = self.create()
        self.assertEqual(self.request("/chat-api/send", self.body(second, "failed-extract-question", "Where do I live?"))[0], 200)
        self.assertIn("I live in Oslo.", self.chat.calls[-1][1][0]["content"])
        self.assertEqual(self.chat.purposes().count("extract"), 1)

    def test_early_fact_in_open_chat_survives_whole_round_window_without_duplicate_excerpt(self):
        self.service.req.settings.recall_mode = "facts_and_sources"
        self.connect()
        key = self.create()
        store = self.service.req.store("alice")
        sid = store.record("web_conversation", key)["session_ids"][-1]
        for index in range(8):
            user = "我的猫叫山椒。" if index == 0 else "这是一轮普通闲聊 %d。" % index
            store.append_turn(sid, "user", user, self.service.clock())
            store.append_turn(sid, "assistant", "收到。", self.service.clock())
        code, reply = self.request("/chat-api/send", self.body(key, "open-chat-early-fact", "我的猫叫什么？"))
        self.assertEqual(code, 200)
        messages = self.chat.calls[-1][1]
        self.assertIn("我的猫叫山椒。", '\n'.join(m['content'] for m in messages))
        self.assertEqual(sum(m['content'].count('我的猫叫山椒。') for m in messages), 1)
        self.assertEqual(store.session(sid)["turns_done"], 0)
        self.assertEqual(store.records("job"), [])
        self.assertFalse(any("山椒" in item["text"] for item in reply["memory_items"]))

    def test_visible_memory_state_distinguishes_saved_originals_and_ready_index(self):
        self.connect()
        key = self.create()
        self.assertEqual(self.request("/chat-api/send", self.body(key))[0], 200)
        memory = self.request("/chat-api/messages?id=" + key)[1]["memory"]
        self.assertEqual((memory["status"], memory["saved_messages"], memory["processed_messages"]), ("pending", 2, 0))
        self.assertEqual(self.request("/chat-api/conversations")[1]["conversations"][0]["memory"], memory)
        self.request("/chat-api/close", {"conversation_id": key})
        self.assertTrue(self.service.drain(timeout=3))
        memory = self.request("/chat-api/messages?id=" + key)[1]["memory"]
        self.assertEqual((memory["status"], memory["saved_messages"], memory["processed_messages"]), ("ready", 2, 2))
        self.assertFalse(memory["can_retry"])
        self.assertGreaterEqual(memory["last_duration_s"], 0)

    def test_failed_memory_is_visible_and_retry_is_explicit_and_account_scoped(self):
        def failed(_):
            raise ModelError("synthetic extraction rejected", status=400)
        self.service.bg.models.memory = self.chat
        self.chat.handlers["extract"] = failed
        self.connect()
        key = self.create()
        self.request("/chat-api/send", self.body(key, "status-source-message", "I live in Oslo."))
        self.request("/chat-api/close", {"conversation_id": key})
        self.assertTrue(self.service.drain(timeout=3))
        memory = self.request("/chat-api/messages?id=" + key)[1]["memory"]
        self.assertEqual(memory["status"], "failed")
        self.assertTrue(memory["can_retry"])
        self.assertEqual(self.chat.purposes().count("extract"), 1)
        self.request("/chat-api/connect", {"token": "synthetic-bob"})
        self.assertEqual(self.request("/chat-api/retry-memory", {"conversation_id": key})[0], 404)
        self.request("/chat-api/connect", {"token": "synthetic-alice"})
        self.chat.handlers["extract"] = '{"facts": [{"text": "I live in Oslo.", "rounds": [1]}]}'
        self.assertEqual(self.request("/chat-api/retry-memory", {"conversation_id": key}), (200, {"ok": True, "retried": 1}))
        self.assertTrue(self.service.drain(timeout=3))
        memory = self.request("/chat-api/messages?id=" + key)[1]["memory"]
        self.assertEqual(memory["status"], "ready")
        self.assertFalse(memory["can_retry"])
        self.assertEqual(self.chat.purposes().count("extract"), 2)
        self.assertEqual(self.request("/chat-api/retry-memory", {"conversation_id": key})[0], 400)

    def test_rename_archive_and_concurrent_change(self):
        self.connect()
        key = self.create()
        self.assertEqual(self.request("/chat-api/rename", {"conversation_id": key, "title": "renamed"})[0], 200)
        self.assertEqual(self.request("/chat-api/conversations")[1]["conversations"][0]["title"], "renamed")
        lock = self.server.web_chat.lock("alice", key)
        lock.acquire()
        try:
            self.assertEqual(self.request("/chat-api/archive", {"conversation_id": key})[0], 409)
        finally:
            lock.release()
        self.assertEqual(self.request("/chat-api/archive", {"conversation_id": key})[0], 200)
        self.assertEqual(self.request("/chat-api/conversations")[1]["conversations"], [])
        self.assertEqual(self.chat.purposes(), [])


if __name__ == "__main__":
    unittest.main()
