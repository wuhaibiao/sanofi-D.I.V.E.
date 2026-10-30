import json
import unittest
from datetime import datetime
from types import SimpleNamespace

from helpers import settings
from memsys.aggregate_batch import paged_aggregate, source_speaker
from memsys.config import Models
from memsys.evidence_batch import paged_answer
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.queryops import operation_for
from memsys.read import Candidate, plan_query
from memsys.schema import Episode, Fact, EPISODE_ACTIVE


class SpeakerDifferenceTest(unittest.TestCase):
    def test_previous_state_precedes_personal_best_latest_default(self):
        question = 'What was my previous personal best for the park race?'
        model = ScriptedChat({'query':json.dumps(dict(operation='latest_state',needs_all=False))})
        models = Models(chat=None,memory=model,embedder=HashEmbedder(),embedding_name='hash')
        plan = plan_query(models,question,datetime(2025,5,1),settings(semantic_validation=True))
        self.assertEqual(plan.operation,'historical_state')
        self.assertTrue(plan.needs_all)
        self.assertEqual(operation_for('What is my personal best for the park race?'),'latest_state')
        self.assertEqual(operation_for('What were my previous and current addresses?'),'compare_states')

    def test_numeric_difference_requires_both_operands_despite_lookup_plan(self):
        questions = ['How much did I save choosing the ferry instead of a taxi?',
                     'How many more trees do I own now than before?',
                     'How much older is my brother than my sister?', '比出租车省了多少钱？']
        model = ScriptedChat({'query': json.dumps(dict(operation='lookup', needs_all=False))})
        models = Models(chat=None, memory=model, embedder=HashEmbedder(), embedding_name='hash')
        for question in questions:
            with self.subTest(question=question):
                self.assertEqual(operation_for(question), 'difference')
                plan = plan_query(models, question, datetime(2025, 5, 1), settings(semantic_validation=True))
                self.assertEqual(plan.operation, 'difference')
                self.assertTrue(plan.needs_all)
        self.assertEqual(operation_for('How many days passed between my two visits?'), 'date_difference')
        self.assertEqual(operation_for('What is the difference between my hobbies and my job?'), 'lookup')
        self.assertEqual(operation_for('How many trees do I own?'), 'count_entities')

    def test_literal_speaker_attribution_handles_ambiguity_and_mixed_quotes(self):
        e = Episode(1, 's', 0, datetime(2025, 5, 1),
                    'I paid $90. Same wording.', 'A generic estimate is $250. Same wording.', 1, EPISODE_ACTIVE)
        self.assertEqual(source_speaker(e, 'I paid $90.'), 'user')
        self.assertEqual(source_speaker(e, 'A generic estimate is $250.'), 'assistant')
        self.assertEqual(source_speaker(e, 'Same wording.'), 'ambiguous')
        self.assertEqual(source_speaker(e, e.text()), 'mixed')
        self.assertEqual(source_speaker(e, 'The user paid $250.'), 'ambiguous')
        f = Fact(1, 'The user paid $90.', 'personal', 'user', 'price', '90', None,
                 e.at, e.at.date(), None, 'current', None, False, 1, 1, 's')
        self.assertEqual(source_speaker(f, f.text), 'derived')

    def test_page_speaker_metadata_is_copied_from_original_turn(self):
        e = Episode(1, 's', 0, datetime(2025, 5, 1), 'I collect postcards.',
                    'I suggested looking at a museum shop.', 1, EPISODE_ACTIVE)
        result = SimpleNamespace(ranked=[Candidate('episode', 1)], items={'E1': e},
                                 coverage={'search_complete': True})
        def answer(messages):
            memory = messages[-1]['content']
            self.assertIn('speaker=assistant', memory)
            self.assertIn(e.assistant_text, memory)
            return 'NOTES: E1\nANSWER: A museum shop.'
        model = ScriptedChat({'evidence_page': json.dumps(dict(reviewed=['E1'], items=[dict(
            source='E1', quote=e.assistant_text, speaker='user')])), 'answer': answer})
        _, response, audit = paged_answer(model, 'Which place did you suggest?', result, {},
                                          settings(), e.at.date())
        self.assertEqual(response, 'A museum shop.')
        self.assertEqual(audit['status'], 'evidence_checked')

    def test_numeric_contribution_cannot_forge_its_speaker(self):
        e = Episode(1, 's', 0, datetime(2025, 5, 1), 'I spent 3 days in Oslo.', '', 1, EPISODE_ACTIVE)
        result = SimpleNamespace(ranked=[Candidate('episode', 1)], items={'E1': e},
                                 coverage={'search_complete': True})
        item = dict(source='E1', quote=e.text(), value='3', unit='days', speaker='assistant')
        model = ScriptedChat({'aggregate_page': json.dumps(dict(reviewed=['E1'], operation='sum',
                                                               unit='days', items=[item]))})
        _, answer, audit = paged_aggregate(model, 'What is my total travel duration?', result, {},
                                           settings(), e.at.date())
        self.assertEqual(answer, '3 days')
        self.assertEqual(audit['candidate_contributions'][0]['speaker'], 'user')


if __name__ == '__main__':
    unittest.main()
