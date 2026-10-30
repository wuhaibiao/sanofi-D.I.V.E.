"""Synthetic contracts for original-text ingestion and bounded retrieval; no benchmarks."""
from datetime import datetime
import hashlib
import json
from unittest import TestCase
from unittest.mock import patch

from memsys.config import MemorySettings, Models
from memsys.budget import budget_counter
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder
from memsys.indexing import episode_index
from memsys.qdrant import QdrantSettings
from memsys.raw_chunks import chunk_ranges, raw_episode_index, render_chunks, companion_spans, fuse_ranks, choose_spans
from memsys.schema import FORGOTTEN, Episode
from memsys.store import Store
from memsys.write import Writer
from tests.qdrant_fixture import Engine

NOW = datetime(2025, 8, 11, 9)


class ForbiddenChat:
    def chat(self, *args, **kwargs):
        raise AssertionError("raw memory must not invoke a generation model")


class RawChunksTest(TestCase):
    def setUp(self):
        self.engine = Engine()
        self.storage = QdrantSettings(namespace="raw-chunks-synthetic-tests")
        self.settings = MemorySettings(recall_mode="raw_chunks", rerank_backend="fusion",
            raw_chunk_chars=128, raw_chunk_overlap_chars=24, max_raw_chunks=3)
        self.models = Models(chat=None, memory=ForbiddenChat(), embedder=HashEmbedder(), embedding_name="hash-256")

    def store(self, user="alice"):
        return Store(user_id=user, settings=self.storage, client=self.engine)

    def system(self):
        return MemorySystem(self.models, self.settings,
            store_factory=lambda **kw: self.store(kw['user_id']))

    def ingest(self, user="alice", text="I joined the riverside photography club today.", assistant="Welcome to the club."):
        store = self.store(user)
        sid = store.create_session(NOW)
        store.append_turn(sid, "user", text, NOW)
        if assistant:
            store.append_turn(sid, "assistant", assistant, NOW)
        report = Writer(store, self.models, self.settings).consolidate(sid, closed_at=NOW)
        return store, sid, report

    def test_overlapping_spans_keep_every_original_character_and_role(self):
        user = "  第一段，记录天气与地点。\n\n" * 40 + "最后一个事实。"
        assistant = "An answer with several sentences. " * 30
        index = raw_episode_index([(user, assistant)], self.settings)
        for role, text in (("User", user), ("Assistant", assistant)):
            covered = set()
            for chunk in index.chunks[0]:
                if chunk.role != role:
                    continue
                self.assertLessEqual(chunk.end - chunk.start, self.settings.raw_chunk_chars)
                self.assertEqual(index.texts[chunk.vector_index], role + ": " + text[chunk.start:chunk.end])
                covered.update(range(chunk.start, chunk.end))
            self.assertEqual(covered, set(range(len(text))))
        spans = list(chunk_ranges(user, 128, 24))
        self.assertTrue(any(b[0] < a[1] for a, b in zip(spans, spans[1:])))

    def test_ingestion_never_extracts_or_schedules_fact_backfill(self):
        store, sid, report = self.ingest(text="Hi. I borrowed a green telescope this morning.")
        self.assertEqual(store.facts(), [])
        self.assertGreater(len(store.records("episode_chunk")), 0)
        self.assertEqual(store.backlog_count(), 0)
        self.assertEqual(store.session(sid)['turns_done'], 2)
        self.assertEqual(store.session(sid)['status'], 'closed')
        self.assertEqual(report.deferred, 0)
        self.assertEqual(report.extract_attempts, [])
        self.assertEqual(report.extract_quality, 'not_applicable_raw_chunks')
        self.assertEqual(Writer(store, self.models, self.settings).backfill(), [])

    def test_greetings_and_user_only_messages_are_indexed_without_a_gate(self):
        store, sid, report = self.ingest(text="Hello!", assistant="")
        self.assertEqual(report.skipped, 0)
        self.assertEqual(store.session(sid)['turns_done'], 1)
        self.assertEqual(len(store.records('episode_chunk')), 1)

    def test_recall_is_tenant_scoped_original_text_and_ignores_fact_store(self):
        store, sid, _ = self.ingest()
        self.ingest('bob', text='My photography club meets in a private blue tower.')
        store.add_fact(text="An invented photography location: purple moon.", kind="personal", subject="user",
            attribute="club", value="purple moon", event=None, recorded_at=NOW, valid_from=NOW.date(), session_id=sid, importance=.5)
        with patch('memsys.core.plan_query', side_effect=AssertionError('no query planner')):
            result = self.system().recall('alice', 'Which photography club did I join?', NOW)
        self.assertTrue(result.items)
        self.assertTrue(all(row['kind'] == 'episode' for row in result.items))
        self.assertEqual(result.trace['selected']['facts'], [])
        self.assertNotIn('purple moon', result.context)
        self.assertNotIn('private blue tower', result.context)
        self.assertIn('riverside photography club', result.memory)
        self.assertIn('2025-08-11', result.memory)
        for item in result.items:
            for chunk in item['chunks']:
                episode = store.episodes([chunk['episode_id']])[0]
                original = episode.user_text if chunk['role'] == 'User' else episode.assistant_text
                self.assertEqual(chunk['text_sha256'], hashlib.sha256(original[chunk['start']:chunk['end']].encode()).hexdigest())

    def test_ranking_and_return_caps_do_not_trigger_additional_model_selection(self):
        self.settings.raw_chunk_chars = 64
        self.settings.raw_chunk_overlap_chars = 12
        self.settings.rerank_backend = 'bge'
        self.settings.rerank_candidates = 4
        self.settings.max_raw_chunks = 2
        self.ingest(text=('Photography observation at the river. ' * 18), assistant=('Photography lessons and examples. ' * 18))
        class Ranker:
            def rank(_, query, documents):
                self.assertLessEqual(len(documents), 4)
                return {d.key: float(i) for i, d in enumerate(documents)}, {'backend':'bge','inference_and_load_s':0.01}
        with patch('memsys.reranking.local_reranker', return_value=Ranker()):
            result = self.system().recall('alice', 'Photography observation?', NOW)
        self.assertLessEqual(len(result.trace['selected']['chunks']), 2)
        self.assertEqual(result.trace['reranking']['scored_candidates'], 4)

    def test_budget_and_forgotten_sources(self):
        store, _, _ = self.ingest(text='Photography note. ' * 30, assistant='Camera settings. ' * 20)
        result = self.system().recall('alice', 'Photography?', NOW, budget=320)
        self.assertLessEqual(len(result.memory.encode()), 320)
        ids = [e.id for e in store.episodes()]
        store.set_status('episode', ids, FORGOTTEN)
        result = self.system().recall('alice', 'Photography?', NOW)
        self.assertEqual(result.items, [])
        self.assertEqual(result.memory, '')

    def test_repeated_literal_statements_keep_their_separate_sources(self):
        store, _, _ = self.ingest(text='I photographed a heron today.', assistant='')
        self.ingest(text='I photographed a heron today.', assistant='')
        result = self.system().recall('alice', 'What did I photograph?', NOW)
        self.assertEqual(len(result.items), 2)
        self.assertEqual(len({item['key'] for item in result.items}), 2)

    def test_configuration_rejects_nonprogressing_spans_and_preserves_legacy_index_identity(self):
        for changes in ({'raw_chunk_chars':63}, {'raw_chunk_chars':100,'raw_chunk_overlap_chars':50}, {'max_raw_chunks':0}):
            with self.assertRaises(ValueError):
                MemorySettings.from_dict(dict(recall_mode='raw_chunks', **changes))
        legacy = MemorySettings()
        self.assertNotEqual(legacy.index_fingerprint('hash-256'), self.settings.index_fingerprint('hash-256'))
        before = episode_index([('User original body.', 'Assistant original body.')], legacy)
        legacy.raw_chunk_chars = 6000
        self.assertEqual(before, episode_index([('User original body.', 'Assistant original body.')], legacy))

    def test_render_preserves_round_order_roles_and_literal_list_numbers(self):
        early = Episode(11, 'ceramics', 0, NOW, 'What should I pack?', '2. An apron\n4. A towel', .5, 'active')
        late = Episode(12, 'ceramics', 1, NOW, 'I have packed both.', 'Your bag is ready.', .5, 'active')
        other = Episode(13, 'travel', 0, NOW, 'My ferry is booked.', '', .5, 'active')
        parents = {e.id:e for e in [early, late, other]}
        def span(e, role):
            text = e.user_text if role=='User' else e.assistant_text
            return dict(episode_id=e.id, role=role, start=0, end=len(text))
        memory, episodes = render_chunks([span(late,'Assistant'), span(early,'Assistant'),
            span(early,'User'), span(other,'User'), span(late,'User')], parents)
        self.assertEqual([e.id for e,_ in episodes], [11,12,13])
        self.assertLess(memory.index('What should I pack?'), memory.index('2. An apron'))
        self.assertLess(memory.index('4. A towel'), memory.index('I have packed both.'))
        self.assertIn('2. An apron\n4. A towel', memory)
        self.assertIn('round=1', memory)
        self.assertIn('round=2', memory)

    def test_companions_are_bounded_literal_prefixes_from_the_same_round(self):
        e = Episode(11,'ceramics',0,NOW,'Original booking notes. '*20,'Packing advice.',.5,'active')
        row = dict(episode_id=e.id,role='Assistant',start=0,end=len(e.assistant_text))
        companions = companion_spans([row],{e.id:e},self.settings.raw_chunk_chars)
        self.assertEqual(len(companions),1)
        neighbor=companions[0]
        self.assertEqual(neighbor['text'],e.user_text[:128])
        self.assertEqual((neighbor['start'],neighbor['end']),(0,128))
        self.assertEqual(neighbor['source'],e.key)
        self.assertTrue(neighbor['prefix_limited'])
        self.assertEqual(companion_spans([row,dict(episode_id=e.id,role='User',start=0,end=128)],{e.id:e},128),[])

    def test_assistant_hit_returns_original_question_without_another_call(self):
        user='My booked workshop is a late morning wheel-throwing lesson.'
        assistant='Pack a towel and a spare apron.'
        store,_,_=self.ingest(text=user,assistant=assistant)
        chunks=store.records('episode_chunk')
        aid=next(c['id'] for c in chunks if c['role']=='Assistant')
        uid=next(c['id'] for c in chunks if c['role']=='User')
        self.settings.max_raw_chunks=1
        with patch.object(Store,'vector_search',return_value=[(aid,.9),(uid,.8)]), \
             patch.object(Store,'fts_search',return_value=[(aid,2.),(uid,1.)]):
            result=self.system().recall('alice','What should I pack?',NOW)
        self.assertEqual(result.trace['selected']['chunks'],['C'+str(aid)])
        self.assertEqual(len(result.trace['raw_chunks']['companions']),1)
        self.assertLess(result.memory.index(user),result.memory.index(assistant))
        self.assertEqual(result.trace['memory'],result.memory)
        self.assertEqual(result.items[0]['speakers'],['user','assistant'])
        self.assertEqual(len(result.items[0]['companions']),1)
        neighbor=result.items[0]['companions'][0]
        self.assertEqual(neighbor['text_sha256'],hashlib.sha256(user.encode()).hexdigest())

    def test_initial_retrieval_and_bge_ranks_both_contribute(self):
        rows=[dict(key=str(i),retrieval_rank=i,fusion_score=1/i,rerank_score=score)
              for i,score in enumerate([0.,3.,2.,1.],1)]
        fuse_ranks(rows,60)
        ordered=sorted(rows,key=lambda r:-r['rank_fusion_score'])
        self.assertEqual({r['retrieval_rank'] for r in ordered[:2]},{1,2})
        self.assertEqual(rows[0]['rerank_rank'],4)
        self.assertGreater(rows[0]['rank_fusion_score'],rows[2]['rank_fusion_score'])

    def test_diversity_changes_priority_without_merging_separate_events(self):
        texts=['Clean the oak bowl with a soft cloth.','Clean the oak bowl with a soft cloth.',
               'Wrap the glazed plate in a linen pad for transport.']
        parents={i:Episode(i,'session-'+str(i),0,NOW,text,'',.5,'active') for i,text in enumerate(texts,1)}
        def candidates():
            return [dict(key='C'+str(i),episode_id=i,role='User',start=0,end=len(text),text=text,
                         retrieval_rank=i,rank_fusion_score=.1-(i-1)*.001) for i,text in enumerate(texts,1)]
        self.settings.max_raw_chunks=2
        chosen,_=choose_spans(candidates(),parents,self.settings,budget_counter(self.settings),5000)
        self.assertEqual([c['episode_id'] for c in chosen],[1,3])
        self.settings.max_raw_chunks=3
        chosen,_=choose_spans(candidates(),parents,self.settings,budget_counter(self.settings),5000)
        self.assertEqual({c['episode_id'] for c in chosen},{1,2,3})

    def test_already_returned_user_context_does_not_consume_an_anchor_slot(self):
        first=Episode(11,'ceramics',0,NOW,'My studio booking is confirmed.','Bring a cloth.',.5,'active')
        other=Episode(12,'travel',0,NOW,'I booked a ferry.','',.5,'active')
        parents={e.id:e for e in [first,other]}
        rows=[]
        for rank,(e,role) in enumerate([(first,'Assistant'),(first,'User'),(other,'User')],1):
            text=e.user_text if role=='User' else e.assistant_text
            rows.append(dict(key='C'+str(rank),episode_id=e.id,role=role,start=0,end=len(text),text=text,
                             retrieval_rank=rank,rank_fusion_score=1/rank))
        self.settings.max_raw_chunks=2
        chosen,companions=choose_spans(rows,parents,self.settings,budget_counter(self.settings),5000)
        self.assertEqual([c['key'] for c in chosen],['C1','C3'])
        self.assertEqual(len(companions),1)
        self.assertEqual(rows[1]['omission_reason'],'already_in_returned_spans')

    def test_companion_text_is_included_in_the_same_context_budget(self):
        first=Episode(11,'ceramics',0,NOW,'Long original notes. '*20,'Bring a cloth.',.5,'active')
        row=dict(key='C1',episode_id=11,role='Assistant',start=0,end=len(first.assistant_text),
                 text=first.assistant_text,retrieval_rank=1,rank_fusion_score=.1)
        counter=budget_counter(self.settings)
        chosen,companions=choose_spans([row],{11:first},self.settings,counter,200)
        memory,_=render_chunks(chosen+companions,{11:first})
        self.assertLessEqual(counter.count(memory),200)
        self.assertEqual(row['omission_reason'],'context_budget')
