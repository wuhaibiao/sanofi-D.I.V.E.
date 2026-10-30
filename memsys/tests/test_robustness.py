"""健壮性回归测试：每个用例对应一个曾经存在的缺陷（模型输出不规整、进程中途被杀、误操作等）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from helpers import decisions_json, dt, fact, facts_json, models, settings

from memsys.bench.judge import JudgeIncomplete, judge
from memsys.bench.jsonl import append_jsonl, read_jsonl
from memsys.bench.longmemeval import load
from memsys.bench.report import build_report
from memsys.bench.runner import ConfigChanged, Runner
from memsys.bench.scale import measure, to_markdown
from memsys.cli import ChatApp, RealClock, SimClock, _fake_models
from memsys.config import AppConfig, MemorySettings, Models
from memsys.core import MemorySystem, _safe_name
from memsys.fake import HashEmbedder, HeuristicChat, ScriptedChat
from memsys.llm import ModelError, UsageLedger
from memsys.read import plan_query
from memsys.update import RESOLVE_BATCH

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mini_oracle.json")


def ingest(system, user, sessions):
    """sessions: [(时间, 用户的话), ...]，每段一个会话。"""
    for when, text in sessions:
        handle = system.open_session(user, dt(when))
        system.add_turn(handle, "user", text, dt(when))
        system.close_session(handle, dt(when))


class ForgetTest(unittest.TestCase):
    def system(self, **memory):
        system = MemorySystem(models(memory=memory, chat={"answer": lambda m: "ANSWER: ok"}), settings())
        self.addCleanup(system.close)
        return system

    def test_forgetting_a_fact_also_forgets_the_round_it_came_from(self):
        system = self.system(extract=[facts_json(fact("The user lives in Boston.", [1], "home_city"))])
        ingest(system, "u", [("2023-01-01T10:00", "I live in Boston.")])
        self.assertEqual(system.forget("u", "F1"), ["F1", "E1"])
        self.assertEqual(system.search("u", "Boston").ranked, [])

    def test_shared_original_is_withheld_but_other_fact_is_kept(self):
        system = self.system(extract=[facts_json(fact("The user lives in Boston.", [1], "home_city"),
                                                 fact("The user works at Moderna.", [1], "employer"))])
        ingest(system, "u", [("2023-01-01T10:00", "I live in Boston and work at Moderna.")])
        self.assertEqual(system.forget("u", "F1"), ["F1"])
        self.assertEqual(system.store("u").episodes()[0].status, "withheld")
        self.assertEqual([f.key for f in system.facts("u")], ["F2"])
        system.settings.query_analysis = False
        self.assertNotIn("Boston", system.recall("u", "Boston", dt("2023-03-01T10:00")).memory)

    def test_forgotten_newer_version_never_reaches_the_context(self):
        query = json.dumps({"queries": ["Boston"], "keywords": ["Boston"]})
        system = self.system(extract=[facts_json(fact("The user lives in Boston.", [1], "home_city")),
                                      facts_json(fact("The user lives in Denver.", [1], "home_city"))],
                             resolve=decisions_json(("N1", "update", "F1")), query=query)
        ingest(system, "u", [("2023-01-01T10:00", "I live in Boston."), ("2023-02-01T10:00", "I moved to Denver.")])
        system.forget("u", "F2")
        trace = system.respond("u", "Did I live in Boston?", dt("2023-03-01T10:00")).trace
        self.assertIn("F1", trace["selected"]["facts"])      # 旧版本被检索到 → 版本链补全不能把 F2 带回来
        self.assertNotIn("F2", trace["selected"]["facts"])
        self.assertNotIn("Denver", trace["memory"])


class WriteRobustnessTest(unittest.TestCase):
    def test_two_updates_of_the_same_fact_in_one_session_form_one_chain(self):
        system = MemorySystem(models(memory={
            "extract": [facts_json(fact("The user lives in Boston.", [1], "home_city")),
                        facts_json(fact("The user lives in Denver.", [1], "home_city"),
                                   fact("The user's home city is now Austin.", [1], "home_city"))],
            "resolve": decisions_json(("N1", "update", "F1"), ("N2", "update", "F1"))}), settings())
        self.addCleanup(system.close)
        ingest(system, "u", [("2023-01-01T10:00", "I live in Boston."),
                             ("2023-02-01T10:00", "I moved to Denver, no wait, Austin.")])
        self.assertEqual([f.key for f in system.history("u", "F1")], ["F1", "F2", "F3"])
        self.assertEqual([f.key for f in system.facts("u")], ["F3"])

    def test_resolve_runs_in_batches_and_a_broken_batch_only_affects_itself(self):
        n = RESOLVE_BATCH + 5
        first = facts_json(*[fact("The user owns item number %d." % i, [1], "item_%d" % i) for i in range(n)])
        replies = ["not json at all", decisions_json(*[("N%d" % (RESOLVE_BATCH + i + 1), "duplicate",
                                                        "F%d" % (RESOLVE_BATCH + i + 1)) for i in range(5)])]
        system = MemorySystem(models(memory={"extract": [first, first], "resolve": replies}), settings())
        self.addCleanup(system.close)
        ingest(system, "u", [("2023-01-01T10:00", "I own many items.")])
        handle = system.open_session("u", dt("2023-02-01T10:00"))
        system.add_turn(handle, "user", "I still own many items.", dt("2023-02-01T10:00"))
        report, _ = system.close_session(handle, dt("2023-02-01T10:00"))
        self.assertEqual(system.models.memory.purposes().count("resolve"), 2)
        self.assertEqual((len(report.added), report.duplicates), (RESOLVE_BATCH, 5))

    def test_scalar_rounds_and_missing_rounds_are_tolerated(self):
        item = fact("The user has a dog named Max.", [1])
        item["rounds"] = 1
        orphan = fact("The user likes jazz.", [])
        system = MemorySystem(models(memory={"extract": json.dumps({"facts": [item, orphan]})}), settings())
        self.addCleanup(system.close)
        ingest(system, "u", [("2023-01-01T10:00", "I have a dog named Max and I like jazz.")])
        store = system.store("u")
        self.assertEqual(store.sources([1, 2]), {1: [1], 2: [1]})   # 只有一轮时出处没有歧义

    def test_wrong_number_of_embeddings_fails_instead_of_dropping_facts(self):
        class Short(HashEmbedder):
            def embed(self, texts, **kwargs):
                return super().embed(texts, **kwargs)[:-1]
        m = models(memory={"extract": facts_json(fact("The user has a cat.", [1]))})
        system = MemorySystem(Models(chat=m.chat, memory=m.memory, embedder=Short(), embedding_name="short"),
                              settings())
        self.addCleanup(system.close)
        handle = system.open_session("u", dt("2023-01-01T10:00"))
        system.add_turn(handle, "user", "I have a cat.", dt("2023-01-01T10:00"))
        with self.assertRaises(ModelError):
            system.close_session(handle, dt("2023-01-01T10:00"))
        self.assertEqual(system.store("u").open_sessions(), [handle.session_id])   # 原文还在，可以重试


class CoreRobustnessTest(unittest.TestCase):
    def test_query_plan_tolerates_wrong_types(self):
        m = models(memory={"query": json.dumps({"queries": "where do I live", "keywords": ["Boston", None, 3],
                                                "needs_all": "false", "focus": None})})
        plan = plan_query(m, "Where do I live?", dt("2023-02-01T10:00"), settings())
        self.assertEqual(plan.queries, ["Where do I live?", "where do I live"])
        self.assertEqual(plan.keywords, ["Boston", "3"])
        self.assertFalse(plan.needs_all)
        self.assertEqual(plan.focus, "any")

    def test_user_ids_never_share_a_store(self):
        names = {_safe_name(u) for u in ("张三", "李四", "a/b", "a_b", "a b", "..", "alice")}
        self.assertEqual(len(names), 7)
        self.assertEqual(_safe_name("alice"), "alice")       # 普通名字不变，已有的库照样能打开
        self.assertEqual(_safe_name("张三"), "张三")
        self.assertNotIn("/", _safe_name("../../etc/passwd"))
        self.assertEqual(_safe_name("gpt4_0000abcd"), "gpt4_0000abcd")

    def test_turns_cannot_be_added_to_a_closed_session(self):
        system = MemorySystem(models(memory={"extract": facts_json()}, chat={"answer": "ANSWER: hi"}), settings())
        self.addCleanup(system.close)
        handle = system.open_session("u", dt("2023-01-01T10:00"))
        system.close_session(handle, dt("2023-01-01T10:00"))
        with self.assertRaises(ValueError):
            system.add_turn(handle, "user", "I have 3 cats", dt("2023-01-01T10:05"))
        with self.assertRaises(ValueError):
            system.respond("u", "I have 3 cats", dt("2023-01-01T10:05"), session=handle)

    def test_failed_reindex_keeps_the_old_index(self):
        class Down:
            def embed(self, texts, **kwargs):
                raise ModelError("embedding service down")
        system = MemorySystem(models(memory={"extract": facts_json(fact("The user has a cat named Luna.", [1]))}),
                              settings())
        self.addCleanup(system.close)
        ingest(system, "u", [("2023-01-01T10:00", "I have a cat named Luna.")])
        system.models = Models(chat=system.models.chat, memory=system.models.memory, embedder=Down(),
                               embedding_name="other")
        with self.assertRaises(ModelError):
            system.reindex("u")
        store = system.store("u")
        self.assertEqual(store.get_meta("embedding_model"), "hash-256")
        self.assertEqual(len(store.vectors("fact", ("current",))[0]), 1)

    def test_a_failed_in_flight_flush_does_not_lose_the_reply(self):
        def extract(_messages):
            raise ModelError("extract down")
        system = MemorySystem(models(memory={"extract": extract, "query": "{}"}, chat={"answer": "ANSWER: hi"}),
                              settings(flush_rounds=1))
        self.addCleanup(system.close)
        handle = system.open_session("u", dt("2023-01-01T10:00"))
        reply = system.respond("u", "I have a dog", dt("2023-01-01T10:00"), session=handle)
        self.assertEqual(reply.text, "hi")
        self.assertIn("整理失败", reply.trace["flushed"])
        self.assertEqual(len(system.store("u").turns(handle.session_id)), 2)

    def test_confirming_after_the_old_fact_changed_again_keeps_the_chain_whole(self):
        system = MemorySystem(models(memory={
            "extract": [facts_json(fact("The user lives in Boston.", [1], "home_city")),
                        facts_json(fact("The user stays in Denver a lot.", [1], "home_city")),
                        facts_json(fact("The user lives in Austin.", [1], "home_city"))],
            "resolve": [decisions_json(("N1", "uncertain", "F1")), decisions_json(("N1", "update", "F1"))]}),
            settings(confirm_conflicts=True))
        self.addCleanup(system.close)
        ingest(system, "u", [("2023-01-01T10:00", "I live in Boston."), ("2023-02-01T10:00", "I'm in Denver a lot."),
                             ("2023-03-01T10:00", "I moved to Austin.")])
        [item] = system.pending("u")
        system.confirm("u", item["id"], accept=True)
        self.assertEqual([f.key for f in system.history("u", "F1")], ["F1", "F3", "F2"])
        self.assertEqual([f.key for f in system.facts("u")], ["F2"])


class ChatAppTest(unittest.TestCase):
    def app(self, clock, chat=None, memory=None, idle_minutes=0.0, trace_dir=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        heuristic = HeuristicChat()
        m = Models(chat=chat or heuristic, memory=memory or heuristic, embedder=HashEmbedder(),
                   embedding_name="hash-256", names={})
        system = MemorySystem(m, settings(), data_dir=tmp.name, trace_dir=os.path.join(tmp.name, "traces"))
        self.addCleanup(system.close)
        out = []
        app = ChatApp(system, "u", clock, None, UsageLedger(), out=out.append, idle_minutes=idle_minutes)
        return app, out, tmp.name

    def test_quit_exits_even_when_consolidation_fails(self):
        class Down:
            def chat(self, *args, **kwargs):
                raise ModelError("network down")
        app, out, _ = self.app(SimClock(dt("2023-01-01T10:00")))
        app.handle("I have a dog named Max")
        app.system.models = Models(chat=Down(), memory=Down(), embedder=HashEmbedder(), embedding_name="hash-256")
        self.assertFalse(app.handle("/quit"))
        self.assertIn("下次启动会自动补整理", out[-1])
        self.assertEqual(app.system.store("u").open_sessions(), ["s1"])

    def test_unexpected_errors_do_not_end_the_program(self):
        def broken(_messages):
            raise ZeroDivisionError("bug")
        app, out, _ = self.app(SimClock(dt("2023-01-01T10:00")), chat=ScriptedChat({"answer": broken}),
                               memory=ScriptedChat({"query": "{}"}))
        self.assertTrue(app.handle("hello there, I'm Bob"))
        self.assertIn("ZeroDivisionError", out[-1])

    def test_idle_sessions_are_closed_before_the_next_message(self):
        clock = SimClock(dt("2023-01-01T10:00"))
        app, out, _ = self.app(clock, idle_minutes=30)
        app.handle("I have a dog named Max")
        clock.current = dt("2023-01-01T10:20")
        app.handle("He is three years old")
        self.assertEqual(app.session.session_id, "s1")
        clock.current = dt("2023-01-01T11:00")
        app.handle("I also adopted a cat")
        self.assertEqual(app.session.session_id, "s2")
        self.assertTrue(any("空闲超过 30 分钟" in line for line in out))
        self.assertEqual(app.system.store("u").session("s1")["status"], "closed")

    def test_advancing_the_simulated_clock_runs_the_forgetting_policy(self):
        clock = SimClock(dt("2023-01-01T10:00"))
        app, out, _ = self.app(clock)
        app.system.settings.archive_threshold = 0.9          # 让任何闲置的记忆都够得上归档
        app.handle("I have a dog named Max")
        app.handle("/end")
        app.handle("/time +1y")
        self.assertTrue(any("遗忘：归档" in line for line in out))
        counts = app.system.store("u").counts()
        self.assertEqual(counts["fact"], {"archived": 1})
        self.assertEqual(counts["episode"], {"active": 1})   # 这次检查时它还支撑着当前事实，所以保留

    def test_traces_of_replies_at_the_same_moment_are_all_kept(self):
        app, _, data = self.app(SimClock(dt("2023-01-01T10:00")))
        app.handle("I have a dog named Max")
        app.handle("I also have a cat")
        self.assertEqual(len(os.listdir(os.path.join(data, "traces"))), 2)

    def test_recovery_failure_at_start_is_reported_not_fatal(self):
        class Down:
            def chat(self, *args, **kwargs):
                raise ModelError("network down")
        app, out, _ = self.app(RealClock(), memory=Down())
        handle = app.system.open_session("u", dt("2023-01-01T10:00"))
        app.system.add_turn(handle, "user", "I have a dog named Max", dt("2023-01-01T10:00"))
        app.start()
        self.assertIn("补整理上次的会话失败", out[0])
        self.assertIn("记忆系统已就绪", out[-1])


class BenchRobustnessTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = os.path.join(tmp.name, "run")
        self.items = [item for item, _ in load(FIXTURE)]
        self.golds = {gold.question_id: gold for _, gold in load(FIXTURE)}

    def test_resume_survives_a_half_written_line(self):
        Runner(_fake_models(), settings(), self.out).run(self.items[:1])
        with open(os.path.join(self.out, "hypotheses.jsonl"), "a") as fh:
            fh.write('{"question_id": "tr_cat", "hypo')            # 进程在写这一行时被杀
        counts = Runner(_fake_models(), settings(), self.out).run(self.items)
        self.assertEqual((counts["skipped"], counts["answered"]), (1, 3))
        self.assertEqual(sorted(r["question_id"] for r in read_jsonl(os.path.join(self.out, "hypotheses.jsonl"))),
                         sorted(self.golds))

    def test_append_starts_a_new_line_after_a_broken_one(self):
        path = os.path.join(os.path.dirname(self.out), "x.jsonl")
        with open(path, "w") as fh:
            fh.write('{"a": 1}\n{"broken')
        append_jsonl(path, {"b": 2})
        self.assertEqual(read_jsonl(path), [{"a": 1}, {"b": 2}])

    def test_half_ingested_store_is_rebuilt_not_appended_to(self):
        item = self.items[0]                                   # ku_city：两个会话
        system = MemorySystem(_fake_models(), settings(), data_dir=os.path.join(self.out, "stores"))
        session = item.sessions[0]
        handle = system.open_session(item.question_id, session.date, session.session_id)
        for turn in session.turns:
            system.add_turn(handle, turn.role, turn.content, session.date)
        system.close_session(handle, session.date)             # 第一个会话写完后进程被杀
        system.close()
        Runner(_fake_models(), settings(), self.out).run([item])
        system = MemorySystem(_fake_models(), settings(), data_dir=os.path.join(self.out, "stores"))
        self.addCleanup(system.close)
        self.assertEqual(system.store(item.question_id).external_ids(), [s.session_id for s in item.sessions])

    def test_resuming_with_another_config_is_refused_unless_allowed(self):
        Runner(_fake_models(), settings(), self.out, data_info={"file": "a.json", "sha256": "1"}).run(self.items[:1])
        with self.assertRaises(ConfigChanged) as ctx:
            Runner(_fake_models(), settings(context_budget_tokens=2000), self.out,
                   data_info={"file": "a.json", "sha256": "1"}).run(self.items)
        self.assertIn("context_budget_tokens", str(ctx.exception))
        with self.assertRaises(ConfigChanged):
            Runner(_fake_models(), settings(), self.out, data_info={"file": "a.json", "sha256": "2"}).run(self.items)
        Runner(_fake_models(), settings(context_budget_tokens=2000), self.out, allow_config_change=True,
               data_info={"file": "a.json", "sha256": "1"}).run(self.items)
        with open(os.path.join(self.out, "run_config.json"), encoding="utf-8") as fh:
            config = json.load(fh)
        self.assertEqual(len(config["history"]), 1)
        self.assertIn("context_budget_tokens", config["history"][0]["changed"][0])
        self.assertIn("答案来自不同配置", build_report(self.out, self.golds))

    def test_settings_added_later_do_not_count_as_a_change(self):
        Runner(_fake_models(), settings(), self.out).run(self.items[:1])
        path = os.path.join(self.out, "run_config.json")
        with open(path, encoding="utf-8") as fh:
            config = json.load(fh)
        del config["memory"]["max_episodes"]                  # 旧版本没有这个参数，取的是默认值
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        self.assertEqual(Runner(_fake_models(), settings(), self.out).run(self.items)["answered"], 3)

    def test_timings_and_report_cover_retrieval_latency_and_scale(self):
        Runner(_fake_models(), settings(), self.out, data_info={"file": "mini.json", "sha256": "abc"}).run(self.items)
        [timing] = [t for t in read_jsonl(os.path.join(self.out, "timings.jsonl")) if t["question_id"] == "ku_city"]
        for key in ("plan_s", "search_s", "rerank_s", "generate_s", "memory_facts", "memory_episodes"):
            self.assertIn(key, timing)
        judge(os.path.join(self.out, "hypotheses.jsonl"), self.golds, HeuristicChat(), "fake")
        text = build_report(self.out, self.golds)
        for expected in ("mini.json（sha256 前 16 位 abc）", "context_budget_tokens=", "读取路径分阶段耗时",
                         "**检索**", "## 检索规模", "| 0–50 |"):
            self.assertIn(expected, text)

    def test_one_failing_judgement_does_not_lose_the_others(self):
        hyp = os.path.join(os.path.dirname(self.out), "hypotheses.jsonl")
        with open(hyp, "w", encoding="utf-8") as fh:
            for qid in ("ku_city", "tr_cat", "ku_city"):        # 重复的题只评一次
                fh.write(json.dumps({"question_id": qid, "hypothesis": "Seattle"}) + "\n")

        def flaky(messages):
            if "Luna" in messages[-1]["content"]:
                raise ModelError("judge down")
            return "yes"
        with self.assertRaises(JudgeIncomplete) as ctx:
            judge(hyp, self.golds, ScriptedChat({"judge": flaky}), "j")
        self.assertEqual([q for q, _ in ctx.exception.failures], ["tr_cat"])
        self.assertEqual([r["question_id"] for r in read_jsonl(ctx.exception.out_path)], ["ku_city"])
        judge(hyp, self.golds, ScriptedChat({"judge": "no"}), "j")   # 重跑只补失败的题
        self.assertEqual(sorted(r["question_id"] for r in read_jsonl(ctx.exception.out_path)), ["ku_city", "tr_cat"])

    def test_scale_benchmark(self):
        rows = measure([20, 60], dim=64, queries=3)
        self.assertEqual([(r.items, r.facts, r.episodes) for r in rows], [(20, 10, 10), (60, 30, 30)])
        self.assertIn("| 60 | 30 | 30 |", to_markdown(rows, 64, 3))


class ConfigValidationTest(unittest.TestCase):
    def test_bad_values_are_reported_clearly(self):
        base = {"models": {"m": {"model": "x", "base_url": "http://localhost"}},
                "roles": {"chat": "m", "memory": "m", "embedding": "m"}}
        for memory, message in (({"context_budget_tokens": "3500"}, "non-negative integer"),
                                ({"fact_budget_share": 1.5}, "between 0 and 1"),
                                ({"llm_rerank": "yes"}, "true or false"),
                                ({"contex_budget": 1}, "unknown memory settings")):
            with self.subTest(memory=memory):
                with self.assertRaisesRegex(ValueError, message):
                    AppConfig.from_dict(dict(base, memory=memory))
        with self.assertRaisesRegex(ValueError, "config.models.m"):
            AppConfig.from_dict(dict(base, models={"m": {"model": "x"}}))
        MemorySettings.from_dict({"max_active_facts": 100, "min_similarity": 0})


if __name__ == "__main__":
    unittest.main()
