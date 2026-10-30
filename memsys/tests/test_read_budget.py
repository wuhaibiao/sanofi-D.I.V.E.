"""Replay outages and deadlines through actual HTTP clients and read routes."""
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from helpers import settings
from memsys.bench.runner import Runner
from memsys.cli import _fake_models
from memsys.config import MemorySettings, Models
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.llm import ModelConfig, ModelError, OpenAICompatClient
from memsys.protocols import AnthropicMessagesClient, ResponsesClient
from memsys.read_budget import ReadBudget, ReadExecutionError, current_read_budget, read_scope
from memsys.read_retry import ReadTransportChat
from test_llm import FakeServer, HI, chat_reply
from test_protocols import message_reply, response_reply


def outage():
    return ModelError('HTTP429: server_error: Endpoint is unavailable.', status=429)


class ReadBudgetTest(unittest.TestCase):
    def test_http_adapters_have_no_nested_retries_and_writer_keeps_its_policy(self):
        for cls, reply in [(OpenAICompatClient, chat_reply('OK')),
                           (ResponsesClient, response_reply()), (AnthropicMessagesClient, message_reply())]:
            with self.subTest(client=cls.__name__):
                server = FakeServer()
                self.addCleanup(server.close)
                client = cls(ModelConfig('test', server.base_url, max_retries=3))
                server.responses = [(429, 'Endpoint is unavailable.', {})] * 4 + [(200, reply, {})]
                budget = ReadBudget(retry_wait_s=0)
                wrapper = ReadTransportChat(client, budget)
                with self.assertRaises(ModelError):
                    wrapper.chat(HI, purpose='answer')
                self.assertEqual(len(server.requests), 2)
                self.assertEqual(budget.additional_retries, 1)
                with patch('memsys.llm.time.sleep'):
                    result = client.chat(HI, purpose='extract')
                self.assertEqual(result.text, 'OK')
                self.assertEqual(len(server.requests), 5)
                self.assertEqual(client.config.max_retries, 3)
                self.assertIsNone(current_read_budget())

    def test_query_answer_and_review_share_two_retries(self):
        def respond(_messages):
            raise outage()
        model = ScriptedChat({'query': respond, 'answer': respond, 'answer_review': respond})
        budget = ReadBudget(retry_wait_s=0)
        reader = ReadTransportChat(model, budget)
        for purpose in ('query', 'answer', 'answer_review'):
            with self.assertRaises(ModelError):
                reader.chat(HI, purpose=purpose)
        self.assertEqual(model.purposes(), ['query', 'query', 'answer', 'answer', 'answer_review'])
        self.assertEqual(budget.additional_retries, 2)
        self.assertEqual(len([e for e in budget.events if e['kind'] == 'network_attempt']), 5)

    def test_remaining_deadline_caps_timeout_and_never_accepts_a_late_answer(self):
        now = [10.0]
        budget = ReadBudget(timeout_s=180, clock=lambda: now[0])
        seen = []
        def send(timeout):
            seen.append(timeout)
            now[0] += 181
            return 'late answer'
        now[0] += 90
        with self.assertRaises(ReadExecutionError) as raised:
            budget.call(send, purpose='answer', timeout_s=120)
        self.assertEqual(raised.exception.read_state, 'timeout')
        self.assertEqual(seen, [90.0])
        self.assertEqual(budget.additional_retries, 0)

    def test_retry_wait_cannot_consume_the_rest_of_the_deadline(self):
        budget = ReadBudget(timeout_s=10, sleeper=lambda _: self.fail('must not wait'))
        with self.assertRaises(ReadExecutionError):
            budget.call(lambda _: (_ for _ in ()).throw(outage()), purpose='answer')
        self.assertEqual(budget.additional_retries, 0)

    def test_cancel_during_wait_stops_before_next_request(self):
        stop = threading.Event()
        budget = ReadBudget(cancel_event=stop, sleeper=lambda _: stop.set())
        sends = []
        def send(_):
            sends.append(1)
            raise outage()
        with self.assertRaises(ReadExecutionError) as raised:
            budget.call(send, purpose='answer')
        self.assertEqual(raised.exception.read_state, 'cancelled')
        self.assertEqual(len(sends), 1)
        self.assertIn('elapsed_s', budget.events[-1])

    def test_slow_response_chunks_cannot_extend_deadline(self):
        now = [0.0]
        budget = ReadBudget(timeout_s=2, clock=lambda: now[0])
        class Stream:
            def read1(self, _):
                now[0] += 0.8
                return b'x'
        with self.assertRaises(ReadExecutionError):
            OpenAICompatClient._read_bounded(Stream(), budget)
        self.assertLess(now[0], 3)

    def test_deadline_propagates_from_query_instead_of_using_heuristic_fallback(self):
        model = ScriptedChat({'query': lambda _: (_ for _ in ()).throw(
            ReadExecutionError('expired', 'timeout'))})
        system = MemorySystem(Models(model, model, HashEmbedder(), 'hash'), settings())
        self.addCleanup(system.close)
        with self.assertRaises(ReadExecutionError) as raised:
            system.respond('u', 'How many maps?', datetime(2025, 1, 1))
        self.assertEqual(model.purposes(), ['query'])
        self.assertEqual(raised.exception.read_state, 'timeout')
        self.assertIn('events', raised.exception.read_transport)

    def test_http_failure_retains_pending_state_and_failure_trace(self):
        server = FakeServer()
        self.addCleanup(server.close)
        server.responses = [(503, 'down', {})] * 2
        model = OpenAICompatClient(ModelConfig('test', server.base_url, max_retries=9))
        with tempfile.TemporaryDirectory() as out:
            system = MemorySystem(Models(model, None, HashEmbedder(), 'hash'),
                                  settings(query_analysis=False, semantic_validation=False, read_retry_wait_s=0),
                                  trace_dir=out)
            self.addCleanup(system.close)
            with self.assertRaises(ModelError) as raised:
                system.respond('u', 'What was the color?', datetime(2025, 1, 1), tags={'question_id': 'pending'})
            self.assertEqual(raised.exception.read_state, 'service_error')
            trace = json.loads((Path(out)/'pending.json').read_text())
            self.assertFalse(trace['completed'])
            self.assertNotIn('answer', trace)
            self.assertEqual(len(server.requests), 2)
            self.assertTrue(all('elapsed_s' in e for e in trace['read_transport']['events']))

    def test_two_consecutive_service_failures_stop_queued_items_and_keep_existing_answers(self):
        from memsys.bench.longmemeval import load
        items = [i for i, _ in load(str(Path(__file__).parent/'fixtures/mini_oracle.json'))]
        def fail(_):
            raise outage()
        models = _fake_models()
        models.chat = ScriptedChat({'answer_no_memory': fail})
        with tempfile.TemporaryDirectory() as out:
            first = {'question_id':items[0].question_id, 'hypothesis':'already saved'}
            (Path(out)/'hypotheses.jsonl').write_text(json.dumps(first)+'\n')
            runner = Runner(models, settings(read_retry_wait_s=0), out, mode='no-memory')
            counts = runner.run(items)
            self.assertEqual(counts, dict(answered=0, errors=2, skipped=1, not_started=1))
            self.assertEqual(json.loads((Path(out)/'hypotheses.jsonl').read_text()), first)
            execution = json.loads((Path(out)/'execution.json').read_text())
            self.assertEqual(execution['status'], 'incomplete')
            self.assertEqual(execution['completed'], 1)
            self.assertEqual(len(models.chat.calls), 4)

    def test_read_policy_changes_do_not_change_write_settings_fingerprint(self):
        self.assertEqual(settings().write_fingerprint(), settings(read_timeout_s=90, read_retries_total=0).write_fingerprint())
        self.assertNotEqual(settings().read_fingerprint(), settings(read_timeout_s=90).read_fingerprint())
        for value in (0, -1, float('inf'), float('nan'), True):
            with self.assertRaises(ValueError):
                MemorySettings.from_dict({'read_timeout_s': value})

    def test_review_failure_preserves_draft_and_retrieved_evidence_without_publishing_answer(self):
        model=ScriptedChat({'answer':'ANSWER: amber',
            'answer_review':lambda _: (_ for _ in ()).throw(outage())})
        with tempfile.TemporaryDirectory() as out:
            system=MemorySystem(Models(model,None,HashEmbedder(),'hash'),
                settings(query_analysis=False,semantic_validation=True,read_retry_wait_s=0),trace_dir=out)
            self.addCleanup(system.close)
            at=datetime(2025,5,1)
            handle=system.open_session('u',at)
            system.add_turn(handle,'user','I chose amber as the color.',at)
            system.close_session(handle,at)
            with self.assertRaises(ModelError):
                system.respond('u','What color did I choose?',datetime(2025,5,2),tags={'question_id':'review-failure'})
            trace=json.loads((Path(out)/'review-failure.json').read_text())
            self.assertFalse(trace['completed'])
            self.assertIn('I chose amber',trace['memory'])
            self.assertNotIn('answer',trace)
            self.assertEqual(trace['read_outputs'],[dict(purpose='answer',model='fake',finish_reason='stop',text='ANSWER: amber')])


if __name__ == '__main__':
    unittest.main()
