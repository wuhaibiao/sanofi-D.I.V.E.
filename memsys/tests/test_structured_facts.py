"""Structured fact retention, identity, indexing and failure atomicity."""
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from memsys.config import MemorySettings, Models
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.llm import ModelError
from memsys.fact_content import index_signature
from memsys.qdrant import QdrantSettings
from memsys.read import Retriever, heuristic_plan
from memsys.store import Store
from memsys.write import Writer
from tests.qdrant_fixture import Engine

NOW=datetime(2026,10,13,12)


class StructuredFactFixture:
    def setUp(self):
        self.engine=Engine()
        self.settings=MemorySettings(rerank_backend='fusion',max_history_share=0)
        self.model=ScriptedChat({})
        self.models=Models(None,self.model,HashEmbedder(),'hash-256')
        self.system=MemorySystem(self.models,self.settings,storage=QdrantSettings(namespace='structured-tests'),
            store_factory=lambda **kw:Store(client=self.engine,**kw))
        self.store=self.system.store('fixture')

    def write(self,value,*,text='The assistant supplied a fixture assignment.',source='Retain this exact assignment.',items=None,extra=None):
        sid=self.store.create_session(NOW)
        self.store.append_turn(sid,'user','Keep the fixture information.',NOW)
        self.store.append_turn(sid,'assistant',source,NOW)
        entry=dict(text=text,value=value,kind='assistant',subject='fixture_team',attribute='assignment',rounds=[1])
        entry.update(extra or {})
        self.model.handlers['extract']=json.dumps({'facts':items if items is not None else [entry]})
        return sid,Writer(self.store,self.models,self.settings).consolidate(sid,closed_at=NOW)


class StructuredFactTests(StructuredFactFixture, unittest.TestCase):

    def test_long_values_entities_and_conditions_are_not_clipped(self):
        value='payload_fixture_'*55+'END_OF_COMPLETE_VALUE'
        subject='entity_fixture_'*12
        condition='condition_fixture_'*15
        _,report=self.write(value,extra={'subject':subject,'semantics':{'qualifiers':[condition],'relative_time':'relative_fixture_'*40}})
        f=self.store.facts()[0]
        self.assertEqual(f.value,value)
        self.assertEqual(f.subject,subject)
        self.assertEqual(f.semantics['qualifiers'],[condition])
        self.assertEqual(f.semantics['relative_time'],'relative_fixture_'*40)
        self.assertEqual(report.extract_calls,1)

    def test_table_relationships_and_sequence_order_survive_storage(self):
        value={'type':'table','columns':['day','early','late'],
               'rows':[['Friday','member_fixture_2','member_fixture_1'],['Saturday','member_fixture_1','member_fixture_2']]}
        _,report=self.write(value)
        self.assertEqual(json.loads(self.store.facts()[0].value),value)
        self.assertEqual(report.extract_calls,1)
        value={'type':'sequence','columns':['step','actor','action'],
               'rows':[[2,'actor_fixture','align'],[3,'actor_fixture','seal']]}
        self.write(value,text='The fixture process has ordered actions.')
        f=next(f for f in self.store.facts() if 'ordered' in f.text)
        self.assertEqual(json.loads(f.value)['rows'][0][0],2)
        recalled=self.system.recall('fixture','List all fixture processes.',NOW,budget=3000,save_trace=False)
        self.assertIn('"align"',recalled.context)
        self.assertIn('"seal"',recalled.context)
        self.assertTrue(all(i['kind']=='fact' for i in recalled.items))

    def test_invalid_table_is_quarantined_without_becoming_a_searchable_fact(self):
        _, report = self.write({'type':'table','columns':['day','person'],'rows':[['Friday']]})
        self.assertEqual(self.store.facts(),[])
        sessions=self.store.records('session')
        self.assertEqual(sessions[0]['turns_done'],2)
        self.assertEqual(len(self.store.turns(sessions[0]['id'])),2)
        self.assertEqual(report.extract_rejected_count, 1)
        self.assertFalse(report.extract_protocol_complete)
        self.assertEqual(self.model.purposes(),['extract'])

    def test_changed_value_with_same_summary_is_not_lost_to_dedup(self):
        self.write({'assignment':'member_fixture_1'})
        self.write({'assignment':'member_fixture_2'})
        _,again=self.write({'assignment':'member_fixture_2'})
        self.assertEqual(len(self.store.facts()),2)
        self.assertEqual(again.duplicates,1)
        self.assertEqual(again.added,[])
        f=next(f for f in self.store.facts() if json.loads(f.value)['assignment']=='member_fixture_2')
        self.assertEqual(len(self.store.sources([f.id])[f.id]),2)

    def test_same_batch_distinct_values_are_not_merged(self):
        entries=[dict(text='A fixture assignment was given.',value={'member':n},rounds=[1]) for n in ('alpha','beta')]
        self.write('',items=entries)
        self.assertEqual(len(self.store.facts()),2)

    def test_zero_and_false_are_retained(self):
        self.write(0,text='The fixture counter was reset.')
        self.write(False,text='The fixture switch is disabled.')
        self.assertEqual({f.value for f in self.store.facts()},{'0','false'})

    def test_value_only_name_is_searchable_without_original_retrieval(self):
        self.write({'location':'azuritecobaltfixture'})
        with patch.object(self.store,'episodes',side_effect=AssertionError('original retrieval')):
            result=Retriever(self.store,self.models,self.settings).search(heuristic_plan('azuritecobaltfixture'))
        self.assertTrue(result.ranked)
        self.assertTrue(all(c.kind=='fact' for c in result.ranked))
        self.assertTrue(any(c.bm25_rank is not None for c in result.ranked))

    def test_literal_structure_fills_gap_without_a_second_model_call(self):
        _,report=self.write('a general summary',source='1. fixture_alpha\n2. fixture_beta')
        self.assertEqual(report.extract_calls,1)
        self.assertEqual(report.structure_warnings,[])
        self.assertEqual(report.literal_structures,1)
        self.assertTrue(any('fixture_beta' in f.value for f in self.store.facts()))
        self.assertEqual(self.model.purposes(),['extract'])

    def test_old_fact_index_is_rebuilt_with_full_value(self):
        self.write({'code':'azuritecobaltfixture'})
        manifest=json.loads(self.store.get_meta('index_manifest'))
        manifest.pop('fact_content_format');manifest.pop('fact_content_sha256')
        self.store.set_meta('index_manifest',json.dumps(manifest))
        self.store.set_meta('index_fingerprint','legacy-fact-sentence-only')
        with patch.object(self.models.embedder,'embed',wraps=self.models.embedder.embed) as embed:
            self.system.reindex('fixture')
            supplied=[text for call in embed.call_args_list for text in call.args[0]]
            self.assertTrue(any('azuritecobaltfixture' in text and 'Value:' in text for text in supplied))
            count=embed.call_count
            self.system.reindex('fixture')
            self.assertEqual(embed.call_count,count)
        self.assertEqual(json.loads(self.store.get_meta('index_manifest'))['fact_content_format'],index_signature()['fact_content_format'])


if __name__=='__main__':unittest.main()
