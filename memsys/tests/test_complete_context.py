import json
import re
import unittest
from datetime import datetime
from types import SimpleNamespace

from helpers import settings
from memsys.budget import budget_counter
from memsys.config import Models
from memsys.core import MemorySystem
from memsys.evidence_batch import complete_answer, paged_answer
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.read import Candidate
from memsys.schema import Episode, Fact, EPISODE_ACTIVE


class CompleteContextTest(unittest.TestCase):
    def config(self, **extra):
        return settings(complete_episode_context=True, semantic_validation=True,
                        budget_mode='utf8', answer_input_budget_tokens=16000, **extra)

    def test_core_keeps_full_originals_and_omits_duplicate_derived_summary(self):
        source = ['My ferry ticket for the island visit cost $41.', 'The taxi quote for that visit was $97.']
        assistant = 'For a different route, a ferry might cost $12.'
        def answer(messages):
            text = messages[-1]['content']
            for quote in source + [assistant]:
                self.assertIn(quote, text)
            self.assertNotIn('Incorrect derived price $999', text)
            return 'NOTES: E1, E2: 97 - 41 = 56.\nANSWER: 56'
        model = ScriptedChat({'answer': answer, 'answer_review': json.dumps(dict(responsive=True, supported=True))})
        cfg = self.config(query_analysis=False, max_history_share=0, min_similarity=0)
        system = MemorySystem(Models(chat=model, memory=None, embedder=HashEmbedder(), embedding_name='hash'), cfg)
        self.addCleanup(system.close)
        store = system.store('u')
        for n, text in enumerate(source):
            at = datetime(2025, 5, n+1)
            sid = store.create_session(at)
            eid = store.add_episode(sid, 0, at, text, assistant if n == 0 else '', 1, EPISODE_ACTIVE)
            if n == 0:
                store.add_fact(text='Incorrect derived price $999', kind='personal', subject='user',
                    attribute='price', value='999', event=None, recorded_at=at, valid_from=at.date(),
                    session_id=sid, importance=1, episode_ids=[eid])
        reply = system.respond('u', 'How much did I save choosing the ferry instead of the taxi? Reply with only the number.',
                               datetime(2025, 5, 4))
        audit = reply.trace['aggregation']
        self.assertEqual(reply.text, '56')
        self.assertEqual(audit['mode'], 'complete_original_context')
        self.assertEqual(audit['reviewed_sources'], ['E1','E2'])
        self.assertNotIn('evidence_page', model.purposes())
        self.assertLessEqual(audit['synthesis_memory_count'], cfg.context_budget_tokens)
        self.assertLessEqual(audit['synthesis_prompt_count'], cfg.answer_input_budget_tokens)

    def test_full_originals_over_budget_fall_back_without_truncating_the_answer_scope(self):
        episodes = [Episode(n, 's', n-1, datetime(2025, 5, 1), '', text + ' Unrelated details.'*40,
                            1, EPISODE_ACTIVE) for n,text in [(1,'Try the green binder.'),(2,'Try blue dividers.')]]
        result = SimpleNamespace(ranked=[Candidate('episode', e.id) for e in episodes],
                                 items={e.key:e for e in episodes}, coverage={'search_complete':True})
        def page(messages):
            text = messages[-1]['content']
            ids = re.findall(r'\[(E\d+)\]', text)
            items = [dict(source=e.key,quote=q) for e,q in zip(episodes,['Try the green binder.','Try blue dividers.'])
                     if e.key in ids and q in text]
            return json.dumps(dict(reviewed=ids,items=items))
        model = ScriptedChat({'evidence_page':page,'answer':'ANSWER: green binder, blue dividers',
                              'answer_review':json.dumps(dict(responsive=True,supported=True))})
        cfg = self.config(context_budget_tokens=400)
        self.assertIsNone(complete_answer(model,'List both suggestions.',result,{},cfg,'2025-05-04'))
        self.assertEqual(model.purposes(), [])
        _, answer, audit = paged_answer(model,'List both suggestions.',result,{},cfg,'2025-05-04')
        self.assertEqual(answer,'green binder, blue dividers')
        self.assertTrue(audit['pages_complete'])
        self.assertEqual(audit['reviewed_sources'], ['E1','E2'])

    def test_incomplete_coverage_or_missing_original_cannot_use_complete_context(self):
        e = Episode(1,'s',0,datetime(2025,5,1),'I own a cabin.','',1,EPISODE_ACTIVE)
        f = Fact(1,'I own a cabin.','personal','user','home','cabin',None,e.at,e.at.date(),None,
                 'current',None,False,1,1,'s')
        result = SimpleNamespace(ranked=[Candidate('episode',1),Candidate('fact',1)],items={'E1':e,'F1':f},
                                 coverage={'search_complete':False})
        model = ScriptedChat({})
        cfg = self.config()
        self.assertIsNone(complete_answer(model,'Where do I live?',result,{'F1':{1}},cfg,'2025-05-04'))
        result.coverage['search_complete'] = True
        for missing in [{}, {'F1':{2}}]:
            self.assertIsNone(complete_answer(model,'Where do I live?',result,missing,cfg,'2025-05-04'))
        self.assertEqual(model.purposes(), [])

    def test_complete_context_still_rejects_fabricated_numeric_proof(self):
        e = Episode(1,'s',0,datetime(2025,5,1),'My only terrarium has 3 snails.','',1,EPISODE_ACTIVE)
        result = SimpleNamespace(ranked=[Candidate('episode',1)],items={'E1':e},coverage={'search_complete':True})
        proof = dict(operation='count_groups',unit='snails',result='9',items=[dict(
            source='E1',quote=e.text(),value='9',unit='snails',quantity_label='snails')])
        output = 'CALCULATION: '+json.dumps(proof)+'\nANSWER: 9'
        model = ScriptedChat({'answer':output,'calculation_repair':output,'answer_repair':output,
                              'answer_review':json.dumps(dict(responsive=True,supported=True))})
        _, answer, audit = complete_answer(model,'What is my total number of snails?',result,{},self.config(),'2025-05-04')
        self.assertEqual(audit['status'],'invalid')
        self.assertNotEqual(answer,'9')

    def test_switch_changes_read_fingerprint_and_preserves_written_memories(self):
        before = settings(complete_episode_context=False)
        after = settings(complete_episode_context=True)
        self.assertEqual(before.write_fingerprint(), after.write_fingerprint())
        self.assertNotEqual(before.read_fingerprint(), after.read_fingerprint())


if __name__ == '__main__':
    unittest.main()
