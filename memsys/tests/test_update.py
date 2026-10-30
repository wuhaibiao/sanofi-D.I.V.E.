"""更新模块（update.py）：置信度怎么来、怎么参与裁决、读取和遗忘；以及历史很短时的上下文上限。"""

from __future__ import annotations

import dataclasses
import json
import unittest
from datetime import datetime

from helpers import decisions_json, dt, fact, facts_json, settings

from memsys.config import Models
from memsys.core import MemorySystem
from memsys.fake import HashEmbedder, ScriptedChat
from memsys.lifecycle import retention
from memsys.read import effective_budget
from memsys.schema import FACT_CURRENT
from memsys.textutil import estimate_tokens
from memsys.update import certainty_to_confidence


def with_certainty(item, certainty):
    return dict(item, certainty=certainty)


def system_with(memory, **overrides):
    models = Models(chat=None, memory=ScriptedChat(memory), embedder=HashEmbedder(), embedding_name="hash-256")
    return MemorySystem(models, settings(**overrides))


def say(system, at, text):
    handle = system.open_session("u", dt(at))
    system.add_turn(handle, "user", text, dt(at))
    return system.close_session(handle, dt(at))[0]


class ConfidenceTest(unittest.TestCase):
    def test_certainty_maps_to_confidence(self):
        self.assertEqual(certainty_to_confidence("firm"), 0.9)
        self.assertEqual(certainty_to_confidence("Tentative"), 0.5)
        self.assertEqual(certainty_to_confidence(None), 0.8)
        self.assertEqual(certainty_to_confidence("whatever"), 0.8)

    def test_tentative_statement_does_not_replace_a_firm_fact(self):
        system = system_with({
            "extract": [facts_json(with_certainty(fact("User lives in Boston.", [1], "home_city"), "firm")),
                        facts_json(with_certainty(fact("User is thinking about moving to Denver.", [1], "home_city"),
                                                  "tentative"))],
            "resolve": [decisions_json(("N1", "update", "F1"))]})
        say(system, "2024-01-05T10:00:00", "I live in Boston.")
        report = say(system, "2024-02-05T10:00:00", "I'm thinking about moving to Denver.")
        boston, denver = system.store("u").facts()
        self.assertEqual(report.tentative, 1)
        self.assertEqual(report.updates, [])
        self.assertEqual((boston.status, boston.superseded_by), (FACT_CURRENT, None))
        self.assertTrue(denver.uncertain)
        self.assertLessEqual(denver.confidence, 0.5)
        self.assertIn("打算或拿不准", report.summary())

    def test_firm_statement_replaces_and_tentative_conflicts_wait_for_confirmation(self):
        system = system_with({
            "extract": [facts_json(with_certainty(fact("User is thinking about moving to Denver.", [1], "home_city"),
                                                  "tentative")),
                        facts_json(with_certainty(fact("User moved to Denver.", [1], "home_city"), "firm")),
                        facts_json(with_certainty(fact("User might move to Austin.", [1], "home_city"), "tentative"))],
            "resolve": [decisions_json(("N1", "update", "F1")), decisions_json(("N1", "update", "F2"))]},
            confirm_conflicts=True)
        say(system, "2024-01-05T10:00:00", "Maybe Denver.")
        report = say(system, "2024-03-05T10:00:00", "I moved to Denver!")
        self.assertEqual(report.updates, [(1, 2)])                 # 确定的说法取代了打算
        report = say(system, "2024-04-05T10:00:00", "Austin, maybe?")
        self.assertEqual(report.tentative, 1)
        self.assertEqual(len(report.pending), 1)                   # 对话模式：等用户确认
        pending = system.pending("u")[0]
        system.confirm("u", pending["id"], accept=True)
        store = system.store("u")
        self.assertEqual(store.fact(3).confidence, 1.0)            # 用户亲口确认
        self.assertEqual(store.fact(2).superseded_by, 3)

    def test_repetition_raises_confidence(self):
        system = system_with({
            "extract": [facts_json(with_certainty(fact("User might adopt a cat.", [1], "pet"), "tentative")),
                        facts_json(with_certainty(fact("User adopted a cat.", [1], "pet"), "firm"))],
            "resolve": [decisions_json(("N1", "duplicate", "F1"))]})
        say(system, "2024-01-05T10:00:00", "I might adopt a cat.")
        say(system, "2024-01-06T10:00:00", "I adopted a cat.")
        cat = system.store("u").fact(1)
        self.assertEqual((cat.mentions, cat.confidence), (2, 0.9))

    def test_reading_and_forgetting_use_confidence(self):
        query = json.dumps({"queries": ["trip to Lisbon"], "keywords": ["Lisbon", "trip"]})
        system = system_with({
            "extract": [facts_json(with_certainty(fact("User booked a trip to Lisbon.", [1], "trip_lisbon"), "firm"),
                                   with_certainty(fact("User may take a trip to Lisbon.", [1], "trip_lisbon_maybe"),
                                                  "tentative"))],
            "query": [query, query]})
        say(system, "2024-01-05T10:00:00", "Lisbon trip!")
        rec = system.recall("u", "Am I going to Lisbon?", dt("2024-01-10T09:00:00"), save_trace=False)
        firm, tentative = system.store("u").facts()
        order = [c["key"] for c in rec.trace["candidates"] if c["key"].startswith("F")]
        self.assertEqual(order[:2], ["F1", "F2"])                   # 同样相关时，确定的排在前面
        self.assertIn("tentative · User may take a trip to Lisbon.", rec.memory)
        self.assertNotIn("tentative · User booked", rec.memory)
        now = datetime(2025, 1, 1)
        self.assertLess(retention(tentative, now, system.settings), retention(firm, now, system.settings))


class HistoryCapTest(unittest.TestCase):
    def test_effective_budget(self):
        s = settings()
        self.assertEqual(effective_budget(s, None), 3500)
        self.assertEqual(effective_budget(s, 10000), 3500)
        self.assertEqual(effective_budget(s, 2000, question_tokens=50), 1950)
        self.assertEqual(effective_budget(s, 100), 300)                             # 下限
        self.assertEqual(effective_budget(dataclasses.replace(s, max_history_share=0), 100), 3500)

    def test_context_never_exceeds_a_short_history(self):
        long_reply = "Here is a detailed plan for your garden. " * 40
        system = system_with({"extract": [facts_json(fact("User grows tomatoes on the balcony.", [1], "garden"))]},
                             query_analysis=False)
        handle = system.open_session("u", dt("2024-01-05T10:00:00"))
        system.add_turn(handle, "user", "I grow tomatoes on my balcony. How do I keep them healthy?",
                        dt("2024-01-05T10:00:00"))
        system.add_turn(handle, "assistant", long_reply, dt("2024-01-05T10:00:00"))
        system.close_session(handle, dt("2024-01-05T10:00:00"))
        question = "What do I grow on my balcony?"
        rec = system.recall("u", question, dt("2024-02-01T09:00:00"), save_trace=False)
        history = rec.trace["history_tokens"]
        self.assertGreater(history, 300)
        self.assertLessEqual(estimate_tokens(rec.memory) + estimate_tokens(question), history)
        self.assertIn("tomatoes", rec.memory)
        self.assertEqual(rec.trace["effective_budget"], history - estimate_tokens(question))


if __name__ == "__main__":
    unittest.main()
