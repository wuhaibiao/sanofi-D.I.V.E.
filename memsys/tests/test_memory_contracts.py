"""Independent content, provenance, selection and single-call host contracts."""
import copy
from datetime import datetime
import json
from pathlib import Path
import unittest

from memsys.fact_selection import order_facts
from memsys.memory_compaction import covers_literal
from memsys.read import Candidate, QueryPlan, SearchResult, Retriever
from memsys.relational_memory import ground_relations, normalize_relations
from memsys.structure_memory import assistant_structures
from memsys.write import NewFact, Round, WriteReport
from memsys.write_coverage import source_coverage
from tests.test_structured_facts import StructuredFactFixture

FIXTURE=json.loads((Path(__file__).parent/'fixtures/memory_contracts_synthetic.json').read_text())
NOW=datetime(2026,9,9)


class LiteralContracts(unittest.TestCase):
    def test_short_details_and_nested_values_keep_parent_associations(self):
        for case in FIXTURE['structures']:
            with self.subTest(case=case['name']):
                structures=assistant_structures(case['source'])
                value=json.dumps([s.value for s in structures],ensure_ascii=False)
                for text in case['required']:self.assertIn(text,value)
                for text in case.get('excluded',[]):self.assertNotIn(text,value)
                self.assertTrue(all(s.quote in case['source'] for s in structures))
        first=assistant_structures(FIXTURE['structures'][0]['source'])[0].value
        self.assertEqual(first['items'][1]['children'][0]['name'],'Material')
        self.assertEqual(first['items'][1]['children'][0]['details'],'birch plywood')
        self.assertEqual([x['position'] for x in first['items']],[4,5,6])

    def test_title_only_model_value_does_not_cover_detail_or_children(self):
        value=assistant_structures(FIXTURE['structures'][0]['source'])[0].value
        self.assertTrue(covers_literal(value,value))
        shortened=copy.deepcopy(value)
        for item in shortened['items']:
            item.pop('details',None);item.pop('children',None)
        self.assertFalse(covers_literal(shortened,value))
        swapped=copy.deepcopy(value)
        swapped['items'][1]['children'].reverse()
        self.assertFalse(covers_literal(swapped,value))
        wrong_context=copy.deepcopy(value);wrong_context['headers']=['A different workshop']
        self.assertFalse(covers_literal(wrong_context,value))

    def test_long_details_are_reported_whole_as_omitted_not_truncated(self):
        value=assistant_structures('3. Shield: '+'ceramic. '*100+'\n4. Seal: wax')[0].value
        self.assertEqual(value['omitted_positions'],[3])
        self.assertNotIn('details',value['items'][0])
        self.assertFalse(covers_literal(value,value))

    def test_markdown_titles_keep_values_and_child_scope_after_blank_line(self):
        value=assistant_structures('1. **Shell:** matte glaze\n2. **Base:**\n\n  - **Material:** cork\n3. End.')[0].value
        self.assertEqual(value['items'][0]['details'],'matte glaze')
        self.assertEqual(value['items'][1]['children'],[dict(name='Material',details='cork')])


class StateContracts(unittest.TestCase):
    def test_a_future_until_date_does_not_prove_a_relationship_has_ended(self):
        from memsys.state_evidence import termination_witness
        relation=normalize_relations([dict(subject='Mira',predicate='leads',object='Juniper',state='ended')])[0]
        self.assertIsNone(termination_witness(relation,[('user','Mira led Juniper until 2027-08-12.')],NOW.date()))
    def test_termination_of_a_different_object_or_actor_does_not_transfer(self):
        from memsys.state_evidence import termination_witness
        relation=normalize_relations([dict(subject='user',predicate='owns',object='mandolin',state='ended')])[0]
        for source in ('I sold the banjo and my mandolin is still here.',
                       'Rosa sold a mandolin.', 'I might have sold my mandolin.',
                       'Did I no longer own the mandolin?', 'Suppose I sold my mandolin.'):
            self.assertIsNone(termination_witness(relation,[('user',source)]))

    def test_a_completed_disposition_predicate_uses_its_literal_source(self):
        from memsys.state_evidence import termination_witness
        relation=normalize_relations([dict(subject='user',predicate='sold',object='mandolin',state='ended')])[0]
        self.assertIsNotNone(termination_witness(relation,[('user','I sold my mandolin.')]))

    def test_termination_requires_an_affirmative_object_specific_witness(self):
        for case in FIXTURE['states']:
            with self.subTest(source=case['source']):
                subject=case.get('subject','user')
                fact=NewFact('A proposed fixture observation.','personal',subject,'fixture_state',case['object'],None,[1],.6,
                    source_quotes=[dict(round=1,role='user',quote=case['source'])],
                    relations=normalize_relations([dict(subject=subject,predicate=case['predicate'],object=case['object'],state=case['asserted'])]))
                report=WriteReport('fixture')
                ground_relations([fact],[Round(1,0,NOW,case['source'],'')],report=report)
                self.assertEqual(fact.relations[0]['state'],case['expected'])
                self.assertTrue(fact.relations[0]['source_grounded'])
                if case['expected']=='unknown':
                    self.assertEqual(fact.relations[0]['state_evidence']['status'],'unverified')
                    self.assertLessEqual(fact.confidence,.5)
                    self.assertTrue(report.relation_warnings)


class SourceAccountingContracts(unittest.TestCase):
    def test_nonempty_facts_cannot_hide_an_unaccounted_round(self):
        rounds=[Round(1,0,NOW,'Keep this.','A named choice.'),Round(2,1,NOW,'Another part.','Another named choice.')]
        fact=NewFact('A named choice.','assistant','assistant','choice','A named choice.',None,[1],.6)
        records,warnings=source_coverage({'facts':[{}]},rounds,[fact])
        self.assertEqual(len(records),4)
        self.assertEqual(len(warnings),4)
        self.assertTrue(all(r['semantic_coverage']=='not_verified' for r in records))

    def test_coverage_declarations_validate_round_roles_and_literal_omissions(self):
        rounds=[Round(1,0,NOW,'Thanks.','A general explanation.')]
        data={'coverage':[dict(round=1,role='user',decision='no_new_memory',reason='greeting',omitted=[]),
                          dict(round=1,role='assistant',decision='no_new_memory',reason='background',
                               omitted=[dict(quote='A general explanation.',reason='background')])]}
        records,warnings=source_coverage(data,rounds,[])
        self.assertTrue(all(r['accounting']=='declared' for r in records))
        self.assertEqual(warnings[0]['code'],'declared_source_omissions')
        data['coverage'][1]['omitted'][0]['quote']='An invented quotation.'
        self.assertEqual(source_coverage(data,rounds,[])[0][1]['accounting'],'invalid')

    def test_sampled_omission_accounting_never_claims_complete_coverage(self):
        rounds=[Round(1,0,NOW,'A general introduction. Further explanation.','')]
        data={'coverage':[dict(round=1,role='user',decision='no_new_memory',reason='background',
            omitted=[dict(quote='A general introduction.',reason='background')],omissions_complete=False)]}
        records,warnings=source_coverage(data,rounds,[])
        self.assertEqual(records[0]['accounting'],'declared')
        self.assertFalse(records[0]['omissions_complete'])
        self.assertEqual(records[0]['semantic_coverage'],'not_verified')
        self.assertEqual(warnings[0]['code'],'declared_source_omissions_incomplete')

    def test_oversized_omission_audit_is_invalid_without_rejecting_facts(self):
        text='General prose. '*20
        rounds=[Round(1,0,NOW,text,'')]
        data={'coverage':[dict(round=1,role='user',decision='no_new_memory',reason='background',
            omitted=[dict(quote=text,reason='background')])]}
        records,warnings=source_coverage(data,rounds,[])
        self.assertEqual(records[0]['accounting'],'invalid')
        self.assertEqual(warnings[0]['code'],'source_accounting_invalid')


class SelectionContracts(StructuredFactFixture,unittest.TestCase):
    def pool(self):
        ids=[]
        for i in range(9):
            self.write('Optional supplier '+str(i),text=FIXTURE['retrieval']['assistant_noise']+str(i))
        for i,text in enumerate(FIXTURE['retrieval']['personal_events']):
            self.write(text,text=text,extra=dict(kind='event',subject='user',
                semantics=dict(value_type='event_quantity',object_key='tool-'+str(i),event_key='workshop-'+str(i))))
        facts=self.store.facts()
        facts.sort(key=lambda f:f.kind!='assistant')
        return SearchResult([Candidate('fact',f.id) for f in facts],{f.key:f for f in facts},False)

    def test_personal_collection_protects_independent_user_events(self):
        result=self.pool();plan=QueryPlan([FIXTURE['retrieval']['question']],[],None,True,'past','heuristic')
        ordered,audit=order_facts(result,plan)
        self.assertEqual({c.key for c in ordered},{c.key for c in result.ranked})
        self.assertTrue(all(result.items[c.key].kind=='event' for c in ordered[:3]))
        self.settings.max_facts_all=3
        selection=Retriever(self.store,self.models,self.settings).select(result,plan,8000,NOW.date())
        self.assertEqual(len(selection.facts),3)
        self.assertTrue(all(f.kind=='event' for f in selection.facts))
        self.assertEqual(selection.ordering['scope'],'user')

    def test_assistant_recall_preserves_assistant_priority(self):
        result=self.pool();plan=QueryPlan([FIXTURE['retrieval']['assistant_question']],[],None,False,'past','heuristic')
        result.ranked.reverse()
        ordered,audit=order_facts(result,plan)
        self.assertEqual(audit['scope'],'assistant')
        self.assertEqual(result.items[ordered[0].key].kind,'assistant')

    def test_new_recommendation_requests_do_not_become_assistant_history_queries(self):
        result=self.pool()
        for question in ('Can you recommend a tool for my project?','你能推荐适合我的工具吗？'):
            plan=QueryPlan([question],[],None,False,'any','heuristic',operation='advice')
            ordered,audit=order_facts(result,plan)
            self.assertEqual(audit['scope'],'user')
            self.assertNotEqual(result.items[ordered[0].key].kind,'assistant')




if __name__=='__main__':unittest.main()
