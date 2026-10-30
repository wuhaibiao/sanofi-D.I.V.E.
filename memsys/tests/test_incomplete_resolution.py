import json
import unittest
from datetime import datetime

from helpers import fact, facts_json, settings
from memsys.config import Models
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.llm import ChatResult, IncompleteJSONError, ModelError, chat_json
from memsys.schema import FACT_CURRENT


def reply(text, reason):
    return ChatResult(text=text, model='fake', prompt_tokens=0, completion_tokens=0,
                      cached_tokens=0, cost=0, latency_s=0, finish_reason=reason)


class IncompleteResolutionTest(unittest.TestCase):
    def test_truncated_decision_preserves_old_and_new_with_original_sources(self):
        decision = json.dumps({'decisions':[dict(new='N1',action='update',target='F1',reason='new city')]})
        for reason in ['length','incomplete']:
            with self.subTest(reason=reason):
                memory = ScriptedChat({'extract':[
                    facts_json(fact('User lives in Porto.',[1],'home_city')),
                    facts_json(fact('User now lives in Rennes.',[1],'home_city'))],
                    'resolve':lambda messages: reply(decision,reason)})
                system = MemorySystem(Models(chat=None,memory=memory,embedder=HashEmbedder(),embedding_name='hash'),
                                      settings(resolve_similarity=0))
                self.addCleanup(system.close)
                reports=[]
                for day,text in [(1,'I live in Porto.'),(5,'I now live in Rennes.')]:
                    at=datetime(2025,5,day)
                    session=system.open_session('u',at)
                    system.add_turn(session,'user',text,at)
                    reports.append(system.close_session(session,at)[0])
                stored=system.store('u').facts()
                self.assertEqual(len(stored),2)
                self.assertTrue(all(f.status==FACT_CURRENT and f.superseded_by is None for f in stored))
                self.assertEqual(reports[-1].updates,[])
                self.assertEqual(set(system.store('u').sources(f.id for f in stored)),{1,2})
                self.assertIn('resolve',memory.purposes())
                self.assertEqual(system.stats('u')['backlog'],0)

    def test_incomplete_json_cannot_apply_even_a_parseable_partial_decision(self):
        model=ScriptedChat({'resolve':lambda messages:reply('{"decisions":[]}','length')})
        with self.assertRaises(IncompleteJSONError) as caught:
            chat_json(model,[{'role':'user','content':'Return JSON.'}],purpose='resolve')
        self.assertEqual(caught.exception.finish_reason,'length')

    def test_account_failure_still_propagates_and_cannot_be_a_missing_decision(self):
        def failure(messages):
            raise ModelError('Insufficient account funds',status=402)
        model=ScriptedChat({'extract':[
            facts_json(fact('User lives in Porto.',[1],'home_city')),
            facts_json(fact('User now lives in Rennes.',[1],'home_city'))], 'resolve':failure})
        system=MemorySystem(Models(chat=None,memory=model,embedder=HashEmbedder(),embedding_name='hash'),
                            settings(resolve_similarity=0))
        self.addCleanup(system.close)
        at=datetime(2025,5,1)
        session=system.open_session('u',at)
        system.add_turn(session,'user','I live in Porto.',at)
        system.close_session(session,at)
        later=datetime(2025,5,5)
        session=system.open_session('u',later)
        system.add_turn(session,'user','I now live in Rennes.',later)
        with self.assertRaises(ModelError) as caught:
            system.close_session(session,later)
        self.assertTrue(caught.exception.account_problem)
        self.assertNotIsInstance(caught.exception,IncompleteJSONError)
        stored=system.store('u').facts()
        self.assertEqual(len(stored),1)
        self.assertEqual(stored[0].status,FACT_CURRENT)
        self.assertIsNone(stored[0].superseded_by)


if __name__=='__main__':
    unittest.main()
