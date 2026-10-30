"""Source-independent failure isolation and provenance recovery checks."""
from datetime import datetime
import json
import unittest

from memsys.config import MemorySettings, Models
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.llm import BadJSONError, ChatResult, IncompleteJSONError
from memsys.qdrant import QdrantSettings
from memsys.store import Store
from memsys.write import Writer
from tests.qdrant_fixture import Engine

AT = datetime(2034, 2, 8)


class ExtractionIsolationTests(unittest.TestCase):
    def setup_writer(self, entries=None, *, raw=None, reply=None):
        self.store = Store(user_id='synthetic-isolation', client=Engine(),
            settings=QdrantSettings(namespace='independent-entry-isolation'))
        self.sid = self.store.create_session(AT)
        for role, content in [('user', 'The display base is hexagonal.'), ('assistant', 'Noted.'),
                              ('user', 'The carrying sleeve is blue.'), ('assistant', 'Understood.')]:
            self.store.append_turn(self.sid, role, content, AT)
        self.raw = raw if raw is not None else json.dumps({'facts': entries or []})
        self.model = ScriptedChat({'extract': reply or self.raw})
        self.writer = Writer(self.store, Models(None, self.model, HashEmbedder(), 'hash-256'),
            MemorySettings(rerank_backend='fusion', extract_max_tokens=4096, extract_max_tokens_limit=4096))
        return self.writer

    def fact(self, **extra):
        return dict(text='The display base is hexagonal.', kind='personal', subject='display base',
            attribute='shape', value='hexagonal', rounds=[1], **extra)

    def test_bad_neighbor_does_not_discard_valid_fact_and_is_fully_auditable(self):
        bad = [{'text': 42, 'rounds': [1]}, ['positional', 'row'],
               dict(text='An unsupported fixture assertion.', rounds=[99]),
               dict(text='A malformed matrix.', rounds=[2], value={'type': 'table', 'columns': ['a', 'b'], 'rows': [['x']]})]
        self.setup_writer([self.fact(), *bad])
        report = self.writer.consolidate(self.sid, closed_at=AT)
        self.assertEqual([f.text for f in self.store.facts()], ['The display base is hexagonal.'])
        self.assertEqual(report.extract_rejected_count, 4)
        self.assertFalse(report.extract_protocol_complete)
        self.assertEqual(self.store.session(self.sid)['turns_done'], 4)
        self.assertEqual(self.model.purposes(), ['extract'])
        audit = self.store.record('extraction_diagnostic', report.extraction_diagnostic_id)
        self.assertEqual(audit['responses'][0]['text'], self.raw)
        self.assertEqual(len(audit['rejected']), 4)
        self.assertEqual(audit['status'], 'partial')
        self.assertEqual(len(self.store.turns(self.sid)), 4)
        health = self.store.record('extraction_health', 'current')
        self.assertEqual(health['rejected_entries'], 4)
        self.assertFalse(health['semantic_completeness_verified'])

    def test_missing_source_is_repaired_only_by_unique_literal_role_quote(self):
        entry = self.fact();entry['rounds'] = ['not-a-source']
        entry['source_quotes'] = [{'round': 77, 'role': 'user', 'quote': 'The display base is hexagonal.'}]
        self.setup_writer([entry])
        report = self.writer.consolidate(self.sid, closed_at=AT)
        self.assertTrue(report.extract_protocol_complete)
        self.assertEqual(report.extract_repaired_count, 1)
        self.assertEqual(report.extract_repairs[0]['method'], 'literal_unique_quotes')
        fact = self.store.facts()[0]
        source = next(episode for episode in self.store.episodes() if episode.round_idx == 0)
        self.assertEqual(self.store.sources([fact.id])[fact.id], [source.id])
        self.assertEqual(len(self.store.record('fact', fact.id)['spans']), 1)
        self.assertIsNone(self.store.record('extraction_health', 'current'))

    def test_repeated_quote_cannot_establish_a_unique_round(self):
        self.setup_writer([dict(text='A fixture summary.', source_quotes=[dict(role='assistant', quote='Noted.')])])
        self.store.append_turn(self.sid, 'user', 'Another source round.', AT)
        self.store.append_turn(self.sid, 'assistant', 'Noted.', AT)
        report = self.writer.consolidate(self.sid, closed_at=AT)
        self.assertEqual(self.store.facts(), [])
        self.assertEqual(report.extract_rejected_count, 1)

    def test_explicit_invalid_round_is_not_guessed_even_in_one_round(self):
        self.setup_writer([dict(text='A different object has a label.', rounds=[99])])
        self.sid = self.store.create_session(AT)
        self.store.append_turn(self.sid, 'user', 'A separate source sentence.', AT)
        report = self.writer.consolidate(self.sid, closed_at=AT)
        self.assertEqual(self.store.facts(), [])
        self.assertFalse(report.extract_protocol_complete)

    def test_all_rejected_rows_close_processing_but_never_claim_complete_extraction(self):
        self.setup_writer([{'text': 7}, ['invalid']])
        report = self.writer.consolidate(self.sid, closed_at=AT)
        self.assertEqual(self.store.facts(), [])
        self.assertEqual(self.store.session(self.sid)['status'], 'closed')
        self.assertEqual(self.store.session(self.sid)['turns_done'], 4)
        self.assertEqual(len(self.store.episodes()), 2)
        self.assertFalse(report.extract_protocol_complete)
        self.assertIn('partial', report.extract_quality)
        self.assertEqual(self.store.backlog_count(), 0)

    def test_unparseable_output_remains_atomic_and_preserves_full_failure(self):
        self.setup_writer(raw='{"facts":[')
        with self.assertRaises(BadJSONError) as raised:
            self.writer.consolidate(self.sid, closed_at=AT)
        self.assertEqual(self.store.facts(), [])
        self.assertEqual(self.store.episodes(), [])
        self.assertEqual(self.store.session(self.sid)['turns_done'], 0)
        report = raised.exception.extraction_report
        self.assertEqual(report['extract_calls'], 1)
        audit = self.store.record('extraction_diagnostic', raised.exception.extraction_diagnostic_id)
        self.assertEqual(audit['responses'][0]['text'], self.raw)
        self.assertEqual(audit['status'], 'failed')

    def test_capped_output_does_not_publish_salvaged_facts(self):
        raw = json.dumps({'facts': [self.fact()]})
        self.setup_writer(raw=raw, reply=lambda _: ChatResult(raw, 'authored', 1, 4096, 0, 0, 0, 'length'))
        with self.assertRaises(IncompleteJSONError) as raised:
            self.writer.consolidate(self.sid, closed_at=AT)
        self.assertEqual(self.store.facts(), [])
        self.assertEqual(self.store.session(self.sid)['turns_done'], 0)
        audit = self.store.record('extraction_diagnostic', raised.exception.extraction_diagnostic_id)
        self.assertEqual(audit['responses'][0]['text'], raw)
        self.assertEqual(audit['report']['extract_attempts'][0]['finish_reason'], 'length')


if __name__ == '__main__':
    unittest.main()
