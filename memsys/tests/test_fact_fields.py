"""Field retention and atomic budget behavior using synthetic memories."""
import json
import unittest
from datetime import datetime
from types import SimpleNamespace

from memsys.budget import TokenCounter
from memsys.config import MemorySettings, Models
from memsys.core import MemorySystem
from memsys.fact_content import fact_text, value_text
from memsys.fake import HashEmbedder
from memsys.qdrant import QdrantSettings
from memsys.read import Selection, fit_to_budget, render_memory
from memsys.store import Store
from tests.qdrant_fixture import Engine

NOW = datetime(2026, 10, 13, 12)


class FactFieldTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.settings = MemorySettings(rerank_backend='fusion', max_history_share=0)
        self.system = MemorySystem(Models(None, None, HashEmbedder(), 'hash-256'), self.settings,
            storage=QdrantSettings(namespace='field-retention'),
            store_factory=lambda **kw: Store(client=self.engine, **kw))
        self.store = self.system.store('synthetic')
        self.sid = self.store.create_session(NOW)

    def add(self, text, value, semantics=None):
        fid = self.store.add_fact(text=text, value=value, kind='assistant', subject='team_fixture',
            attribute='schedule', event=None, recorded_at=NOW, valid_from=NOW.date(),
            session_id=self.sid, importance=.8, semantics=semantics)
        return self.store.fact(fid)

    def test_existing_literal_is_not_repeated_and_different_number_is_retained(self):
        self.assertEqual(fact_text(SimpleNamespace(text='The sample weighs 100 grams.', value='100 grams')),
                         'The sample weighs 100 grams.')
        self.assertIn('Value: 10', fact_text(SimpleNamespace(text='The sample weighs 100 grams.', value='10')))

    def test_structured_values_preserve_order_and_unicode_without_repr(self):
        value={'day': '周五', 'assignments': ['member_fixture_2', 'member_fixture_1']}
        text=value_text(value)
        self.assertEqual(json.loads(text), value)
        self.assertNotIn("'day'", text)
        self.assertIn('周五', text)

    def test_full_stored_value_and_conditions_reach_context_and_api(self):
        value=json.dumps({'Friday': ['member_fixture_2', 'member_fixture_1']})
        f=self.add('The assistant supplied a rotation.', value,
                   {'value_type':'other','qualifiers':['shift order: 06:00–14:00,14:00–22:00']})
        recalled=self.system.recall('synthetic','List all team rotations.',NOW,budget=2000,save_trace=False)
        self.assertIn(value, recalled.context)
        self.assertIn('06:00–14:00', recalled.context)
        self.assertEqual(recalled.items[0]['value'], value)
        self.assertEqual(recalled.items[0]['memory_kind'], 'assistant')
        self.assertEqual(recalled.items[0]['subject'], f.subject)
        self.assertEqual(recalled.items[0]['sentence'], f.text)

    def test_value_is_whole_or_fact_is_omitted_at_the_actual_budget(self):
        value='structured_payload_fixture_'*30+'FINAL_FIELD_SENTINEL'
        f=self.add('An exact sequence was retained.', value)
        selection=Selection([f], [], 0, coverage={'search_complete':True})
        memory=fit_to_budget(selection,NOW.date(),180,counter=TokenCounter('utf8'))
        self.assertNotIn('structured_payload_fixture_', memory)
        self.assertEqual(selection.facts, [])
        self.assertEqual(selection.omitted_facts, [f.key])
        self.assertFalse(selection.coverage['context_complete'])
        full=render_memory(Selection([f],[],0),NOW.date())
        self.assertIn('FINAL_FIELD_SENTINEL', full)

    def test_fact_pages_preserve_values_across_page_boundaries(self):
        value='value_fixture_'*60+'END_OF_VALUE_FIXTURE'
        self.add('A long structured value is retained.',value)
        self.settings.context_budget_tokens=300
        pages=self.system.recall_pages('synthetic','List all retained values.',NOW)
        combined=''.join(i['text'] for p in pages['pages'] for i in p['items'])
        self.assertIn(value,combined)
        self.assertTrue(pages['pages_complete'])
        self.assertTrue(all(i['value']==value for p in pages['pages'] for i in p['items']))
        self.assertTrue(all(i['kind']=='fact' for p in pages['pages'] for i in p['items']))


if __name__=='__main__': unittest.main()
