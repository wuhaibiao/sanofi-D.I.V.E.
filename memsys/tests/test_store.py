from __future__ import annotations

import unittest
from datetime import date

from helpers import dt

from memsys.schema import ARCHIVED, FACT_CURRENT, FACT_SUPERSEDED
from memsys.store import EmbeddingMismatch, Store
from memsys.timeutil import parse_span


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)

    def add_fact(self, text, attribute="", event=None, session="s1"):
        with self.store.transaction():
            return self.store.add_fact(text=text, kind="personal", subject="user", attribute=attribute, value=text,
                                       event=parse_span(event), recorded_at=dt("2023-05-01T10:00"),
                                       valid_from=None, session_id=session, importance=0.6)

    def test_sessions_and_turn_log(self):
        s1 = self.store.create_session(dt("2023-05-01T10:00"))
        s2 = self.store.create_session(dt("2023-05-02T10:00"), external_id="ext")
        self.assertEqual((s1, s2), ("s1", "s2"))
        self.store.append_turn(s1, "user", "hello", dt("2023-05-01T10:00"))
        self.store.append_turn(s1, "assistant", "hi", dt("2023-05-01T10:01"))
        self.assertEqual([r["content"] for r in self.store.turns(s1)], ["hello", "hi"])
        self.assertEqual([r["content"] for r in self.store.turns(s1, 1)], ["hi"])
        with self.store.transaction():
            self.store.mark_consolidated(s1, 2, dt("2023-05-01T11:00"))
        self.assertEqual(self.store.open_sessions(), ["s2"])
        self.assertEqual(self.store.session(s1)["turns_done"], 2)
        self.assertEqual(self.store.history_tokens(), 3)  # "hello" 2 + "hi" 1

    def test_full_text_search_in_english_and_chinese_respects_status(self):
        with self.store.transaction():
            e1 = self.store.add_episode("s1", 0, dt("2023-05-01T10:00"), "I moved to Seattle", "Welcome!", 0.6, "active")
            e2 = self.store.add_episode("s1", 1, dt("2023-05-01T10:00"), "我住在杭州", "好的", 0.6, "active")
        self.assertEqual([i for i, _ in self.store.fts_search("episode", '"seattle"', ("active",), 5)], [e1])
        self.assertEqual([i for i, _ in self.store.fts_search("episode", '"杭州"', ("active",), 5)], [e2])
        with self.store.transaction():
            self.store.set_status("episode", [e1], ARCHIVED)
        self.assertEqual(self.store.fts_search("episode", '"seattle"', ("active",), 5), [])
        self.assertEqual(self.store.fts_search("episode", 'AND OR (', ("active",), 5), [])

    def test_supersede_builds_a_version_chain(self):
        a = self.add_fact("The user lives in Boston.", "home_city", "2022-01")
        b = self.add_fact("The user lives in Seattle.", "home_city", "2023-05")
        with self.store.transaction():
            self.store.supersede(a, b, date(2023, 5, 1))
        self.assertEqual([f.id for f in self.store.chain(b)], [a, b])
        self.assertEqual([f.id for f in self.store.chain(a)], [a, b])
        old = self.store.fact(a)
        self.assertEqual((old.status, old.superseded_by, old.valid_to), (FACT_SUPERSEDED, b, date(2023, 5, 1)))
        self.assertEqual([f.id for f in self.store.slot_facts("user", "home_city")], [b])
        self.assertEqual(self.store.fact(b).status, FACT_CURRENT)

    def test_duplicates_raise_mentions_and_add_sources(self):
        fact_id = self.add_fact("The user has a cat.")
        with self.store.transaction():
            self.store.note_duplicate(fact_id, [7], 0.9)
        self.assertEqual(self.store.fact(fact_id).mentions, 2)
        self.assertEqual(self.store.fact(fact_id).confidence, 0.9)
        self.assertEqual(self.store.sources([fact_id]), {fact_id: [7]})
        self.assertEqual(self.store.facts_of_episodes([7]), {7: [fact_id]})

    def test_vectors_are_normalized_and_filtered_by_status(self):
        a = self.add_fact("one")
        b = self.add_fact("two")
        with self.store.transaction():
            self.store.put_vectors("fact", [(a, [3.0, 4.0]), (b, [0.0, 2.0])])
        ids, vecs = self.store.vectors("fact", (FACT_CURRENT,))
        self.assertEqual(ids, [a, b])
        self.assertAlmostEqual(vecs[0][0], 0.6, places=5)
        with self.store.transaction():
            self.store.set_status("fact", [a], ARCHIVED)
        self.assertEqual(self.store.vectors("fact", (FACT_CURRENT,))[0], [b])

    def test_embedding_model_is_pinned(self):
        self.store.check_embedding_model("model-a")
        self.store.check_embedding_model("model-a")
        with self.assertRaises(EmbeddingMismatch):
            self.store.check_embedding_model("model-b")
        self.store.replace_vectors("model-b", {"fact": []})
        self.store.check_embedding_model("model-b")

    def test_nested_transactions_commit_or_roll_back_as_one(self):
        # sqlite3 的 `with conn:` 嵌套时内层会提前提交；Store.transaction 只让最外层提交
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.add_fact("written before the inner block")
                self.store.set_meta("k", "v")          # 内部又开了一层事务
                raise RuntimeError("boom")
        self.assertEqual(self.store.facts(), [])
        self.assertIsNone(self.store.get_meta("k"))

    def test_pending_and_forget(self):
        a, b = self.add_fact("old"), self.add_fact("new")
        with self.store.transaction():
            pid = self.store.add_pending(b, a, dt("2023-05-01T10:00"), "unclear")
        self.assertEqual([r["id"] for r in self.store.pending()], [pid])
        with self.store.transaction():
            self.store.resolve_pending(pid, "accepted")
        self.assertEqual(self.store.pending(), [])
        self.store.forget("fact", a)
        self.assertEqual(self.store.fact(a).status, "forgotten")
        with self.assertRaises(KeyError):
            self.store.forget("fact", 999)


if __name__ == "__main__":
    unittest.main()
