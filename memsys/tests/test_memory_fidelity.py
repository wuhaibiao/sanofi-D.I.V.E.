"""Source-independent checks for advice presentation and coverage claims."""
from dataclasses import asdict
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from memsys import prompts
from memsys.budget import TokenCounter
from memsys.fake import ScriptedChat
from memsys.read import Candidate, QueryPlan, Retriever, SearchResult, Selection, fit_to_budget, render_memory
from memsys.webchat import WebChat
from memsys.write import NewFact, Round
from memsys.write_coverage import source_coverage
from tests.test_structured_facts import StructuredFactFixture

AT = datetime(2032, 4, 17, 10)


class AdviceFactPresentation(StructuredFactFixture, unittest.TestCase):
    def add(self, text, *, kind, at=AT, value='', semantics=None):
        sid = self.store.create_session(at)
        fid = self.store.add_fact(text=text, value=value, kind=kind, subject='user' if kind != 'assistant' else 'assistant',
            attribute='synthetic_activity', event=None, recorded_at=at, valid_from=at.date(),
            session_id=sid, importance=.8, semantics=semantics)
        return self.store.fact(fid)

    def select(self, facts, *, operation='advice', budget=10000):
        result = SearchResult([Candidate('fact', f.id) for f in facts], {f.key: f for f in facts}, False)
        plan = QueryPlan(['How should I practise next?'], [], None, False, 'any', 'test', operation=operation)
        return Retriever(self.store, self.models, self.settings).select(result, plan, budget, AT.date())

    def test_user_context_precedes_older_assistant_options_without_changing_states_or_facts(self):
        suggested = self.add('The assistant suggested a class.', kind='assistant', at=AT-timedelta(days=7))
        cancelled = self.add('The user cancelled the live demonstration.', kind='event', at=AT-timedelta(days=2),
                             semantics={'event_status': 'cancelled'})
        current = self.add('The user is preparing a short recorded welcome.', kind='preference')
        unchanged = [asdict(f) for f in self.store.facts()]
        selected = self.select([suggested, current, cancelled])
        memory = render_memory(selected, AT.date())
        self.assertLess(memory.index(cancelled.key), memory.index(current.key))
        self.assertLess(memory.index(current.key), memory.index(suggested.key))
        self.assertIn('Event status: cancelled', memory)
        for f in (suggested, cancelled, current):
            self.assertEqual(memory.count('['+f.key+']'), 1)
            self.assertEqual(memory.count(f.text), 1)
        self.assertEqual([asdict(f) for f in self.store.facts()], unchanged)
        self.assertEqual(selected.episodes, [])

    def test_ordinary_recall_keeps_global_chronology(self):
        older = self.add('The assistant described a sample kit.', kind='assistant', at=AT-timedelta(days=4))
        newer = self.add('The user is comparing activities.', kind='personal')
        selected = self.select([newer, older], operation='historical_state')
        text = render_memory(selected, AT.date())
        self.assertLess(text.index(older.key), text.index(newer.key))
        self.assertNotIn('user context first', text)

    def test_budget_uses_actual_advice_rendering_and_preserves_complete_values(self):
        user = self.add('The user has a short rehearsal slot.', kind='preference',
                        value=json.dumps({'minutes': 15, 'place': 'lunchtime room'}))
        noise = self.add('The assistant suggested optional activities.', kind='assistant', value='optional ' * 80)
        one = Selection([user], [], 0, ordering={'scope': 'user', 'operation': 'advice'})
        limit = len(render_memory(one, AT.date()).encode())
        selected = self.select([noise, user], budget=limit)
        memory = fit_to_budget(selected, AT.date(), limit, counter=TokenCounter('utf8'))
        self.assertEqual([f.key for f in selected.facts], [user.key])
        self.assertIn(user.value, memory)
        self.assertLessEqual(len(memory.encode()), limit)

    def test_actual_host_supplies_task_rules_and_generates_only_the_answer(self):
        user = self.add('The user is preparing a recorded welcome.', kind='preference')
        question = 'Any advice for my recorded welcome?'
        recall = self.system.recall('fixture', question, AT, budget=10000, save_trace=False)
        expected = 'Try a brief recording using three cue words.'
        model = ScriptedChat({'answer': expected})
        host = WebChat(SimpleNamespace(req=self.system, models=SimpleNamespace(chat=model, memory=None)))
        messages = [dict(role='system', content=host.system_prompt+'\n'+recall.context),
                    dict(role='user', content=question)]
        with patch.object(self.store, 'episodes', side_effect=AssertionError('original retrieval')):
            response = host.answer(messages)
        self.assertIn(prompts.PERSONALIZATION_RULES, model.calls[0][1][0]['content'])
        self.assertEqual(model.purposes(), ['answer'])
        self.assertEqual(response.text, expected)
        self.assertFalse(hasattr(response,'evidence_audit'))
        self.assertNotIn('advice_task',model.calls[0][1][0]['content'])
        self.assertIn('let them determine the practical',model.calls[0][1][0]['content'])
        self.assertIn(user.key, recall.memory)


class CoverageMeaning(unittest.TestCase):
    def test_one_supported_assertion_is_not_proof_that_all_source_details_survived(self):
        source = 'The rig outlet faces northwest. Its vent stays open while idle.'
        fact = NewFact('The rig outlet faces northwest.', 'assistant', 'rig', 'outlet', 'northwest', None, [1], .6,
                       source_quotes=[dict(round=1, role='assistant', quote='The rig outlet faces northwest.')])
        declarations = {'coverage': [dict(round=1, role='assistant', decision='retained')]}
        records, warnings = source_coverage(declarations, [Round(1, 0, AT, '', source)], [fact])
        self.assertEqual(records[0]['accounting'], 'declared')
        self.assertEqual(records[0]['accounting_basis'], 'fact_source_links_only')
        self.assertFalse(records[0]['content_completeness_verified'])
        self.assertEqual(records[0]['semantic_coverage'], 'not_verified')
        self.assertEqual(warnings, [])


if __name__ == '__main__':
    unittest.main()
