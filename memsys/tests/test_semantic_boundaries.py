"""Authored data/provenance boundaries, independent of public evaluation labels.

Semantic role/state/relative-time outcomes are checked separately with a real
model. Scripted chat here tests fallback content, publication and call limits.
"""
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from memsys.memory_compaction import covers_literal
from memsys.schema import EPISODE_WITHHELD
from memsys.structure_memory import assistant_fields
from memsys.write import Writer
from tests.test_structured_facts import StructuredFactFixture


class LabelledFieldTests(unittest.TestCase):
    def test_scalar_values_keep_literal_labels_units_and_heading_scope(self):
        text = '# Observatory kit\nSupplies:\n* Lens sleeves: 84 units\n* Cable ties: 210 units\n'
        fields = assistant_fields(text)
        self.assertEqual(len(fields), 2)
        self.assertEqual(fields[0].value, dict(type='fields', headers=['Observatory kit', 'Supplies'],
            items=[dict(label='Lens sleeves', value='84 units')]))
        self.assertEqual(fields[1].value['items'], [dict(label='Cable ties', value='210 units')])
        for item in fields:
            self.assertIn(item.quote, text)
            self.assertTrue(all(quote in text for quote in item.context_quotes))

    def test_chinese_fields_and_time_values_are_preserved_without_conversion(self):
        fields = assistant_fields('# 展厅装配\n灯光：\n* 灯条长度：6.25米\n* 开始时间：06:35')
        self.assertEqual([f.value['items'][0]['value'] for f in fields], ['6.25米', '06:35'])
        self.assertEqual(fields[1].value['headers'], ['展厅装配', '灯光'])

    def test_code_urls_bare_times_and_table_cells_are_not_scalar_fields(self):
        source = ('~~~text\nCount: 93\n```\nCount: 94\n~~~\n'
                  '1. Meet at 06:35.\n08:20\nVisit https://sample.example/77\n'
                  '| name | value |\n| --- | --- |\n| Counts: 18 | 11 |\n')
        self.assertEqual(assistant_fields(source), [])

    def test_long_values_are_not_truncated_and_prose_is_not_rewritten(self):
        source = 'Notes: ' + 'Part 18 has an explanatory qualification. ' * 30
        self.assertEqual(assistant_fields(source), [])
        self.assertEqual(assistant_fields('The equipment weighs about eighteen kilos.'), [])

    def test_changing_heading_does_not_transfer_a_number_to_another_configuration(self):
        fields = assistant_fields('# Kit west\nCount: 14\n# Kit east\nCount: 29')
        self.assertEqual([f.value['headers'] for f in fields], [['Kit west'], ['Kit east']])
        self.assertEqual([f.value['items'][0]['value'] for f in fields], ['14', '29'])

    def test_exact_coverage_keeps_same_labels_values_and_context(self):
        value = assistant_fields('# Batch nine\nUnits: 43')[0].value
        self.assertTrue(covers_literal(value, value))
        self.assertFalse(covers_literal(dict(value, headers=['Other batch']), value))
        self.assertFalse(covers_literal(dict(value, items=[dict(label='Units', value='44')]), value))


class WriteQualityTests(StructuredFactFixture, unittest.TestCase):
    def test_empty_semantic_result_keeps_explicit_fields_as_assistant_evidence_once(self):
        source = '# Observatory kit\nSupplies:\n* Lens sleeves: 84 units'
        sid, report = self.write('', items=[], source=source)
        self.assertEqual(report.extract_model_facts, 0)
        self.assertEqual(report.extract_quality, 'not_semantically_verified')
        self.assertEqual(report.quality_warnings[0]['code'], 'empty_model_extraction')
        self.assertEqual(self.model.purposes(), ['extract'])
        self.assertEqual(self.store.session(sid)['turns_done'], 2)
        facts = self.store.facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].kind, 'assistant')
        self.assertEqual(facts[0].subject, 'assistant')
        self.assertEqual(json.loads(facts[0].value)['items'][0]['value'], '84 units')
        row = self.store.record('fact', facts[0].id)
        self.assertTrue(any(s['quote'] == '* Lens sleeves: 84 units' for s in row['spans']))
        self.assertTrue(any(s['quote'] == '# Observatory kit' for s in row['spans']))
        self.assertIn('内容覆盖', report.summary())

    def test_zero_fact_response_is_visible_without_retry_or_invented_fact(self):
        _, report = self.write('', items=[], source='A general conceptual explanation.')
        self.assertEqual(self.store.facts(), [])
        self.assertEqual(sum(w['code']=='empty_model_extraction' for w in report.quality_warnings), 1)
        self.assertTrue(any(w['code']=='source_accounting_unreported' for w in report.quality_warnings))
        self.assertEqual(report.retry_rounds, [])
        self.assertEqual(self.model.purposes(), ['extract'])

    def test_greetings_do_not_trigger_a_semantic_coverage_warning(self):
        at = datetime(2026, 9, 11)
        sid = self.store.create_session(at)
        self.store.append_turn(sid, 'user', '谢谢', at)
        self.store.append_turn(sid, 'assistant', '好的', at)
        self.model.handlers['extract'] = '{"facts":[]}'
        report = Writer(self.store, self.models, self.settings).consolidate(sid, closed_at=at)
        self.assertEqual(report.quality_warnings, [])
        self.assertTrue(report.source_coverage)
        self.assertEqual(self.store.facts(), [])

    def test_protocol_valid_nonempty_facts_do_not_claim_semantic_verification(self):
        _, report = self.write('Planned enclosure', text='The assistant proposed an enclosure.')
        self.assertEqual(report.extract_model_facts, 1)
        self.assertEqual(report.extract_quality, 'not_semantically_verified')
        self.assertTrue(report.quality_warnings)
        self.assertTrue(all(w['code']=='source_accounting_unreported' for w in report.quality_warnings))

    def test_exact_field_coverage_does_not_duplicate_a_richer_structured_fact(self):
        source = '# Observatory kit\nSupplies:\n* Lens sleeves: 84 units'
        value = assistant_fields(source)[0].value
        _, report = self.write(value, source=source)
        self.assertEqual(len(self.store.facts()), 1)
        self.assertEqual(report.covered_literal_structures, 1)

    def test_withheld_numeric_fields_cannot_be_restored(self):
        with patch('memsys.write.assistant_structures', return_value=[]):
            self.write('', items=[], source='# Observatory kit\nUnits: 84')
        self.store.set_status('episode', [self.store.episodes()[0].id], EPISODE_WITHHELD)
        self.model.calls.clear()
        self.assertEqual(Writer(self.store, self.models, self.settings).restore_structures(), [])
        self.assertEqual(self.store.facts(), [])
        self.assertEqual(self.model.calls, [])
