import json
import re
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from helpers import settings
from memsys.aggregate_batch import paged_aggregate
from memsys.aggregation import answer_with_validation, inspect_calculation
from memsys.config import Models
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.llm import ModelError
from memsys.evidence_batch import identity_context, paged_answer
from memsys.budget import budget_counter
from memsys.queryops import operation_for
from memsys.read import Candidate, plan_query, question_qualifiers
from memsys.schema import Episode, EPISODE_ACTIVE
from memsys.semantic_review import select_excerpts


class CrossSessionPrecisionTest(unittest.TestCase):
    def proof(self, items, total, evidence, unit='days'):
        output = 'CALCULATION: ' + json.dumps(dict(operation='sum', unit=unit, items=items, result=str(total)))
        return inspect_calculation(output + '\nANSWER: ' + str(total), evidence)[1]

    def test_word_hyphenated_and_converted_durations(self):
        quotes = ['My visit lasted four days.', 'I took a 3-day trip.', 'I spent one week there.']
        evidence = {'E%d' % (n+1): text for n, text in enumerate(quotes)}
        items = [dict(source=key, quote=text, value=str(value), unit='days')
                 for (key, text), value in zip(evidence.items(), [4, 3, 7])]
        items[2].update(source_value='1', source_unit='weeks')
        audit = self.proof(items, 14, evidence)
        self.assertEqual((audit['status'], audit['computed']), ('checks_passed', '14'))
        for extra in [dict(items[2], value='8'), dict(items[2], source_unit='months'),
                      dict(items[2], source_value='2')]:
            self.assertEqual(self.proof([extra], extra['value'], evidence)['status'], 'invalid')

    def test_literal_date_range_and_explicit_duration_can_be_summed(self):
        evidence = {'E1': '[E1] 2025-06-12\nUser: I visited Kyoto from May 3rd to 10th.',
                    'E2': 'User: My last 5-day trip to Denver was fun.'}
        items = [dict(source='E1', quote='I visited Kyoto from May 3rd to 10th.', value='7', unit='days',
                      duration=dict(start_text='May 3rd', end_text='10th')),
                 dict(source='E2', quote=evidence['E2'], value='5', unit='days')]
        self.assertEqual(self.proof(items, 12, evidence)['status'], 'checks_passed')
        for duration in [dict(start_text='May 3rd', end_text='12th'),
                         dict(start_text='May 3rd', end_text='10th', inclusive=True),
                         dict(start_text='May', end_text='10th')]:
            self.assertEqual(self.proof([dict(items[0], duration=duration)], 7, evidence)['status'], 'invalid')
        disconnected = '[E1] 2025-06-12\nUser: I arrived May 3rd. I bought a book on May 10th.'
        item = dict(items[0], quote=disconnected, duration=dict(start_text='May 3rd', end_text='May 10th'))
        self.assertEqual(self.proof([item], 7, {'E1': disconnected})['status'], 'invalid')

    def test_date_range_pages_preserve_source_header_for_year(self):
        e = Episode(1, 's', 0, datetime(2024, 3, 12), 'I traveled from February 26th to March 2nd.', '', 1, EPISODE_ACTIVE)
        result = SimpleNamespace(ranked=[Candidate('episode', 1)], items={'E1': e}, coverage={'search_complete': True})
        item = dict(source='E1', quote=e.text(), value='5', unit='days',
                    duration=dict(start_text='February 26th', end_text='March 2nd'), value_type='independent')
        chat = ScriptedChat({'aggregate_page': json.dumps(dict(reviewed=['E1'], operation='sum', unit='days', items=[item])),
            'aggregate_reconcile': json.dumps(dict(groups=[dict(kind='independent', scope='travel', members=['C1'], keep=['C1'])],
                                                 excluded=[], insufficient=False))})
        _, answer, audit = paged_aggregate(chat, 'What is the total number of days spent traveling?', result, {},
                                         settings(semantic_validation=True), datetime(2024, 3, 15).date())
        self.assertEqual(answer, '5 days')
        self.assertEqual(audit['status'], 'checks_passed')
        self.assertEqual(len(audit['page_outputs']), 1)

    def test_percentage_requires_both_operands_even_if_planner_says_lookup(self):
        question = 'What percentage of my cabin price is my planned repair cost?'
        self.assertEqual(operation_for(question), 'ratio')
        chat = ScriptedChat({'query': json.dumps(dict(operation='lookup', needs_all=False))})
        plan = plan_query(Models(chat=None, memory=chat, embedder=HashEmbedder(), embedding_name='hash'), question, datetime(2025, 6, 1),
                          settings(semantic_validation=True))
        self.assertEqual(plan.operation, 'ratio')
        self.assertTrue(plan.needs_all)

    def test_unnamed_cumulative_count_is_synthesized_with_earlier_identity(self):
        earlier = 'I tried three climbing gyms in my city.'
        later = 'Speaking of climbing gyms in my city, I have now tried five different ones so far.'
        def page(messages):
            entries = re.findall(r'\[(E\d+)\][^\n]*\nL1: ([^\n]+)', messages[-1]['content'])
            return json.dumps(dict(reviewed=[key for key, _ in entries],
                                   items=[dict(source=key, line_start=1, line_end=1) for key, _ in entries]))
        proof = dict(operation='count_groups', unit='gyms', result='5',
            items=[dict(source='E2', quote='User: '+later, value='5', unit='gyms', quantity_label='different ones')])
        chat = ScriptedChat({'evidence_page': page,
            'answer': 'CALCULATION: '+json.dumps(proof)+'\nANSWER: 5 gyms.',
            'answer_review': json.dumps(dict(responsive=True, supported=True))})
        system = MemorySystem(Models(chat=chat, memory=None, embedder=HashEmbedder(), embedding_name='hash'),
            settings(semantic_validation=True, query_analysis=False, min_similarity=0, max_history_share=0))
        self.addCleanup(system.close)
        store = system.store('u')
        for day, text in [(2, earlier), (9, later)]:
            at = datetime(2025, 5, day)
            sid = store.create_session(at)
            eid = store.add_episode(sid, 0, at, text, '', 1, EPISODE_ACTIVE)
            store.add_fact(text=text, kind='personal', subject='user', attribute='gyms', value='',
                event=None, recorded_at=at, valid_from=at.date(), session_id=sid, importance=1,
                episode_ids=[eid], semantics={'value_type':'cumulative','object_key':'climbing gyms'})
        reply = system.respond('u', 'How many climbing gyms have I tried in my city?', datetime(2025, 5, 12))
        self.assertEqual(reply.text, '5 gyms.')
        self.assertEqual(reply.trace['aggregation']['mode'], 'paged_evidence')
        self.assertEqual(reply.trace['aggregation']['answer_validation']['computed'], '5')
        self.assertNotIn('aggregate_page', chat.purposes())

    def test_evidence_retains_previous_object_statement_without_crossing_sessions(self):
        episodes = [Episode(1, 'a', 0, datetime(2025, 5, 1), 'I am repairing my workshop roof.', '', 1, EPISODE_ACTIVE),
                    Episode(2, 'a', 1, datetime(2025, 5, 1), 'The repair estimate is $720.', '', 1, EPISODE_ACTIVE),
                    Episode(3, 'b', 0, datetime(2025, 5, 1), 'A different repair cost $900.', '', 1, EPISODE_ACTIVE)]
        result = SimpleNamespace(items={e.key:e for e in episodes})
        s = settings(semantic_validation=True)
        expanded, audit = identity_context([('[E2] 2025-05-01', 'estimate is $720')], result, s, budget_counter(s))
        self.assertEqual({header.split(']')[0] for header, _ in expanded}, {'[E1','[E2'})
        self.assertTrue(any('workshop roof' in q for _,q in expanded))
        self.assertEqual(sum('estimate is $720' in q for _,q in expanded), 1)

    def test_compaction_only_accepts_exact_substrings_and_known_records(self):
        records = [('[E1] 2025-05-01', 'I own a kayak. It cost $480. Other irrelevant details follow.')]
        good = dict(keep=['C1'], excerpts=[dict(id='C1', quote='I own a kayak. It cost $480.')], insufficient=False)
        s = settings(semantic_validation=True)
        selected, audit = select_excerpts(ScriptedChat({'evidence_select':json.dumps(good)}),
            'What did my kayak cost?', records, s, '2025-05-02', content_budget=100)
        self.assertEqual(selected[0][1], good['excerpts'][0]['quote'])
        for bad in [dict(id='C1',quote='It cost $500.'),dict(id='C2',quote='It cost $480.')]:
            with self.assertRaises(ValueError):
                select_excerpts(ScriptedChat({'evidence_select':json.dumps(dict(good,excerpts=[bad]))}),
                    'What did my kayak cost?', records, s, '2025-05-02', content_budget=100)

    def test_qualifiers_cannot_exclude_one_coordinated_operand(self):
        question = 'What is the total duration of visits to Oslo and Riga?'
        self.assertEqual(question_qualifiers(question, ['total duration', 'visits to Oslo', 'Riga', 'Oslo and Riga']),
                         ['total duration', 'Oslo and Riga'])
        self.assertEqual(question_qualifiers('What did I spend on red or blue fabric?', ['red', 'blue fabric']), [])
        self.assertEqual(question_qualifiers('How many plants in my greenhouse?', ['in my greenhouse']), ['in my greenhouse'])

    def test_explicit_latest_wording_cannot_be_routed_to_a_bounded_lookup(self):
        chat = ScriptedChat({'query':json.dumps(dict(operation='lookup',needs_all=False))})
        plan = plan_query(Models(chat=None,memory=chat,embedder=HashEmbedder(),embedding_name='hash'),
            'Which musical instrument did I purchase most recently?',datetime(2025,5,1),settings(semantic_validation=True))
        self.assertEqual(plan.operation,'latest_state')
        self.assertTrue(plan.needs_all)

    def test_spelled_quantity_values_and_answers_preserve_checked_arithmetic(self):
        quote = 'I now have four different small greenhouses.'
        spec = dict(operation='count_groups', unit='greenhouses', result='four',
            items=[dict(source='E1', quote=quote, quantity_label='different small greenhouses', value='four', unit='greenhouses')])
        output = 'CALCULATION: '+json.dumps(spec)+'\nANSWER: four greenhouses.'
        audit = inspect_calculation(output, {'E1':quote})[1]
        self.assertEqual((audit['status'],audit['computed']), ('checks_passed','4'))
        self.assertEqual(inspect_calculation(output.replace('ANSWER: four','ANSWER: five'),{'E1':quote})[1]['status'], 'invalid')
        _,audit = inspect_calculation(output.replace('different small greenhouses', 'greenhouses', 1), {'E1':quote})
        self.assertEqual(audit['status'], 'invalid')


    def test_indefinite_article_counts_one_explicit_group_member(self):
        quote = 'I have a niece and two nephews.'
        items = [dict(source='E1',quote=excerpt,value=value,unit='relatives',quantity_label=label)
                 for value,label,excerpt in [('1','niece','a niece'),('2','nephews','two nephews')]]
        spec = dict(operation='count_groups',items=items,unit='relatives',result='3')
        output = 'CALCULATION: '+json.dumps(spec)+'\nANSWER: 3'
        self.assertEqual(inspect_calculation(output,{'E1':quote})[1]['status'],'checks_passed')
        spec.update(items=[dict(items[0],value='2')],result='2')
        self.assertEqual(inspect_calculation('CALCULATION: '+json.dumps(spec)+'\nANSWER: 2',{'E1':quote})[1]['status'],'invalid')

    def test_required_recovery_proof_cannot_be_omitted_or_disabled(self):
        messages = [dict(role='user',content='Verify the total.')]
        chat = ScriptedChat({'answer':['ANSWER: 14','ANSWER: 14']})
        _,answer,audit = answer_with_validation(chat,messages,evidence={'E1':'I paid $14.'},
            question='What is the total amount paid?',operation='sum_events',require_calculation=True)
        self.assertEqual(audit['status'],'invalid')
        self.assertIn("couldn't verify this total",answer)
        with self.assertRaises(ValueError):
            answer_with_validation(chat,messages,evidence={},enabled=False,require_calculation=True)

    def test_lookup_repair_does_not_request_a_calculation_placeholder(self):
        def answer(messages):
            if len(messages)==1:
                return 'ANSWER: I do not know.'
            return ('ANSWER: Brookfield University.' if 'Omit CALCULATION' in messages[-1]['content']
                    else 'CALCULATION: not applicable\nANSWER: Brookfield University.')
        chat = ScriptedChat({'answer':answer,'answer_review':[
            json.dumps(dict(responsive=False,supported=False,reason='The university is in the source.')),
            json.dumps(dict(responsive=True,supported=True))]})
        _,text,audit = answer_with_validation(chat,[dict(role='user',content='Where was the lecture?')],
            evidence={'E1':'I gave the lecture at Brookfield University.'},question='Where was the lecture?',
            operation='lookup',semantic_enabled=True)
        self.assertEqual(text,'Brookfield University.')
        self.assertEqual(audit['status'],'not_provided')

    def test_verified_date_arithmetic_is_supplied_to_semantic_review(self):
        quote = '[E1] 2025-05-01\nUser: I completed the workshop today.'
        proof = dict(operation='date_difference',unit='days',result='14',
            start=dict(source='E1',quote=quote,date_text='2025-05-01',date='2025-05-01'),end=dict(source='TODAY'))
        def review(messages):
            return json.dumps(dict(responsive=True,supported='Program-verified arithmetic' in messages[-1]['content']))
        chat = ScriptedChat({'answer':'CALCULATION: '+json.dumps(proof)+'\nANSWER: 14','answer_review':review})
        _,text,audit = answer_with_validation(chat,[dict(role='user',content='Calculate elapsed days.')],
            evidence={'E1':quote},question='How many days ago did I complete the workshop?',today='2025-05-15',semantic_enabled=True)
        self.assertEqual((text,audit['status']),('14','checks_passed'))
        self.assertEqual(len(audit['semantic_reviews']),1)

    def test_aggregate_recovery_rereads_originals_and_still_requires_proof(self):
        at = datetime(2025,5,1)
        users = ['My glass studio is the first activity.', 'That activity took 10 minutes.',
                 'I spent 4 minutes on a separate weaving activity.']
        def collect(messages):
            ids = re.findall(r'\[(E\d+)\]',messages[1]['content'])
            return json.dumps(dict(reviewed=ids,items=[dict(source=key,line_start=1,line_end=1) for key in ids]))
        quantities = [dict(source='E%d'%n,quote='User: '+users[n-1],value=str(value),unit='minutes')
                      for n,value in [(2,10),(3,4)]]
        proof = 'CALCULATION: '+json.dumps(dict(operation='sum',unit='minutes',items=quantities,result='14'))+'\nANSWER: 14'
        for draft in (proof,'ANSWER: 14'):
            with self.subTest(valid_proof=draft==proof):
                chat = ScriptedChat({'aggregate_page':json.dumps(dict(reviewed=['E1','E2','E3'],operation='sum',unit='minutes',
                    items=[dict(x,value_type='independent',scope='activity') for x in quantities])),
                    'aggregate_reconcile':json.dumps(dict(insufficient=True,reason='The numeric records lack identity context.')),
                    'evidence_page':collect,'answer':draft,'answer_review':json.dumps(dict(responsive=True,supported=True))})
                system = MemorySystem(Models(chat=chat,memory=None,embedder=HashEmbedder(),embedding_name='hash'),
                    settings(semantic_validation=True,query_analysis=False,min_similarity=0,max_history_share=0))
                try:
                    store=system.store('u');sid=store.create_session(at)
                    for n,text in enumerate(users):store.add_episode(sid,n,at,text,'',1,EPISODE_ACTIVE)
                    reply=system.respond('u','What is the total time for the glass studio and weaving activities? Reply with only the number.',at)
                    audit=reply.trace['aggregation']
                    self.assertEqual(audit['aggregation_recovery']['status'],'incomplete')
                    self.assertIn('evidence_page',chat.purposes())
                    self.assertEqual(reply.text=='14',draft==proof)
                finally:system.close()

    def page_retry_case(self, mode, repair, status=None):
        e = Episode(1, 's', 0, datetime(2025, 5, 1), 'I paid $14 for the workshop.', '', 1, EPISODE_ACTIVE)
        page = {'E1':dict(text=e.text(),rendered='[E1] 2025-05-01\n'+e.text(),can_contribute=True)}
        result = SimpleNamespace(items={'E1':e},coverage={'search_complete':True})
        def unavailable(messages):
            raise ModelError('JSON response was not completed: length',status=status)
        item = dict(source='E1',quote=e.text(),value='14',unit='usd')
        payload = dict(reviewed=['E1'],items=[item])
        if mode == 'aggregate':
            payload.update(operation='sum',unit='usd')
        if repair == 'bad_quote':
            payload['items'][0]['quote'] = 'I paid $900 for the workshop.'
        handlers = {mode+'_page':unavailable,mode+'_page_repair':
                    unavailable if repair == 'unavailable' else json.dumps(payload),'answer':'ANSWER: 14'}
        model = ScriptedChat(handlers)
        target = paged_aggregate if mode == 'aggregate' else paged_answer
        module = 'memsys.aggregate_batch' if mode == 'aggregate' else 'memsys.evidence_batch'
        with patch(module+'.aggregation_pages',return_value=[page]):
            reply = target(model,'What amount did I pay? Reply with only the number.',result,{},settings(),e.at.date())
        return model,reply

    def test_unfinished_page_retries_with_full_evidence_and_coverage(self):
        for mode in ('evidence','aggregate'):
            with self.subTest(mode=mode):
                model,(_,answer,audit) = self.page_retry_case(mode,'good')
                self.assertEqual(answer,'14')
                self.assertTrue(audit['pages_complete'])
                self.assertEqual(len(audit['page_validation_failures']),1)
                repair = model.calls[1][1]
                self.assertIn('I paid $14 for the workshop.',repair[1]['content'])
                self.assertFalse(any(m['role']=='assistant' for m in repair))

    def test_page_retry_cannot_publish_unverified_or_still_unavailable_evidence(self):
        for mode in ('evidence','aggregate'):
            for repair in ('bad_quote','unavailable'):
                with self.subTest(mode=mode,repair=repair):
                    model,(_,answer,audit) = self.page_retry_case(mode,repair)
                    self.assertFalse(audit['pages_complete'])
                    self.assertNotIn('answer',model.purposes())
                    self.assertEqual(len(model.calls),2)
                    self.assertEqual(len(audit['page_validation_failures']),2)
                    if repair == 'bad_quote':
                        # Keep the rejected draft for diagnosis without treating it
                        # as accepted evidence or allowing answer generation.
                        self.assertEqual(audit['page_outputs'][0]['data']['items'][0]['quote'],
                                         'I paid $900 for the workshop.')
                        self.assertEqual(audit['page_outputs'][0]['attempt'],2)

    def test_account_failure_is_propagated_without_page_retry(self):
        for mode in ('evidence','aggregate'):
            with self.subTest(mode=mode):
                with self.assertRaises(ModelError) as caught:
                    self.page_retry_case(mode,'good',status=403)
                self.assertTrue(caught.exception.account_problem)


if __name__ == '__main__':
    unittest.main()
