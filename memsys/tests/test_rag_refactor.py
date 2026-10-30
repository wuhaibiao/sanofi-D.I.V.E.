"""自拟机制回归；不把替身模型的通过率当作真实问答正确率。"""

import dataclasses
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from helpers import dt, settings, models, fact, facts_json
from memsys.aggregation import answer_with_validation
from memsys.config import MemorySettings
from memsys.core import MemorySystem
from memsys.indexing import episode_index, stored_chunks
from memsys.model_tokens import bounded_spans
from memsys.read import Retriever, heuristic_plan, plan_query
from memsys.reranking import RerankFailure, local_reranker
from memsys.schema import EPISODE_WITHHELD
from memsys.store import EmbeddingMismatch, Store


class CharacterTokenizer:
    def count(self, text, special=True):
        return len(text) + (2 if special else 0)

    def pair_count(self, question, text):
        return len(question) + len(text) + 4

    def manifest(self):
        return {"sha256":"self-authored-character-tokenizer", "unit":"model_tokens", "includes_special_tokens":True}


class IndexCompatibilityTest(unittest.TestCase):
    def test_retired_settings_cannot_enable_removed_paths(self):
        self.assertEqual(MemorySettings().rerank_backend, 'bge')
        self.assertNotIn('semantic_chunking', MemorySettings().to_dict())
        with self.assertWarns(FutureWarning):
            old = MemorySettings.from_dict({'chunk_strategy':'legacy', 'semantic_chunking':False,
                                            'parent_max_tokens':768})
        self.assertEqual(old.chunk_strategy, 'legacy')
        with self.assertRaisesRegex(ValueError, 'removed'):
            MemorySettings.from_dict({'semantic_chunking':True})
        with self.assertRaisesRegex(ValueError, 'chunk_strategy'):
            MemorySettings.from_dict({'chunk_strategy':'parent'})

    def test_fresh_store_does_not_create_retired_tables_or_columns(self):
        store = Store(); self.addCleanup(store.close)
        tables = {r['name'] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(tables & {'episode_parents','parent_fragments','semantic_vectors'})
        columns = {r['name'] for r in store.db.execute('PRAGMA table_info(episode_chunks)')}
        self.assertEqual(columns, {'id','episode_id','ordinal','role','start','end'})


    def test_opening_pre_fts_disk_index_backfills_safe_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder)/'old.sqlite')
            store = Store(path)
            text = 'The marker is cinnabar.'
            with store.transaction():
                eid = store.add_episode('s',0,dt('2025-01-01'),text,'',.8,'active')
                store.put_vectors('episode',[(eid,[1.,0.])])
                store.put_episode_chunks(eid,[('User',0,len(text),[1.,0.])])
                store.db.execute('DROP TABLE fts_chunks')
            store.close()
            reopened = Store(path)
            try:
                self.assertTrue(reopened.fts_search('episode_chunk','"cinnabar"',('active',),10))
                self.assertTrue(reopened.validate_index()['valid'])
                with reopened.transaction():
                    reopened.set_status('episode',[eid],EPISODE_WITHHELD)
                self.assertEqual(reopened.fts_search('episode_chunk','"cinnabar"',('active',),10),[])
            finally:
                reopened.close()


class FlatIndexMigrationTest(unittest.TestCase):
    def setUp(self):
        self.settings = settings(embed_chunk_tokens=64, embed_chars=160, query_analysis=False)

    def system(self, s=None):
        system = MemorySystem(models(memory={'extract':'{"facts":[]}'}), s or self.settings)
        self.addCleanup(system.close)
        return system

    def ingest(self, system, user, assistant="", session=None):
        session = session or system.open_session("u", dt("2025-01-01"))
        system.add_turn(session,"user",user,dt("2025-01-01"))
        if assistant:
            system.add_turn(session,"assistant",assistant,dt("2025-01-01"))
        system.close_session(session,dt("2025-01-01"))
        return session

    def test_reindex_retires_old_schema_without_restoring_removed_generations(self):
        system = self.system()
        self.ingest(system, 'I catalogued a fluorite specimen.')
        store = system.store('u')
        source = store.index_source_fingerprint()
        with store.transaction():
            store.db.execute('ALTER TABLE episode_chunks ADD COLUMN parent_id INTEGER')
            store.db.execute("ALTER TABLE episode_chunks ADD COLUMN prefix TEXT NOT NULL DEFAULT ''")
            store.db.execute('ALTER TABLE episode_chunks ADD COLUMN token_count INTEGER NOT NULL DEFAULT 0')
            store.db.execute('CREATE TABLE episode_parents (id INTEGER PRIMARY KEY, episode_id INTEGER, ordinal INTEGER, reason TEXT)')
            store.db.execute('CREATE TABLE parent_fragments (parent_id INTEGER, ordinal INTEGER, episode_id INTEGER, role TEXT, start INTEGER, end INTEGER)')
            store.db.execute('CREATE TABLE semantic_vectors (model TEXT, digest TEXT, vec BLOB)')
            store.set_meta('index_manifest', json.dumps({'strategy':'parent'}))
        with self.assertRaisesRegex(EmbeddingMismatch, 'retired'):
            system.recall('u','fluorite',dt('2025-01-02'))
        system.reindex('u')
        self.assertEqual(store.index_source_fingerprint(), source)
        self.assertTrue(store.validate_index()['valid'])
        tables = {r['name'] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(tables & {'episode_parents','parent_fragments','semantic_vectors'})
        columns = {r['name'] for r in store.db.execute('PRAGMA table_info(episode_chunks)')}
        self.assertEqual(columns, {'id','episode_id','ordinal','role','start','end'})
        current_vectors = store.vectors('episode_chunk',('active',))
        with self.assertRaisesRegex(EmbeddingMismatch, 'retired'):
            system.rollback_index('u')
        self.assertEqual(store.vectors('episode_chunk',('active',)), current_vectors)

    def test_unicode_offsets_full_coverage_and_bounded_inputs(self):
        originals = {"User":"条件：只有晴天才收集晶体。\n" * 28,
                     "Assistant":"1. Keep the label.\n2. Keep the date.\n```py\nprint('晶体')\n```\n" * 10 + "结束"}
        plan = episode_index([(originals['User'], originals['Assistant'])],self.settings)
        for role,text in originals.items():
            spans = sorted((c.start,c.end) for c in plan.chunks[0] if c.role==role)
            end=0
            for start,right in spans:
                self.assertLessEqual(start,end); end=max(end,right)
            self.assertEqual(end,len(text))
        for chunk in plan.chunks[0]:
            encoded=plan.texts[chunk.vector_index]
            self.assertEqual(encoded,chunk.role+": "+originals[chunk.role][chunk.start:chunk.end])
            self.assertLessEqual(chunk.end-chunk.start,self.settings.embed_chars)

    def test_short_answer_recovers_question_role_and_original_citations(self):
        system=self.system()
        self.ingest(system,"Which mineral was placed in cabinet seven?","The specimen is fluorite.")
        recall=system.recall("u","fluorite",dt("2025-01-02"))
        self.assertIn("User: Which mineral",recall.memory)
        self.assertIn("Assistant: The specimen is fluorite.",recall.memory)
        self.assertNotIn("User question:",recall.memory)
        self.assertEqual(system.store("u").index_coverage()['chunked_episodes'],1)

    def test_child_keyword_search_and_forgetting_respect_status(self):
        system=self.system()
        self.ingest(system,"I catalogued a specimen.","Notes. "*30+"The marker is cinnabar.")
        store=system.store("u")
        self.assertTrue(store.fts_search("episode_chunk",'"cinnabar"',('active',),100))
        system.forget("u","E1")
        self.assertEqual(store.fts_search("episode_chunk",'"cinnabar"',('active','archived'),100),[])
        system.reindex("u")
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM sqlite_master WHERE name="episode_parents"').fetchone()[0],0)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM fts_chunks').fetchone()[0],0)


    def test_reindex_reuses_facts_is_idempotent_and_rolls_back_index_only(self):
        system=self.system(settings(embed_chunk_tokens=64,chunk_strategy='legacy',embed_chars=90))
        system.models.memory= models(memory={'extract':facts_json(fact('I collected a fluorite specimen.',[1],attribute='mineral',value='fluorite'))}).memory
        self.ingest(system,"I collected a specimen.","The mineral is fluorite. "*6)
        store=system.store('u')
        old_chunks=[dict(r) for r in store.db.execute('SELECT * FROM episode_chunks')]
        source=store.index_source_fingerprint()
        facts_before=[dict(r) for r in store.db.execute('SELECT * FROM facts')]
        fact_vectors_before=store.vectors('fact',('current',))
        self.assertEqual(len(facts_before),1)
        system.settings=self.settings
        with patch.object(system.models.embedder,'embed',wraps=system.models.embedder.embed) as embed:
            system.reindex('u')
        self.assertNotIn(facts_before[0]['text'],embed.call_args[0][0])
        self.assertEqual(store.vectors('fact',('current',)),fact_vectors_before)
        self.assertEqual([dict(r) for r in store.db.execute('SELECT * FROM facts')],facts_before)
        self.assertTrue(store.validate_index()['valid'])
        generation=store.get_meta('active_index_generation')
        system.reindex('u')
        self.assertEqual(store.get_meta('active_index_generation'),generation)
        self.assertEqual(store.index_source_fingerprint(),source)
        system.rollback_index('u')
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM sqlite_master WHERE name="episode_parents"').fetchone()[0],0)
        self.assertEqual([dict(r) for r in store.db.execute('SELECT * FROM episode_chunks')],old_chunks)
        self.assertEqual(store.index_source_fingerprint(),source)
        self.assertEqual(len(store.index_generations()),2)

    def test_migration_failure_and_source_change_keep_previous_index(self):
        system=self.system(settings(chunk_strategy='legacy'))
        self.ingest(system,'I kept a garnet specimen.')
        store=system.store('u'); old=list(store.vectors('episode_chunk',('active',))[0])
        system.settings=self.settings
        with patch.object(store,'validate_index',side_effect=ValueError('simulated interruption')):
            with self.assertRaisesRegex(ValueError,'simulated interruption'):
                system.reindex('u')
        self.assertEqual(store.vectors('episode_chunk',('active',))[0],old)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM sqlite_master WHERE name="episode_parents"').fetchone()[0],0)
        system.reindex('u')
        system.forget('u','E1')
        with self.assertRaisesRegex(EmbeddingMismatch,'lifecycle changed'):
            system.rollback_index('u')

    def test_rollback_of_retained_pre_fts_generation_restores_searchable_index(self):
        system = self.system(settings(chunk_strategy='legacy'))
        self.ingest(system,'I catalogued a cinnabar specimen.')
        store = system.store('u')
        old_vectors = store.vectors('episode_chunk',('active',))
        with store.transaction():
            store.db.execute('DELETE FROM fts_chunks')
            store.db.execute("DELETE FROM meta WHERE key IN ('index_manifest','index_fingerprint')")
        system.settings = self.settings
        system.reindex('u')
        system.rollback_index('u')
        self.assertEqual(store.vectors('episode_chunk',('active',)),old_vectors)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM sqlite_master WHERE name="episode_parents"').fetchone()[0],0)
        self.assertTrue(store.fts_search('episode_chunk','"cinnabar"',('active',),10))
        self.assertTrue(store.validate_index()['valid'])

    def test_chunk_change_requires_migration_while_rerank_does_not(self):
        system=self.system()
        self.ingest(system,'I own a mineral box.')
        system.settings=dataclasses.replace(self.settings,embed_chars=150)
        with self.assertRaisesRegex(EmbeddingMismatch,'fingerprint'):
            system.recall('u','mineral box',dt('2025-01-02'))
        system.reindex('u')
        system.settings=dataclasses.replace(system.settings,rerank_backend='fusion')
        system.recall('u','mineral box',dt('2025-01-02'))

    def test_index_knobs_do_not_invalidate_fact_extraction_fingerprint(self):
        other=dataclasses.replace(self.settings,embed_chars=128,rerank_backend='bge')
        self.assertEqual(self.settings.write_fingerprint(),other.write_fingerprint())
        self.assertNotEqual(self.settings.index_fingerprint('same'),other.index_fingerprint('same'))
        rerank_only=dataclasses.replace(self.settings,rerank_backend='bge')
        self.assertEqual(self.settings.index_fingerprint('same'),rerank_only.index_fingerprint('same'))
        self.assertNotEqual(self.settings.read_fingerprint(),rerank_only.read_fingerprint())

    def test_declared_embedding_revision_recomputes_fact_vectors(self):
        from memsys.fake import HashEmbedder
        s=self.settings
        system=self.system(s)
        system.models.memory=models(memory={'extract':facts_json(fact('I catalogued quartz.',[1],attribute='mineral',value='quartz'))}).memory
        self.ingest(system,'I catalogued quartz. '+ 'Quartz samples are labeled. '*30)
        store=system.store('u')
        old_revision=s.write_fingerprint()
        system.settings=dataclasses.replace(s,embedding_model_revision='self-authored-next-revision')
        system.models.embedder=HashEmbedder(dim=128)
        system.reindex('u')
        self.assertEqual({len(v) for v in store.vectors('fact',('current',))[1]},{128})
        self.assertNotEqual(system.settings.write_fingerprint(),old_revision)


class RerankWindowTest(unittest.TestCase):

    def test_long_sentence_and_pair_budget_never_drop_tail(self):
        tokenizer=CharacterTokenizer(); text='段'*180+'A final mineral label.'
        spans=list(bounded_spans(text,lambda p:tokenizer.pair_count('a query',p),48,8))
        self.assertEqual(spans[0][0],0); self.assertEqual(spans[-1][1],len(text))
        last=0
        for left,right in spans:
            self.assertLessEqual(left,last); last=max(last,right)
            self.assertLessEqual(tokenizer.pair_count('a query',text[left:right]),48)
        with self.assertRaises(ValueError):
            list(bounded_spans('text',lambda p:tokenizer.pair_count('q'*60,p),48))


class RerankAndPolicyTest(unittest.TestCase):
    def fixture(self, s=None):
        store=Store(); self.addCleanup(store.close)
        with store.transaction():
            for i in range(5):
                store.add_episode('s1',i,dt('2025-01-01'),'I placed specimen %d in the tray.'%i,'Recorded.',.8,'active')
        return Retriever(store,models(),s or settings(rerank_backend='bge',rerank_candidates=2))

    def test_bge_complete_aggregation_keeps_unscored_candidates_for_paging(self):
        retriever=self.fixture()
        plan=heuristic_plan('List every specimen'); plan.needs_all=True
        result=retriever.search(plan); before={c.key for c in result.ranked}
        class Backend:
            def rank(self,question,documents):
                scores={d.key:float(d.key[1:]) for d in documents}
                return scores,{'backend':'bge','windows':[]}
        with patch('memsys.reranking.local_reranker',return_value=Backend()):
            retriever.rerank(result,'List every specimen',dt('2025-01-02'),plan)
        self.assertEqual({c.key for c in result.ranked},before)
        self.assertGreater(len(before),2)
        self.assertEqual(result.rerank_trace['scored_candidates'],2)
        self.assertEqual(result.rerank_trace['eligible_candidates'],len(before))
        self.assertTrue(result.rerank_trace['full_candidate_set_retained'])
        self.assertTrue(result.coverage['search_complete'])

    def test_bge_failure_is_visible_and_strict_evaluation_fails(self):
        retriever=self.fixture(); result=retriever.search(heuristic_plan('specimen'))
        keys=[c.key for c in result.ranked]
        with patch('memsys.reranking.local_reranker') as backend:
            backend.return_value.rank.side_effect=RerankFailure('self-authored loading failure')
            retriever.rerank(result,'specimen',dt('2025-01-02'))
            self.assertEqual([c.key for c in result.ranked],keys)
            self.assertEqual(result.rerank_trace['backend'],'fusion')
            self.assertIn('loading failure',result.rerank_error)
            retriever.settings.rerank_strict=True
            with self.assertRaises(RerankFailure):
                retriever.rerank(result,'specimen',dt('2025-01-02'))

    def test_shared_inference_object_for_same_configuration(self):
        self.assertIs(local_reranker(MemorySettings()),local_reranker(MemorySettings()))

    def test_optional_query_preserves_complete_search_and_complex_planning(self):
        m=models(memory={'query':'{"queries":[],"needs_all":true,"operation":"enumerate"}'})
        s=settings(query_policy='on_risk',semantic_operation_validation=True)
        simple=plan_query(m,'Which mineral label was blue?',dt('2025-01-02'),s)
        self.assertEqual(simple.source,'direct'); self.assertEqual(m.memory.calls,[])
        complete=plan_query(m,'List every mineral label',dt('2025-01-02'),s)
        self.assertTrue(complete.needs_all); self.assertEqual(complete.source,'llm')

    def test_review_off_does_not_disable_page_semantics_or_program_checks(self):
        s=MemorySettings(answer_review_policy='off')
        self.assertTrue(s.page_semantics); self.assertTrue(s.operation_semantics)
        self.assertTrue(s.aggregation_validation)
        m=models(chat={'answer':'ANSWER: The specimen is fluorite.'})
        _,answer,audit=answer_with_validation(m.chat,[{'role':'user','content':'Which mineral?'}],
            evidence={'E1':'The specimen is fluorite.'},question='Which mineral?',semantic_enabled=True,review_policy='off')
        self.assertIn('fluorite',answer)
        self.assertFalse(audit['model_review']['triggered'])
        self.assertEqual([call[0] for call in m.chat.calls],['answer'])

    def test_on_risk_review_skips_simple_and_keeps_multi_source_review(self):
        m=models(chat={'answer':'ANSWER: fluorite','answer_review':'{"responsive":true,"supported":true,"reason":"grounded"}'})
        common=dict(question='Which mineral?',semantic_enabled=True,review_policy='on_risk')
        _,_,audit=answer_with_validation(m.chat,[{'role':'user','content':'Which mineral?'}],evidence={'E1':'fluorite'},**common)
        self.assertFalse(audit['model_review']['triggered'])
        _,_,audit=answer_with_validation(m.chat,[{'role':'user','content':'Which mineral?'}],evidence={'E1':'fluorite','E2':'garnet'},**common)
        self.assertTrue(audit['model_review']['triggered'])
        self.assertIn('semantic_reviews',audit)


if __name__=='__main__':
    unittest.main()
