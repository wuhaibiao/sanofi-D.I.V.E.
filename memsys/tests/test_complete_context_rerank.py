import dataclasses
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from helpers import settings
from memsys.config import Models
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.read import Retriever
from memsys.schema import EPISODE_ACTIVE


class CompleteContextRerankTest(unittest.TestCase):
    def system(self, context_budget=3500, text='I now use amber as my current color.'):
        self.inputs=[]
        def answer(messages):
            self.inputs.append([dict(m) for m in messages])
            return 'ANSWER: amber'
        model=ScriptedChat({'answer':answer,'answer_review':json.dumps(dict(responsive=True,supported=True))})
        cfg=settings(query_analysis=False, min_similarity=0, max_history_share=0,
                     semantic_validation=True, complete_episode_context=True,
                     complete_context_skip_rerank=True, context_budget_tokens=context_budget)
        system=MemorySystem(Models(model,None,HashEmbedder(),'hash'),cfg)
        self.addCleanup(system.close)
        store=system.store('u')
        at=datetime(2025,5,1)
        sid=store.create_session(at)
        store.add_episode(sid,0,at,text,'',1,EPISODE_ACTIVE)
        return system

    def test_skipping_rerank_preserves_exact_answer_input_and_support_review(self):
        system=self.system()
        original=Retriever.rerank
        with patch('memsys.core.Retriever.rerank',autospec=True,side_effect=original) as rerank:
            system.settings=dataclasses.replace(system.settings,complete_context_skip_rerank=False)
            first=system.respond('u','What is my current color?',datetime(2025,5,2))
            self.assertEqual(rerank.call_count,1)
            rerank.reset_mock()
            system.settings=dataclasses.replace(system.settings,complete_context_skip_rerank=True)
            second=system.respond('u','What is my current color?',datetime(2025,5,2))
            rerank.assert_not_called()
        self.assertEqual(self.inputs[0],self.inputs[1])
        self.assertEqual(first.text,second.text)
        self.assertEqual(second.trace['aggregation']['mode'],'complete_original_context')
        self.assertTrue(second.trace['reranking']['skipped'])
        self.assertEqual(second.trace['aggregation']['answer_validation']['semantic_reviews'][0]['supported'],True)

    def test_over_budget_originals_keep_normal_reranking(self):
        system=self.system(context_budget=100,text='I now use amber. '+('Other detail. '*100))
        original=Retriever.rerank
        with patch('memsys.core.Retriever.rerank',autospec=True,side_effect=original) as rerank:
            trace,_,_,result=system._retrieve(system.store('u'),'u','What is my current color?',
                datetime(2025,5,2),system.settings,{},allow_complete_context=True)
            self.assertEqual(rerank.call_count,1)
        self.assertFalse(trace['reranking'].get('skipped',False))
        self.assertIsNone(getattr(result,'complete_context',None))

    def test_changed_origin_links_restore_normal_retrieval(self):
        system=self.system()
        origin=system._complete_origins
        calls=[]
        def change_once(store,result):
            actual=origin(store,result)
            calls.append(1)
            if len(calls)==2:
                return dict(actual,E1={999})
            return actual
        original=Retriever.rerank
        with patch.object(system,'_complete_origins',side_effect=change_once), \
             patch('memsys.core.Retriever.rerank',autospec=True,side_effect=original) as rerank:
            reply=system.respond('u','What is my current color?',datetime(2025,5,2))
            self.assertEqual(rerank.call_count,1)
        self.assertIn('origin_links_changed',reply.trace['complete_context_revalidation'])
        self.assertEqual(reply.text,'amber')

    def test_all_searchable_originals_fit_without_query_embedding_or_reranker(self):
        system=self.system()
        planner=ScriptedChat({'query':lambda _: (_ for _ in ()).throw(AssertionError('query must not run'))})
        system.models.by_purpose['query']=planner
        system.settings=dataclasses.replace(system.settings,query_analysis=True,complete_context_direct_query=True)
        with patch('memsys.core.Retriever.search',side_effect=AssertionError('vector search must not run')), \
             patch('memsys.core.Retriever.rerank',side_effect=AssertionError('rerank must not run')):
            reply=system.respond('u','What is my current color?',datetime(2025,5,2))
        self.assertEqual(reply.text,'amber')
        self.assertEqual(planner.purposes(),[])
        self.assertEqual(reply.trace['plan']['source'],'direct_complete')
        self.assertTrue(reply.trace['reranking']['query_skipped'])
        self.assertEqual(reply.trace['aggregation']['reviewed_sources'],['E1'])

    def test_direct_query_over_budget_uses_existing_retrieval(self):
        system=self.system(context_budget=100,text='I use amber. '+('Other detail. '*100))
        system.settings=dataclasses.replace(system.settings,complete_context_direct_query=True)
        original=Retriever.search
        with patch('memsys.core.Retriever.search',autospec=True,side_effect=original) as search:
            trace,_,_,_=system._retrieve(system.store('u'),'u','What is my current color?',
                datetime(2025,5,2),system.settings,{},allow_complete_context=True)
            self.assertEqual(search.call_count,1)
        self.assertNotEqual(trace['plan']['source'],'direct_complete')

    def test_direct_query_excludes_forgotten_sources(self):
        from memsys.schema import FORGOTTEN
        system=self.system()
        store=system.store('u')
        at=datetime(2025,4,1)
        sid=store.create_session(at)
        store.add_episode(sid,0,at,'I use lavender.','',1,FORGOTTEN)
        system.settings=dataclasses.replace(system.settings,complete_context_direct_query=True)
        reply=system.respond('u','What is my current color?',datetime(2025,5,2))
        self.assertNotIn('lavender',self.inputs[0][-1]['content'])
        self.assertEqual(reply.trace['aggregation']['reviewed_sources'],['E1'])

    def test_active_dialogue_keeps_unflushed_recent_state(self):
        system=self.system()
        system.settings=dataclasses.replace(system.settings,complete_context_direct_query=True)
        def answer(messages):
            text=messages[-1]['content']
            self.assertIn('I now use jade as my current color.',text)
            return 'ANSWER: jade'
        system.models.chat=ScriptedChat({'answer':answer,
            'answer_review':json.dumps(dict(responsive=True,supported=True))})
        at=datetime(2025,5,2)
        handle=system.open_session('u',at)
        system.add_turn(handle,'user','I now use jade as my current color.',at)
        with patch('memsys.core.complete_answer',side_effect=AssertionError('QA synthesis must not omit active dialogue')):
            reply=system.respond('u','What is my current color?',at,session=handle)
        self.assertEqual(reply.text,'jade')
        self.assertEqual(reply.trace['mode'],'chat')
        self.assertFalse(reply.trace['reranking'].get('query_skipped',False))
