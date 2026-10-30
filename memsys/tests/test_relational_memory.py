"""Pre-authored generic contracts; no public evaluation questions or labels."""
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from memsys.core import MemorySystem
from memsys.config import Models, MemorySettings
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.llm import ModelError
from memsys.qdrant import QdrantError
from memsys.read import QueryPlan, Retriever
from memsys.relational_memory import RelationIndex
from memsys.working_memory import select_window
from tests.qdrant_fixture import Engine

AT = datetime(2026, 8, 12)


def entity(name, kind='other', **kwargs):
    return dict(name=name, type=kind, **kwargs)


def edge(subject, predicate, target, **kwargs):
    return dict(subject=subject, predicate=predicate, object=target, **kwargs)


class RelationalMemoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.client = patch('memsys.core.Qdrant', return_value=self.engine)
        self.client.start()
        self.addCleanup(self.client.stop)
        self.writer = ScriptedChat({})
        self.models = Models(ScriptedChat({'answer': 'A fixture response.'}), self.writer, HashEmbedder(), 'hash-256')
        self.settings = MemorySettings(query_analysis=False, query_policy='off', rerank_backend='fusion',
            min_similarity=.01, budget_mode='utf8', context_budget_tokens=12000, max_history_share=0,
            answer_input_budget_tokens=24000)
        self.system = MemorySystem(self.models, self.settings)
        self.addCleanup(self.system.close)

    def remember(self, text, entities, relations, *, user='fixture', kind='personal', quotes=True, at=AT):
        data = dict(entities=entities, facts=[dict(text=text, kind=kind, subject='user', attribute='fixture_observation',
            value=text, rounds=[1], relations=relations, source_quotes=[dict(round=1, role='user', quote=text)] if quotes else [])])
        self.writer.handlers['extract'] = json.dumps(data)
        session = self.system.open_session(user, at)
        self.system.add_turn(session, 'user', text, at)
        report, _ = self.system.close_session(session, at)
        return report.added[0], report

    def recall_only_seed(self, fid, query, user='fixture'):
        store = self.system.store(user)
        with patch.object(store, 'vector_search', return_value=[(fid, .99)]), patch.object(store, 'fts_search', return_value=[]), \
                patch.object(store, 'episodes', side_effect=AssertionError('originals read during facts-only recall')):
            return self.system.recall(user, query, AT)

    def chain(self):
        one, _ = self.remember('I work with Dana.', [entity('Dana', 'person')], [edge('user', 'works_with', 'Dana')])
        two, _ = self.remember('Dana leads Prism.', [entity('Dana', 'person'), entity('Prism', 'project')], [edge('Dana', 'leads', 'Prism')])
        three, _ = self.remember('Prism is based in Delft.', [entity('Prism', 'project'), entity('Delft', 'place')], [edge('Prism', 'based_in', 'Delft')])
        return one, two, three

    def test_two_hops_retrieve_related_facts_without_sources_or_extra_generation(self):
        one, two, three = self.chain()
        recall = self.recall_only_seed(one, "Where is Dana's project based?")
        self.assertEqual({row['key'] for row in recall.items}, {'F%d' % fid for fid in (one, two, three)})
        self.assertEqual(len(recall.items), 3)
        self.assertEqual(recall.trace['relations']['added_candidates'], 2)
        self.assertEqual(recall.trace['selected']['episodes'], [])
        self.assertIn('Delft', recall.memory)
        self.assertTrue(all(row['kind'] == 'fact' for row in recall.items))
        self.assertTrue(all(p['payload']['kind'] not in ('entity', 'relation')
            for name, points in self.engine.collections.items() if '_dense_' in name or name.endswith('_lexical')
            for p in points.values()))

    def test_explicit_alias_is_reused_across_sessions(self):
        one, _ = self.remember('Prism, also known as PR, is led by Dana.',
            [entity('Prism', 'project', aliases=['PR']), entity('Dana', 'person')], [edge('Dana', 'leads', 'Prism')])
        two, _ = self.remember('PR is based in Delft.', [entity('PR', 'project'), entity('Delft', 'place')], [edge('PR', 'based_in', 'Delft')])
        projects = [row for row in self.system.store('fixture').records('entity') if row['type'] == 'project']
        self.assertEqual(len(projects), 1)
        recall = self.recall_only_seed(one, 'Where is Prism based?')
        self.assertIn('F%d' % two, recall.trace['selected']['facts'])

    def test_alias_declaration_has_own_source_span_when_fact_quote_is_shorter(self):
        text = 'Prism is also known as PR. I participate in Prism.'
        self.writer.handlers['extract'] = json.dumps(dict(entities=[entity('Prism', 'project', aliases=['PR'])],
            facts=[dict(text='The user participates in Prism.', kind='personal', subject='user', attribute='project',
                value='Prism', rounds=[1], relations=[edge('user', 'participates_in', 'Prism')],
                source_quotes=[dict(round=1, role='user', quote='I participate in Prism.')])]))
        session = self.system.open_session('fixture', AT)
        self.system.add_turn(session, 'user', text, AT)
        report, _ = self.system.close_session(session, AT)
        row = self.system.store('fixture').record('fact', report.added[0])
        self.assertTrue(any('also known as PR' in span['quote'] for span in row['spans']))
        recall = self.recall_only_seed(report.added[0], 'What does PR refer to?')
        self.assertIn('object_aliases', recall.memory)
        self.assertIn('PR', recall.memory)

    def test_cooccurrence_does_not_verify_alias_and_distinct_identities_stay_separate(self):
        self.remember('Prism and PR are separate projects.', [entity('Prism', 'project', aliases=['PR']), entity('PR', 'project')],
            [edge('Prism', 'distinct_from', 'PR')])
        projects = [row for row in self.system.store('fixture').records('entity') if row['type'] == 'project']
        self.assertEqual(len(projects), 2)
        for identity in ('account-A', 'account-B'):
            self.remember('I work with Dana identified by ' + identity + '.',
                [entity('Dana', 'person', identity=identity)], [edge('user', 'works_with', 'Dana')])
        people = [row for row in self.system.store('fixture').records('entity') if row['type'] == 'person']
        self.assertEqual(len(people), 2)
        self.assertEqual({row['identity'] for row in people}, {'account-A', 'account-B'})

    def test_user_hub_does_not_import_unrelated_projects(self):
        one, _ = self.remember('I participate in Prism.', [entity('Prism', 'project')], [edge('user', 'participates_in', 'Prism')])
        two, _ = self.remember('I participate in Aster.', [entity('Aster', 'project')], [edge('user', 'participates_in', 'Aster')])
        recall = self.recall_only_seed(one, 'Tell me about Prism.')
        self.assertNotIn('F%d' % two, recall.trace['selected']['facts'])

    def test_named_user_hub_also_cannot_import_unrelated_projects(self):
        one, _ = self.remember('Morgan participates in Prism.', [entity('Morgan', 'user'), entity('Prism', 'project')],
            [edge('Morgan', 'participates_in', 'Prism')])
        two, _ = self.remember('Morgan participates in Aster.', [entity('Morgan', 'user'), entity('Aster', 'project')],
            [edge('Morgan', 'participates_in', 'Aster')])
        recall = self.recall_only_seed(one, 'What does Morgan participate in?')
        self.assertNotIn('F%d' % two, recall.trace['selected']['facts'])

    def test_forgotten_bridge_cannot_expand_into_a_visible_neighbor(self):
        one, two, three = self.chain()
        self.system.forget('fixture', 'F%d' % two)
        recall = self.recall_only_seed(one, 'What project does Dana lead?')
        self.assertNotIn('F%d' % two, recall.trace['selected']['facts'])
        self.assertNotIn('F%d' % three, recall.trace['selected']['facts'])

    def test_relation_indexes_are_tenant_isolated(self):
        one, _, _ = self.chain()
        self.remember('Prism is based in Oslo.', [entity('Prism', 'project'), entity('Oslo', 'place')],
            [edge('Prism', 'based_in', 'Oslo')], user='other')
        recall = self.recall_only_seed(one, "Where is Dana's project based?")
        self.assertNotIn('Oslo', recall.memory)

    def test_explicit_closure_is_recalled_and_stops_old_current_relation(self):
        first, _ = self.remember('Dana leads Prism.', [entity('Dana', 'person'), entity('Prism', 'project')],
            [edge('Dana', 'leads', 'Prism', state='active')], at=datetime(2026, 7, 1))
        closed, _ = self.remember('Dana stopped leading Prism.', [entity('Dana', 'person'), entity('Prism', 'project')],
            [edge('Dana', 'leads', 'Prism', state='ended')], at=datetime(2026, 8, 1))
        location, _ = self.remember('Prism is based in Delft.', [entity('Prism', 'project'), entity('Delft', 'place')],
            [edge('Prism', 'based_in', 'Delft')])
        store = self.system.store('fixture')
        plan = QueryPlan(['What project does Dana lead now?'], [], None, False, 'current', 'heuristic')
        with patch.object(store, 'vector_search', return_value=[(first, .99)]), patch.object(store, 'fts_search', return_value=[]):
            result = Retriever(store, self.models, self.settings, today=AT.date()).search(plan)
        self.assertIn('F%d' % closed, result.items)
        self.assertNotIn('F%d' % location, result.items)

    def test_known_ended_relation_is_not_a_current_traversal_bridge(self):
        one, _ = self.remember('Dana led Prism until 2025-01-31.', [entity('Dana', 'person'), entity('Prism', 'project')],
            [edge('Dana', 'leads', 'Prism', state='ended', valid_to='2025-01-31')])
        two, _ = self.remember('Prism is based in Delft.', [entity('Prism', 'project'), entity('Delft', 'place')], [edge('Prism', 'based_in', 'Delft')])
        store = self.system.store('fixture')
        plan = QueryPlan(['What project does Dana lead now?'], [], None, False, 'current', 'heuristic')
        with patch.object(store, 'vector_search', return_value=[(one, .99)]), patch.object(store, 'fts_search', return_value=[]):
            result = Retriever(store, self.models, self.settings, today=AT.date()).search(plan)
        self.assertNotIn('F%d' % two, result.items)

    def test_repeated_undated_event_titles_get_distinct_occurrence_nodes(self):
        text = 'I attended the Kestrel meetup.'
        first, _ = self.remember(text, [entity('Kestrel meetup', 'event')], [edge('user', 'attended', 'Kestrel meetup')], kind='event')
        second, _ = self.remember(text, [entity('Kestrel meetup', 'event')], [edge('user', 'attended', 'Kestrel meetup')], kind='event', at=datetime(2026, 8, 13))
        self.assertNotEqual(first, second)
        events = [row for row in self.system.store('fixture').records('entity') if row['type'] == 'event']
        self.assertEqual(len(events), 2)

    def test_missing_quotes_exclude_relations_without_losing_the_fact(self):
        _, report = self.remember('Dana leads Prism.', [entity('Dana', 'person'), entity('Prism', 'project')],
            [edge('Dana', 'leads', 'Prism')], quotes=False)
        self.assertTrue(report.relation_warnings)
        self.assertEqual(self.system.store('fixture').records('relation'), [])
        self.assertEqual(len(self.system.facts('fixture')), 1)

    def test_failed_publication_exposes_neither_relations_nor_cursor_advance(self):
        self.writer.handlers['extract'] = json.dumps(dict(entities=[entity('Prism', 'project')], facts=[
            dict(text='I participate in Prism.', kind='personal', subject='user', attribute='participates_in', value='Prism',
                 rounds=[1], source_quotes=[dict(round=1, role='user', quote='I participate in Prism.')],
                 relations=[edge('user', 'participates_in', 'Prism')])]))
        session = self.system.open_session('fixture', AT)
        self.system.add_turn(session, 'user', 'I participate in Prism.', AT)
        self.engine.fail_stage = True
        with self.assertRaises(QdrantError):
            self.system.close_session(session, AT)
        self.engine.fail_stage = False
        store = self.system.store('fixture')
        self.assertEqual(store.session(session.session_id)['turns_done'], 0)
        self.assertEqual(store.facts(), [])
        self.assertEqual(store.records('relation'), [])

    def test_graph_limits_bound_expansion(self):
        one, _, _ = self.chain()
        self.settings.relation_max_nodes = 2
        self.settings.relation_max_edges = 2
        self.settings.relation_max_candidates = 2
        trace = self.recall_only_seed(one, 'What does Dana lead?').trace['relations']
        self.assertLessEqual(trace['visited_nodes'], 2)
        self.assertLessEqual(trace['visited_edges'], 2)
        self.assertLessEqual(len(trace['candidate_fact_ids']), 2)

    def test_rebuild_uses_facts_only_and_preserves_relation_counts(self):
        self.chain()
        store = self.system.store('fixture')
        before = len(store.records('relation'))
        with patch.object(store, 'episodes', side_effect=AssertionError('original read')), \
                patch.object(self.writer, 'chat', side_effect=AssertionError('extra extraction')):
            result = RelationIndex(store).rebuild()
        self.assertEqual(result['relations'], before)

    def test_host_completion_consumes_omitted_fact_pool_once_within_budget(self):
        for name in ('Prism', 'Aster', 'Kestrel'):
            self.remember('I participate in ' + name + '.', [entity(name, 'project')], [edge('user', 'participates_in', name)])
        self.settings.max_facts_all = 1
        store = self.system.store('fixture')
        with patch.object(store, 'episodes', side_effect=AssertionError('original read')):
            recall = self.system.recall('fixture', 'List all projects.', AT, budget=4000, coverage_budget=12000)
        self.assertTrue(recall.trace['coverage_supplement']['attempted'])
        self.assertTrue(recall.trace['coverage']['context_complete'])
        self.assertEqual(len(recall.items), 3)
        self.assertLessEqual(recall.trace['memory_tokens'], 12000)

    def test_incomplete_host_completion_keeps_explicit_subset_warning(self):
        self.chain()
        self.settings.max_facts_all = 1
        self.settings.answer_fact_limit = 2
        recall = self.system.recall('fixture', 'List all projects and people.', AT, budget=4000, coverage_budget=12000)
        self.assertFalse(recall.trace['coverage']['context_complete'])
        self.assertIn('known subset', recall.memory)
        self.assertLessEqual(len(recall.items), 2)

    def test_host_completion_relaxes_fact_cap_without_requiring_more_bytes(self):
        for name in ('Prism', 'Aster', 'Kestrel'):
            self.remember('I participate in ' + name + '.', [entity(name, 'project')],
                          [edge('user', 'participates_in', name)])
        self.settings.max_facts_all = 1
        recall = self.system.recall('fixture', 'List all projects.', AT,
                                    budget=12000, coverage_budget=12000)
        self.assertEqual(len(recall.items), 3)
        self.assertTrue(recall.trace['coverage_supplement']['attempted'])
        self.assertTrue(recall.trace['coverage']['context_complete'])
        self.assertLessEqual(recall.trace['memory_tokens'], 12000)

    def test_host_completion_never_shrinks_initial_budget_or_fact_cap(self):
        for name in ('Prism', 'Aster', 'Kestrel'):
            self.remember('I participate in ' + name + '.', [entity(name, 'project')],
                          [edge('user', 'participates_in', name)])
        self.settings.max_facts_all = 2
        self.settings.answer_fact_limit = 1
        recall = self.system.recall('fixture', 'List all projects.', AT,
                                    budget=4000, coverage_budget=12000)
        self.assertEqual(len(recall.items), 2)
        self.assertFalse(recall.trace['coverage']['context_complete'])
        self.assertIn('known subset', recall.memory)

    def test_host_completion_can_use_lower_authorized_budget_to_relax_cap(self):
        for name in ('Prism', 'Aster', 'Kestrel'):
            self.remember('I participate in ' + name + '.', [entity(name, 'project')],
                          [edge('user', 'participates_in', name)])
        self.settings.max_facts_all = 1
        recall = self.system.recall('fixture', 'List all projects.', AT,
                                    budget=12000, coverage_budget=4000)
        self.assertEqual(len(recall.items), 3)
        self.assertLessEqual(recall.trace['memory_tokens'], 12000)

    def test_zero_host_budget_cannot_expand_or_discard_initial_selection(self):
        self.chain()
        self.settings.max_facts_all = 1
        recall = self.system.recall('fixture', 'List all projects and people.', AT,
                                    budget=4000, coverage_budget=0)
        self.assertEqual(len(recall.items), 1)
        self.assertFalse(recall.trace['coverage_supplement']['attempted'])

    def test_host_adapter_uses_configured_memory_model_when_chat_role_is_absent(self):
        self.models.chat = None
        self.writer.handlers['answer'] = 'A complete fixture answer.'
        reply = self.system.respond('fixture', 'A general fixture request.', AT)
        self.assertEqual(reply.text, 'A complete fixture answer.')
        self.assertEqual(self.writer.purposes().count('answer'), 1)

    def test_window_keeps_whole_rounds_and_excludes_current_input_from_history_budget(self):
        self.settings.chat_window_turns = 1
        self.settings.chat_window_tokens = 200
        history = [dict(role=role, content=content, session_id='s', idx=i) for i, (role, content) in enumerate([
            ('user', 'old request'), ('assistant', 'old answer'), ('user', 'new request'), ('assistant', 'new answer'),
            ('user', 'Mandatory latest input ' * 40)])]
        window = select_window(history, self.settings)
        self.assertEqual([row['idx'] for row in window.messages], [2, 3, 4])
        self.assertEqual([row['idx'] for row in window.dropped], [0, 1])
        self.assertLessEqual(window.trace['history_units'], 200)

    def test_window_flushes_only_evicted_complete_rounds_before_recall(self):
        self.settings.chat_window_turns = 1
        session = self.system.open_session('fixture', AT)
        text = 'I participate in Prism.'
        for role, content in [('user', text), ('assistant', 'Acknowledged.'), ('user', 'A recent request.'),
                              ('assistant', 'A recent answer.'), ('user', 'A pending current question.')]:
            self.system.add_turn(session, role, content, AT)
        self.writer.handlers['extract'] = json.dumps(dict(entities=[entity('Prism', 'project')], facts=[dict(
            text=text, kind='personal', subject='user', attribute='project', value='Prism', rounds=[1],
            source_quotes=[dict(round=1, role='user', quote=text)], relations=[edge('user', 'participates_in', 'Prism')])]))
        store = self.system.store('fixture')
        window = self.system.prepare_window(store, store.conversation_turns(session.session_id))
        self.assertEqual(store.session(session.session_id)['turns_done'], 2)
        self.assertEqual([row['idx'] for row in window.messages], [2, 3, 4])
        self.assertTrue(window.trace['eviction_checked'])
        self.assertEqual(len(store.facts()), 1)

    def test_failed_window_extraction_keeps_saved_messages_and_watermark(self):
        self.settings.chat_window_turns = 0
        session = self.system.open_session('fixture', AT)
        self.system.add_turn(session, 'user', 'I participate in Prism.', AT)
        self.system.add_turn(session, 'assistant', 'Acknowledged.', AT)
        self.writer.handlers['extract'] = lambda _: (_ for _ in ()).throw(ModelError('fixture extraction failed'))
        store = self.system.store('fixture')
        with self.assertRaisesRegex(ModelError, 'fixture extraction failed'):
            self.system.prepare_window(store, store.conversation_turns(session.session_id))
        self.assertEqual(store.session(session.session_id)['turns_done'], 0)
        self.assertEqual(len(store.turns(session.session_id)), 2)


if __name__ == '__main__':
    unittest.main()
