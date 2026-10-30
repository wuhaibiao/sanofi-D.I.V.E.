import json
import re
import unittest

from helpers import dt, models, settings
from memsys.core import MemorySystem

AT = dt('2025-05-04T10:00')


class PagedEvidenceTest(unittest.TestCase):
    def system(self, bad_quote=False, **overrides):
        def page(messages):
            found = re.findall(r'\[(E\d+)\][^\n]*\n([^\n]+)', messages[1]['content'])
            return json.dumps(dict(reviewed=[key for key, _ in found],
                items=[dict(source=key, quote='Invented quotation.' if bad_quote else match.group())
                       for key, text in found for match in [re.search(r'R\d+:.*?dividers\.', text)] if match]))
        config = dict(query_analysis=False, max_history_share=0, budget_mode='utf8',
                      # Each retained excerpt now carries its source speaker.
                      context_budget_tokens=800, min_similarity=0)
        config.update(overrides)
        system = MemorySystem(models(chat={'evidence_page': page, 'evidence_page_repair': page,
                                           'answer': 'NOTES: Reviewed recommendations.\nANSWER: ring binder, tab dividers'}),
                              settings(**config))
        self.addCleanup(system.close)
        store = system.store('u')
        with store.transaction():
            sid = store.create_session(AT)
            for n in range(8):
                store.add_episode(sid, n, AT, 'Recommendation record R%d: ring binder, tab dividers.' % n +
                                  ' Irrelevant packing detail.' * 6, '', .8, 'active')
        return system

    def test_list_question_uses_all_pages_and_bounded_evidence_synthesis(self):
        system = self.system()
        reply = system.respond('u', 'List all recommended organizers in original order.', AT)
        self.assertEqual(reply.text, 'ring binder, tab dividers')
        audit = reply.trace['aggregation']
        self.assertEqual(audit['mode'], 'paged_evidence')
        self.assertTrue(audit['pages_complete'])
        self.assertGreater(audit['pages_processed'], 1)
        self.assertEqual(len(audit['reviewed_sources']), 8)
        self.assertLessEqual(audit['synthesis_memory_count'], 800)
        prompt = [messages for purpose, messages in system.models.chat.calls if purpose == 'answer'][0][1]['content']
        self.assertNotIn('CALCULATION:', prompt)
        for n in range(8):
            self.assertIn('R%d:' % n, prompt)

    def test_invented_excerpts_cannot_reach_final_answer(self):
        system = self.system(bad_quote=True)
        reply = system.respond('u', 'List all recommended organizers.', AT)
        self.assertFalse(reply.trace['aggregation']['pages_complete'])
        self.assertIn('not an exact contiguous', reply.trace['aggregation']['error'])
        self.assertNotIn('answer', system.models.chat.purposes())

    def test_complete_evidence_over_budget_cannot_be_truncated_to_partial_answer(self):
        system = self.system(context_budget_tokens=200)
        reply = system.respond('u', 'List all recommended organizers.', AT)
        self.assertFalse(reply.trace['aggregation']['pages_complete'])
        self.assertIn('synthesis context budget', reply.trace['aggregation']['error'])
        self.assertNotIn('answer', system.models.chat.purposes())

    def test_paged_date_synthesis_cannot_publish_an_unproved_numeric_interval(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from memsys.evidence_batch import paged_answer
        from memsys.fake import ScriptedChat
        page = {'E1': {'text': 'Today I calibrated the telescope.',
                      'rendered': '[E1] 2025-01-03\nToday I calibrated the telescope.', 'can_contribute': True}}
        page_reply = json.dumps(dict(reviewed=['E1'], items=[dict(source='E1', quote=page['E1']['text'])]))
        model = ScriptedChat({'evidence_page': page_reply, 'answer': 'ANSWER: 15'})
        with patch('memsys.evidence_batch.aggregation_pages', return_value=[page]):
            _, answer, audit = paged_answer(model, 'How many days since the telescope calibration?',
                SimpleNamespace(coverage={'search_complete': True}, items={}), {}, settings(), AT.date())
        self.assertIn("couldn't verify this date interval", answer)
        self.assertEqual(audit['status'], 'invalid')
        self.assertEqual(audit['date_validation']['status'], 'invalid')
