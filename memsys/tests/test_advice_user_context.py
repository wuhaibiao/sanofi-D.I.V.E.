import json
import unittest
from datetime import datetime
from types import SimpleNamespace

from helpers import settings
from memsys.budget import budget_counter
from memsys.evidence_batch import complete_answer
from memsys.fake import ScriptedChat
from memsys.read import Candidate
from memsys.schema import Episode, EPISODE_ACTIVE
from memsys.semantic_review import review_response
from memsys.user_context import literal_user_index


class AdviceUserContextTest(unittest.TestCase):
    def sources(self):
        episodes = [
            Episode(1, 'old', 0, datetime(2025, 4, 2),
                    'I prefer recorded mystery audio on SoundShelf. Live video is not for me.',
                    'A related visual series is available elsewhere. ' * 18, 1, EPISODE_ACTIVE),
            Episode(2, 'new', 0, datetime(2025, 4, 9),
                    'I might try those earbuds next month. Have they been reviewed?',
                    'They might help your listening experience.', 1, EPISODE_ACTIVE)]
        result = SimpleNamespace(ranked=[Candidate('episode', e.id) for e in episodes],
            items={e.key:e for e in episodes}, coverage={'search_complete':True})
        return episodes, result

    def config(self, **extra):
        return settings(complete_episode_context=True, semantic_validation=True,
            budget_mode='utf8', context_budget_tokens=16000, answer_input_budget_tokens=24000, **extra)

    def test_index_preserves_questions_plans_source_roles_and_full_originals(self):
        episodes, result = self.sources()
        def answer(messages):
            content = messages[-1]['content']
            self.assertIn('Original user statements, copied verbatim', content)
            for e in episodes:
                self.assertEqual(content.count('User: '+e.user_text), 2)
                self.assertEqual(content.count('Assistant: '+e.assistant_text), 1)
            self.assertIn('speaker=mixed', content)
            self.assertIn('specific format, platform', content)
            return 'ANSWER: A recorded mystery audio program on SoundShelf fits your preference.'
        model = ScriptedChat({'answer':answer, 'answer_review':json.dumps(dict(responsive=True,supported=True))})
        _, text, audit = complete_answer(model, 'Suggest something for this evening.',result,{},
            self.config(), '2025-04-10', operation='advice')
        self.assertIn('SoundShelf',text)
        self.assertTrue(audit['user_statement_index']['applied'])
        self.assertEqual(audit['original_context'].count('I might try those earbuds'),1)

    def test_optional_index_never_drops_originals_when_only_originals_fit(self):
        episodes,result=self.sources()
        def answer(messages):
            content=messages[-1]['content']
            self.assertNotIn('Original user statements, copied verbatim',content)
            for e in episodes:self.assertIn(e.text(),content)
            return 'ANSWER: Choose a recorded audio program.'
        model=ScriptedChat({'answer':answer,'answer_review':json.dumps(dict(responsive=True,supported=True))})
        # Measure the original complete memory, then allow no duplicate index.
        original='\n\n'.join('[%s] %s session=%s round=%s speaker=mixed\n%s' %
            (e.key,e.at.date(),e.session_id,e.round_idx,e.text()) for e in episodes)
        cfg=self.config();cfg.context_budget_tokens=budget_counter(cfg).count(original)
        _,_,audit=complete_answer(model,'Suggest an audio program.',result,{},cfg,'2025-04-10',operation='advice')
        self.assertFalse(audit['user_statement_index']['applied'])
        self.assertEqual(audit['original_context'],original)
        self.assertLessEqual(audit['synthesis_memory_count'],cfg.context_budget_tokens)

    def test_semantic_review_index_skips_when_prompt_allowance_is_full(self):
        evidence={'E1':'[E1] 2025-04-02 speaker=mixed\nUser: I only use recorded audio.\nAssistant: Try a live broadcast.'}
        cfg=self.config();counter=budget_counter(cfg)
        observed=[]
        def review(messages):
            observed.append(messages)
            return json.dumps(dict(responsive=True,supported=True))
        model=ScriptedChat({'answer_review':review})
        review_response(model,'Suggest a program.','Try recorded audio.',evidence,'lookup',(),counter,24000)
        base=counter.messages(observed[-1])
        # advice rules use more space; find the exact unindexed prompt allowance.
        review_response(model,'Suggest a program.','Try recorded audio.',evidence,'advice',(),counter,24000)
        indexed=observed[-1]
        index=literal_user_index(evidence)
        unindexed=[indexed[0],dict(role='user',content=indexed[1]['content'][len(index)+2:])]
        limit=counter.messages(unindexed)
        self.assertGreater(limit,base)
        review_response(model,'Suggest a program.','Try recorded audio.',evidence,'advice',(),counter,limit)
        self.assertEqual(counter.messages(observed[-1]),limit)
        self.assertNotIn('Original user statements, copied verbatim',observed[-1][1]['content'])

    def test_other_evidence_shapes_and_assistant_only_records_are_not_user_statements(self):
        evidence={'E1':'[E1] speaker=assistant\nAssistant: You could try a bicycle.',
                  'F1':'[F1] derived summary\nThe user bought a bicycle.',
                  'E2':'[E2] speaker=mixed\nUser: Could I try a bicycle?\nAssistant: Yes.'}
        index=literal_user_index(evidence)
        self.assertIn('Could I try a bicycle?',index)
        self.assertNotIn('bought',index)
        self.assertNotIn('Assistant:',index)
        self.assertNotIn('[E1]',index)


if __name__=='__main__':unittest.main()
