import json
import unittest
from datetime import datetime
from types import SimpleNamespace

from helpers import settings
from memsys.evidence_batch import complete_answer
from memsys.fake import ScriptedChat
from memsys.queryops import operation_for
from memsys.read import Candidate
from memsys.schema import Episode, EPISODE_ACTIVE
from memsys.user_context import literal_user_index


class QuantityUserContextTest(unittest.TestCase):
    def test_full_user_relation_is_copied_without_assistant_reinterpretation(self):
        source='[E1] 2025-04-01 speaker=mixed\nUser: I own a case with a pen, which comes in 70 finishes.\nAssistant: The case has 70 finishes.'
        index=literal_user_index({'E1':source},'quantity')
        self.assertIn('a case with a pen, which comes in 70 finishes.',index)
        self.assertNotIn('Assistant:',index)
        self.assertNotIn('The case has 70 finishes.',index)
        self.assertEqual(index.count('70 finishes'),1)

    def test_count_index_preserves_complete_original_and_proof_review(self):
        e=Episode(1,'s',0,datetime(2025,4,1),'I have 7 ceramic mugs.',
                  'You could also buy 4 more mugs.',1,EPISODE_ACTIVE)
        result=SimpleNamespace(ranked=[Candidate('episode',1)],items={'E1':e},coverage={'search_complete':True})
        proof={'operation':'count_groups','unit':'mugs','items':[{'source':'E1',
               'quote':e.user_text,'quantity_label':'ceramic mugs','value':'7','unit':'mugs'}],'result':'7'}
        def answer(messages):
            supplied=messages[-1]['content']
            self.assertEqual(supplied.count('User: '+e.user_text),2)
            self.assertEqual(supplied.count('Assistant: '+e.assistant_text),1)
            return 'CALCULATION: '+json.dumps(proof)+'\nANSWER: 7 ceramic mugs.'
        def review(messages):
            self.assertIn('Original user statements, copied verbatim for quantity scope',messages[-1]['content'])
            return json.dumps(dict(responsive=True,supported=True))
        model=ScriptedChat({'answer':answer,'answer_review':review})
        _,text,audit=complete_answer(model,'How many mugs do I own?',result,{},
            settings(complete_episode_context=True,semantic_validation=True,budget_mode='utf8',
                     context_budget_tokens=12000,answer_input_budget_tokens=24000),'2025-04-02',
            operation='count_entities')
        self.assertEqual(text,'7 ceramic mugs.')
        self.assertTrue(audit['user_statement_index']['applied'])
        self.assertEqual(audit['answer_validation']['status'],'checks_passed')
        self.assertEqual(audit['original_context'].count(e.user_text),1)

    def test_uncertain_cause_assessment_uses_advice_without_reclassifying_recall(self):
        self.assertEqual(operation_for('Could you advise me? Do you think it might be the workshop ventilation?'),'advice')
        self.assertEqual(operation_for('What could be causing my poor concentration?'),'advice')
        self.assertEqual(operation_for('Which workshop did I attend?'),'lookup')
        self.assertEqual(operation_for('How many days passed between my two visits?'),'date_difference')


if __name__=='__main__':
    unittest.main()
