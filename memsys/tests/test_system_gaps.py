"""六项系统缺口的机制验收，使用独立临时库和自拟会话。"""
from __future__ import annotations

import json
import random
import re
import tempfile
import unittest
import contextlib
import io
from pathlib import Path

from helpers import dt, fact, facts_json, models, settings
from memsys.aggregate_batch import aggregation_pages
from memsys.budget import TokenCounter
from memsys.config import MemorySettings
from memsys.core import MemorySystem
from memsys.maintenance import apply_cleanup, cleanup_plan, integrity, queue_repair, repair_plan
from memsys.read import QueryPlan, Retriever
from memsys.schema import EPISODE_WITHHELD, FACT_CURRENT, FORGOTTEN
from memsys.store import StorageLimitExceeded, Store
from memsys.vecmath import VectorRows, scores

AT = dt("2025-05-04T10:00")


def save(system, text, close=True):
    from memsys.write import Writer
    handle = system.open_session("u", AT)
    system.add_turn(handle, "user", text, AT)
    if close:
        report, _ = system.close_session(handle, AT)
    else:
        report = Writer(system.store("u"), system.models, system.settings).consolidate(handle.session_id)
    return handle, report


class BudgetTest(unittest.TestCase):
    def test_strong_tail_evidence_is_not_crowded_out_by_weak_source_expansion(self):
        import math
        system = MemorySystem(models(), settings(budget_mode="utf8", query_analysis=False,
                              max_history_share=0, context_budget_tokens=600, min_similarity=.4))
        self.addCleanup(system.close)
        system.models.embedder.embed = lambda texts, **kwargs: [[1.0, 0.0] for text in texts]
        store = system.store("u")
        tail = "The cobalt instrument serial is R9-K2."
        prefix = "Generic notes about furniture and rooms. " * 80
        with store.transaction():
            sid = store.create_session(AT)
            first = store.add_episode(sid, 0, AT, "I grow herbs on the balcony.", "", .8, "active")
            second = store.add_episode(sid, 1, AT, prefix + tail, "", .8, "active")
            fid = store.add_fact(text="The user grows herbs.", kind="personal", subject="user", attribute="plant",
                                 value="herbs", event=None, recorded_at=AT, valid_from=None, session_id=sid,
                                 importance=.6, episode_ids=[first])
            weak = [.5, math.sqrt(.75)]
            strong = [.95, math.sqrt(1-.95**2)]
            store.put_vectors("fact", [(fid, weak)])
            store.put_episode_chunks(first, [("User", 0, 27, weak)])
            store.put_episode_chunks(second, [("User", len(prefix), len(prefix+tail), strong)])
        recall = system.recall("u", "设备编号是什么？", AT, save_trace=False)
        episode_candidates = [row for row in recall.trace["candidates"] if row["key"].startswith("E")]
        self.assertEqual(episode_candidates[0]["key"], "E2")
        self.assertIn(tail, recall.memory)
        self.assertLessEqual(TokenCounter("utf8").count(recall.memory), 600)

    def test_utf8_split_is_lossless_for_multilingual_emoji_and_code(self):
        counter = TokenCounter("utf8")
        text = "中文🙂 é 한글 العربية a=123\n" * 30
        pieces = counter.split(text, 31)
        self.assertEqual("".join(pieces), text)
        self.assertTrue(all(counter.count(p) <= 31 for p in pieces))
        self.assertLessEqual(counter.count(counter.truncate(text, 40)), 40)
        with self.assertRaises(ValueError):
            counter.split("🙂", 1)

    def test_missing_local_tokenizer_never_falls_back_silently(self):
        with self.assertRaises(ValueError):
            TokenCounter("tokenizer", "/does/not/exist/tokenizer.json")
        with self.assertRaises(ValueError):
            MemorySettings.from_dict({"budget_mode": "tokenizer"})

    def test_local_tokenizer_count_and_fingerprint(self):
        try:
            from tokenizers import Tokenizer, models as tokenizer_models, pre_tokenizers
        except ImportError:
            self.skipTest("optional tokenizers dependency is checked in the extended runtime")
        with tempfile.TemporaryDirectory() as tmp:
            tokenizer = Tokenizer(tokenizer_models.WordLevel({"[UNK]": 0, "hello": 1, "world": 2}, unk_token="[UNK]"))
            tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
            path = str(Path(tmp) / "tokenizer.json")
            tokenizer.save(path)
            counter = TokenCounter("tokenizer", path)
            self.assertEqual(counter.count("hello world"), 2)
            self.assertEqual(counter.count("hello world"), len(tokenizer.encode("hello world", add_special_tokens=False).ids))
            self.assertEqual(len(counter.manifest()["tokenizer_sha256"]), 64)
            config = settings(budget_mode="tokenizer", budget_tokenizer_path=path)
            original = config.write_fingerprint()
            tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel()
            tokenizer.save(path)
            self.assertNotEqual(config.write_fingerprint(), original)

    def test_strict_write_and_index_cover_full_text(self):
        from memsys.indexing import episode_index
        from memsys.write import Round, WriteReport, Writer
        config = settings(budget_mode="utf8", extract_chunk_tokens=200, embed_chunk_tokens=64)
        system = MemorySystem(models(memory={"extract": facts_json()}), config)
        self.addCleanup(system.close)
        text = "我记录🙂号码123，以及很长的说明。" * 25
        writer = Writer(system.store("u"), system.models, config)
        parts = writer._parts(Round(1, 0, AT, text, text), WriteReport("preview"))
        counter = TokenCounter("utf8")
        self.assertTrue(all(counter.count(p.render()) <= 200 for p in parts))
        self.assertEqual("".join(p.assistant_text for p in parts), text)
        index = episode_index([(text, "")], config)
        self.assertEqual("".join(t[len("User: "):] for t in index.texts), text)
        self.assertTrue(all(counter.count(t) <= 64 for t in index.texts))

    def test_question_exceeding_full_prompt_budget_is_rejected_before_answer(self):
        system = MemorySystem(models(chat={"answer": "ANSWER: ignored"}),
                              settings(query_analysis=False, answer_input_budget_tokens=100))
        self.addCleanup(system.close)
        with self.assertRaisesRegex(ValueError, "answer rules and question"):
            system.respond("u", "Long question " * 100, AT)
        self.assertEqual(system.models.chat.calls, [])


class AggregatePagingTest(unittest.TestCase):
    def test_fact_and_original_on_different_pages_count_only_original_once(self):
        def page(messages):
            found = re.findall(r"\[(F\d+|E\d+)\]([^\n]*)\n([^\n]+)", messages[-1]['content'])
            return json.dumps({'reviewed': [key for key, _, _ in found], 'operation': 'sum', 'unit': 'USD',
                'items': [dict(source=key, quote=text, value=re.search(r'\$(\d+)', text).group(1), unit='USD')
                          for key, header, text in found if 'context_only' not in header]})
        system = MemorySystem(models(chat={'aggregate_page': page}), settings(query_analysis=False,
            max_history_share=0, budget_mode='utf8', context_budget_tokens=600, min_similarity=0))
        self.addCleanup(system.close)
        store = system.store('u')
        with store.transaction():
            sid = store.create_session(AT)
            for n in range(8):
                raw = 'I paid $%d for receipt R%d.' % (n+7, n)
                eid = store.add_episode(sid, n, AT, raw, '', .8, 'active')
                store.add_fact(text='The user paid $%d for receipt R%d.' % (n+7, n), kind='event', subject='user',
                    attribute='payment', value=str(n+7), event=None, recorded_at=AT, valid_from=None,
                    session_id=sid, importance=.6, episode_ids=[eid])
        reply = system.respond('u', 'What is the total I paid? Reply with only the total number of dollars.', AT)
        self.assertEqual(reply.text, '84')
        self.assertGreater(reply.trace['aggregation']['pages_processed'], 1)
        self.assertEqual(reply.trace['aggregation']['contributions'], 8)
        self.assertEqual(len(reply.trace['aggregation']['reviewed_sources']), 16)

    def system(self, count=130, **overrides):
        def page(messages):
            found = re.findall(r"\[(F\d+)\][^\n]*\n([^\n]+)", messages[-1]["content"])
            return json.dumps({"reviewed": [key for key, _ in found], "operation": "sum", "unit": "USD",
                "items": [{"source": key, "quote": text, "value": "1", "unit": "USD"} for key, text in found]})
        config = dict(query_analysis=False, max_history_share=0, budget_mode="utf8", context_budget_tokens=600,
                      max_facts_all=2, dense_k=1, sparse_k=1, min_similarity=1.0)
        config.update(overrides)
        system = MemorySystem(models(chat={"aggregate_page": page}), settings(**config))
        self.addCleanup(system.close)
        store = system.store("u")
        with store.transaction():
            for n in range(count):
                text = "The user paid $1 for receipt %d." % n
                store.add_fact(text=text, kind="event", subject="user", attribute="payment", value="1",
                               event=None, recorded_at=AT, valid_from=None, session_id="s1", importance=.6)
        return system

    def test_more_than_topk_and_single_context_limit_are_aggregated(self):
        system = self.system()
        reply = system.respond("u", "What is the total I paid? Reply with only the total number of dollars.", AT)
        self.assertEqual(reply.text, "130")
        self.assertEqual(reply.trace["aggregation"]["status"], "checks_passed")
        self.assertGreater(reply.trace["aggregation"]["pages_processed"], 1)
        self.assertEqual(len(reply.trace["aggregation"]["reviewed_sources"]), 130)
        self.assertTrue(all(n <= system.settings.answer_input_budget_tokens for n in reply.trace["aggregation"]["page_prompt_counts"]))

    def test_missing_page_review_cannot_pass_as_complete_total(self):
        system = self.system(8)
        system.models.chat.handlers["aggregate_page"] = json.dumps({"reviewed": [], "operation": "sum", "unit": "USD", "items": []})
        system.models.chat.handlers["aggregate_page_repair"] = system.models.chat.handlers["aggregate_page"]
        reply = system.respond("u", "What is the total I paid?", AT)
        self.assertEqual(reply.trace["aggregation"]["status"], "incomplete")
        self.assertIn("cannot give a complete", reply.text)
        self.assertEqual(len(reply.trace["aggregation"]["page_validation_failures"]), 2)

    def test_word_groups_repair_wrong_sum_schema_with_literal_evidence(self):
        def response(messages, corrected=False):
            found = re.findall(r"\[(E\d+)\][^\n]*\n([^\n]+)", messages[1]["content"])
            items = [dict(source=key, quote=text, value="3", unit="people") for key, text in found]
            if corrected:
                for item in items:
                    item["quantity_label"] = "apprentices"
            return json.dumps(dict(reviewed=[key for key, _ in found],
                                   operation="count_groups" if corrected else "sum", unit="people", items=items))
        system = MemorySystem(models(chat={"aggregate_page": response,
                                           "aggregate_page_repair": lambda m: response(m, True)}),
                              settings(query_analysis=False, max_history_share=0, budget_mode="utf8",
                                       context_budget_tokens=600, min_similarity=0))
        self.addCleanup(system.close)
        store = system.store("u")
        with store.transaction():
            sid = store.create_session(AT)
            for n in range(8):
                store.add_episode(sid, n, AT, "Three apprentices joined separate workshop G%d." % n, "", .8, "active")
        reply = system.respond("u", "How many people joined across all workshops? Reply with only the number.", AT)
        self.assertEqual(reply.text, "24")
        audit = reply.trace["aggregation"]
        self.assertTrue(audit["pages_complete"])
        self.assertGreater(len(audit["page_validation_failures"]), 0)
        self.assertTrue(all(n <= system.settings.answer_input_budget_tokens for n in audit["page_attempt_prompt_counts"]))

    def test_owned_object_pages_repair_unit_drift_and_deduplicate(self):
        def response(messages, corrected=False):
            found = re.findall(r"\[(E\d+)\][^\n]*\n([^\n]+)", messages[1]["content"])
            unit = "instruments" if "Established calculation" in messages[0]["content"] and not corrected else "weather instruments"
            return json.dumps(dict(reviewed=[key for key, _ in found], operation="count_unique", unit=unit,
                items=[dict(source=key, quote=text, entity="thermometer") for key, text in found]))
        system = MemorySystem(models(chat={"aggregate_page": response,
                                           "aggregate_page_repair": lambda m: response(m, True)}),
                              settings(query_analysis=False, max_history_share=0, budget_mode="utf8",
                                       context_budget_tokens=600, min_similarity=0))
        self.addCleanup(system.close)
        store = system.store("u")
        with store.transaction():
            sid = store.create_session(AT)
            for n in range(8):
                store.add_episode(sid, n, AT, "My only weather instrument is a thermometer. Update T%d." % n, "", .8, "active")
        reply = system.respond("u", "How many distinct weather instruments do I own? Reply with only the number.", AT)
        self.assertEqual(reply.text, "1")
        self.assertTrue(reply.trace["aggregation"]["pages_complete"])
        self.assertIn("pages disagree", reply.trace["aggregation"]["page_validation_failures"][0]["error"])

    def test_repair_cannot_bypass_invalid_amount_evidence(self):
        system = self.system(8)
        def invalid(messages):
            found = re.findall(r"\[(F\d+)\][^\n]*\n([^\n]+)", messages[1]["content"])
            return json.dumps(dict(reviewed=[key for key, _ in found], operation="sum", unit="USD",
                items=[dict(source=key, quote=text, value="99", unit="USD") for key, text in found]))
        system.models.chat.handlers.update(aggregate_page=invalid, aggregate_page_repair=invalid)
        reply = system.respond("u", "What is the total I paid?", AT)
        self.assertFalse(reply.trace["aggregation"]["pages_complete"])
        self.assertEqual(len(reply.trace["aggregation"]["page_validation_failures"]), 2)
        self.assertIn("page evidence failed", reply.trace["aggregation"]["error"])

    def test_candidate_and_page_limits_fail_closed_without_model_calls(self):
        for override in ({"aggregate_candidate_limit": 5}, {"aggregate_max_pages": 1}):
            system = self.system(30, **override)
            reply = system.respond("u", "What is the total I paid?", AT)
            self.assertFalse(reply.trace["aggregation"]["pages_complete"])
            self.assertEqual(system.models.chat.calls, [])

    def test_long_episode_pages_preserve_all_text(self):
        system = self.system(0)
        store = system.store("u")
        text = "A long record of independent payments. " * 100
        with store.transaction():
            sid = store.create_session(AT)
            store.add_episode(sid, 0, AT, text, "", .8, "active")
        plan = QueryPlan(["payments"], ["payments"], None, True, "any", "test")
        result = Retriever(store, system.models, system.settings).search(plan)
        pages = aggregation_pages(result, system.settings, AT.date())
        self.assertEqual("".join(p["E1"]["text"] for p in pages), "User: " + text)
        counter = TokenCounter("utf8")
        self.assertTrue(all(counter.count("\n\n".join(e["rendered"] for e in p.values())) <= 600 for p in pages))

    def test_contribution_limit_refuses_partial_total(self):
        system = self.system(30, aggregate_contribution_limit=2)
        reply = system.respond("u", "What is the total I paid?", AT)
        self.assertEqual(reply.trace["aggregation"]["status"], "incomplete")
        self.assertIn("aggregate_contribution_limit", reply.trace["aggregation"]["error"])


class SelectiveForgetTest(unittest.TestCase):
    def test_verified_literal_and_unique_quote_preserve_unstructured_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            city = fact("The user lives in Quito.", [1], "home_city", value="Quito")
            city["source_quotes"] = [{"round": 1, "role": "user", "quote": "I live in Quito."}]
            system = MemorySystem(models(memory={"extract": facts_json(city)}), settings(query_analysis=False), data_dir=tmp)
            handle, _ = save(system, "I live in Quito. I knit scarves on weekends.", close=False)
            system.forget("u", "F1")
            store = system.store("u")
            self.assertEqual(store.episodes()[0].status, "active")
            self.assertNotIn("Quito", store.episodes()[0].text())
            self.assertIn("scarves", store.episodes()[0].text())
            self.assertIn("scarves", system._conversation(store, handle.session_id))
            self.assertNotIn("Quito", system._conversation(store, handle.session_id))
            self.assertIn("Quito", store.turns(handle.session_id)[0]["content"])
            self.assertEqual(store.vectors("episode", ("active",))[0], [])
            system.reindex("u")
            self.assertNotIn("Quito", system.recall("u", "Quito scarves", AT).memory)
            system.close()
            system = MemorySystem(models(), settings(query_analysis=False), data_dir=tmp)
            self.addCleanup(system.close)
            self.assertNotIn("Quito", system.store("u").episodes()[0].text())
            self.assertIn("scarves", system.store("u").episodes()[0].text())

    def test_ambiguous_quote_uses_conservative_withholding(self):
        city = fact("The user lives in Quito.", [1], "home_city", value="Quito")
        city["source_quotes"] = [{"round": 1, "role": "user", "quote": "Quito"}]
        system = MemorySystem(models(memory={"extract": facts_json(city, fact("The user knits scarves.", [1], "hobby"))}), settings())
        self.addCleanup(system.close)
        save(system, "I live in Quito. Quito is familiar. I knit scarves.")
        system.forget("u", "F1")
        self.assertEqual(system.store("u").episodes()[0].status, EPISODE_WITHHELD)


class MaintenanceTest(unittest.TestCase):
    def test_explicit_legacy_repair_completes_once_and_never_guesses_from_empty(self):
        system = MemorySystem(models(memory={"extract": "malformed"}), settings())
        self.addCleanup(system.close)
        handle, _ = save(system, "I collect amber fossils.")
        store = system.store("u")
        with store.transaction():
            store.clear_backlog([1])  # 模拟旧版本没有队列记录
        preview = repair_plan(store)
        self.assertEqual(preview["episodes"], [])
        self.assertIn(handle.session_id, preview["unproven_empty_sessions"])
        plan = repair_plan(store, [handle.session_id])
        queue_repair(store, plan)
        system.models = models(memory={"extract": facts_json(fact("The user collects amber fossils.", [1], "hobby"))})
        system.rebuild("u")
        self.assertEqual(store.backlog_count(), 0)
        self.assertEqual(system.facts("u")[0].mentions, 1)
        self.assertEqual(repair_plan(store, [handle.session_id])["episodes"], [])

    def test_failure_log_and_forgotten_sources(self):
        system = MemorySystem(models(memory={"extract": facts_json()}), settings())
        self.addCleanup(system.close)
        handle, _ = save(system, "I collect fossils.")
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "failures.jsonl"
            log.write_text(json.dumps({"session_id": handle.session_id, "extract_failures": 1}))
            self.assertEqual(len(repair_plan(system.store("u"), failure_log=log)["episodes"]), 1)
            system.forget("u", "E1")
            self.assertEqual(repair_plan(system.store("u"), failure_log=log)["episodes"], [])

    def test_cleanup_backs_up_erases_payload_and_preserves_live_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = "UNIQUE_SENSITIVE_PAYLOAD_427"
            system = MemorySystem(models(memory={"extract": facts_json(
                fact(secret, [1], "secret"), fact("The user knits scarves.", [1], "hobby"))}), settings(), data_dir=tmp)
            self.addCleanup(system.close)
            save(system, "My secret is " + secret + ". I knit scarves.")
            system.forget("u", "F1")
            store = system.store("u")
            plan = cleanup_plan(store)
            self.assertIn(secret, store.turns("s1")[0]["content"])
            result = apply_cleanup(store, plan, str(Path(tmp) / "before.sqlite"))
            self.assertTrue(all(result["integrity"].values()))
            self.assertEqual(store.fact(1).text, "")
            self.assertEqual(store.fact(2).status, FACT_CURRENT)
            self.assertNotIn(secret, store.turns("s1")[0]["content"])
            self.assertNotIn(secret.encode(), Path(store.path).read_bytes())
            self.assertIn(secret.encode(), (Path(tmp) / "before.sqlite").read_bytes())
            self.assertEqual(cleanup_plan(store)["facts"], [])

    def test_stale_cleanup_plan_never_deletes_or_creates_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "memory.sqlite"))
            self.addCleanup(store.close)
            plan = cleanup_plan(store)
            store.set_meta("new", "evidence")
            backup = str(Path(tmp) / "before.sqlite")
            with self.assertRaisesRegex(ValueError, "stale"):
                apply_cleanup(store, plan, backup)
            self.assertFalse(Path(backup).exists())

    def test_cli_reviewed_preview_can_be_applied_and_tampering_is_rejected(self):
        from memsys.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            db, preview = Path(tmp) / "memory.sqlite", Path(tmp) / "preview.json"
            store = Store(str(db))
            plan = cleanup_plan(store)
            plan["facts"] = [999]
            with self.assertRaisesRegex(ValueError, "changed"):
                apply_cleanup(store, plan, str(Path(tmp) / "bad.sqlite"))
            self.assertFalse((Path(tmp) / "bad.sqlite").exists())
            store.close()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["cleanup", "--db", str(db), "--out", str(preview)]), 0)
                self.assertEqual(main(["cleanup", "--db", str(db), "--plan", str(preview), "--backup",
                                       str(Path(tmp) / "before.sqlite"), "--apply"]), 0)
            self.assertTrue((Path(tmp) / "before.sqlite").exists())

    def test_same_source_retry_does_not_reinforce_and_cap_rolls_back_growth(self):
        store = Store()
        self.addCleanup(store.close)
        with store.transaction():
            sid = store.create_session(AT)
            eid = store.add_episode(sid, 0, AT, "I own an instrument.", "", .8, "active")
            fid = store.add_fact(text="The user owns an instrument.", kind="personal", subject="user", attribute="item",
                                 value="instrument", event=None, recorded_at=AT, valid_from=None, session_id=sid,
                                 importance=.6, confidence=.8, episode_ids=[eid])
            store.note_duplicate(fid, [eid], .9)
        self.assertEqual((store.fact(fid).mentions, store.fact(fid).confidence), (1, .8))
        store.max_bytes = store.allocated_bytes()
        with self.assertRaises(StorageLimitExceeded):
            store.append_turn(sid, "user", "large record " * 10000, AT)
        self.assertEqual(store.turns(sid), [])
        self.assertTrue(all(integrity(store).values()))


class VectorBackendTest(unittest.TestCase):
    def test_accelerator_matches_reference_scores_and_order(self):
        from memsys import vecmath
        if vecmath._np is None:
            self.skipTest("optional numpy dependency is checked in the extended runtime")
        rng = random.Random(42)
        rows = VectorRows([[rng.random() for _ in range(64)] for _ in range(300)])
        query = [rng.random() for _ in range(64)]
        reference, fast = scores(query, rows, "python"), scores(query, rows, "numpy")
        self.assertLess(max(abs(a-b) for a,b in zip(reference, fast)), 1e-12)
        self.assertEqual(sorted(range(300), key=lambda i: -reference[i]), sorted(range(300), key=lambda i: -fast[i]))
        dense = rows.dense
        scores(query, rows, "numpy")
        self.assertIs(rows.dense, dense)
        with self.assertRaises(ValueError):
            scores([1], rows)
