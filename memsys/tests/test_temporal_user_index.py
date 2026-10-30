import json
import unittest
from datetime import datetime
from types import SimpleNamespace

from helpers import settings
from memsys.budget import budget_counter
from memsys.evidence_batch import prepare_complete_context
from memsys.read import Candidate
from memsys.schema import Episode,EPISODE_ACTIVE
from memsys.user_context import temporal_user_index


class TemporalUserIndexTest(unittest.TestCase):
    def fixture(self):
        episodes=[Episode(1,'one',0,datetime(2025,4,1),'I started Lantern Road today.',
                          'You could finish it soon.',1,EPISODE_ACTIVE),
                  Episode(2,'two',0,datetime(2025,4,8),'I finished Lantern Road today. I am now reading another book.',
                          'A different novel might interest you.',1,EPISODE_ACTIVE)]
        result=SimpleNamespace(ranked=[Candidate('episode',e.id) for e in episodes],
            items={e.key:e for e in episodes},coverage={'search_complete':True})
        return episodes,result

    def test_relative_lookup_copies_completion_cue_and_keeps_every_original(self):
        episodes,result=self.fixture()
        s=settings(complete_episode_context=True,semantic_validation=True,budget_mode='utf8',context_budget_tokens=8000,
                   answer_input_budget_tokens=16000)
        prepared=prepare_complete_context('Which novel did I finish a week ago?',result,{},s,'2025-04-15')
        self.assertTrue(prepared['index_applied'])
        self.assertIn('I finished Lantern Road today.',prepared['messages'][-1]['content'])
        for e in episodes:self.assertIn(e.text(),prepared['memory'])
        self.assertNotIn('2025-04-15',temporal_user_index(prepared['evidence'],'Which novel did I finish a week ago?'))

    def test_index_is_optional_when_only_full_originals_fit(self):
        episodes,result=self.fixture()
        s=settings(complete_episode_context=True,semantic_validation=True,budget_mode='utf8',context_budget_tokens=8000,
                   answer_input_budget_tokens=16000)
        first=prepare_complete_context('Which novel did I finish a week ago?',result,{},s,'2025-04-15')
        s.context_budget_tokens=budget_counter(s).count(first['memory'])
        second=prepare_complete_context('Which novel did I finish a week ago?',result,{},s,'2025-04-15')
        self.assertFalse(second['index_applied'])
        self.assertEqual(first['memory'],second['memory'])

    def test_plans_are_not_rewritten_and_assistant_events_are_excluded(self):
        evidence={'E1':'[E1] 2025-04-01\nUser: I might finish the craft tomorrow.\nAssistant: You finished today.',
                  'F1':'[F1] 2025-04-01\nUser: An invented derived completion today.'}
        index=temporal_user_index(evidence,'When did I finish?')
        self.assertIn('I might finish the craft tomorrow.',index)
        self.assertNotIn('You finished today.',index)
        self.assertNotIn('invented derived',index)
