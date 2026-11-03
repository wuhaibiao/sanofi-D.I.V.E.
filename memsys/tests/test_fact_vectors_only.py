"""Authored source canaries prove that originals never reach the vector index."""
from dataclasses import replace
from datetime import datetime
import json
import unittest

from memsys.config import MemorySettings, Models
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.indexing import episode_index
from memsys.qdrant import QdrantSettings
from memsys.store import Store, EmbeddingMismatch
from memsys.write import Writer
from tests.qdrant_fixture import Engine

NOW = datetime(2026, 11, 1, 10)
SOURCE = 'For my invented notebook project, I chose blue. SOURCE_ONLY_CANARY stays in the original.'
FACT = 'The user chose a blue notebook.'


class RecordingEmbedder(HashEmbedder):
    def __init__(self):
        super().__init__()
        self.inputs = []

    def embed(self, texts, **kwargs):
        self.inputs.extend(texts)
        return super().embed(texts, **kwargs)


class FactVectorsOnlyTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.storage = QdrantSettings(namespace='authored-fact-vectors')
        self.embedder = RecordingEmbedder()
        self.chat = ScriptedChat({'extract': json.dumps({'facts':[dict(text=FACT,kind='personal',
            subject='user',attribute='notebook_color',value='blue',rounds=[1])]})})
        self.models = Models(None,self.chat,self.embedder,'authored-hash')
        self.settings = MemorySettings(index_originals=False,extract_context_memories=0,rerank_backend='fusion')
        self.store = Store(user_id='alice',client=self.engine,settings=self.storage)

    def write(self, settings=None):
        sid = self.store.create_session(NOW)
        self.store.append_turn(sid,'user',SOURCE,NOW)
        Writer(self.store,self.models,settings or self.settings).consolidate(sid,closed_at=NOW)
        return sid

    def test_write_indexes_facts_and_preserves_sources_without_original_vectors(self):
        sid = self.write()
        self.assertTrue(self.embedder.inputs)
        self.assertTrue(all('SOURCE_ONLY_CANARY' not in text for text in self.embedder.inputs))
        self.assertEqual(len(self.store.vectors('fact',('current',))[0]),1)
        self.assertEqual(self.store.vectors('episode',('active',))[0],[])
        self.assertEqual(self.store.records('episode_chunk'),[])
        self.assertEqual(self.store.turns(sid)[0]['content'],SOURCE)
        self.assertEqual(self.store.episodes()[0].user_text,SOURCE)
        self.assertEqual(self.store.session(sid)['status'],'closed')
        self.assertTrue(self.store.sources([self.store.facts()[0].id])[self.store.facts()[0].id])
        self.assertFalse(json.loads(self.store.get_meta('index_manifest'))['index_originals'])

    def test_empty_extraction_does_not_call_embedding_for_original_fallback(self):
        self.chat.handlers['extract'] = '{"facts":[]}'
        self.write()
        self.assertEqual(self.embedder.inputs,[])
        self.assertEqual(len(self.store.episodes()),1)
        self.assertEqual(self.store.index_coverage()['indexed_episodes'],0)
        self.assertEqual(self.store.get_meta('index_fingerprint'),self.settings.index_fingerprint('authored-hash'))

    def test_disabled_mode_skips_both_prefix_and_chunk_vector_plans(self):
        for chunked in (True,False):
            plan = episode_index([(SOURCE,'Assistant source canary')],replace(self.settings,episode_chunk_vectors=chunked))
            self.assertEqual((plan.texts,plan.chunks,plan.representatives),([],{},{}))

    def test_reindex_removes_old_original_vectors_without_reembedding_facts(self):
        enabled = replace(self.settings,index_originals=True)
        self.write(enabled)
        self.assertTrue(self.store.vectors('episode',('active',))[0])
        self.assertTrue(self.store.records('episode_chunk'))
        before = self.store.vectors('fact',('current',))
        self.embedder.inputs.clear()
        system = MemorySystem(self.models,self.settings,
            store_factory=lambda **kw: Store(user_id=kw['user_id'],settings=self.storage,client=self.engine))
        self.addCleanup(system.close)
        self.assertEqual(system.reindex('alice'),1)
        self.assertEqual(self.embedder.inputs,[])
        self.assertEqual(self.store.vectors('fact',('current',)),before)
        self.assertEqual(self.store.vectors('episode',('active',))[0],[])
        self.assertEqual(self.store.records('episode_chunk'),[])
        self.assertEqual(self.store.episodes()[0].user_text,SOURCE)
        self.assertEqual(system.reindex('alice'),1)
        self.assertTrue(self.store.validate_index()['valid'])
        self.assertEqual(self.store.vector_search('episode',[1.]+[0.]*255,('active',),5),[])

    def test_index_policy_is_frozen_and_cannot_silently_switch_on_next_write(self):
        enabled = replace(self.settings,index_originals=True)
        self.assertNotEqual(enabled.index_fingerprint('authored-hash'),self.settings.index_fingerprint('authored-hash'))
        self.write()
        with self.assertRaises(EmbeddingMismatch):
            self.write(enabled)

    def test_raw_recall_requires_original_vectors(self):
        with self.assertRaisesRegex(ValueError,'requires facts recall'):
            replace(self.settings,recall_mode='raw_chunks').validate()


if __name__ == '__main__':
    unittest.main()
