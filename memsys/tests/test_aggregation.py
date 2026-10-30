from __future__ import annotations

import json
import unittest

from helpers import dt, settings
from memsys.aggregation import answer_with_validation, inspect_calculation
from memsys.config import Models
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.llm import ChatResult, ModelError


def output(items, result='165', unit='USD', operation='sum', answer=None):
    spec = dict(operation=operation, items=items, result=result, unit=unit)
    return 'NOTES: quoted entries.\nCALCULATION: ' + json.dumps(spec) + '\nANSWER: ' + (answer or result)


class CalculationTest(unittest.TestCase):
    def setUp(self):
        self.evidence = {'E1': 'User: I paid $120 for the lodge booking.',
                         'E2': 'User: I paid $45 for the rail ticket.'}
        self.items = [{'source': 'E1', 'quote': 'I paid $120 for the lodge booking.', 'value': '120', 'unit': 'USD'},
                      {'source': 'E2', 'quote': 'I paid $45 for the rail ticket.', 'value': '45', 'unit': '$'}]

    def inspect(self, text, evidence=None, origins=None):
        return inspect_calculation(text, evidence or self.evidence, origins)

    def test_truncated_answer_is_retried_without_replaying_partial_as_final(self):
        replies = iter([ChatResult('ANSWER: 999', 'fake', 1, 1024, 0, 0, 0, 'length'),
                        ChatResult('NOTES: original date.\nANSWER: 3', 'fake', 1, 10, 0, 0, 0, 'stop')])
        model = ScriptedChat({'answer': lambda messages: next(replies)})
        messages = [{'role': 'user', 'content': 'How many calendar months ago?'}]
        _, answer, audit = answer_with_validation(model, messages, evidence={})
        self.assertEqual(answer, '3')
        self.assertEqual(len(audit['generation_attempts']), 2)
        self.assertFalse(any(m['role'] == 'assistant' for m in model.calls[1][1]))

    def test_repeated_empty_or_incomplete_generation_is_an_error(self):
        for text, reason in [('', 'stop'), ('ANSWER: 999', 'length'), ('NOTES: Still working', 'stop')]:
            model = ScriptedChat({'answer': lambda m: ChatResult(text, 'fake', 1, 5, 0, 0, 0, reason)})
            with self.assertRaisesRegex(ModelError, 'empty or incomplete'):
                answer_with_validation(model, [{'role': 'user', 'content': 'Question'}], evidence={}, enabled=False)
            self.assertEqual(len(model.calls), 2)

    def test_exact_quotes_units_and_decimal_arithmetic(self):
        evidence = {'E1': 'Paid $0.10.', 'E2': 'Paid $0.20.'}
        items = [{'source': key, 'quote': quote, 'value': val, 'unit': 'USD'}
                 for key, quote, val in [('E1', 'Paid $0.10.', '0.10'), ('E2', 'Paid $0.20.', '0.20')]]
        clean, audit = self.inspect(output(items, '0.30', answer='0.30 dollars.'), evidence)
        self.assertEqual(audit['status'], 'checks_passed')
        self.assertEqual(audit['computed'], '0.30')
        self.assertNotIn('CALCULATION', clean)

    def test_wrong_sum_or_answer_is_detected(self):
        for result, answer in [('164', '164 dollars'), ('165', '164 dollars')]:
            _, audit = self.inspect(output(self.items, result, answer=answer))
            self.assertEqual(audit['status'], 'invalid')
            self.assertEqual(audit['computed'], '165')

    def test_missing_source_invented_quote_and_wrong_literal_are_rejected(self):
        for field, value in [('source', 'E99'), ('quote', 'I paid $500.'), ('value', '500')]:
            items = [dict(self.items[0], **{field: value}), self.items[1]]
            _, audit = self.inspect(output(items))
            self.assertEqual(audit['status'], 'invalid')

    def test_duplicate_evidence_and_fact_episode_overlap_are_rejected(self):
        _, audit = self.inspect(output([self.items[0], self.items[0]], '240'))
        self.assertIn('duplicate contribution', ' '.join(audit['errors']))
        evidence = {**self.evidence, 'F1': 'The user paid $120 for lodging.'}
        items = [self.items[0], {'source': 'F1', 'quote': evidence['F1'], 'value': '120', 'unit': 'USD'}]
        _, audit = self.inspect(output(items, '240'), evidence, {'E1': {1}, 'F1': {1}})
        self.assertIn('original episode', ' '.join(audit['errors']))

    def test_two_separate_contributions_in_one_episode_are_allowed(self):
        evidence = {'E1': 'Paid $120 for the lodge. Paid $45 for the ticket.'}
        items = [dict(self.items[0], quote='Paid $120 for the lodge.'),
                 dict(self.items[1], source='E1', quote='Paid $45 for the ticket.')]
        self.assertEqual(self.inspect(output(items), evidence, {'E1': {1}})[1]['status'], 'checks_passed')

    def test_mixed_units_and_year_as_currency_are_rejected(self):
        mixed = [self.items[0], dict(self.items[1], unit='EUR')]
        self.assertEqual(self.inspect(output(mixed))[1]['status'], 'invalid')
        quote = 'In 2025 I paid €120 and $45.'
        for value in ['2025', '120']:
            item = dict(source='E1', quote=quote, value=value, unit='USD')
            audit = self.inspect(output([item], value), {'E1': quote})[1]
            self.assertEqual(audit['status'], 'invalid')

    def test_chinese_adjacent_quantity_unit(self):
        quote = '我买了3本书。后来又买了2本书。'
        items = [{'source': 'E1', 'quote': '我买了3本书。', 'value': '3', 'unit': '本'},
                 {'source': 'E1', 'quote': '后来又买了2本书。', 'value': '2', 'unit': '本'}]
        self.assertEqual(self.inspect(output(items, '5', unit='本', answer='5本。'), {'E1': quote})[1]['status'], 'checks_passed')

    def test_unique_names_deduplicate_but_substring_names_fail(self):
        evidence = {'E1': 'Alice and Bob joined.', 'E2': 'Alice returned.'}
        items = [{'source': 'E1', 'quote': evidence['E1'], 'entity': name} for name in ['Alice', 'Bob']]
        items += [{'source': 'E2', 'quote': evidence['E2'], 'entity': 'Alice'}]
        _, audit = self.inspect(output(items, '2', unit='people', operation='count_unique'), evidence)
        self.assertEqual(audit['status'], 'checks_passed')
        self.assertEqual(audit['duplicates_removed'], 1)
        _, audit = self.inspect(output([{'source': 'E1', 'quote': 'Annette joined.', 'entity': 'Ann'}],
                                      '1', operation='count_unique'), {'E1': 'Annette joined.'})
        self.assertEqual(audit['status'], 'invalid')

    def test_explicit_word_and_digit_group_quantities_have_auditable_counts(self):
        evidence = {'E1': 'Three apprentices joined.', 'E2': 'One supervisor joined.',
                    'E3': 'Twenty-one visitors arrived.', 'E4': '2 guests arrived.'}
        items = [dict(source=source, quote=quote, value=str(value), quantity_label=noun, unit='people')
                 for (source, quote), value, noun in zip(evidence.items(), [3, 1, 21, 2],
                                                       ['apprentices', 'supervisor', 'visitors', 'guests'])]
        _, audit = self.inspect(output(items, '27', unit='people', operation='count_groups'), evidence)
        self.assertEqual((audit['status'], audit['computed']), ('checks_passed', '27'))

    def test_group_count_cannot_use_dates_invented_counts_or_currency(self):
        quote = 'In 2025 Three apprentices joined. Each apprentice received 2 awards.'
        base = dict(source='E1', quote=quote, value='3', quantity_label='apprentices', unit='people')
        for altered in [dict(base, value='2025'), dict(base, quantity_label='apprentice', value='2'),
                        dict(base, value='4'), dict(base, unit='USD')]:
            audit = self.inspect(output([altered], altered['value'], unit=altered['unit'], operation='count_groups'),
                                 {'E1': quote})[1]
            self.assertEqual(audit['status'], 'invalid')

    def test_individually_identified_object_can_be_counted_without_an_invented_number(self):
        quote = 'My only weather instrument is a thermometer.'
        item = dict(source='E1', quote=quote, entity='thermometer')
        audit = self.inspect(output([item], '1', unit='instruments', operation='count_unique'), {'E1': quote})[1]
        self.assertEqual(audit['status'], 'checks_passed')

    def test_missing_unsupported_malformed_and_nonfinite_are_unverified(self):
        self.assertEqual(self.inspect('ANSWER: 165')[1]['status'], 'not_provided')
        self.assertEqual(self.inspect(output(self.items, operation='multiply'))[1]['status'], 'unsupported')
        self.assertEqual(self.inspect('CALCULATION: broken\nANSWER: 165')[1]['status'], 'invalid')
        for value in ['NaN', 'Infinity', True, '1e9']:
            _, audit = self.inspect(output([dict(self.items[0], value=value)], '120'))
            self.assertEqual(audit['status'], 'invalid')

    def test_single_repair_uses_evidence_and_does_not_expose_json(self):
        model = ScriptedChat({'answer': [output(self.items, '164'), output(self.items)]})
        notes, answer, audit = answer_with_validation(model, [{'role': 'user', 'content': 'Total?'}], evidence=self.evidence)
        self.assertEqual(answer, '165')
        self.assertEqual(audit['status'], 'checks_passed')
        self.assertEqual(len(audit['attempts']), 2)
        self.assertEqual(len(model.calls), 2)
        self.assertNotIn('CALCULATION', notes)
        self.assertIn('do not invent evidence', model.calls[1][1][-1]['content'].lower())

    def test_repair_failure_rejects_unverified_total_and_preserves_draft_in_trace(self):
        def unavailable(_):
            raise ModelError('model unavailable')
        # callable handler after the first output
        replies = [output(self.items, '164')]
        def handler(messages):
            return replies.pop() if replies else unavailable(messages)
        model = ScriptedChat({'answer': handler})
        _, answer, audit = answer_with_validation(model, [{'role': 'user', 'content': 'Total?'}], evidence=self.evidence)
        self.assertIn("couldn't verify", answer)
        self.assertEqual(audit['rejected_answer'], '164')
        self.assertEqual(audit['status'], 'invalid')
        self.assertIn('repair_error', audit)
        model = ScriptedChat({'answer': [output(self.items, '164'), output(self.items, '164')]})
        self.assertEqual(answer_with_validation(model, [], evidence=self.evidence)[2]['status'], 'invalid')
        self.assertEqual(len(model.calls), 2)

    def test_no_calculation_and_disabled_mode_do_not_add_a_call(self):
        model = ScriptedChat({'answer': 'ANSWER: 165'})
        _, answer, audit = answer_with_validation(model, [], evidence=self.evidence)
        self.assertEqual(answer, '165')
        self.assertEqual(audit['status'], 'not_provided')
        self.assertEqual(len(audit['generation_attempts']), 1)
        self.assertEqual(len(model.calls), 1)
        model = ScriptedChat({'answer': output(self.items, '164')})
        self.assertEqual(answer_with_validation(model, [], evidence=self.evidence, enabled=False)[1], '164')
        self.assertEqual(len(model.calls), 1)

    def test_repair_cannot_bypass_failed_check_by_omitting_calculation(self):
        model = ScriptedChat({'answer': [output(self.items, '164'), 'ANSWER: 165']})
        _, answer, audit = answer_with_validation(model, [], evidence=self.evidence, question='合计多少？')
        self.assertEqual(answer, '目前无法根据引用证据核实这个合计。')
        self.assertEqual(audit['status'], 'not_provided')
        self.assertEqual(audit['rejected_answer'], '165')

    def test_core_uses_selected_evidence_and_records_audit(self):
        model = ScriptedChat({'extract': '{"facts": []}', 'answer': [output(self.items, '164'), output(self.items)]})
        system = MemorySystem(Models(model, model, HashEmbedder(), 'hash'),
                              settings(query_analysis=False, max_history_share=0))
        self.addCleanup(system.close)
        at = dt('2025-02-01T10:00')
        for text in self.evidence.values():
            session = system.open_session('u', at)
            system.add_turn(session, 'user', text.removeprefix('User: '), at)
            system.close_session(session, at)
        reply = system.respond('u', 'What is the total I paid for the lodge and rail ticket?', at)
        self.assertEqual(reply.text, '165')
        self.assertEqual(reply.trace['aggregation']['status'], 'checks_passed')
        calls = [messages for purpose, messages in model.calls if purpose == 'answer']
        self.assertIn('CALCULATION:', calls[0][1]['content'])
        self.assertIn('$120', calls[0][1]['content'])
        self.assertNotIn('CALCULATION:', reply.notes)


class FilteredGenerationTest(unittest.TestCase):
    def test_filtered_partial_answer_is_not_published_or_format_retried(self):
        from memsys.llm import ChatResult, FunctionChat, ModelError
        calls = []
        def blocked(messages):
            calls.append(messages)
            return ChatResult('ANSWER: 42', 'provider', 0, 0, 0, 0, 0, 'content_filter')
        with self.assertRaisesRegex(ModelError, 'content_filter'):
            answer_with_validation(FunctionChat(blocked), [], evidence={}, enabled=False)
        self.assertEqual(len(calls), 1)

    def test_partial_valid_json_is_not_accepted_as_completed_page_evidence(self):
        from memsys.llm import ChatResult, FunctionChat, ModelError, chat_json
        for reason in ('length', 'incomplete', 'content_filter', 'refusal'):
            model = FunctionChat(lambda messages: ChatResult('{"reviewed":["E1"],"items":[]}',
                                  'provider', 0, 0, 0, 0, 0, reason))
            with self.assertRaisesRegex(ModelError, reason):
                chat_json(model, [], purpose='evidence_page')


if __name__ == '__main__':
    unittest.main()
