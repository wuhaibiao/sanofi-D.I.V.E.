"""Self-authored storage/context contracts; no public evaluation data or labels."""
from dataclasses import asdict
from datetime import datetime, timedelta
import json
import unittest

from memsys.fact_content import fact_text, value_text
from memsys.llm import ModelError
from memsys.budget import TokenCounter
from memsys.memory_compaction import covers_literal
from memsys.read import Candidate, QueryPlan, Retriever, SearchResult, Selection, fit_to_budget, render_memory
from memsys.schema import normalize_semantics
from memsys.timeutil import DateSpan
from memsys.write import Writer
from tests.test_structured_facts import StructuredFactFixture

AT = datetime(2031, 11, 2, 10)


class MemoryV6Tests(StructuredFactFixture, unittest.TestCase):
    def add(self, text, value, *, kind='assistant', semantics=None, at=AT, event=None):
        sid = self.store.create_session(at)
        fid = self.store.add_fact(text=text, value=value_text(value), kind=kind,
            subject='fixture_unit', attribute='procedure', event=event, recorded_at=at,
            valid_from=at.date(), session_id=sid, importance=.8, semantics=semantics)
        return self.store.fact(fid)

    def test_same_round_complete_table_is_not_stored_twice_and_source_span_survives(self):
        table = dict(type='table', columns=['port', 'fixture'], rows=[['left', 'cobalt_fixture'], ['right', 'amber_fixture']])
        source = '| port | fixture |\n| --- | --- |\n| left | cobalt_fixture |\n| right | amber_fixture |'
        _, report = self.write(table, source=source)
        self.assertEqual(report.extract_calls, 1)
        self.assertEqual((report.literal_structures, report.covered_literal_structures), (1, 1))
        facts = self.store.facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(json.loads(facts[0].value), table)
        self.assertTrue(self.store.record('fact', facts[0].id)['spans'])

    def test_richer_named_list_retains_details_without_duplicate_literal(self):
        value = dict(type='list', items=[dict(position=1, name='Inspect fixture', detail='Check the seal'),
                                        dict(position=2, name='Attach probe', detail='Use port B')])
        source = '1. Inspect fixture - Check the seal.\n2. Attach probe - Use port B.'
        _, report = self.write(value, source=source)
        self.assertEqual(report.covered_literal_structures, 1)
        self.assertEqual(len(self.store.facts()), 1)
        self.assertIn('Check the seal', self.store.facts()[0].value)

    def test_missing_cells_or_wrong_order_never_omit_literal_source(self):
        source = '| port | fixture |\n| --- | --- |\n| left | cobalt_fixture |'
        wrong = dict(type='table', columns=['port', 'fixture'], rows=[['right', 'cobalt_fixture']])
        _, report = self.write(wrong, source=source)
        self.assertEqual(report.covered_literal_structures, 0)
        self.assertEqual(len(self.store.facts()), 2)
        literal = dict(type='list', items=[dict(position=4, name='Inspect fixture'), dict(position=5, name='Attach probe')])
        self.assertFalse(covers_literal(['Inspect fixture', 'Attach probe'], literal))
        literal['items'][0]['position'] = 1
        literal['items'][1]['position'] = 2
        self.assertFalse(covers_literal(['Attach probe', 'Inspect fixture'], literal))

    def test_boolean_position_is_not_the_same_as_numeric_one(self):
        literal = dict(type='list', items=[dict(position=1, name='Inspect fixture')])
        self.assertFalse(covers_literal(dict(type='list', items=[dict(position=True, name='Inspect fixture')]), literal))

    def test_sequence_rows_cover_actions_only_with_exact_positions_and_text(self):
        literal = dict(type='list', items=[dict(position=7, statement='Align fixture.'), dict(position=9, statement='Seal fixture.')])
        sequence = dict(type='sequence', columns=['position', 'actor', 'statement'],
            rows=[[7, 'unspecified', 'Align fixture.'], [9, 'unspecified', 'Seal fixture.']])
        self.assertTrue(covers_literal(sequence, literal))
        sequence['rows'][1][0] = 8
        self.assertFalse(covers_literal(sequence, literal))

    def test_user_fact_does_not_replace_assistant_literal(self):
        value = dict(type='list', items=['Inspect fixture'])
        _, report = self.write(value, source='1. Inspect fixture', extra={'kind': 'personal'})
        self.assertEqual(report.covered_literal_structures, 0)
        self.assertEqual(len(self.store.facts()), 2)

    def test_repeated_value_shares_one_copy_but_keeps_all_fact_descriptions_and_records(self):
        value = dict(type='list', items=['fixture_payload_' + str(i) for i in range(35)])
        first = self.add('A fixture inspection procedure was recorded.', value)
        second = self.add('The procedure also defines the fixture setup.', value)
        before = [asdict(f) for f in self.store.facts()]
        memory = render_memory(Selection([first, second], [], 0), AT.date())
        self.assertEqual(memory.count(value_text(value)), 1)
        anchor = min((first, second), key=lambda f: (f.when().start, f.id))
        self.assertIn('same structured value as [' + anchor.key + ']', memory)
        self.assertIn(first.text, memory)
        self.assertIn(second.text, memory)
        self.assertEqual([asdict(f) for f in self.store.facts()], before)
        limit = len(memory.encode())
        selection = Selection([first, second], [], 0)
        self.assertEqual(fit_to_budget(selection, AT.date(), limit, counter=TokenCounter('utf8')), memory)
        self.assertEqual(len(selection.facts), 2)

    def test_changed_conditions_or_recording_time_never_share_a_value(self):
        value = dict(type='list', items=['cobalt_fixture', 'amber_fixture'])
        first = self.add('A fixture sequence was recorded.', value, semantics={'qualifiers': ['dry setup']})
        second = self.add('A fixture sequence was recorded.', value, semantics={'qualifiers': ['wet setup']})
        third = self.add('A later fixture sequence was recorded.', value,
                         semantics={'qualifiers': ['dry setup']}, at=AT + timedelta(days=1))
        memory = render_memory(Selection([first, second, third], [], 0), AT.date())
        self.assertEqual(memory.count(value_text(value)), 3)

    def test_budget_removal_cannot_leave_a_dangling_value_reference(self):
        value = {'fixture_data': 'synthetic_payload_' * 20}
        first = self.add('First fixture description.', value)
        second = self.add('Second fixture description.', value)
        anchor, remaining = sorted((first, second), key=lambda f: (f.when().start, f.id))
        single = render_memory(Selection([remaining], [], 0), AT.date())
        selection = Selection([remaining, anchor], [], 0)
        memory = fit_to_budget(selection, AT.date(), len(single.encode()), counter=TokenCounter('utf8'))
        self.assertEqual(selection.facts, [remaining])
        self.assertNotIn('same structured value', memory)
        self.assertIn(value_text(value), memory)

    def test_explicit_planned_and_completed_states_are_distinct_and_indexed(self):
        _, first = self.write('cobalt_fixture', text='A fixture operation was recorded.',
            extra={'kind': 'event', 'event_date': '2031-11-08', 'certainty': 'firm',
                   'semantics': {'event_status': 'planned'}})
        _, second = self.write('cobalt_fixture', text='A fixture operation was recorded.',
            extra={'kind': 'event', 'event_date': '2031-11-08', 'certainty': 'firm',
                   'semantics': {'event_status': 'completed'}})
        self.assertEqual(len(self.store.facts()), 2)
        self.assertEqual((first.extract_calls, second.extract_calls), (1, 1))
        facts = self.store.facts()
        memory = render_memory(Selection(facts, [], 0), AT.date())
        self.assertIn('planned for 2031-11-08', memory)
        self.assertIn('Event status: planned', memory)
        self.assertIn('Event status: completed', memory)
        self.assertNotIn('happened ', memory)
        self.assertTrue(all('Event status:' in fact_text(f) for f in facts))

    def test_unknown_event_date_and_relative_anchor_are_preserved_in_api_and_pages(self):
        fact = self.add('A fixture service was discussed.', 'fixture service', kind='event',
            semantics={'relative_time': 'two days earlier', 'event_status': 'unknown'})
        recalled = self.system.recall('fixture', 'List all fixture service records.', AT + timedelta(days=5), budget=3000, save_trace=False)
        item = next(i for i in recalled.items if i['key'] == fact.key)
        self.assertIsNone(item['event_date'])
        self.assertEqual(item['recorded_at'], AT.isoformat())
        self.assertIn('relative to noted date: "two days earlier"', recalled.memory)
        pages = self.system.recall_pages('fixture', 'List all fixture service records.', AT)
        self.assertTrue(any('event=unknown' in p['context'] and 'noted=2031-11-02' in p['context'] for p in pages['pages']))

    def test_legacy_or_invalid_state_does_not_invent_completion(self):
        self.assertNotIn('event_status', normalize_semantics({'value_type': 'state'}))
        self.assertNotIn('event_status', normalize_semantics({'event_status': ['completed']}))
        self.assertEqual(normalize_semantics({'event_status': ' Planned '})['event_status'], 'planned')
        fact = self.add('A fixture operation was mentioned.', 'fixture operation', kind='event', event=DateSpan.day(AT.date()))
        memory = render_memory(Selection([fact], [], 0), AT.date())
        self.assertIn('event date 2031-11-02', memory)
        self.assertNotIn('Event status: completed', memory)

    def test_undated_event_without_occurrence_id_does_not_merge_across_sessions(self):
        extra = {'kind': 'event', 'semantics': {'event_status': 'completed'}}
        self.write('fixture inspection', text='A fixture inspection was completed.', source='The inspection date was not recorded.', extra=extra)
        self.write('fixture inspection', text='A fixture inspection was completed.', source='Another inspection date was not recorded.', extra=extra)
        self.assertEqual(len(self.store.facts()), 2)

    def test_unanchored_events_from_two_rounds_keep_distinct_sources(self):
        sid = self.store.create_session(AT)
        self.store.append_turn(sid, 'user', 'An undated fixture inspection was completed.', AT)
        self.store.append_turn(sid, 'assistant', 'Noted.', AT)
        self.store.append_turn(sid, 'user', 'Another undated inspection was completed.', AT)
        facts = [dict(text='A fixture inspection was completed.', value='fixture inspection', kind='event',
            subject='fixture_unit', attribute='inspection', rounds=[n]) for n in (1, 2)]
        self.model.handlers['extract'] = json.dumps({'facts': facts})
        report = Writer(self.store, self.models, self.settings).consolidate(sid, closed_at=AT)
        self.assertEqual(report.extract_calls, 1)
        self.assertEqual(len(self.store.facts()), 2)
        sources = self.store.sources([f.id for f in self.store.facts()])
        self.assertEqual(len({tuple(x) for x in sources.values()}), 2)

    def test_explicit_value_status_is_preserved_without_reading_prose(self):
        self.write({'activity': 'fixture operation', 'status': 'planned'},
            text='An operation was discussed.', extra={'kind': 'event'})
        fact = self.store.facts()[0]
        self.assertEqual(fact.semantics['event_status'], 'planned')
        self.assertIn('Event status: planned', fact_text(fact))
        self.assertIsNone(fact.event)

    def test_status_word_in_plain_value_or_assistant_example_is_not_a_user_event(self):
        self.write('completed', text='An operation label was discussed.', extra={'kind': 'event'})
        self.write({'status': 'completed'}, text='An assistant example was given.', extra={'kind': 'assistant'})
        self.assertTrue(all('event_status' not in f.semantics for f in self.store.facts()))

    def test_conflicting_explicit_statuses_are_quarantined_without_guessing_state(self):
        _, report = self.write({'status': 'planned'}, extra={'kind': 'event', 'semantics': {'event_status': 'completed'}})
        self.assertEqual(self.store.facts(), [])
        self.assertEqual(report.extract_rejected_count, 1)
        self.assertFalse(report.extract_protocol_complete)

    def test_advice_prioritizes_existing_user_context_without_changing_lookup_selection(self):
        assistant = self.add('A generic fixture workflow was suggested.', 'generic workflow')
        personal = self.add('The user must use the existing fixture rig.', 'existing fixture rig', kind='preference')
        self.settings.max_facts = 1
        retriever = Retriever(self.store, self.models, self.settings)
        def result():
            return SearchResult([Candidate('fact', assistant.id, score=1), Candidate('fact', personal.id, score=.8)],
                {f.key: f for f in (assistant, personal)}, False)
        advice = retriever.select(result(), QueryPlan(['fixture workflow'], [], None, False, 'any', 'synthetic', operation='advice'), budget=4000, today=AT.date())
        lookup = retriever.select(result(), QueryPlan(['fixture workflow'], [], None, False, 'any', 'synthetic', operation='lookup'), budget=4000, today=AT.date())
        self.assertEqual(advice.facts, [personal])
        self.assertEqual(lookup.facts, [assistant])
        self.assertIn('memory_kind=preference', render_memory(advice, AT.date()))


if __name__ == '__main__':
    unittest.main()
