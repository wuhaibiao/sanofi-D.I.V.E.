from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from helpers import dt, settings
from memsys.config import Models
from memsys.core import MemorySystem
from memsys.fake import ScriptedChat
from memsys.indexing import episode_index
from memsys.llm import ModelError
from memsys.read import Retriever, heuristic_plan
from memsys.store import Store
from memsys.textutil import estimate_tokens


class ConceptEmbedder:
    """自拟语义映射，让完全不同措辞的尾部命中具有确定性；不声称真实模型效果。"""
    def __init__(self):
        self.inputs = []
        self.fail = False

    def embed(self, texts, **kwargs):
        if self.fail:
            raise ModelError('embedding unavailable')
        self.inputs.extend(texts)
        return [[1., 0.] if 'tandem' in text or '双人自行车' in text else [0., 1.] for text in texts]


class ChunkIndexTest(unittest.TestCase):
    def test_lossless_offsets_roles_and_input_caps(self):
        user = '  我记了一段中文。\n' * 180
        assistant = 'Details and examples.\n\n' * 260 + 'Tail detail.'
        plan = episode_index([(user, assistant)], settings(embed_chars=137, embed_chunk_tokens=48))
        self.assertGreater(len(plan.texts), 10)
        for role, original in [('User', user), ('Assistant', assistant)]:
            pieces = [c for c in plan.chunks[0] if c.role == role]
            self.assertEqual(''.join(original[c.start:c.end] for c in pieces), original)
            self.assertEqual(pieces[0].start, 0)
            self.assertEqual(pieces[-1].end, len(original))
            for chunk in pieces:
                self.assertEqual(plan.texts[chunk.vector_index], role + ': ' + original[chunk.start:chunk.end])
                self.assertLessEqual(chunk.end - chunk.start, 137)
                self.assertLessEqual(estimate_tokens(plan.texts[chunk.vector_index]), 48)

    def test_legacy_mode_and_invalid_chunk_settings(self):
        plan = episode_index([('hello', 'world' * 100)], settings(episode_chunk_vectors=False, embed_chars=20))
        self.assertEqual(len(plan.texts), 1)
        self.assertEqual(len(plan.texts[0]), 20)
        self.assertEqual(plan.chunks, {})
        with self.assertRaises(ValueError):
            settings(embed_chunk_tokens=0).validate()
        with self.assertRaises(ValueError):
            episode_index([('hello', '')], settings(embed_chars=0))

    def system(self, rerank=False):
        embed = ConceptEmbedder()
        model = ScriptedChat({'extract': '{"facts": []}', 'rerank': '{"relevant": ["E1"]}'})
        system = MemorySystem(Models(chat=None, memory=model, embedder=embed, embedding_name='concept'),
                              settings(query_analysis=False, min_similarity=.8, fallback_min_candidates=0,
                                       embed_chars=120, embed_chunk_tokens=64, episode_max_tokens=110,
                                       context_budget_tokens=250, max_history_share=0, llm_rerank=rerank))
        self.addCleanup(system.close)
        return system, embed, model

    def ingest(self, system, user, assistant):
        at = dt('2025-02-01T10:00')
        handle = system.open_session('u', at)
        system.add_turn(handle, 'user', user, at)
        if assistant:
            system.add_turn(handle, 'assistant', assistant, at)
        system.close_session(handle, at)

    def test_semantic_tail_without_shared_keywords_reaches_context_and_rerank(self):
        system, embed, model = self.system(rerank=True)
        tail = 'The recommended vehicle is a tandem.'
        self.ingest(system, 'Please advise.', 'background. ' * 500 + tail)
        recall = system.recall('u', '双人自行车', dt('2025-02-02T10:00'))
        self.assertIn(tail, recall.memory)
        self.assertIn('Assistant:', recall.memory)
        self.assertLessEqual(recall.trace['memory_tokens'], recall.trace['effective_budget'])
        self.assertIn('dense_chunks', recall.trace['candidates'][0]['via'])
        self.assertTrue(recall.trace['candidates'][0]['matched_chunks'])
        self.assertIn(tail, model.calls[-1][1][1]['content'])
        self.assertTrue(any(tail in text for text in embed.inputs))

    def test_user_tail_and_forgetting_use_episode_status(self):
        system, _, _ = self.system()
        self.ingest(system, 'background. ' * 500 + 'I bought a tandem.', '')
        self.assertIn('I bought a tandem.', system.recall('u', '双人自行车', dt('2025-02-02T10:00')).memory)
        store = system.store('u')
        self.assertGreater(len(store.vectors('episode_chunk', ('active',))[0]), 1)
        system.forget('u', 'E1')
        self.assertEqual(store.vectors('episode_chunk', ('active', 'archived'))[0], [])
        self.assertEqual(system.search('u', '双人自行车').ranked, [])

    def test_many_matching_chunks_add_one_rrf_contribution_per_episode(self):
        system, _, _ = self.system()
        self.ingest(system, 'tandem ' * 1000, '')
        result = Retriever(system.store('u'), system.models, system.settings).search(heuristic_plan('双人自行车'))
        self.assertEqual(len(result.ranked), 1)
        self.assertAlmostEqual(result.ranked[0].score, 1 / 61)
        self.assertLessEqual(len(result.ranked[0].chunks), 4)

    def test_reindex_is_atomic_idempotent_and_supports_legacy_database(self):
        system, embed, _ = self.system()
        self.ingest(system, 'background. ' * 100 + 'I bought a tandem.', '')
        store = system.store('u')
        old_ids, old_vectors = store.vectors('episode_chunk', ('active',))
        embed.fail = True
        system.models.embedding_name = 'replacement'
        with self.assertRaises(ModelError):
            system.reindex('u')
        self.assertEqual(store.get_meta('embedding_model'), 'concept')
        self.assertEqual(store.vectors('episode_chunk', ('active',)), (old_ids, old_vectors))
        embed.fail = False
        self.assertEqual(system.reindex('u'), 1)
        self.assertEqual(len(store.vectors('episode_chunk', ('active',))[0]), len(old_ids))
        self.assertEqual(system.reindex('u'), 1)
        self.assertEqual(len(store.vectors('episode_chunk', ('active',))[0]), len(old_ids))
        system.settings.episode_chunk_vectors = False
        system.reindex('u')
        self.assertEqual(store.vectors('episode_chunk', ('active',))[0], [])
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM episode_chunks').fetchone()[0], 0)

    def test_old_schema_opens_and_legacy_vectors_remain_searchable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'old.sqlite')
            store = Store(path)
            with store.transaction():
                eid = store.add_episode('s1', 0, dt('2025-02-01'), 'tandem', '', .6, 'active')
                store.put_vectors('episode', [(eid, [1., 0.])])
                store.set_meta('embedding_model', 'concept')
            store.close()
            connection = sqlite3.connect(path)
            connection.execute('DROP TABLE episode_chunks')
            connection.commit()
            connection.close()
            store = Store(path)
            try:
                models = Models(None, None, ConceptEmbedder(), 'concept')
                result = Retriever(store, models, settings(min_similarity=.8)).search(heuristic_plan('双人自行车'))
                self.assertEqual([c.key for c in result.ranked], ['E1'])
                self.assertEqual(store.vectors('episode_chunk', ('active',))[0], [])
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
