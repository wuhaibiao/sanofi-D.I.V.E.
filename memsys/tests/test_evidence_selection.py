"""Budgeted retrieval must retain complementary, authoritative source evidence."""
from datetime import date
import unittest

from helpers import dt, models, settings
from memsys.budget import budget_counter
from memsys.read import Candidate, QueryPlan, Retriever, SearchResult, fit_to_budget
from memsys.schema import ARCHIVED, FORGOTTEN
from memsys.store import Store


class EvidenceSelectionTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)

    def episode(self, text, at='2025-04-01T10:00', assistant=''):
        stamp = dt(at)
        sid = self.store.create_session(stamp)
        return self.store.add_episode(sid, 0, stamp, text, assistant, 0.8, 'active')

    def fact(self, text, eid, at='2025-04-01T10:00', value=''):
        episode = self.store.episodes([eid])[0]
        stamp = dt(at)
        return self.store.add_fact(text=text, kind='personal', subject='user', attribute='equipment',
            value=value or text, event=None, recorded_at=stamp, valid_from=stamp.date(),
            session_id=episode.session_id, importance=0.8, confidence=0.9, episode_ids=[eid])

    def result(self, keys):
        items = {}
        for key in keys:
            items[key] = (self.store.fact(int(key[1:])) if key.startswith('F')
                          else self.store.episodes([int(key[1:])])[0])
        return SearchResult([Candidate('fact' if key.startswith('F') else 'episode', int(key[1:]),
                             score=1.0/(index+1)) for index, key in enumerate(keys)], items, False)

    def select(self, result, queries, budget=1100, focus='any', operation='lookup', **overrides):
        options = dict(budget_mode='utf8', selection_policy='coverage', episode_max_tokens=850,
                       max_episodes=4)
        options.update(overrides)
        s = settings(**options)
        plan = QueryPlan(queries, [], None, False, focus, 'test', operation=operation)
        selected = Retriever(self.store, models(), s).select(result, plan, budget, date(2025, 6, 1))
        memory = fit_to_budget(selected, date(2025, 6, 1), budget, counter=budget_counter(s))
        self.assertLessEqual(budget_counter(s).count(memory), budget)
        return selected, memory

    def test_compare_keeps_both_objects_instead_of_spending_budget_on_first_long_source(self):
        first = self.episode('The amber scanner costs $18.\n' + 'Scanner background notes.\n'*55)
        duplicate = self.episode('Additional amber scanner setup guidance.\n'*30)
        second = self.episode('The violet camera costs $27.')
        result = self.result(['E%d'%eid for eid in [first, duplicate, second]])
        selected, memory = self.select(result,
            ['Compare the amber scanner and violet camera prices.', 'amber scanner price', 'violet camera price'],
            budget=800, operation='difference')
        self.assertIn('$18', memory)
        self.assertIn('$27', memory)
        self.assertIn('E%d'%second, [e.key for e,_ in selected.episodes])
        self.assertIn('selection_policy', selected.selection_trace)

    def test_latest_version_pulls_its_actual_source_even_when_only_old_source_was_ranked(self):
        old_source = self.episode('The panel setting is amber.\n' + 'Panel notes.\n'*50)
        new_source = self.episode('Background.\n'*80 + 'Correction: the panel setting is violet.', '2025-05-01T10:00')
        old = self.fact('Panel setting is amber.', old_source, value='amber')
        new = self.fact('Panel setting is violet.', new_source, '2025-05-01T10:00', value='violet')
        self.store.supersede(old, new, date(2025,5,1))
        selected, memory = self.select(self.result(['F%d'%old,'E%d'%old_source]),
            ['What is the current panel setting?'], budget=700, focus='current')
        self.assertEqual([f.id for f in selected.facts], [new])
        self.assertIn('Correction: the panel setting is violet.', memory)
        self.assertIn(new_source, [e.id for e,_ in selected.episodes])

    def test_clipped_away_query_terms_do_not_suppress_complementary_source(self):
        first = self.episode('Amber scanner notes.\n'*18 + 'The violet camera costs $27.')
        duplicate = self.episode('More amber scanner notes.\n'*20)
        second = self.episode('The violet camera costs $27.')
        selected, memory = self.select(self.result(['E%d'%eid for eid in (first, duplicate, second)]),
            ['amber scanner', 'violet camera'], budget=470, operation='difference',
            query_focused_excerpts=False, max_episodes=2)
        self.assertEqual([episode.id for episode,_ in selected.episodes], [first,second])
        self.assertIn('$27', memory)
        decision = selected.selection_trace['decisions'][0]
        self.assertEqual(decision['retrieved_query_matches'], [0,1])
        self.assertEqual(decision['query_matches'], [0])

    def test_historical_comparison_keeps_original_and_updated_sources(self):
        early = self.episode('My study lamp was cobalt.')
        later = self.episode('I replaced my study lamp with an amber lamp.', '2025-05-01T10:00')
        old = self.fact('Study lamp is cobalt.', early, value='cobalt')
        new = self.fact('Study lamp is amber.', later, '2025-05-01T10:00', value='amber')
        self.store.supersede(old,new,date(2025,5,1))
        selected, memory = self.select(self.result(['F%d'%old]),
            ['Compare my previous and current study lamps.'], budget=1400, focus='past', operation='compare_states')
        self.assertEqual({f.id for f in selected.facts}, {old,new})
        self.assertEqual({e.id for e,_ in selected.episodes}, {early,later})
        self.assertIn('was cobalt',memory)
        self.assertIn('replaced my study lamp',memory)

    def test_forgotten_latest_does_not_resurrect_old_fact_or_source(self):
        early = self.episode('The device password is amber.')
        later = self.episode('The device password is violet.', '2025-05-01T10:00')
        old = self.fact('Password is amber.',early,value='amber')
        new = self.fact('Password is violet.',later,'2025-05-01T10:00',value='violet')
        self.store.supersede(old,new,date(2025,5,1))
        self.store.forget('fact',new)
        selected, memory = self.select(self.result(['F%d'%old]),
            ['What is the device password now?'], focus='current')
        self.assertEqual(selected.facts,[])
        self.assertEqual(selected.episodes,[])
        self.assertNotIn('violet',memory)
        self.assertNotIn('amber',memory)

    def test_stale_ranked_original_is_refreshed_through_safe_view(self):
        eid = self.episode('Private code is AMBER-19. The device is cobalt.')
        fid = self.fact('Private code is AMBER-19.',eid,value='AMBER-19')
        self.store.record_span(fid,eid,'user','Private code is AMBER-19.')
        stale = self.result(['E%d'%eid])
        self.store.forget('fact',fid)
        _, memory = self.select(stale,['What color is the device?'])
        self.assertNotIn('AMBER-19',memory)

    def test_same_words_on_different_dates_remain_distinct_events(self):
        one = self.episode('I attended the pottery workshop.')
        two = self.episode('I attended the pottery workshop.', '2025-05-01T10:00')
        selected, memory = self.select(self.result(['E%d'%one,'E%d'%two]),
            ['List my pottery workshop visits.'], operation='list_events')
        self.assertEqual({e.id for e,_ in selected.episodes},{one,two})
        self.assertIn('2025-04-01',memory)
        self.assertIn('2025-05-01',memory)

    def test_missing_and_withheld_fact_sources_are_reported_not_fabricated(self):
        eid=self.episode('I use a violet telescope.')
        fid=self.fact('Telescope is violet.',eid,value='violet')
        result=self.result(['F%d'%fid])
        self.store.set_status('episode',[eid],'withheld')
        selected,_=self.select(result,['What telescope do I use?'])
        self.assertEqual(selected.episodes,[])
        self.assertEqual(selected.selection_trace['unsupported_facts'],['F%d'%fid])

    def test_archived_sources_can_support_selected_archived_fact(self):
        eid=self.episode('I used a cobalt tripod.')
        fid=self.fact('Tripod is cobalt.',eid,value='cobalt')
        self.store.set_status('episode',[eid],ARCHIVED)
        self.store.set_status('fact',[fid],ARCHIVED)
        selected,memory=self.select(self.result(['F%d'%fid]),['What tripod did I use?'],focus='past')
        self.assertIn('cobalt tripod',memory)
        self.assertEqual([e.id for e,_ in selected.episodes],[eid])

    def test_tiny_budget_never_claims_semantic_completeness(self):
        eid=self.episode('The violet instrument requires a lithium battery.')
        selected,_=self.select(self.result(['E%d'%eid]),['Which battery?'],budget=25)
        self.assertFalse(selected.selection_trace['semantic_completeness_proven'])
        self.assertNotEqual(selected.selection_trace.get('context_complete'),True)

    def test_pruning_recomputes_source_support_and_query_coverage(self):
        eid=self.episode('I own a violet telescope.')
        fid=self.fact('Telescope is violet.',eid,value='violet')
        selected,_=self.select(self.result(['F%d'%fid]),['violet telescope'],budget=700)
        self.assertEqual(selected.selection_trace['unsupported_facts'],[])
        selected.episodes=[]
        fit_to_budget(selected,date(2025,6,1),700,counter=budget_counter(settings(budget_mode='utf8')))
        self.assertEqual(selected.selection_trace['unsupported_facts'],['F%d'%fid])
        self.assertEqual(selected.selection_trace['covered_queries'],[])

    def test_selection_policy_is_read_only_and_invalid_values_are_rejected(self):
        from memsys.config import MemorySettings
        ranked=MemorySettings.from_dict({'selection_policy':'ranked'})
        coverage=MemorySettings.from_dict({'selection_policy':'coverage'})
        self.assertEqual(ranked.write_fingerprint(),coverage.write_fingerprint())
        self.assertEqual(ranked.index_fingerprint('hash-256'),coverage.index_fingerprint('hash-256'))
        with self.assertRaisesRegex(ValueError,'selection_policy'):
            MemorySettings.from_dict({'selection_policy':'unbounded'})


if __name__=='__main__':
    unittest.main()
