import json
import unittest

from helpers import settings
from memsys.budget import budget_counter
from memsys.fake import ScriptedChat
from memsys.semantic_review import select_excerpts


class BoundedSelectionTest(unittest.TestCase):
    def inputs(self):
        clauses = ['I paid $17 for the reed basket.', 'I paid $23 for the stone tray.']
        excerpts = [('[' + key + '] 2025-05-01', clause + '\n' + 'Unrelated background. ' * 160)
                    for key, clause in zip(['E1', 'E2'], clauses)]
        return clauses, excerpts

    def model(self, clauses, fault=None):
        def compact(messages):
            records = json.loads(messages[-1]['content'].split('Records:\n', 1)[1])
            retained = [(record['id'], clause) for record in records for clause in clauses if clause in record['quote']]
            return json.dumps(dict(reviewed=[] if fault == 'coverage' else [r['id'] for r in records],
                keep=[rid for rid, _ in retained],
                excerpts=[dict(id=rid, quote='I paid $900 for the reed basket.' if fault == 'quote' else clause)
                          for rid, clause in retained], insufficient=False))
        def select(messages):
            records = json.loads(messages[-1]['content'].split('Records:\n', 1)[1])
            return json.dumps(dict(keep=[r['id'] for r in records], insufficient=False))
        return ScriptedChat({'evidence_compact': compact, 'evidence_select': select})

    def run_selection(self, model, excerpts, **overrides):
        s = settings(semantic_validation=True, budget_mode='utf8', answer_input_budget_tokens=4000, **overrides)
        selected, audit = select_excerpts(model, 'What did I pay for the reed basket and the stone tray?',
            excerpts, s, '2025-05-02', content_budget=2000)
        return selected, audit, s

    def test_overlong_evidence_preserves_both_operands_and_reads_every_literal_piece(self):
        clauses, excerpts = self.inputs()
        model = self.model(clauses)
        selected, audit, s = self.run_selection(model, excerpts)
        self.assertEqual(selected, [(excerpts[n][0], clause) for n, clause in enumerate(clauses)])
        self.assertEqual(audit['mode'], 'bounded_batch_compaction')
        self.assertGreater(len(audit['batches']), 1)
        reconstructed = {}
        for batch in audit['batches']:
            for record in batch['records']:
                reconstructed[record['source']] = reconstructed.get(record['source'], '') + record['quote']
        self.assertEqual(reconstructed, dict(excerpts))
        counter = budget_counter(s)
        self.assertTrue(all(counter.messages(messages) <= s.answer_input_budget_tokens
                            for _, messages in model.calls))
        self.assertEqual(model.purposes()[-1], 'evidence_select')

    def test_unreviewed_or_fabricated_batch_evidence_never_reaches_global_selection(self):
        clauses, excerpts = self.inputs()
        for fault in ['coverage', 'quote']:
            with self.subTest(fault=fault):
                model = self.model(clauses, fault)
                with self.assertRaises(ValueError):
                    self.run_selection(model, excerpts)
                self.assertNotIn('evidence_select', model.purposes())

    def test_page_limit_rejects_overflow_without_selecting_a_partial_answer(self):
        clauses, excerpts = self.inputs()
        model = self.model(clauses)
        with self.assertRaisesRegex(ValueError, 'aggregate_max_pages'):
            self.run_selection(model, excerpts, aggregate_max_pages=1)
        self.assertEqual(model.calls, [])


if __name__ == '__main__':
    unittest.main()
