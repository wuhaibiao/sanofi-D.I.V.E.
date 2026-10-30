"""Cause assessment preserves provenance and does not change unrelated prompts."""
import json
import unittest
from unittest.mock import patch

from memsys.advice_intent import CAUSE_ANALYSIS_RULES,causal_advice_rules
from memsys.budget import TokenCounter
from memsys.fake import ScriptedChat
from memsys.queryops import operation_rules
from memsys.semantic_review import review_response


class CausalAdviceTest(unittest.TestCase):
    def test_only_causal_advice_gets_the_extra_instructions(self):
        questions=('Do you think it might be the workshop ventilation?',
                   'What could be causing my poor concentration?',
                   'Could this explain the buzzing noise?',
                   '你觉得会不会是我的书桌位置导致的？')
        for question in questions:
            self.assertEqual(causal_advice_rules(question,'advice'),CAUSE_ANALYSIS_RULES)
            self.assertEqual(causal_advice_rules(question,'lookup'),'')
            self.assertEqual(causal_advice_rules(question,'count_entities'),'')
        self.assertEqual(causal_advice_rules('Recommend a recorded mystery program.','advice'),'')

    def test_unrelated_operation_prompts_are_byte_identical(self):
        for operation in ('lookup','count_entities','date_difference','advice'):
            self.assertEqual(operation_rules(operation,['available']),
                operation_rules(operation,['available'],'Suggest a recorded mystery program.'))

    def test_reviewer_gets_timing_and_uncertainty_without_asserted_new_actions(self):
        observed=[]
        def review(messages):
            observed.extend(messages)
            return json.dumps(dict(responsive=True,supported=True))
        model=ScriptedChat({'answer_review':review})
        evidence={'E1':'[E1] 2025-04-01 speaker=user\nUser: My workshop has a noisy fan. I might move my desk later.'}
        review_response(model,'Do you think it might be the workshop ventilation?',
            'The noisy fan could contribute. Has its use or your desk position changed recently?',
            evidence,'advice',[],TokenCounter('utf8'),65536)
        content=observed[-1]['content']
        self.assertIn(CAUSE_ANALYSIS_RULES,content)
        self.assertIn('I might move my desk later.',content)
        self.assertIn('never as something the user already did',content)
        self.assertNotIn('You moved your desk',content)

    def test_causal_instructions_are_in_the_prompt_fingerprint(self):
        from memsys.bench.provenance import prompt_fingerprint
        before=prompt_fingerprint()
        with patch('memsys.advice_intent.CAUSE_ANALYSIS_RULES',CAUSE_ANALYSIS_RULES+' Synthetic diagnostic marker.'):
            self.assertNotEqual(before,prompt_fingerprint())
