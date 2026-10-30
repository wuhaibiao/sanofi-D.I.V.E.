"""One bounded protocol repair never invents a relationship or changes accepted IDs."""
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from helpers import fact,facts_json,settings
from memsys.config import Models
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder,ScriptedChat
from memsys.llm import ChatResult,ModelError
from memsys.schema import FACT_CURRENT,FACT_SUPERSEDED
from memsys.update import Updater
from memsys.write import NewFact


def response(*rows):
    return json.dumps({'decisions':list(rows)})


class ResolveProtocolTest(unittest.TestCase):
    def system(self,replies):
        memory=ScriptedChat({'extract':[
            facts_json(fact('User uses the cedar workshop.',[1],'workspace')),
            facts_json(fact('User now uses the silver workshop.',[1],'workspace'))],
            'resolve':replies})
        system=MemorySystem(Models(chat=None,memory=memory,embedder=HashEmbedder(),embedding_name='hash'),
                            settings(resolve_similarity=0,confirm_conflicts=True))
        self.addCleanup(system.close)
        at=datetime(2025,4,1)
        sid=system.open_session('u',at);system.add_turn(sid,'user','I use the cedar workshop.',at)
        system.close_session(sid,at)
        return system,memory

    def close_second(self,system):
        at=datetime(2025,4,5)
        sid=system.open_session('u',at);system.add_turn(sid,'user','I now use the silver workshop.',at)
        return system.close_session(sid,at)[0]

    def test_unlinked_uncertainty_is_valid_without_an_invented_target(self):
        system,memory=self.system(response(dict(new='N1',action='uncertain',target=None)))
        report=self.close_second(system)
        self.assertEqual(report.resolve_fallbacks,0);self.assertEqual(report.resolve_repairs,[])
        self.assertEqual(len(report.uncertain),1);self.assertEqual(report.pending,[])
        old,new=system.store('u').facts()
        self.assertEqual(old.status,FACT_CURRENT);self.assertIsNone(old.superseded_by)
        self.assertTrue(new.uncertain);self.assertLessEqual(new.confidence,0.5)
        self.assertEqual(memory.purposes().count('resolve'),1)

    def test_missing_update_target_is_repaired_once_and_original_error_is_retained(self):
        system,memory=self.system([
            response(dict(new='N1',action='update',target=None)),
            response(dict(new='N1',action='update',target='F1'))])
        report=self.close_second(system)
        self.assertEqual(report.resolve_fallbacks,0)
        self.assertEqual(report.resolve_repairs[0]['attempts'],1)
        self.assertEqual(report.resolve_repairs[0]['status'],'complete')
        self.assertIn('action=update',next(iter(report.resolve_repairs[0]['initial_errors'].values())))
        self.assertEqual(memory.purposes().count('resolve'),2)
        old,new=system.store('u').facts()
        self.assertEqual(old.status,FACT_SUPERSEDED);self.assertEqual(old.superseded_by,new.id)
        self.assertEqual(set(system.store('u').sources([old.id,new.id])),{old.id,new.id})

    def test_invalid_target_after_single_repair_preserves_old_and_new(self):
        system,memory=self.system(response(dict(new='N1',action='update',target='F999999')))
        report=self.close_second(system)
        self.assertEqual(memory.purposes().count('resolve'),2)
        self.assertEqual(report.resolve_fallbacks,1)
        self.assertEqual(report.resolve_repairs[0]['status'],'incomplete')
        self.assertTrue(all(f.status==FACT_CURRENT and f.superseded_by is None for f in system.store('u').facts()))

    def test_truncated_initial_json_is_not_applied_or_protocol_retried(self):
        text=response(dict(new='N1',action='update',target='F1'))
        reply=ChatResult(text,'fake',0,0,0,0,0,'length')
        system,memory=self.system(lambda messages:reply)
        report=self.close_second(system)
        self.assertEqual(memory.purposes().count('resolve'),1)
        self.assertEqual(report.resolve_repairs,[])
        self.assertTrue(all(f.status==FACT_CURRENT for f in system.store('u').facts()))

    def test_repair_cannot_override_previously_accepted_global_ids(self):
        system,memory=self.system([
            response(dict(new='N1',action='duplicate',target='F1'),dict(new='N2',action='update',target=None)),
            response(dict(new='N1',action='update',target='F1'),dict(new='N2',action='uncertain',target=None))])
        updater=Updater(system.store('u'),system.models,settings(resolve_similarity=0))
        facts=[NewFact('User uses the cedar workshop.','personal','user','workspace','cedar',None,[1],3),
               NewFact('User may use the silver workshop.','personal','user','workspace','silver',None,[1],3)]
        vectors=system.models.embedder.embed([fact.text for fact in facts])
        choices=updater.resolve(facts,vectors,datetime(2025,4,5))
        self.assertEqual([choice.action for choice,_ in choices],['duplicate','uncertain'])
        self.assertEqual(updater.last_errors,[])
        prompt=memory.calls[-1][1][-1]['content']
        self.assertNotIn('[N1]',prompt);self.assertIn('[N2]',prompt)
        self.assertEqual(updater.last_repairs[0]['indexes'],[2])

    def test_duplicate_decision_ids_do_not_silently_use_last_wins(self):
        system,memory=self.system([
            response(dict(new='N1',action='duplicate',target='F1'),dict(new='N1',action='update',target='F1')),
            response(dict(new='N1',action='add',target=None))])
        report=self.close_second(system)
        self.assertEqual(report.updates,[]);self.assertEqual(report.duplicates,0)
        self.assertIn('duplicate resolve decision',next(iter(report.resolve_repairs[0]['initial_errors'].values())))
        self.assertTrue(all(f.status==FACT_CURRENT for f in system.store('u').facts()))

    def test_repair_account_failure_propagates_without_committing_new_values(self):
        calls=[]
        def resolve(messages):
            calls.append(messages)
            if len(calls)==1:return response(dict(new='N1',action='update',target=None))
            raise ModelError('Insufficient account funds',status=402)
        system,_=self.system(resolve)
        with self.assertRaises(ModelError):self.close_second(system)
        stored=system.store('u').facts()
        self.assertEqual(len(stored),1);self.assertEqual(stored[0].status,FACT_CURRENT)

    def test_repair_does_not_accept_parseable_but_truncated_new_decisions(self):
        text=response(dict(new='N1',action='update',target='F1'))
        count=[]
        def resolve(messages):
            count.append(messages)
            return response(dict(new='N1',action='update',target=None)) if len(count)==1 else ChatResult(text,'fake',0,0,0,0,0,'length')
        system,_=self.system(resolve)
        report=self.close_second(system)
        self.assertEqual(len(count),2);self.assertEqual(report.resolve_fallbacks,1)
        self.assertTrue(all(f.status==FACT_CURRENT for f in system.store('u').facts()))

    def test_over_budget_repair_does_not_call_or_drop_source_context(self):
        system,memory=self.system(response(dict(new='N1',action='update',target=None)))
        updater=Updater(system.store('u'),system.models,settings(resolve_similarity=0,memory_input_budget_tokens=20))
        facts=[NewFact('User now uses the silver workshop.','personal','user','workspace','silver',None,[1],3)]
        vectors=system.models.embedder.embed([f.text for f in facts])
        class Counter:
            def messages(self,messages):
                return 21 if 'only protocol repair attempt' in messages[-1]['content'] else 10
        with patch('memsys.update.budget_counter',return_value=Counter()):
            choices=updater.resolve(facts,vectors,datetime(2025,4,5))
        self.assertEqual(memory.purposes().count('resolve'),1)
        self.assertEqual(choices[0][0].action,'new')
        self.assertEqual(updater.last_repairs[0]['status'],'skipped')
        self.assertEqual(updater.last_repairs[0]['attempts'],0)
        self.assertIn('missing resolve target',updater.last_errors[0])

    def test_uncertain_named_but_unavailable_target_is_not_silently_unlinked(self):
        system,memory=self.system(response(dict(new='N1',action='uncertain',target='F999999')))
        report=self.close_second(system)
        self.assertEqual(memory.purposes().count('resolve'),2)
        self.assertEqual(report.resolve_fallbacks,1)
        self.assertEqual(report.uncertain,[])
        self.assertTrue(all(f.status==FACT_CURRENT for f in system.store('u').facts()))
