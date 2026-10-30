"""Nested quantity cues remain user-only, conditional and operation-scoped."""
import json
import unittest
from unittest.mock import patch

from memsys.budget import TokenCounter
from memsys.fake import ScriptedChat
from memsys.quantity_scope import QUANTITY_RELATION_RULES, relative_quantity_rules
from memsys.semantic_review import RESPONSE_RULES, review_response
from memsys.user_context import literal_user_index


class QuantityRelationTest(unittest.TestCase):
    def evidence(self, user, assistant=''):
        return {'E1': '[E1] 2025-04-01 speaker=mixed\nUser: ' + user
                + ('\nAssistant: ' + assistant if assistant else '')}

    def test_general_nested_relations_trigger_a_risk_not_an_attachment_decision(self):
        for user in ('I have a case with a pen, which comes in 70 finishes.',
                     'I saved the wrapper from a boxed collection, which had twenty variants.',
                     'The drawer containing my repair kit, which includes 6 tools, is blue.'):
            evidence=self.evidence(user)
            self.assertEqual(relative_quantity_rules(evidence,'count_entities'),QUANTITY_RELATION_RULES)
            self.assertIn(user,literal_user_index(evidence,'quantity'))
        self.assertIn('not proof that the wording is ambiguous',QUANTITY_RELATION_RULES)
        self.assertIn('must not be used as a contribution in arithmetic',QUANTITY_RELATION_RULES)

    def test_assistant_and_derived_numeric_clauses_do_not_trigger(self):
        evidence=self.evidence('I have a case with a pen.',
            'The case with the pen, which comes in 70 finishes, looks nice.')
        self.assertEqual(relative_quantity_rules(evidence,'count_entities'),'')
        evidence={'F1':'[F1] 2025-04-01\nUser: A case with a pen, which comes in 70 finishes.'}
        self.assertEqual(relative_quantity_rules(evidence,'count_entities'),'')
        self.assertEqual(relative_quantity_rules(self.evidence('I have 7 ceramic mugs.'),'count_entities'),'')

    def test_unrelated_review_prompts_remain_byte_identical(self):
        evidence=self.evidence('I have a case with a pen, which comes in 70 finishes.')
        for operation in ('lookup','advice'):
            observed=[]
            model=ScriptedChat({'answer_review':lambda messages: observed.extend(messages)
                or json.dumps(dict(responsive=True,supported=True))})
            review_response(model,'Describe the case.','It contains a pen.',evidence,operation,[],
                TokenCounter('utf8'),65536)
            self.assertEqual(observed[0]['content'],RESPONSE_RULES)

    def test_quantity_review_has_original_user_context_and_conditional_rules(self):
        evidence=self.evidence('I have a case with a pen, which comes in 70 finishes.',
            'The case itself has 70 finishes.')
        observed=[]
        model=ScriptedChat({'answer_review':lambda messages: observed.extend(messages)
            or json.dumps(dict(responsive=True,supported=True))})
        review_response(model,'How many finishes does the pen come in?',
            'The wording mentions 70 finishes but does not resolve the attachment.',
            evidence,'count_entities',[],TokenCounter('utf8'),65536)
        self.assertIn(QUANTITY_RELATION_RULES,observed[0]['content'])
        indexed=observed[1]['content'].split('Caller date',1)[0]
        self.assertIn('User: I have a case with a pen, which comes in 70 finishes.',indexed)
        self.assertNotIn('Assistant:',indexed)

    def test_quantity_rules_are_in_prompt_fingerprint(self):
        from memsys.bench.provenance import prompt_fingerprint
        before=prompt_fingerprint()
        with patch('memsys.quantity_scope.QUANTITY_RELATION_RULES',QUANTITY_RELATION_RULES+' Diagnostic marker.'):
            self.assertNotEqual(before,prompt_fingerprint())
