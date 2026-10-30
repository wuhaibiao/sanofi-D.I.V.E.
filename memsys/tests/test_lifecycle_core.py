from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date

from helpers import decisions_json, dt, fact, facts_json, models, settings

from memsys.core import MemorySystem
from memsys.lifecycle import retention, run_lifecycle
from memsys.schema import EPISODE_ACTIVE, FACT_CURRENT
from memsys.store import EmbeddingMismatch, Store


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)

    def add_fact(self, text, recorded, importance=0.4):
        with self.store.transaction():
            return self.store.add_fact(text=text, kind="personal", subject="user", attribute="", value=text,
                                       event=None, recorded_at=dt(recorded), valid_from=None, session_id="s1",
                                       importance=importance)

    def add_episode(self, idx, when, salience):
        with self.store.transaction():
            return self.store.add_episode("s1", idx, dt(when), "small talk %d" % idx, "ok", salience, EPISODE_ACTIVE)

    def test_long_superseded_facts_are_archived(self):
        old, new = self.add_fact("lives in Boston", "2022-01-01T00:00"), self.add_fact("lives in Seattle", "2023-01-01T00:00")
        with self.store.transaction():
            self.store.supersede(old, new, date(2023, 1, 1))
        self.assertEqual(run_lifecycle(self.store, dt("2023-06-01T00:00"), settings()).archived_facts, [])
        report = run_lifecycle(self.store, dt("2024-02-01T00:00"), settings(archive_after_days=10_000))
        self.assertEqual(report.archived_facts, [old])

    def test_idle_low_value_rounds_are_archived_but_rounds_behind_current_facts_stay(self):
        linked = self.add_episode(0, "2023-01-01T00:00", 0.2)
        idle = self.add_episode(1, "2023-01-01T00:00", 0.2)
        fid = self.add_fact("important fact", "2023-01-01T00:00", importance=1.0)
        with self.store.transaction():
            self.store.add_sources(fid, [linked])
        report = run_lifecycle(self.store, dt("2024-01-01T00:00"), settings())
        self.assertEqual(report.archived_episodes, [idle])
        self.assertEqual(self.store.episodes([linked])[0].status, EPISODE_ACTIVE)
        self.assertEqual(self.store.fact(fid).status, FACT_CURRENT)
        self.assertIn("归档", report.summary())

    def test_being_used_keeps_memories_alive(self):
        a = self.add_fact("used often", "2023-01-01T00:00")
        b = self.add_fact("never used", "2023-01-01T00:00")
        for _ in range(5):
            self.store.touch("fact", [a], dt("2023-12-01T00:00"))
        now = dt("2024-01-01T00:00")
        self.assertGreater(retention(self.store.fact(a), now, settings()), retention(self.store.fact(b), now, settings()))
        report = run_lifecycle(self.store, now, settings(max_active_facts=1, archive_after_days=10_000))
        self.assertEqual(report.archived_facts, [b])


class MemorySystemTest(unittest.TestCase):
    def system(self, memory=None, chat=None, **overrides):
        system = MemorySystem(models(memory=memory, chat=chat), settings(**overrides))
        self.addCleanup(system.close)
        return system

    def test_chat_mode_logs_turns_writes_memory_and_reuses_it_later(self):
        extraction = facts_json(fact("The user adopted a cat named Luna on 2023-03-03.", [1], "pet", "2023-03-03"))
        answers = ["NOTES: nothing yet\nANSWER: Congratulations on Luna!",
                   'NOTES: F1 says 2023-03-03.\nCALCULATION: ' + json.dumps(dict(
                       operation='date_difference', unit='days',
                       start=dict(source='F1', quote='The user adopted a cat named Luna on 2023-03-03.',
                                  date_text='2023-03-03', date='2023-03-03'),
                       end=dict(source='TODAY', date='2023-03-20'), result='17')) + '\nANSWER: 17 days ago.']
        query = json.dumps({"queries": ["cat adoption"], "keywords": ["Luna", "cat"], "time_range": None,
                            "needs_all": False, "focus": "any"})
        system = self.system(memory={"extract": [extraction], "query": query}, chat={"answer": answers})

        s1 = system.open_session("alice", dt("2023-03-04T10:00"))
        reply = system.respond("alice", "I adopted a cat named Luna yesterday!", dt("2023-03-04T10:00"), session=s1)
        self.assertEqual(reply.text, "Congratulations on Luna!")
        report, _ = system.close_session(s1, dt("2023-03-04T10:05"))
        self.assertEqual(len(report.added), 1)

        s2 = system.open_session("alice", dt("2023-03-20T08:00"))
        reply = system.respond("alice", "How many days ago did I adopt Luna?", dt("2023-03-20T08:00"), session=s2)
        self.assertEqual(reply.text, "17 days ago.")
        trace = reply.trace
        self.assertEqual(trace["selected"]["facts"], ["F1"])
        self.assertIn("happened 2023-03-03 (17 days ago)", trace["memory"])
        self.assertEqual(trace["plan"]["source"], "llm")
        self.assertEqual(trace['aggregation']['status'], 'checks_passed')
        answer_prompt = system.models.chat.calls[-1][1][-1]["content"]
        self.assertIn("## Current conversation", answer_prompt)
        self.assertIn("User: How many days ago did I adopt Luna?", answer_prompt)
        store = system.store("alice")
        self.assertEqual([r["role"] for r in store.turns(s2.session_id)], ["user", "assistant"])
        self.assertEqual(store.fact(1).access_count, 1)

    def test_qa_mode_does_not_record_turns(self):
        system = self.system(memory={"query": "{}"}, chat={"answer": "ANSWER: I don't have that information."})
        reply = system.respond("bob", "Where does my brother live?", dt("2023-05-01T12:00"))
        self.assertEqual(reply.text, "I don't have that information.")
        self.assertEqual(system.store("bob").history_tokens(), 0)
        self.assertEqual(reply.trace["mode"], "qa")

    def test_recover_consolidates_sessions_left_open(self):
        system = self.system(memory={"extract": facts_json(fact("The user likes jazz.", [1], "music"))})
        s1 = system.open_session("carol", dt("2023-05-01T10:00"))
        system.add_turn(s1, "user", "I really like jazz.", dt("2023-05-01T10:00"))
        system.add_turn(s1, "assistant", "Nice taste!", dt("2023-05-01T10:01"))
        reports = system.recover("carol")
        self.assertEqual((len(reports), len(reports[0].added)), (1, 1))
        self.assertEqual(system.store("carol").open_sessions(), [])
        with self.assertRaises(ValueError):
            system.add_turn(s1, "system", "x", dt("2023-05-01T10:02"))

    def test_long_chat_sessions_are_consolidated_in_flight(self):
        system = self.system(memory={"query": "{}", "extract": [facts_json()] * 5},
                             chat={"answer": lambda _: "ANSWER: ok"}, flush_rounds=2)
        s1 = system.open_session("dave", dt("2023-05-01T10:00"))
        flushed = [system.respond("dave", "I ran %d km today." % i, dt("2023-05-01T10:00"), session=s1).trace["flushed"]
                   for i in range(4)]
        self.assertEqual([f is not None for f in flushed], [False, True, False, True])
        self.assertEqual(system.store("dave").session(s1.session_id)["turns_done"], 8)

    def test_confirm_resolves_pending_conflicts(self):
        first = facts_json(fact("The user lives in Boston.", [1], "home_city", "2023-01"))
        second = facts_json(fact("The user stays in Denver most weeks.", [1], "home_city"))
        for accept, expected in ((True, "superseded"), (False, "current")):
            with self.subTest(accept=accept):
                system = self.system(memory={"extract": [first, second],
                                             "resolve": decisions_json(("N1", "uncertain", "F1"))},
                                     confirm_conflicts=True)
                for when, text in (("2023-01-10T09:00", "I live in Boston."), ("2023-06-01T09:00", "I'm in Denver a lot.")):
                    handle = system.open_session("erin", dt(when))
                    system.add_turn(handle, "user", text, dt(when))
                    system.close_session(handle, dt(when))
                [item] = system.pending("erin")
                system.confirm("erin", item["id"], accept)
                self.assertEqual(system.store("erin").fact(1).status, expected)
                self.assertFalse(system.store("erin").fact(2).uncertain)
                self.assertEqual(system.pending("erin"), [])

    def test_forget_history_stats_and_reindex(self):
        system = self.system(memory={"extract": [facts_json(fact("The user lives in Boston.", [1], "home_city", "2023-01")),
                                                 facts_json(fact("The user lives in Seattle.", [1], "home_city", "2023-05"))],
                                     "resolve": decisions_json(("N1", "update", "F1"))})
        for when, text in (("2023-01-10T09:00", "I live in Boston."), ("2023-06-01T09:00", "I moved to Seattle.")):
            handle = system.open_session("fay", dt(when))
            system.add_turn(handle, "user", text, dt(when))
            system.close_session(handle, dt(when))
        self.assertEqual([f.key for f in system.history("fay", "f2")], ["F1", "F2"])
        self.assertEqual([f.key for f in system.facts("fay")], ["F2"])
        self.assertEqual(system.stats("fay")["counts"]["fact"], {"current": 1, "superseded": 1})
        # 遗忘 E1：只从它抽出的 F1 失去了依据，一起遗忘
        self.assertEqual(system.forget("fay", "E1"), ["E1", "F1"])
        self.assertFalse({"E1", "F1"} & {c.key for c in system.search("fay", "Boston").ranked})
        with self.assertRaises(ValueError):
            system.forget("fay", "X1")

        system.models.embedding_name = "another-model"
        with self.assertRaises(EmbeddingMismatch):
            system.search("fay", "Seattle")
        self.assertEqual(system.reindex("fay"), 2)    # F2 + E2（E1、F1 已遗忘）
        self.assertIn("F2", {c.key for c in system.search("fay", "Seattle").ranked})

    def test_traces_are_saved_when_a_trace_dir_is_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = MemorySystem(models(memory={"query": "{}"}, chat={"answer": "ANSWER: hi"}), settings(),
                                  data_dir=os.path.join(tmp, "data"), trace_dir=os.path.join(tmp, "traces"))
            system.respond("gus", "hello there", dt("2023-05-01T12:00"), tags={"question_id": "q_1"})
            system.close()
            with open(os.path.join(tmp, "traces", "q_1.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["answer"], "hi")
            self.assertTrue(os.path.exists(os.path.join(tmp, "data", "gus.sqlite")))


if __name__ == "__main__":
    unittest.main()
