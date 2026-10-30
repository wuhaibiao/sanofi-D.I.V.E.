import json
import tempfile
import unittest
from datetime import date, datetime
from types import SimpleNamespace

from helpers import settings
from memsys.aggregate_batch import aggregation_pages, materialize_item, paged_aggregate
from memsys.aggregation import answer_with_validation, inspect_calculation, wants_calculation
from memsys.budget import budget_counter
from memsys.config import MemorySettings, Models
from memsys.core import MemorySystem
from memsys.dateproof import inspect_dates
from memsys.evidence_batch import paged_answer
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.llm import ModelError
from memsys.queryops import operation_for
from memsys.read import Candidate, plan_query
from memsys.schema import EPISODE_ACTIVE, Episode, Fact, normalize_semantics
from memsys.semantic_review import reconcile, review_response
from memsys.store import Store
from memsys.update import Decision, Updater
from memsys.write import NewFact, WriteReport, _dedupe

AT = datetime(2025, 4, 12, 10)


class SemanticReviewTest(unittest.TestCase):
    def test_operations_distinguish_state_comparison_duration_and_events(self):
        cases = {
            'How many pages of my field journal have I read so far?': 'latest_state',
            'How many analysts did I supervise when I started, and how many now?': 'compare_states',
            'How much do I need to reach the next membership level?': 'latest_state',
            'How much did I donate in total through my independent events?': 'sum_events',
            'How many different instruments do I own?': 'count_entities',
            'How long did it take to assemble my telescope?': 'lookup',
            'How many days ago did I calibrate the telescope?': 'date_difference',
        }
        for question, operation in cases.items():
            self.assertEqual(operation_for(question), operation)
        self.assertFalse(wants_calculation(next(iter(cases))))
        model = ScriptedChat({'query': json.dumps({'operation': 'sum_events', 'needs_all': True})})
        p = plan_query(Models(chat=None, memory=model, embedder=HashEmbedder(), embedding_name='hash'),
                       next(iter(cases)), AT, settings(semantic_validation=True))
        self.assertEqual(p.operation, 'latest_state')

    def test_production_semantic_default_and_structured_annotation_bounds(self):
        self.assertTrue(MemorySettings().semantic_validation)
        parsed = normalize_semantics({'value_type': {}, 'qualifiers': ['Thursday', 'Tuesday', 'Tuesday', 5]})
        self.assertEqual(parsed['value_type'], 'unknown')
        self.assertEqual(parsed['qualifiers'], ['thursday', 'tuesday'])

    def test_comparison_uses_evidence_pages_and_preserves_two_states(self):
        def page_reply(messages):
            text = messages[-1]['content']
            entries = __import__('re').findall(r'\[(E\d+)\][^\n]*\n(L1:[^\n]*)', text)
            return json.dumps({'reviewed': [key for key, _ in entries],
                'items': [{'source': key, 'line_start': 1, 'line_end': 1} for key, _ in entries]})
        chat = ScriptedChat({'evidence_page': page_reply,
            'evidence_select': json.dumps({'keep': ['C1', 'C2'], 'insufficient': False}),
            'answer': 'NOTES: earlier and later states.\nANSWER: Initially 7 analysts; now 9 analysts.',
            'answer_review': json.dumps({'responsive': True, 'supported': True, 'reason': 'both states retained'})})
        system = MemorySystem(Models(chat=chat, memory=None, embedder=HashEmbedder(), embedding_name='hash'),
            settings(semantic_validation=True, query_analysis=False, budget_mode='utf8',
                     context_budget_tokens=600, max_episodes=1, max_history_share=0))
        self.addCleanup(system.close)
        for when, text in [('2025-04-01T10:00', 'I started supervising 7 analysts.'),
                           ('2025-04-08T10:00', 'I now supervise 9 analysts.')]:
            at = datetime.fromisoformat(when)
            handle = system.open_session('u', at)
            system.add_turn(handle, 'user', text, at)
            system.close_session(handle, at)
        result = system.respond('u', 'How many analysts did I supervise when I started, and how many now?', AT)
        self.assertEqual(result.text, 'Initially 7 analysts; now 9 analysts.')
        self.assertEqual(result.trace['plan']['operation'], 'compare_states')
        self.assertNotIn('aggregate_page', chat.purposes())
        self.assertTrue(result.trace['aggregation']['pages_complete'])
        self.assertNotIn('evidence_select', chat.purposes())
        self.assertEqual(result.trace['aggregation']['semantic_selection']['mode'], 'all_verified_evidence')

    def test_cross_page_reconciliation_removes_old_snapshot_and_repeated_event(self):
        texts = ['I have spent 4 hours on my pottery course so far.',
                 'I have now spent 8 hours on my pottery course in total.',
                 'I spent 5 hours in a separate carving workshop.',
                 'That same carving workshop took 5 hours.']
        episodes = [Episode(n, 's%d' % n, 0, datetime(2025, 4, n, 10), text, '', 1, EPISODE_ACTIVE)
                    for n, text in enumerate(texts, 1)]
        result = SimpleNamespace(ranked=[Candidate('episode', e.id) for e in episodes],
            items={e.key: e for e in episodes}, coverage={'search_complete': True})
        replies = [json.dumps({'reviewed': [e.key], 'operation': 'sum', 'unit': 'hours', 'items': [
            {'source': e.key, 'line_start': 1, 'line_end': 1, 'value': str(value), 'unit': 'hours',
             'value_type': kind} ]}) for e, value, kind in zip(episodes, [4, 8, 5, 5],
                                                             ['cumulative', 'cumulative', 'independent', 'independent'])]
        resolved = {'groups': [
            {'kind': 'cumulative', 'scope': 'pottery course', 'members': ['C1', 'C2'], 'keep': ['C2']},
            {'kind': 'duplicate', 'scope': 'carving workshop', 'members': ['C3', 'C4'], 'keep': ['C3']}],
            'excluded': [], 'insufficient': False}
        chat = ScriptedChat({'aggregate_page': replies, 'aggregate_reconcile': json.dumps(resolved)})
        _, answer, audit = paged_aggregate(chat, 'How many hours did I spend in total on the course and workshop?',
            result, {}, settings(semantic_validation=True, aggregate_page_items=1), AT.date())
        self.assertEqual(answer, '13 hours')
        self.assertEqual(audit['computed'], '13')
        self.assertEqual(audit['semantic_reconciliation']['retained'], ['C2', 'C3'])
        self.assertEqual(audit['pages_processed'], 4)

    def test_semantic_partition_cannot_drop_records_or_keep_older_cumulative_snapshot(self):
        items = [dict(source='E1', quote='I logged 3 hours.', value='3', recorded_at='2025-04-01'),
                 dict(source='E2', quote='I have now logged 6 hours.', value='6', recorded_at='2025-04-03')]
        for members, keep, error in [(['C1'], ['C1'], 'partition'), (['C1', 'C2'], ['C1'], 'older')]:
            reply = {'groups': [{'kind': 'cumulative', 'scope': 'log', 'members': members, 'keep': keep}],
                     'excluded': [], 'insufficient': False}
            chat = ScriptedChat({'aggregate_reconcile': json.dumps(reply)})
            with self.assertRaisesRegex(ValueError, error):
                reconcile(chat, 'Total logged hours?', items, settings(), AT.date())

    def test_missing_container_cannot_pass_using_another_container_quantity(self):
        item = dict(source='E1', quote='My 20-liter vessel contains 3 snails.', value='3', recorded_at='2025-04-01')
        reply = {'groups': [{'kind': 'independent', 'scope': 'vessel', 'members': ['C1'], 'keep': ['C1']}],
                 'excluded': [], 'insufficient': False}
        with self.assertRaisesRegex(ValueError, 'qualifier'):
            reconcile(ScriptedChat({'aggregate_reconcile': json.dumps(reply)}),
                'How many snails are in my 40-liter vessel?', [item], settings(), AT.date())

    def test_line_citations_copy_source_and_count_labels_within_budget(self):
        text = '\n'.join('Line %d has a relevant recorded detail.' % n for n in range(30))
        e = Episode(1, 's', 0, AT, text, '', 1, EPISODE_ACTIVE)
        result = SimpleNamespace(ranked=[Candidate('episode', 1)], items={'E1': e})
        s = settings(semantic_validation=True, budget_mode='utf8', context_budget_tokens=240)
        pages = aggregation_pages(result, s, AT.date())
        parts = []
        for page in pages:
            self.assertLessEqual(budget_counter(s).count('\n\n'.join(v['rendered'] for v in page.values())+'\n\n'), 240)
            part = page['E1']['text']; parts.append(part)
            cited = materialize_item({'source': 'E1', 'line_start': 1, 'line_end': len(part.splitlines())}, page)
            self.assertEqual(cited['quote'], part)
        self.assertEqual(''.join(parts), e.text())
        with self.assertRaises(ValueError):
            materialize_item({'source': 'E1', 'line_start': True, 'line_end': 1}, pages[0])

    def test_derived_summary_is_skipped_only_when_all_originals_are_supplied(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        sid = store.create_session(AT)
        eid = store.add_episode(sid, 0, AT, 'I started with 7 analysts.', '', 1, EPISODE_ACTIVE)
        fid = store.add_fact(text='The user supervised 7 analysts.', kind='personal', subject='user',
            attribute='team', value='7', event=None, recorded_at=AT, valid_from=AT.date(),
            session_id=sid, importance=1)
        result = SimpleNamespace(ranked=[Candidate('fact', fid), Candidate('episode', eid)],
            items={'F%d'%fid:store.fact(fid), 'E%d'%eid:store.episodes([eid])[0]})
        s = settings(semantic_validation=True)
        keys = lambda pages: {k for page in pages for k in page}
        self.assertEqual(keys(aggregation_pages(result, s, AT.date(), {'F%d'%fid:{eid}})), {'E%d'%eid})
        self.assertIn('F%d'%fid, keys(aggregation_pages(result, s, AT.date(), {'F%d'%fid:{eid, eid+1}})))

    def test_off_topic_answer_has_one_grounded_repair_and_is_retained_in_audit(self):
        chat = ScriptedChat({'answer': ['ANSWER: Sunbeam', 'ANSWER: Try a tofu and vegetable grain bowl.'],
            'answer_review': [json.dumps({'responsive': False, 'supported': False, 'reason': 'unrelated word'}),
                              json.dumps({'responsive': True, 'supported': True})]})
        _, answer, audit = answer_with_validation(chat, [{'role': 'user', 'content': 'Meal ideas?'}],
            evidence={'F1': 'The user likes tofu and vegetables.'}, enabled=False, semantic_enabled=True,
            operation='advice', question='Suggest a new meal using my preferences.')
        self.assertEqual(answer, 'Try a tofu and vegetable grain bowl.')
        self.assertEqual(audit['semantic_rejected_answer'], 'Sunbeam')
        self.assertEqual(chat.purposes().count('answer'), 2)

    def test_live_review_includes_recent_unconsolidated_conversation(self):
        def review(messages):
            supplied = messages[-1]['content']
            self.assertIn('RECENT', supplied)
            self.assertIn('I live in Riga.', supplied)
            return json.dumps({'responsive':True,'supported':True})
        chat = ScriptedChat({'answer':'ANSWER: Riga','answer_review':review})
        system = MemorySystem(Models(chat=chat,memory=None,embedder=HashEmbedder(),embedding_name='hash'),
            settings(semantic_validation=True,query_analysis=False))
        self.addCleanup(system.close)
        session = system.open_session('u',AT)
        system.add_turn(session,'user','I live in Riga.',AT)
        reply = system.respond('u','Where do I live?',AT,session=session)
        self.assertEqual(reply.text,'Riga')
        self.assertEqual(system.facts('u'),[])

    def test_scope_annotations_survive_restart_and_prevent_wrong_object_update(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory+'/u.sqlite';store = Store(path)
            old_id = store.add_fact(text='The terrarium has 3 snails.', kind='personal', subject='user',
                attribute='snail_count', value='3', event=None, recorded_at=AT, valid_from=AT.date(),
                session_id='s', importance=1, semantics={'value_type':'state', 'object_key':'terrarium'})
            new = NewFact('The pond has 6 snails.', 'personal', 'user', 'snail_count', '6', None, [1], 1,
                          semantics=normalize_semantics({'value_type':'state', 'object_key':'pond'}))
            report = WriteReport('s2')
            Updater(store, Models(chat=None, memory=None, embedder=HashEmbedder(), embedding_name='hash'),
                    settings()).apply(new, Decision('update', old_id), store.fact(old_id),
                's2', AT, [], report, {})
            self.assertFalse(report.updates)
            with store.transaction():
                pass
            store.close();store=Store(path)
            self.assertEqual(store.fact(old_id).semantics['object_key'], 'terrarium')
            store.forget('fact', old_id)
            self.assertEqual(store.fact(old_id).semantics, {})
            store.close()

    def test_identical_unknown_occurrences_are_not_deduplicated_during_extraction(self):
        records = [NewFact('The user practiced for 3 hours.', 'event', 'user', 'practice', '3', None, [n], 1,
                           semantics={'value_type':'event_quantity'}) for n in (1, 2)]
        self.assertEqual(len(_dedupe(records)), 2)

    def test_response_review_receives_caller_clock_and_does_not_retry_filter_errors(self):
        def inspect(messages):
            self.assertIn('Caller date (TODAY): 2025-04-12', messages[-1]['content'])
            return json.dumps({'responsive': True, 'supported': True})
        chat = ScriptedChat({'answer_review': inspect})
        review_response(chat, 'When?', '8 days ago', {'E1': 'Today I calibrated it.'},
                        'date_difference', (), None, None, today='2025-04-12')
        calls = []
        def blocked(messages):
            calls.append(messages)
            raise ModelError('Invalid prompt: invalid_prompt', status=400)
        chat = ScriptedChat({'answer': 'ANSWER: grain bowl', 'answer_review': blocked})
        _, _, audit = answer_with_validation(chat, [{'role': 'user', 'content': 'Suggest lunch.'}],
            evidence={}, enabled=False, semantic_enabled=True, question='Suggest lunch.')
        self.assertEqual((audit['status'], len(calls), chat.purposes().count('answer')), ('incomplete', 1, 1))


class RelativeDateTest(unittest.TestCase):
    def test_bounded_relative_evidence_reaches_proof_without_calendar_selection_gate(self):
        first = 'I toured the botanical exhibit two weeks before the launch.'
        second = 'I purchased the workbook at the launch.'
        episodes = [Episode(n, 's%d'%n, 0, AT, text, '', 1, EPISODE_ACTIVE)
                    for n,text in enumerate((first,second),1)]
        result = SimpleNamespace(ranked=[Candidate('episode',e.id) for e in episodes],
            items={e.key:e for e in episodes},coverage={'search_complete':True})
        spec = {'operation':'date_difference','unit':'days',
            'start':{'source':'E1','quote':first,'event_text':'botanical exhibit'},
            'end':{'source':'E2','quote':second,'event_text':'workbook'},
            'relation':{'source':'E1','quote':first,'from':'botanical exhibit','to':'launch',
                        'offset_text':'two weeks before'},'result':'14'}
        chat = ScriptedChat({'evidence_page':json.dumps({'reviewed':['E1','E2'],
            'items':[{'source':e.key,'line_start':1,'line_end':1} for e in episodes]}),
            'answer':'CALCULATION: '+json.dumps(spec)+'\nANSWER: 14',
            'answer_review':json.dumps({'responsive':True,'supported':True})})
        _,answer,audit = paged_answer(chat,'How many days before buying the workbook did I tour the exhibit?',
            result,{},settings(semantic_validation=True),AT.date(),operation='date_difference')
        self.assertEqual(answer,'14')
        self.assertEqual(audit['date_validation']['status'],'checks_passed')
        self.assertNotIn('evidence_select',chat.purposes())

    def test_relative_relation_proves_interval_without_calendar_dates(self):
        first = 'I toured the botanical exhibit two weeks before the launch.'
        second = 'I purchased the workbook at the launch.'
        evidence = {'E1': first, 'E2': second}
        spec = {'operation':'date_difference', 'unit':'days',
            'start':{'source':'E1', 'quote':first, 'event_text':'botanical exhibit'},
            'end':{'source':'E2', 'quote':second, 'event_text':'workbook'},
            'relation':{'source':'E1', 'quote':first, 'from':'botanical exhibit', 'to':'launch',
                        'offset_text':'two weeks before'}, 'result':'14'}
        audit = inspect_dates(spec, evidence, '2025-04-12', 'How many days before my workbook purchase did I tour the exhibit?')
        self.assertEqual((audit['status'], audit['computed']), ('checks_passed', '14'))
        for altered in [dict(spec, end={'source':'E2', 'quote':second, 'event_text':'unmentioned device'}),
                        dict(spec, relation={**spec['relation'], 'offset_text':'three weeks before'})]:
            self.assertEqual(inspect_dates(altered, evidence, '2025-04-12', 'Days between the two actions?')['status'], 'invalid')
        approximate = 'I toured the botanical exhibit about two weeks before the launch.'
        variant = {**spec, 'start':{**spec['start'], 'quote':approximate},
                   'relation':{**spec['relation'], 'quote':approximate}}
        self.assertEqual(inspect_dates(variant, {'E1':approximate,'E2':second}, '2025-04-12', 'Days between the actions?')['status'], 'invalid')

    def test_today_in_source_body_resolves_against_recorded_date_not_caller_date(self):
        text = '[E1] conversation on 2025-04-04\nUser: I calibrated the telescope today.'
        spec = {'operation':'date_difference','unit':'days', 'start':{'source':'E1',
            'quote':'User: I calibrated the telescope today.', 'event_text':'calibrated the telescope',
            'relative_text':'today','anchor':'recorded_at'}, 'end':{'source':'TODAY'}, 'result':'8'}
        output = 'CALCULATION: '+json.dumps(spec)+'\nANSWER: 8'
        audit = inspect_calculation(output, {'E1':text}, today='2025-04-12', question='How many days ago did I calibrate the telescope?')[1]
        self.assertEqual((audit['status'],audit['computed']),('checks_passed','8'))
        bad = {**spec,'start':{**spec['start'], 'date':'2025-04-12'}}
        self.assertEqual(inspect_dates(bad, {'E1':text}, '2025-04-12', 'Days ago?')['status'], 'invalid')


if __name__ == '__main__':
    unittest.main()
