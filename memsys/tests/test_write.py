from __future__ import annotations

import dataclasses
import unittest
from datetime import date

from helpers import decisions_json, dt, fact, facts_json, models, settings

from memsys.llm import ModelError
from memsys.schema import EPISODE_ACTIVE, EPISODE_SKIPPED, FACT_CURRENT, FACT_SUPERSEDED
from memsys.store import Store
from memsys.llm import ChatResult
from memsys.write import Round, WriteReport, Writer, build_rounds, salience, salvage_objects
from memsys.textutil import estimate_tokens


def session(store, when, *turns):
    sid = store.create_session(dt(when))
    for role, text in turns:
        store.append_turn(sid, role, text, dt(when))
    return sid


class RoundsTest(unittest.TestCase):
    def test_build_rounds_groups_user_and_assistant(self):
        rows = [{"role": r, "content": c, "at": "2023-05-01T10:00:00"} for r, c in
                [("assistant", "Welcome back"), ("user", "a"), ("user", "b"), ("assistant", "c"),
                 ("user", "d")]]
        self.assertEqual([(u, a) for _, u, a in build_rounds(rows)],
                         [("", "Welcome back"), ("a\nb", "c"), ("d", "")])

    def test_salience_gate(self):
        self.assertLess(salience("Thanks!", "You're welcome."), 0.1)
        self.assertLess(salience("谢谢", "不客气"), 0.1)
        self.assertGreater(salience("I adopted a cat named Luna yesterday.", "Congrats!"), 0.5)
        self.assertGreater(salience("", "x" * 400), salience("", "ok"))


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)

    def writer(self, memory, **overrides):
        self.models = models(memory=memory)
        return Writer(self.store, self.models, settings(**overrides))

    def test_filtered_chunk_retains_all_rounds_without_splitting_or_accepting_partial_facts(self):
        sid = session(self.store, '2025-01-03T10:00',
                      ('user', 'Today I catalogued meteorite Aldora in cabinet A.'),
                      ('assistant', 'I recorded its cabinet label.'),
                      ('user', 'Today I catalogued meteorite Beralon in cabinet B.'),
                      ('assistant', 'I recorded its cabinet label too.'))
        partial = facts_json(fact('Meteorite Aldora is in cabinet A.', [1]))
        result = ChatResult(partial, 'provider', 0, 0, 0, 0, 0, 'content_filter')
        writer = self.writer({'extract': lambda messages: result})
        report = writer.consolidate(sid, dt('2025-01-03T10:05'))
        self.assertEqual(report.retry_rounds, [1, 2])
        self.assertEqual(report.extract_calls, 1)
        self.assertEqual(self.store.backlog_count(), 2)
        self.assertFalse(self.store.facts())

    def test_consolidate_stores_episodes_facts_vectors_and_closes_the_session(self):
        sid = session(self.store, "2023-03-04T10:00",
                      ("user", "I adopted a cat named Luna yesterday. What should I buy?"),
                      ("assistant", "A litter box and kitten food."),
                      ("user", "Thanks!"), ("assistant", "You're welcome."))
        extraction = facts_json(
            fact("The user adopted a cat named Luna on 2023-03-03.", [1, 9], "pet", "2023-03-03", "event"),
            fact("The user adopted a cat named Luna on 2023-03-03!", [1], "pet"),        # 重复，应合并
            {"text": "", "rounds": [1]},                                               # 空文本，丢弃
            fact("Assistant suggested a litter box and kitten food.", [1], "recommended_items",
                 kind="weird-kind"))
        report = self.writer({"extract": extraction}).consolidate(sid, closed_at=dt("2023-03-04T10:30"))

        self.assertEqual((report.rounds, report.skipped, len(report.added)), (2, 1, 2))
        episodes = self.store.episodes()
        self.assertEqual([e.status for e in episodes], [EPISODE_ACTIVE, EPISODE_SKIPPED])
        facts = self.store.facts()
        self.assertEqual(facts[0].event.start, date(2023, 3, 3))
        self.assertEqual(facts[0].valid_from, date(2023, 3, 3))
        self.assertEqual(facts[1].kind, "personal")              # 未知类别归为 personal
        self.assertEqual(self.store.sources([facts[0].id]), {facts[0].id: [episodes[0].id]})
        self.assertEqual(len(self.store.vectors("fact", (FACT_CURRENT,))[0]), 2)
        self.assertEqual(len(self.store.vectors("episode", (EPISODE_ACTIVE,))[0]), 1)
        self.assertEqual(self.store.open_sessions(), [])
        self.assertEqual(self.models.memory.purposes(), ["extract"])   # 没有候选时不调用更新裁决

    def _first_city(self):
        sid = session(self.store, "2023-01-10T09:00", ("user", "I just moved to Boston for my new job."),
                      ("assistant", "Congrats!"))
        self.writer({"extract": facts_json(fact("The user lives in Boston.", [1], "home_city", "2023-01"))}) \
            .consolidate(sid, closed_at=dt("2023-01-10T09:30"))
        return self.store.facts()[0]

    def test_update_supersedes_the_old_fact(self):
        boston = self._first_city()
        sid = session(self.store, "2023-06-02T18:30", ("user", "I relocated to Seattle last month."),
                      ("assistant", "Welcome to Seattle!"))
        writer = self.writer({"extract": facts_json(fact("The user lives in Seattle.", [1], "home_city", "2023-05")),
                              "resolve": decisions_json(("N1", "update", "F%d" % boston.id))})
        report = writer.consolidate(sid, closed_at=dt("2023-06-02T19:00"))

        self.assertEqual(len(report.updates), 1)
        old, new = self.store.chain(boston.id)
        self.assertEqual((old.status, old.valid_to, new.status), (FACT_SUPERSEDED, date(2023, 5, 1), FACT_CURRENT))
        resolve_prompt = self.models.memory.calls[-1][1][-1]["content"]
        self.assertIn("[F%d] The user lives in Boston." % boston.id, resolve_prompt)

    def test_recalling_an_earlier_state_does_not_overwrite_the_current_one(self):
        boston = self._first_city()
        sid = session(self.store, "2023-06-02T18:30", ("user", "Back in 2019 I lived in Chicago."),
                      ("assistant", "Nice!"))
        report = self.writer({"extract": facts_json(fact("The user lived in Chicago in 2019.", [1], "home_city", "2019")),
                              "resolve": decisions_json(("N1", "update", "F%d" % boston.id))}) \
            .consolidate(sid, closed_at=dt("2023-06-02T19:00"))
        self.assertEqual((report.historical, report.updates), (1, []))
        self.assertEqual({f.status for f in self.store.facts()}, {FACT_CURRENT})

    def test_duplicate_is_not_stored_twice(self):
        boston = self._first_city()
        sid = session(self.store, "2023-02-01T09:00", ("user", "As I said, I live in Boston."), ("assistant", "Right."))
        report = self.writer({"extract": facts_json(fact("The user lives in Boston.", [1], "home_city")),
                              "resolve": decisions_json(("N1", "duplicate", "F%d" % boston.id))}) \
            .consolidate(sid, closed_at=dt("2023-02-01T09:30"))
        self.assertEqual((report.duplicates, len(self.store.facts())), (1, 1))
        self.assertEqual(self.store.fact(boston.id).mentions, 2)

    def test_uncertain_conflicts_wait_for_confirmation_only_in_live_mode(self):
        for confirm, expected_pending in ((True, 1), (False, 0)):
            with self.subTest(confirm=confirm):
                self.setUp()
                boston = self._first_city()
                sid = session(self.store, "2023-06-02T18:30", ("user", "I spend most weeks in Denver now."),
                              ("assistant", "OK"))
                report = self.writer({"extract": facts_json(fact("The user stays in Denver most weeks.", [1], "home_city")),
                                      "resolve": decisions_json(("N1", "uncertain", "F%d" % boston.id))},
                                     confirm_conflicts=confirm).consolidate(sid, closed_at=dt("2023-06-02T19:00"))
                self.assertEqual(len(report.uncertain), 1)
                self.assertEqual(len(self.store.pending()), expected_pending)
                self.assertEqual({f.status for f in self.store.facts()}, {FACT_CURRENT})   # 两条都保留

    def test_targets_outside_the_candidates_are_ignored(self):
        self._first_city()
        sid = session(self.store, "2023-06-02T18:30", ("user", "I moved to Seattle."), ("assistant", "OK"))
        self.writer({"extract": facts_json(fact("The user lives in Seattle.", [1], "home_city")),
                     "resolve": decisions_json(("N1", "update", "F999"))}).consolidate(sid)
        self.assertEqual({f.status for f in self.store.facts()}, {FACT_CURRENT})

    def test_bad_extraction_keeps_the_raw_rounds(self):
        sid = session(self.store, "2023-03-04T10:00", ("user", "I adopted a cat named Luna."), ("assistant", "Nice!"))
        report = self.writer({"extract": "sorry, I cannot do that"}).consolidate(sid, closed_at=dt("2023-03-04T11:00"))
        self.assertEqual((report.extract_failures, report.rounds), (1, 1))
        self.assertEqual(len(self.store.episodes(statuses=(EPISODE_ACTIVE,))), 1)
        self.assertEqual(self.store.open_sessions(), [])

    def test_failure_before_commit_leaves_nothing_behind_and_can_be_retried(self):
        self._first_city()
        sid = session(self.store, "2023-06-02T18:30", ("user", "I relocated to Seattle."), ("assistant", "Welcome!"))
        extraction = facts_json(fact("The user lives in Seattle.", [1], "home_city", "2023-05"))

        def broken(_messages):
            raise ModelError("network down")

        with self.assertRaises(ModelError):
            self.writer({"extract": extraction, "resolve": broken}).consolidate(sid, closed_at=dt("2023-06-02T19:00"))
        self.assertEqual(len(self.store.episodes()), 1)          # 只有第一次会话的那一轮
        self.assertEqual(self.store.open_sessions(), [sid])
        self.writer({"extract": extraction, "resolve": decisions_json(("N1", "update", "F1"))}) \
            .consolidate(sid, closed_at=dt("2023-06-02T19:00"))
        self.assertEqual(len(self.store.episodes()), 2)
        self.assertEqual(self.store.open_sessions(), [])

    def test_truncated_extraction_keeps_complete_rounds_and_queues_incomplete_rounds(self):
        sid = session(self.store, "2023-03-04T10:00",
                      ("user", "I did the Walk for Hunger on February 21st."), ("assistant", "Great!"),
                      ("user", "I also volunteered at the Coastal Cleanup on March 7th."), ("assistant", "Nice!"))
        cut = ChatResult(text='{"facts": [{"text": "x", "rounds": [1]}, {"text": "trunc', model="m", prompt_tokens=0,
                         completion_tokens=0, cached_tokens=0, cost=0.0, latency_s=0.0, finish_reason="length")
        replies = [cut,                                                    # 两轮一起：被截断
                   facts_json(fact("The user did the Walk for Hunger on 2023-02-21.", [1], "event", "2023-02-21")),
                   dataclasses.replace(cut, text='{"facts": [{"text": "The user volunteered at the Coastal Cleanup.", "rounds": [2]}, {"te')]

        def extract(_messages):
            return replies.pop(0)

        report = self.writer({"extract": extract}).consolidate(sid, closed_at=dt("2023-03-04T11:00"))
        self.assertEqual(report.extract_failures, 1)
        self.assertEqual(report.retry_rounds, [2])
        self.assertEqual(self.store.backlog_count(), 1)
        self.assertEqual(sorted(f.text for f in self.store.facts()),
                         ["The user did the Walk for Hunger on 2023-02-21."])
        self.assertEqual(salvage_objects('{"facts": [{"a": 1}, {"b": [2, 3]}, {"c": '), [{"a": 1}, {"b": [2, 3]}])
        self.assertEqual(salvage_objects("nothing here"), [])

    def test_extraction_is_chunked_for_long_sessions(self):
        turns = []
        for i in range(6):
            turns += [("user", "I visited museum number %d and loved it. " % i * 20), ("assistant", "Great " * 50)]
        sid = session(self.store, "2023-03-04T10:00", *turns)
        writer = self.writer({"extract": [facts_json()] * 10}, extract_chunk_tokens=600)
        writer.consolidate(sid, closed_at=dt("2023-03-04T11:00"))
        self.assertGreater(self.models.memory.purposes().count("extract"), 1)

    def test_long_assistant_tail_becomes_a_fact_with_the_original_source(self):
        reply = "Here are the steps for a packing guide.\n" * 150 + "Final item: a solar lantern."
        sid = session(self.store, "2025-08-01T09:00", ("user", "Please write my packing guide."),
                      ("assistant", reply))

        def extract(messages):
            text = messages[-1]["content"]
            return facts_json(fact("The assistant recommended a solar lantern.", [],
                                   "packing_item", kind="assistant")) if "Final item: a solar lantern." in text else facts_json()

        report = self.writer({"extract": extract}, extract_chunk_tokens=240).consolidate(sid)
        self.assertEqual((report.rounds, report.extract_split_rounds, report.extract_truncated_rounds), (1, 1, 0))
        self.assertGreater(report.extract_calls, 1)
        self.assertLessEqual(report.extract_max_input_tokens, 240)
        stored = self.store.facts()[0]
        episode = self.store.episodes()[0]
        self.assertIn("solar lantern", stored.text)
        self.assertEqual(episode.assistant_text, reply)
        self.assertEqual(self.store.sources([stored.id]), {stored.id: [episode.id]})

    def test_parts_preserve_long_user_and_assistant_text_without_exceeding_budget(self):
        writer = self.writer({"extract": facts_json()}, extract_chunk_tokens=96)
        user = "我记录每一次独立旅程，不要合并事件。\n" * 60
        assistant = "1. Blue compass.\n2. Green notebook.\n" * 80
        r = Round(123456, 0, dt("2025-08-01T09:00"), user, assistant)
        parts = writer._parts(r, WriteReport("test"))
        self.assertEqual("".join(p.user_text for p in parts), user)
        self.assertEqual("".join(p.assistant_text for p in parts), assistant)
        self.assertTrue(all(p.number == r.number and estimate_tokens(p.render()) <= 96 for p in parts))
        self.assertEqual([p.part for p in parts], list(range(1, len(parts) + 1)))

    def test_prefix_mode_remains_available_as_an_explicit_ablation(self):
        sid = session(self.store, "2025-08-01T09:00", ("user", "Please write my packing guide."),
                      ("assistant", "Details. " * 350 + "Final item: a solar lantern."))

        def extract(messages):
            self.assertNotIn("Final item: a solar lantern.", messages[-1]["content"])
            return facts_json()

        report = self.writer({"extract": extract}, extract_full_rounds=False).consolidate(sid)
        self.assertEqual(report.extract_truncated_rounds, 1)
        self.assertEqual(report.extract_split_rounds, 0)

    def test_failure_in_a_later_part_can_be_retried_without_partial_memory(self):
        sid = session(self.store, "2025-08-01T09:00", ("user", "Remember this itinerary."),
                      ("assistant", "A detailed itinerary.\n" * 120))
        calls = []

        def broken(messages):
            calls.append(messages)
            if len(calls) == 2:
                raise ModelError("part failed")
            return facts_json(fact("The assistant gave an itinerary.", [1], "itinerary", kind="assistant"))

        with self.assertRaises(ModelError):
            self.writer({"extract": broken}, extract_chunk_tokens=160).consolidate(sid)
        self.assertEqual(self.store.episodes(), [])
        self.assertEqual(self.store.facts(), [])
        self.assertEqual(self.store.session(sid)["turns_done"], 0)
        report = self.writer({"extract": facts_json(fact("The assistant gave an itinerary.", [1],
                                                         "itinerary", kind="assistant"))},
                             extract_chunk_tokens=160).consolidate(sid)
        self.assertEqual(len(report.added), 1)
        self.assertEqual(len(self.store.episodes()), 1)

    def test_too_small_extraction_budget_fails_before_model_or_memory_write(self):
        sid = session(self.store, "2025-08-01T09:00", ("user", "Remember this itinerary."))
        writer = self.writer({"extract": facts_json()}, extract_chunk_tokens=1)
        with self.assertRaisesRegex(ValueError, "too small"):
            writer.consolidate(sid)
        self.assertEqual(self.models.memory.calls, [])
        self.assertEqual(self.store.episodes(), [])


if __name__ == "__main__":
    unittest.main()
