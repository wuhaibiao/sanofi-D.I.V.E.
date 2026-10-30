"""Task organization and source uncertainty."""
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

from memsys.budget import TokenCounter
from memsys.read import Candidate, QueryPlan, Retriever, SearchResult, fit_to_budget, render_memory
from memsys.queryops import operation_for
from memsys.schema import normalize_semantics
from memsys.write import Writer
from tests.test_structured_facts import StructuredFactFixture

AT = datetime(2034, 3, 12)


class TaskContextTests(StructuredFactFixture, unittest.TestCase):
    def test_procedural_advice_is_recognized_without_turning_recollection_into_new_goals(self):
        for question in ('How could we improve our rehearsal?', 'What would be sensible first steps for my project?',
                         'Could you give me a practical plan?', '怎样改进我的练习安排？'):
            with self.subTest(question=question): self.assertEqual(operation_for(question), 'advice')
        for question in ('What were the first steps I took?', 'Can you recall the plan you gave me?',
                         'How many rehearsal sessions did I complete?', '我上次的计划是什么？'):
            with self.subTest(question=question): self.assertNotEqual(operation_for(question), 'advice')

    def add(self, text, *, role=None, task=None, kind='preference', status=None, at=AT, value=''):
        sid = self.store.create_session(at)
        semantics = {}
        if role: semantics['context_role'] = role
        if task: semantics['task_key'] = task
        if status: semantics['event_status'] = status
        fid = self.store.add_fact(text=text, kind=kind, subject='user' if kind != 'assistant' else 'assistant',
            attribute='synthetic_context', value=value, event=None, recorded_at=at, valid_from=at.date(),
            session_id=sid, importance=.8, semantics=semantics)
        return self.store.fact(fid)

    def select(self, facts, budget=20000, operation='advice'):
        result = SearchResult([Candidate('fact', f.id) for f in facts], {f.key: f for f in facts}, False)
        plan = QueryPlan(['Any advice for my project?'], [], None, False, 'any', 'test', operation=operation)
        return Retriever(self.store, self.models, self.settings).select(result, plan, budget, AT.date())

    def test_groups_linked_goal_constraints_and_resources_without_repeating_content(self):
        goal = self.add('The user is preparing a short guided tour.', role='goal', task='guided tour', status='planned')
        limit = self.add('The guided tour must fit into twelve minutes.', role='constraint', task='guided tour', value=12)
        tool = self.add('The user has a printed street map for the tour.', role='resource', task='guided tour')
        option = self.add('The assistant suggested a paid audio guide.', kind='assistant', role='goal', task='paid guide')
        text = render_memory(self.select([option, limit, goal, tool]), AT.date())
        self.assertIn('Task candidate: "guided tour"', text)
        self.assertNotIn('Task candidate: "paid guide"', text)
        self.assertLess(text.index(goal.text), text.index('Prior assistant techniques'))
        for fact in (goal, limit, tool, option):
            self.assertEqual(text.count(fact.text), 1)
            self.assertEqual(text.count('[' + fact.key + ']'), 1)
        self.assertIn('does not establish that a plan is current', text)

    def test_cancelled_and_legacy_goals_stay_visible_without_inferred_replacement(self):
        cancelled = self.add('The user cancelled the public rehearsal.', role='goal', task='rehearsal', status='cancelled')
        current = self.add('The user wants to record a short welcome.', at=AT+timedelta(days=1))
        text = render_memory(self.select([cancelled, current]), AT.date())
        self.assertIn('Event status: cancelled', text)
        self.assertIn(current.text, text)
        self.assertIn('Legacy unclassified facts', text)
        self.assertIsNone(self.store.fact(cancelled.id).superseded_by)

    def test_task_headers_are_budgeted_and_removing_a_fact_removes_its_task(self):
        facts = [self.add('The user has a project ' + str(n), role='goal', task='project ' + str(n), value='x' * 250)
                 for n in range(3)]
        selected = self.select(facts)
        text = fit_to_budget(selected, AT.date(), 1450, counter=TokenCounter('utf8'))
        self.assertLessEqual(len(text.encode()), 1450)
        for fact in facts:
            if fact.key in selected.omitted_facts:
                self.assertNotIn(fact.text, text)
                self.assertNotIn('Task candidate: ' + json.dumps(fact.semantics['task_key']), text)

    def test_optional_context_labels_do_not_change_legacy_semantics_identity(self):
        self.assertNotIn('context_role', normalize_semantics({'value_type': 'state'}))
        self.assertNotIn('task_key', normalize_semantics({'value_type': 'state'}))
        self.assertNotIn('context_role', normalize_semantics({'context_role': 'unsupported'}))
        self.assertEqual(normalize_semantics({'context_role': ' Goal ', 'task_key': ' Local Activity '})['task_key'], 'local activity')



    def test_partial_extraction_is_visible_without_recalling_quarantined_content(self):
        sid = self.store.create_session(AT)
        self.store.append_turn(sid, 'user', 'The fixture identifier is amber.', AT)
        self.model.handlers['extract'] = json.dumps({'facts': [dict(text='The fixture identifier is amber.',
            rounds=[1], value='amber'), dict(text='REJECTED_PAYLOAD_MARKER', rounds=[72])]})
        report = Writer(self.store, self.models, self.settings).consolidate(sid, closed_at=AT)
        with patch.object(self.store, 'episodes', side_effect=AssertionError('original fallback')):
            recall = self.system.recall('fixture', 'List all fixture identifiers.', AT, budget=4000, save_trace=False)
        self.assertIn('source coverage is incomplete', recall.memory)
        self.assertNotIn('REJECTED_PAYLOAD_MARKER', recall.context)
        self.assertFalse(recall.trace['coverage']['context_complete'])
        self.assertFalse(recall.trace['coverage']['source_protocol_complete'])
        self.assertEqual(report.extract_rejected_count, 1)
        self.assertEqual(recall.trace['selected']['episodes'], [])


if __name__ == '__main__':
    unittest.main()
