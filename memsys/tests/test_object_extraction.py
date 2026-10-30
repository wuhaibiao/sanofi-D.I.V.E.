"""Named-field extraction, whole-batch integrity and configurable output space."""
import json
import unittest
from datetime import datetime

from memsys import prompts
from memsys.config import MemorySettings, Models
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.llm import BadJSONError, ChatResult, IncompleteJSONError, ModelError
from memsys.qdrant import QdrantSettings
from memsys.store import Store
from memsys.write import Writer
from tests.qdrant_fixture import Engine

AT = datetime(2026, 9, 6)


class CapturedChat(ScriptedChat):
    def __init__(self, reply):
        super().__init__({'extract': reply})
        self.requests = []

    def chat(self, messages, **kwargs):
        self.requests.append(kwargs)
        return super().chat(messages, **kwargs)


def observation(index=0):
    return dict(text='Station %d received a sealed kit.' % index, kind='event',
        subject='station_%d' % index, attribute='received_kit', rounds=[1],
        value={'container': 'crate-%d' % index, 'count': index + 3, 'conditions': ['sealed', 'indoors']},
        semantics={'value_type': 'event_quantity', 'event_status': 'completed'})


class ObjectExtractionTests(unittest.TestCase):
    def writer(self, reply, *, limit=32768, ceiling=None):
        store = Store(user_id='object-fixture', client=Engine(),
                      settings=QdrantSettings(namespace='named-object-synthetic'))
        model = CapturedChat(reply)
        writer = Writer(store, Models(None, model, HashEmbedder(), 'hash-256'),
                        MemorySettings(rerank_backend='fusion', extract_max_tokens=limit,
                                       extract_max_tokens_limit=ceiling or limit))
        sid = store.create_session(AT)
        store.append_turn(sid, 'user', '\n'.join(observation(n)['text'] for n in range(100)), AT)
        return writer, store, model, sid

    def test_prompt_restored_to_previous_named_object_contract(self):
        self.assertIn('Each fact item has:', prompts.EXTRACT_USER)
        self.assertNotIn('fact_fields', prompts.EXTRACT_USER)
        self.assertNotIn('row arrays', prompts.EXTRACT_USER)

    def test_optional_fields_and_key_order_do_not_shift_values(self):
        facts = [observation(n) for n in range(100)]
        facts[1] = dict(reversed(list(facts[1].items())))
        facts[2]['event_date'] = None
        facts[2]['relations'] = []
        writer, store, model, sid = self.writer(json.dumps({'entities': [], 'facts': facts}))
        report = writer.consolidate(sid, closed_at=AT)
        self.assertEqual(len(store.facts()), 100)
        self.assertEqual(report.extract_output_format, 'objects')
        self.assertEqual(model.purposes(), ['extract'])
        self.assertEqual(model.requests[0]['max_tokens'], 32768)
        self.assertEqual(report.extract_output_limit, 32768)
        self.assertEqual(store.session(sid)['turns_done'], 1)
        for fact in store.facts():
            value = json.loads(fact.value)
            self.assertEqual(value['conditions'], ['sealed', 'indoors'])
            self.assertEqual(fact.semantics['event_status'], 'completed')

    def test_invalid_late_fact_is_isolated_from_earlier_valid_fact(self):
        writer, store, model, sid = self.writer(json.dumps({'facts': [observation(), {'text': 123, 'rounds': [1]}]}))
        report = writer.consolidate(sid, closed_at=AT)
        self.assertEqual(len(store.facts()), 1)
        self.assertEqual(report.extract_rejected_count, 1)
        self.assertFalse(report.extract_protocol_complete)
        self.assertEqual(store.session(sid)['turns_done'], 1)
        self.assertEqual(model.purposes(), ['extract'])

    def test_positional_rows_are_rejected_without_guessing_columns(self):
        writer, store, model, sid = self.writer(json.dumps({'facts': [['A fixture', 'event', [1]]]}))
        report = writer.consolidate(sid, closed_at=AT)
        self.assertEqual(store.facts(), [])
        self.assertEqual(report.extract_rejected_count, 1)
        self.assertFalse(report.extract_protocol_complete)
        self.assertEqual(model.purposes(), ['extract'])

    def test_length_stop_even_with_valid_json_keeps_originals_and_watermark(self):
        reply = ChatResult(json.dumps({'facts': [observation()]}), 'authored', 0, 32768, 0, 0, 0, 'length')
        writer, store, model, sid = self.writer(lambda _: reply)
        with self.assertRaises(IncompleteJSONError) as raised:
            writer.consolidate(sid, closed_at=AT)
        self.assertEqual(raised.exception.output_limit, 32768)
        self.assertEqual(raised.exception.completion_tokens, 32768)
        self.assert_unpublished(store, model, sid)

    def test_broken_json_is_not_salvaged_or_reextracted(self):
        writer, store, model, sid = self.writer('{"facts":[')
        with self.assertRaises(BadJSONError):
            writer.consolidate(sid, closed_at=AT)
        self.assert_unpublished(store, model, sid)

    def test_output_growth_retries_the_whole_batch_and_publishes_only_complete_facts(self):
        replies=iter([ChatResult('{"facts":[', 'authored', 1, 32768, 0, 0, 0, 'length'),
                      ChatResult(json.dumps({'facts':[observation()]}), 'authored', 1, 100, 0, 0, 0, 'stop')])
        writer,store,model,sid=self.writer(lambda _:next(replies),ceiling=65536)
        report=writer.consolidate(sid,closed_at=AT)
        self.assertEqual([r['max_tokens'] for r in model.requests],[32768,65536])
        self.assertEqual(model.calls[0][1],model.calls[1][1])
        self.assertEqual(len(store.facts()),1)
        self.assertEqual(report.extract_calls,2)
        self.assertEqual(report.extract_output_limit,65536)
        self.assertEqual(report.extract_attempts[0]['finish_reason'],'length')
        self.assertEqual(store.session(sid)['turns_done'],1)

    def test_explicit_smaller_provider_budget_still_respected(self):
        writer, store, model, sid = self.writer('{"facts":[]}', limit=4096)
        report = writer.consolidate(sid, closed_at=AT)
        self.assertEqual(model.requests[0]['max_tokens'], 4096)
        self.assertEqual(report.extract_output_limit, 4096)

    def assert_unpublished(self, store, model, sid):
        self.assertEqual(store.facts(), [])
        self.assertEqual(store.episodes(), [])
        self.assertEqual(store.session(sid)['turns_done'], 0)
        self.assertEqual(len(store.turns(sid)), 1)
        self.assertEqual(model.purposes(), ['extract'])
