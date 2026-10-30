from __future__ import annotations

import json
import unittest
from datetime import date

from helpers import decisions_json, dt, fact, facts_json, models, settings

from memsys.read import (Candidate, QueryPlan, Retriever, SearchResult, Selection, fit_to_budget,
                         heuristic_plan, parse_answer, plan_query, render_memory)
from memsys.llm import ModelError
from memsys.schema import ARCHIVED
from memsys.store import Store
from memsys.textutil import estimate_tokens
from memsys.timeutil import DateSpan, parse_span
from memsys.write import Writer


def plan(question, **kw):
    base = dict(queries=[question], keywords=[], time_range=None, needs_all=False, focus="any", source="test")
    base.update(kw)
    return QueryPlan(**base)


class PlanTest(unittest.TestCase):
    def test_optional_planner_service_failure_uses_local_plan_with_error(self):
        def unavailable(messages):
            raise ModelError('HTTP 400 invalid_prompt', status=400)
        m = models(memory={'query': unavailable})
        result = plan_query(m, 'How many meteorite samples are on my study shelf?', dt('2025-04-01T10:00'), settings())
        self.assertEqual(result.source, 'service_fallback')
        self.assertTrue(result.needs_all)
        self.assertIn('invalid_prompt', result.error)
        self.assertEqual(len(m.memory.calls), 1)

    def test_optional_planner_does_not_hide_account_failure(self):
        def unavailable(messages):
            raise ModelError('authentication failed', status=401)
        with self.assertRaises(ModelError):
            plan_query(models(memory={'query': unavailable}), 'Where do I live?',
                       dt('2025-04-01T10:00'), settings())

    def test_llm_plan_is_validated(self):
        reply = json.dumps({"queries": ["cat adoption", "Luna"], "keywords": ["Luna", ""],
                            "time_range": {"start": "2023-03-31", "end": "2023-03-01"},
                            "needs_all": 1, "focus": "weird"})
        m = models(memory={"query": reply})
        result = plan_query(m, "When did I adopt Luna?", dt("2023-04-01T10:00"), settings())
        self.assertEqual(result.queries, ["When did I adopt Luna?", "cat adoption", "Luna"])
        self.assertEqual(result.keywords, ["Luna"])
        self.assertEqual(result.time_range, DateSpan(date(2023, 3, 1), date(2023, 3, 31), "day"))
        self.assertEqual((result.needs_all, result.focus, result.source), (True, "any", "llm"))
        self.assertIn("2023/04/01 (Saturday)", m.memory.calls[0][1][-1]["content"])

    def test_bad_plan_falls_back_to_heuristics(self):
        result = plan_query(models(memory={"query": "no idea"}), "How many bikes do I have now?",
                            dt("2023-04-01T10:00"), settings())
        self.assertEqual((result.source, result.needs_all, result.focus), ("fallback", True, "current"))

    def test_query_analysis_can_be_switched_off(self):
        m = models()
        result = plan_query(m, "What was my previous job?", dt("2023-04-01T10:00"), settings(query_analysis=False))
        self.assertEqual((result.source, result.focus), ("heuristic", "past"))
        self.assertEqual(m.memory.calls, [])
        self.assertTrue(heuristic_plan("我一共买了几件乐器？").needs_all)


class RetrievalTest(unittest.TestCase):
    """用脚本化的抽取结果建一个小记忆库，再检查检索、加权和预算。"""

    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.models = models(memory={"extract": [], "resolve": []})
        self.settings = settings()

    def add_session(self, when, user_text, assistant_text, *facts, resolve=None):
        sid = self.store.create_session(dt(when))
        self.store.append_turn(sid, "user", user_text, dt(when))
        self.store.append_turn(sid, "assistant", assistant_text, dt(when))
        self.models.memory.handlers["extract"].append(facts_json(*facts))
        if resolve:
            self.models.memory.handlers["resolve"].append(resolve)
        Writer(self.store, self.models, self.settings).consolidate(sid, closed_at=dt(when))

    def retriever(self, **overrides):
        return Retriever(self.store, self.models, settings(**overrides))

    def test_optional_reranker_service_failure_preserves_candidates_and_error(self):
        self.add_session('2025-04-01T10:00', 'I have a blue bicycle.', '')
        def unavailable(messages):
            raise ModelError('temporary service failure', status=503)
        self.models.memory.handlers['rerank'] = unavailable
        retriever = self.retriever(llm_rerank=True)
        result = retriever.search(plan('blue bicycle', keywords=['bicycle']))
        keys = [candidate.key for candidate in result.ranked]
        revised = retriever.rerank(result, 'What color is my bicycle?', dt('2025-04-02T10:00'))
        self.assertEqual([candidate.key for candidate in revised.ranked], keys)
        self.assertFalse(revised.reranked)
        self.assertIn('service failure', revised.rerank_error)

    def test_optional_reranker_does_not_hide_account_failure(self):
        self.add_session('2025-04-01T10:00', 'I have a blue bicycle.', '')
        def unavailable(messages):
            raise ModelError('permission denied', status=403)
        self.models.memory.handlers['rerank'] = unavailable
        retriever = self.retriever(llm_rerank=True)
        result = retriever.search(plan('blue bicycle', keywords=['bicycle']))
        with self.assertRaises(ModelError):
            retriever.rerank(result, 'What color is my bicycle?', dt('2025-04-02T10:00'))

    def test_long_assistant_excerpt_keeps_matching_tail_and_speaker_within_budget(self):
        reply = "General maintenance notes.\n" * 200 + "The cobalt recorder serial is X4-Z9."
        self.add_session("2025-08-01T09:00", "Please keep my equipment notes.", reply)
        query = plan("cobalt recorder serial", keywords=["cobalt", "recorder", "serial"])
        retriever = self.retriever(episode_max_tokens=200)
        result = retriever.search(query)
        selection = retriever.select(result, query, budget=300, today=date(2025, 8, 2))
        memory = fit_to_budget(selection, date(2025, 8, 2), 300)
        self.assertIn("The cobalt recorder serial is X4-Z9.", memory)
        self.assertIn("Assistant:", memory)
        self.assertIn("other text omitted", memory)
        self.assertLessEqual(estimate_tokens(memory), 300)
        prefix = self.retriever(episode_max_tokens=200, query_focused_excerpts=False).select(
            result, query, budget=300, today=date(2025, 8, 2))
        self.assertNotIn("X4-Z9", render_memory(prefix, date(2025, 8, 2)))

    def test_ordinal_excerpt_preserves_requested_literal_entry_under_tight_budget(self):
        from memsys.budget import TokenCounter
        equipment = '\n'.join('%d. Instrument station %d: generic calibration notes.' % (n, n)
                              if n != 9 else '9. Cobalt interferometer: use the violet filter.' for n in range(1, 16))
        self.add_session('2025-08-01T09:00', 'Keep this ordered equipment checklist.', equipment)
        for question in ('What was the 9th instrument in the equipment list?',
                         'What was the ninth instrument in the equipment list?',
                         '设备清单第9项是什么？'):
            retriever = self.retriever(budget_mode='utf8', episode_max_tokens=260)
            query = plan(question, keywords=['equipment', 'instrument', 'list'])
            selection = retriever.select(retriever.search(query), query, budget=360, today=date(2025, 8, 2))
            memory = fit_to_budget(selection, date(2025, 8, 2), 360, counter=TokenCounter('utf8'))
            self.assertIn('9. Cobalt interferometer: use the violet filter.', memory)
            self.assertIn('Assistant:', memory)
            self.assertIn('other text omitted', memory)
            self.assertLessEqual(TokenCounter('utf8').count(memory), 360)

    def test_ordinal_excerpt_does_not_invent_numbering_for_bullets(self):
        from memsys.budget import TokenCounter
        equipment = '- Amber detector\n- Violet filter\n' + 'Generic equipment notes.\n' * 35
        self.add_session('2025-08-01T09:00', 'Keep these equipment notes.', equipment)
        query = plan('What was the ninth item in the equipment list?', keywords=['equipment'])
        retriever = self.retriever(budget_mode='utf8', episode_max_tokens=260)
        selection = retriever.select(retriever.search(query), query, budget=360, today=date(2025, 8, 2))
        memory = fit_to_budget(selection, date(2025, 8, 2), 360, counter=TokenCounter('utf8'))
        self.assertNotIn('9.', memory)
        self.assertNotIn('ninth', memory)
        self.assertLessEqual(TokenCounter('utf8').count(memory), 360)

    def test_long_user_excerpt_keeps_direct_tail_evidence_as_user_text(self):
        user = "I am recording generic notes for later.\n" * 140 + "I bought a violet compass at Pinebridge Outlet."
        self.add_session("2025-08-01T09:00", user, "Noted.")
        query = plan("violet compass shop", keywords=["violet", "compass"])
        retriever = self.retriever(episode_max_tokens=180)
        selection = retriever.select(retriever.search(query), query, budget=280, today=date(2025, 8, 2))
        memory = fit_to_budget(selection, date(2025, 8, 2), 280)
        self.assertIn("I bought a violet compass at Pinebridge Outlet.", memory)
        self.assertIn("User:", memory)
        self.assertLessEqual(estimate_tokens(memory), 280)

    def test_reranker_can_see_matching_evidence_after_a_long_prefix(self):
        reply = "Generic reference material.\n" * 160 + "The amber sensor has a 48-hour battery."
        self.add_session("2025-08-01T09:00", "Please keep these device notes.", reply)

        def rerank(messages):
            self.assertIn("The amber sensor has a 48-hour battery.", messages[-1]["content"])
            return json.dumps({"relevant": ["E1"]})

        self.models.memory.handlers["rerank"] = rerank
        query = plan("amber sensor battery", keywords=["amber", "sensor", "battery"])
        retriever = self.retriever(llm_rerank=True)
        result = retriever.rerank(retriever.search(query), query.queries[0], dt("2025-08-02T09:00"), plan=query)
        self.assertTrue(result.reranked)
        self.assertEqual(result.ranked[0].key, "E1")

    def test_fact_hits_pull_in_their_source_rounds(self):
        self.add_session("2023-03-04T10:00", "I adopted a cat named Luna yesterday.", "Congratulations!",
                         fact("The user adopted a cat named Luna.", [1], "pet", "2023-03-03"))
        result = self.retriever().search(plan("Luna the cat adoption", keywords=["Luna"]))
        keys = [c.key for c in result.ranked]
        self.assertEqual(set(keys), {"F1", "E1"})
        self.assertIn("expand", next(c for c in result.ranked if c.key == "E1").via)

    def test_time_range_boosts_matching_memories(self):
        self.add_session("2023-03-10T10:00", "I went hiking at Mount Rainier.", "Fun!",
                         fact("The user went hiking at Mount Rainier.", [1], "hike", "2023-03-09"))
        self.add_session("2023-05-10T10:00", "I went hiking at Mount Si.", "Fun!",
                         fact("The user went hiking at Mount Si.", [1], "hike", "2023-05-09"),
                         resolve=decisions_json(("N1", "add", "F1")))
        may = plan("hiking trip", keywords=["hiking"], time_range=parse_span("2023-05"))
        facts = [c.key for c in self.retriever().search(may).ranked if c.kind == "fact"]
        self.assertEqual(facts[0], "F2")
        march = plan("hiking trip", keywords=["hiking"], time_range=parse_span("2023-03"))
        facts = [c.key for c in self.retriever().search(march).ranked if c.kind == "fact"]
        self.assertEqual(facts[0], "F1")

    def test_outdated_facts_are_down_weighted_for_current_questions_and_chained_in(self):
        self.add_session("2023-01-10T09:00", "I live in Boston.", "Nice.",
                         fact("The user lives in Boston city.", [1], "home_city", "2023-01"))
        self.add_session("2023-06-02T18:30", "I moved to Seattle.", "Welcome!",
                         fact("The user lives in Seattle city.", [1], "home_city", "2023-05"),
                         resolve=decisions_json(("N1", "update", "F1")))
        retriever = self.retriever()
        now = plan("which city does the user live in", keywords=["city"], focus="current")
        scores = {c.key: c.score for c in retriever.search(now).ranked}
        self.assertGreater(scores["F2"], scores["F1"])

        # 只选中旧事实时，取代它的新事实也会被补进来
        result = retriever.search(plan("Boston", keywords=["Boston"]))
        result.ranked = [c for c in result.ranked if c.key == "F1"]
        selection = retriever.select(result, plan("Boston"))
        self.assertEqual([f.key for f in selection.facts], ["F1", "F2"])
        text = render_memory(selection, date(2023, 7, 1))
        self.assertIn("replaced by F2", text)

    def test_unrelated_questions_find_nothing(self):
        self.add_session("2023-03-04T10:00", "I adopted a cat named Luna.", "Congrats!",
                         fact("The user adopted a cat named Luna.", [1], "pet"))
        result = self.retriever(min_similarity=0.9).search(plan("quantum chromodynamics lecture"))
        self.assertEqual(result.ranked, [])
        selection = self.retriever().select(result, plan("x"))
        self.assertIn("no relevant memories", render_memory(selection, date(2023, 7, 1)))

    def test_llm_rerank_moves_picked_entries_first_and_survives_bad_output(self):
        self.add_session("2023-03-10T10:00", "I bought a Gucci handbag for $1,200.", "Nice!",
                         fact("The user bought a Gucci handbag for $1,200.", [1], "bought_item"))
        self.add_session("2023-03-12T10:00", "Can you explain budgeting frameworks?", "Sure, the 50/30/20 rule ...",
                         fact("The assistant explained the 50/30/20 budgeting rule.", [1], "budget_rule", kind="assistant"),
                         resolve=decisions_json(("N1", "new", None)))
        retriever = self.retriever(llm_rerank=True)
        query = plan("luxury spending budget", keywords=["budget", "luxury", "handbag"])
        self.models.memory.handlers["rerank"] = ['{"relevant": ["F1", "X9", "F1"]}', "not json"]
        result = retriever.rerank(retriever.search(query), "How much did I spend on luxury items?", dt("2023-04-01T10:00"))
        self.assertTrue(result.reranked)
        self.assertEqual(result.ranked[0].key, "F1")
        self.assertIn("rerank#1", result.ranked[0].via)
        prompt = self.models.memory.calls[-1][1][-1]["content"]
        self.assertIn("[F1] 2023-03-10 · The user bought a Gucci handbag for $1,200.", prompt)
        before = [c.key for c in retriever.search(query).ranked]
        after = retriever.rerank(retriever.search(query), "q", dt("2023-04-01T10:00"))
        self.assertFalse(after.reranked)
        self.assertEqual([c.key for c in after.ranked], before)

    def test_archived_memories_are_a_fallback(self):
        self.add_session("2023-03-04T10:00", "I adopted a cat named Luna.", "Congrats!",
                         fact("The user adopted a cat named Luna.", [1], "pet"))
        with self.store.transaction():
            self.store.set_status("fact", [1], ARCHIVED)
            self.store.set_status("episode", [1], ARCHIVED)
        result = self.retriever().search(plan("Luna", keywords=["Luna"]))
        self.assertTrue(result.used_archive)
        self.assertEqual({c.key for c in result.ranked}, {"F1", "E1"})

    def test_selection_respects_the_token_budget(self):
        for day in range(1, 6):
            self.add_session("2023-03-%02dT10:00" % day, "Day %d: I practiced piano for an hour. " % day * 10,
                             "Keep going! " * 80)
        retriever = self.retriever(context_budget_tokens=600, episode_max_tokens=250, fact_budget_share=0.0)
        result = retriever.search(plan("piano practice", keywords=["piano"]))
        selection = retriever.select(result, plan("piano practice"))
        self.assertLessEqual(selection.context_tokens, 600)
        self.assertGreaterEqual(len(selection.episodes), 1)
        self.assertTrue(all("[truncated]" in text or "other text omitted" in text
                            for _, text in selection.episodes))
        self.assertTrue(all(estimate_tokens(text) <= 250 for _, text in selection.episodes))

    def test_render_memory_shows_dates_and_exact_offsets(self):
        self.add_session("2023-03-04T10:00", "I adopted a cat named Luna yesterday.", "Congrats!",
                         fact("The user adopted a cat named Luna.", [1], "pet", "2023-03-03"))
        retriever = self.retriever()
        result = retriever.search(plan("Luna", keywords=["Luna"]))
        text = render_memory(retriever.select(result, plan("Luna")), date(2023, 3, 20))
        self.assertIn("[F1] happened 2023-03-03 (17 days ago) · noted 2023-03-04 (16 days ago)", text)
        self.assertIn("[E1] conversation on 2023-03-04 (16 days ago)", text)

    def version_chain(self, count=20):
        when = dt("2024-01-01T10:00")
        sid = self.store.create_session(when)
        ids = []
        with self.store.transaction():
            for i in range(count):
                fid = self.store.add_fact(text="User lives at residence version %d. " % i + "Quiet apartment. " * 8,
                    kind="personal", subject="user", attribute="home_city", value=str(i), event=None,
                    recorded_at=when, valid_from=when.date(), session_id=sid, importance=.8, episode_ids=[])
                if ids:
                    self.store.supersede(ids[-1], fid, when.date())
                ids.append(fid)
        first = self.store.fact(ids[0])
        return ids, SearchResult([Candidate("fact", first.id, 1)], {first.key: first}, False)

    def test_current_question_keeps_latest_version_within_budget(self):
        ids, result = self.version_chain()
        retriever = self.retriever(context_budget_tokens=600)
        selection = retriever.select(result, plan("current residence", focus="current"), today=date(2024, 2, 1))
        memory = fit_to_budget(selection, date(2024, 2, 1), 600)
        self.assertEqual([f.id for f in selection.facts], [ids[-1]])
        self.assertEqual(selection.omitted_facts, ["F1"])
        self.assertIn("version 19", memory)
        self.assertLessEqual(estimate_tokens(memory), 600)

    def test_past_question_keeps_matched_history_without_unbounded_chain(self):
        ids, result = self.version_chain()
        selection = self.retriever(max_facts=2).select(result, plan("former residence", focus="past"))
        memory = fit_to_budget(selection, date(2024, 2, 1), 600)
        self.assertEqual([f.id for f in selection.facts], [ids[0], ids[-1]])
        self.assertIn("replaced by F2", memory)
        self.assertLessEqual(estimate_tokens(memory), 600)

    def test_forgotten_latest_version_does_not_restore_old_current_state(self):
        ids, result = self.version_chain()
        self.store.forget("fact", ids[-1])
        selection = self.retriever().select(result, plan("current residence", focus="current"))
        self.assertEqual(selection.facts, [])
        self.assertNotIn("version 19", fit_to_budget(selection, date(2024, 2, 1), 600))

    def test_render_enforces_budget_for_facts_and_extremely_small_limits(self):
        ids, _ = self.version_chain()
        for limit in (0, 1, 10, 80, 600):
            selection = Selection(self.store.facts(ids), [], 0)
            memory = fit_to_budget(selection, date(2035, 2, 1), limit)
            self.assertLessEqual(estimate_tokens(memory), limit)
            self.assertEqual(selection.context_tokens, estimate_tokens(memory))
            self.assertTrue(selection.omitted_facts)

    def test_aggregate_query_preserves_distinct_facts_with_metadata_in_budget(self):
        when = dt("2024-01-01T10:00")
        sid = self.store.create_session(when)
        facts = []
        with self.store.transaction():
            for i in range(40):
                fid = self.store.add_fact(text="User purchased instrument %d." % i, kind="event", subject="user",
                    attribute="purchase", value=str(i), event=None, recorded_at=when, valid_from=when.date(),
                    session_id=sid, importance=.8, episode_ids=[])
                facts.append(self.store.fact(fid))
        result = SearchResult([Candidate("fact", f.id, 1) for f in facts], {f.key: f for f in facts}, False)
        selection = self.retriever().select(result, plan("all instruments", needs_all=True), budget=600,
                                            today=date(2024, 2, 1))
        memory = fit_to_budget(selection, date(2024, 2, 1), 600)
        self.assertGreater(len(selection.facts), 1)
        self.assertEqual(len(selection.facts), len({f.id for f in selection.facts}))
        self.assertLessEqual(estimate_tokens(memory), 600)


class AnswerParsingTest(unittest.TestCase):
    def test_parse_answer(self):
        self.assertEqual(parse_answer("NOTES: F1 says two weeks.\nANSWER: Two weeks."),
                         ("F1 says two weeks.", "Two weeks."))
        self.assertEqual(parse_answer("NOTES：无\nANSWER：不知道"), ("无", "不知道"))
        self.assertEqual(parse_answer("Just an answer"), ("", "Just an answer"))
        self.assertEqual(parse_answer("NOTES: thinking about ANSWER: formats\nANSWER: 3"),
                         ("thinking about ANSWER: formats", "3"))


if __name__ == "__main__":
    unittest.main()
