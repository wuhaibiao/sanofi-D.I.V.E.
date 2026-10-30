"""Record-scoped recall does not manufacture real-world finality or dates."""
import json
import unittest
from unittest.mock import patch

from memsys import prompts
from memsys.aggregation import answer_with_validation
from memsys.fake import ScriptedChat
from memsys.budget import TokenCounter
from memsys.semantic_review import DATE_RESPONSE_RULES,RESPONSE_RULES,review_response


class ScopedTemporalRecallTest(unittest.TestCase):
    def fixture(self):
        evidence={'E1':'User: I calibrated the amber scanner on May 4, 2025.',
                  'E2':'User: I inspected the dome on May 21, 2025.'}
        spec=dict(operation='date_difference',unit='weeks',result=2,
            start=dict(source='E1',quote=evidence['E1'],date_text='May 4, 2025'),
            end=dict(source='E2',quote=evidence['E2'],date_text='May 21, 2025'))
        answer='Using the most recent matching calibration in the available dated records: 2 complete weeks (17 days).'
        response='CALCULATION: '+json.dumps(spec)+'\nANSWER: '+answer
        question='How many weeks passed between my last amber scanner calibration and the dome inspection?'
        return evidence,response,question

    def test_source_proof_and_record_scope_reach_the_independent_reviewer(self):
        evidence,response,question=self.fixture()
        observed=[]
        def review(messages):
            observed.extend(messages)
            return json.dumps(dict(responsive=True,supported=True))
        model=ScriptedChat({'answer':response,'answer_review':review})
        _,answer,audit=answer_with_validation(model,[{'role':'system','content':prompts.DATE_ANSWER_RULES}],
            evidence=evidence,question=question,today='2025-05-30',operation='date_difference',semantic_enabled=True)
        self.assertEqual(audit['status'],'checks_passed')
        self.assertIn('available dated records',answer)
        self.assertIn('without claiming real-world finality',observed[0]['content'])
        self.assertIn('missing endpoint',observed[0]['content'])
        self.assertIn('"elapsed_days": 17',observed[1]['content'])

    def test_qualification_does_not_override_missing_or_contradictory_event_identity(self):
        evidence,response,question=self.fixture()
        model=ScriptedChat({'answer':response,'answer_review':json.dumps(dict(responsive=False,supported=False,
            reason='A newer undated calibration prevents establishing the requested occurrence order.'))})
        _,answer,audit=answer_with_validation(model,[],evidence=evidence,question=question,
            today='2025-05-30',operation='date_difference',semantic_enabled=True)
        self.assertEqual(audit['status'],'incomplete')
        self.assertNotIn('2 complete weeks',answer)

    def test_date_scope_rules_do_not_change_non_date_review_prompts(self):
        observed=[]
        def review(messages):
            observed.extend(messages)
            return json.dumps(dict(responsive=True,supported=True))
        model=ScriptedChat({'answer_review':review})
        review_response(model,'Which instrument is violet?','The spectrometer.',
            {'E1':'The spectrometer is violet.'},'lookup',[],TokenCounter('utf8'),65536)
        self.assertEqual(observed[0]['content'],RESPONSE_RULES)
        self.assertNotIn(DATE_RESPONSE_RULES,observed[0]['content'])

    def test_scoped_date_rules_are_part_of_the_prompt_fingerprint(self):
        from memsys.bench.provenance import prompt_fingerprint
        before=prompt_fingerprint()
        with patch('memsys.semantic_review.DATE_RESPONSE_RULES',DATE_RESPONSE_RULES+' Synthetic diagnostic marker.'):
            self.assertNotEqual(prompt_fingerprint(),before)
        self.assertEqual(prompt_fingerprint(),before)


if __name__=='__main__':
    unittest.main()
