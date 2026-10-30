from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from datetime import datetime

from helpers import settings

from memsys.bench.judge import JudgeIncomplete, anscheck_prompt, judge
from memsys.bench.longmemeval import GoldLabel, HistorySession, ReplayItem, load
from memsys.bench.report import build_report
from memsys.bench.runner import Runner
from memsys.cli import _fake_models
from memsys.config import Models
from memsys.fake import HashEmbedder, HeuristicChat, ScriptedChat
from memsys.llm import ChatResult, ModelError, UsageLedger, UsageRecord
from memsys.schema import Turn

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mini_oracle.json")


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class LoadTest(unittest.TestCase):
    def test_mismatched_history_arrays_cannot_silently_drop_a_session(self):
        from memsys.bench.longmemeval import _split
        with open(FIXTURE, encoding='utf-8') as stream:
            entry = json.load(stream)[0]
        entry['haystack_dates'] = entry['haystack_dates'][:-1]
        with self.assertRaisesRegex(ValueError, 'different lengths'):
            _split(entry)

    def test_sessions_are_sorted_and_labels_never_reach_the_replay_item(self):
        pairs = load(FIXTURE)
        item, gold = pairs[0]
        self.assertEqual([s.session_id for s in item.sessions], ["answer_ku_a", "answer_ku_b"])
        self.assertEqual(item.sessions[0].date, datetime(2023, 1, 10, 9, 0))
        self.assertEqual(item.question_date, datetime(2023, 7, 1, 10, 0))
        # 字段白名单：记忆系统能看到的只有这些
        self.assertEqual([f.name for f in dataclasses.fields(ReplayItem)],
                         ["question_id", "question", "question_date", "sessions"])
        self.assertEqual([f.name for f in dataclasses.fields(HistorySession)], ["session_id", "date", "turns"])
        self.assertEqual([f.name for f in dataclasses.fields(Turn)], ["role", "content"])
        self.assertEqual((gold.question_type, gold.answer), ("knowledge-update", "Seattle"))
        self.assertEqual([g.is_abstention for _, g in pairs], [False, False, True, False])


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.items = [item for item, _ in load(FIXTURE)]
        self.golds = {gold.question_id: gold for _, gold in load(FIXTURE)}

    def out(self, name="run"):
        return os.path.join(self.tmp.name, name)

    def test_full_run_resume_and_ingest_cache(self):
        out = self.out()
        counts = Runner(_fake_models(), settings(), out).run(self.items)
        self.assertEqual(counts, {"answered": 4, "errors": 0, "skipped": 0, "not_started": 0})
        self.assertEqual({r["question_id"] for r in read_jsonl(os.path.join(out, "hypotheses.jsonl"))},
                         {i.question_id for i in self.items})
        self.assertTrue(all(os.path.exists(os.path.join(out, "traces", q + ".json")) for q in self.golds))
        with open(os.path.join(out, "run_config.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["mode"], "memory")

        # 续跑：已有答案的题跳过
        self.assertEqual(Runner(_fake_models(), settings(), out).run(self.items)["skipped"], 4)

        # 重新作答：写入结果直接复用缓存
        os.remove(os.path.join(out, "hypotheses.jsonl"))
        Runner(_fake_models(), settings(), out).run(self.items, workers=2)
        timings = read_jsonl(os.path.join(out, "timings.jsonl"))
        self.assertTrue(all(t["cached_ingest"] for t in timings[-4:]))
        self.assertTrue(all(t["write_coverage"]["extract_truncated_rounds"] == 0 for t in timings))
        self.assertTrue(all(t["write_coverage"]["extract_calls"] > 0 for t in timings))
        self.assertEqual({t["question_id"]: t["write_coverage"] for t in timings[:4]},
                         {t["question_id"]: t["write_coverage"] for t in timings[-4:]})

        # 写入参数一变，缓存失效、自动重建
        os.remove(os.path.join(out, "hypotheses.jsonl"))
        Runner(_fake_models(), settings(gate_threshold=0.2), out).run(self.items)
        self.assertFalse(any(t["cached_ingest"] for t in read_jsonl(os.path.join(out, "timings.jsonl"))[-4:]))

    def test_one_failing_question_does_not_stop_the_run(self):
        def answer(messages):
            if "bakery" in messages[-1]["content"]:
                raise RuntimeError("provider exploded")
            return "ANSWER: fine"
        heuristic = HeuristicChat()
        models = Models(chat=ScriptedChat({"answer": answer}), memory=heuristic, embedder=HashEmbedder(),
                        embedding_name="hash-256", names={"memory": "fake"})
        out = self.out()
        counts = Runner(models, settings(), out).run(self.items)
        self.assertEqual((counts["answered"], counts["errors"]), (3, 1))
        [error] = read_jsonl(os.path.join(out, "errors.jsonl"))
        self.assertEqual(error["question_id"], "ssa_bakery")
        self.assertIn("provider exploded", error["error"])

    def test_separate_ingestion_does_not_answer_and_read_uses_same_fingerprint(self):
        def unexpected_answer(_):
            raise AssertionError('write stage must not invoke the answer model')
        from unittest.mock import patch
        models=_fake_models()
        models.chat=ScriptedChat({'answer':unexpected_answer})
        out=self.out('write_then_read')
        runner=Runner(models,settings(),out)
        with patch('memsys.core.MemorySystem.respond',side_effect=AssertionError('write stage must not answer')):
            answer,timing=runner._memory(self.items[0],ingest_only=True)
        self.assertIsNone(answer)
        self.assertFalse(timing['cached_ingest'])
        self.assertGreater(timing['write_coverage']['extract_calls'],0)
        self.assertFalse(os.path.exists(os.path.join(out,'hypotheses.jsonl')))
        counts=Runner(_fake_models(),settings(),out).run([self.items[0]])
        self.assertEqual(counts['answered'],1)
        self.assertTrue(read_jsonl(os.path.join(out,'timings.jsonl'))[0]['cached_ingest'])

    def test_changed_replay_history_invalidates_same_question_ingest_cache(self):
        item = self.items[0]
        out = self.out('changed_history')
        Runner(_fake_models(), settings(), out).run([item])
        original = item.sessions[0]
        changed_session = dataclasses.replace(original, turns=original.turns +
            (Turn('user', 'I also purchased a wooden ruler.'),))
        changed_item = dataclasses.replace(item, sessions=(changed_session,) + item.sessions[1:])
        os.remove(os.path.join(out, 'hypotheses.jsonl'))
        Runner(_fake_models(), settings(), out).run([changed_item])
        self.assertFalse(read_jsonl(os.path.join(out, 'timings.jsonl'))[-1]['cached_ingest'])

    def test_account_problems_stop_the_whole_run(self):
        def answer(_messages):
            raise ModelError("HTTP 402: Insufficient Balance", status=402)
        models = Models(chat=ScriptedChat({"answer": answer}), memory=HeuristicChat(), embedder=HashEmbedder(),
                        embedding_name="hash-256", names={"memory": "fake"})
        runner = Runner(models, settings(), self.out())
        counts = runner.run(self.items)
        self.assertIn("402", runner.aborted)
        self.assertEqual((counts["answered"], counts["errors"], counts["not_started"]), (0, 1, 3))

    def test_baselines(self):
        chat = ScriptedChat({"answer_full_context": lambda m: "ANSWER: baseline",
                             "answer_no_memory": lambda m: "ANSWER: no idea"})
        models = Models(chat=chat, memory=HeuristicChat(), embedder=HashEmbedder(), embedding_name="hash-256")
        Runner(models, settings(), self.out("full"), mode="full-context").run(self.items[:1])
        prompt = chat.calls[0][1][-1]["content"]
        self.assertLess(prompt.index("Boston"), prompt.index("Seattle"))      # 按时间顺序拼接
        Runner(models, settings(), self.out("none"), mode="no-memory").run(self.items[:1])
        self.assertNotIn("Boston", chat.calls[1][1][-1]["content"])
        with self.assertRaises(ValueError):
            Runner(models, settings(), self.out("x"), mode="cheat")


class JudgeAndReportTest(unittest.TestCase):
    def grade_one(self, model, tmp):
        path = os.path.join(tmp, 'hypotheses.jsonl')
        with open(path, 'w') as stream:
            stream.write(json.dumps({'question_id': 'local_weight', 'hypothesis': '7'}) + '\n')
        gold = GoldLabel('local_weight', 'single-session-user', 'What was the sample weight in grams?', '7')
        return judge(path, {gold.question_id: gold}, model, 'fake', workers=1)

    def test_judge_budget_supports_response_api_minimum_and_records_actual_model(self):
        class BudgetModel:
            def chat(self, messages, **kwargs):
                if kwargs['max_tokens'] < 16:
                    raise ModelError('max_output_tokens below minimum', status=400)
                return ChatResult('yes', 'actual-model', 1, 1, 0, 0, 0, 'stop')
        with tempfile.TemporaryDirectory() as tmp:
            path = self.grade_one(BudgetModel(), tmp)
            record = read_jsonl(path)[0]['autoeval_label']
            self.assertTrue(record['label'])
            self.assertEqual(record['actual_model'], 'actual-model')

    def test_judge_retries_incomplete_or_non_binary_output_instead_of_false_label(self):
        for first in (ChatResult('no', 'fake', 1, 256, 0, 0, 0, 'length'),
                      ChatResult('yesterday', 'fake', 1, 1, 0, 0, 0, 'stop')):
            replies = iter([first, ChatResult('yes', 'fake', 1, 1, 0, 0, 0, 'stop')])
            model = ScriptedChat({'judge': lambda messages: next(replies)})
            with tempfile.TemporaryDirectory() as tmp:
                path = self.grade_one(model, tmp)
                label = read_jsonl(path)[0]['autoeval_label']
                self.assertTrue(label['label'])
                self.assertEqual(len(label['generation_attempts']), 2)

    def test_judge_does_not_write_a_grade_for_repeated_invalid_output(self):
        model = ScriptedChat({'judge': 'No label available'})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(JudgeIncomplete) as error:
                self.grade_one(model, tmp)
            self.assertFalse(os.path.exists(error.exception.out_path))

    def test_filtered_binary_text_is_not_graded_or_format_retried(self):
        model = ScriptedChat({'judge': lambda messages: ChatResult('yes', 'provider', 0, 0, 0, 0, 0, 'content_filter')})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(JudgeIncomplete) as error:
                self.grade_one(model, tmp)
            self.assertFalse(os.path.exists(error.exception.out_path))
        self.assertEqual(len(model.calls), 1)

    def test_judge_prompts_match_the_official_script(self):
        official_standard = (
            "I will give you a question, a correct answer, and a response from a model. Please answer yes if the "
            "response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct "
            "answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If "
            "the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: "
            "Q\n\nCorrect Answer: A\n\nModel Response: R\n\nIs the model response correct? Answer yes or no only.")
        official_abstention = (
            "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes "
            "if the model correctly identifies the question as unanswerable. The model could say that the information "
            "is incomplete, or some other information is given but the asked information is not.\n\nQuestion: Q\n\n"
            "Explanation: A\n\nModel Response: R\n\nDoes the model correctly identify the question as unanswerable? "
            "Answer yes or no only.")
        self.assertEqual(anscheck_prompt("multi-session", "Q", "A", "R"), official_standard)
        self.assertEqual(anscheck_prompt("knowledge-update", "Q", "A", "R", abstention=True), official_abstention)
        self.assertIn("do not penalize off-by-one errors", anscheck_prompt("temporal-reasoning", "Q", "A", "R"))
        self.assertIn("Rubric: A", anscheck_prompt("single-session-preference", "Q", "A", "R"))
        with self.assertRaises(NotImplementedError):
            anscheck_prompt("unknown", "Q", "A", "R")

    def test_offline_rules(self):
        from memsys.bench.judge import offline_label
        from memsys.bench.longmemeval import GoldLabel
        def g(qtype, answer, qid="q"):
            return GoldLabel(qid, qtype, "question?", answer)
        self.assertTrue(offline_label(g("multi-session", "$2,500"), "You spent $2,500 in total."))
        self.assertFalse(offline_label(g("multi-session", "$2,500"), "You spent $800."))
        self.assertTrue(offline_label(g("temporal-reasoning", "17 days. 18 days is also acceptable."), "18 days ago"))
        self.assertTrue(offline_label(g("temporal-reasoning", "14 days"), "It was 15 days."))   # 差 1 天容许
        self.assertTrue(offline_label(g("knowledge-update", "five"), "You have 5 tops now."))
        self.assertTrue(offline_label(g("single-session-user", "Seattle"), "You live in Seattle."))
        self.assertTrue(offline_label(g("single-session-user", "x", "q_abs"), "I don't have that information."))
        self.assertFalse(offline_label(g("single-session-user", "x", "q_abs"), "Your brother lives in Paris."))
        self.assertIsNone(offline_label(g("single-session-preference", "rubric"), "anything"))

    def test_judge_then_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            hyp = os.path.join(tmp, "hypotheses.jsonl")
            golds = {g.question_id: g for _, g in load(FIXTURE)}
            with open(hyp, "w", encoding="utf-8") as fh:
                for qid, text in (("ku_city", "You live in Seattle now."), ("tr_cat", "About a month ago."),
                                  ("ssu_brother_abs", "I don't know."), ("ssa_bakery", "It was Crumb Club.")):
                    fh.write(json.dumps({"question_id": qid, "hypothesis": text}) + "\n")
            log = judge(hyp, golds, HeuristicChat(), "fake")
            judge(hyp, golds, HeuristicChat(), "fake")          # 再跑一次不会重复评分
            labels = {r["question_id"]: r["autoeval_label"]["label"] for r in read_jsonl(log)}
            self.assertEqual(len(read_jsonl(log)), 4)
            self.assertEqual(labels, {"ku_city": True, "tr_cat": False, "ssu_brother_abs": False, "ssa_bakery": True})

            with open(os.path.join(tmp, "timings.jsonl"), "w") as fh:
                for qid in golds:
                    fh.write(json.dumps({"question_id": qid, "ingest_s": 2.0, "answer_s": 1.0, "cached_ingest": False,
                                         "context_tokens": 500, "history_tokens": 2000}) + "\n")
            ledger = UsageLedger(os.path.join(tmp, "usage.jsonl"))
            for purpose in ("extract", "answer"):
                ledger.record(UsageRecord(started_at=0.0, model="m", purpose=purpose, prompt_tokens=1000,
                                          completion_tokens=100, cached_tokens=0, cost=0.01, latency_s=1.0,
                                          retries=0, usage_reported=True, tags={"question_id": "ku_city"}))
            text = build_report(tmp, golds)
            self.assertIn("**Overall** | **50.0% (2/4)**", text)
            self.assertIn("| 拒答题（_abs） | 0.0% (0/1) |", text)
            self.assertIn("| 上下文 / 历史 | 0.25 |", text)
            self.assertIn("| **合计** | 2 | 2000 | 0 | 200 | 0.0200 |", text)
            self.assertIn("temporal-reasoning（1）：tr_cat", text)


class ReportIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.golds = {qid: GoldLabel(qid, "multi-session", "self-authored question", "self-authored answer")
                      for qid in ("local_one", "local_two", "local_three", "local_four")}

    def write_rows(self, name, rows):
        with open(os.path.join(self.tmp.name, name), "w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def test_coverage_and_score_only_include_answers_in_this_dataset(self):
        self.write_rows("hypotheses.jsonl", [{"question_id": qid, "hypothesis": "local reply"}
                                           for qid in ("local_one", "local_two", "outside")])
        self.write_rows("errors.jsonl", [{"question_id": qid} for qid in ("local_one", "local_three")])
        self.write_rows("hypotheses.jsonl.eval-results-offline-rules", [
            {"question_id": qid, "autoeval_label": {"model": "offline-rules", "label": True}}
            for qid in ("local_one", "local_four", "outside")])
        text = build_report(self.tmp.name, self.golds)
        self.assertIn("答案覆盖：2 / 4 题；缺 2 题（失败 1，未开始 1）", text)
        self.assertIn("未评分 1 题", text)
        self.assertIn("100.0% (1/1)", text)
        self.assertNotIn("Benchmark 得分换算", text)
        self.assertIn("不属于当前数据集", text)

    def test_unknown_and_mixed_currency_costs_are_not_reported_as_free(self):
        ledger = UsageLedger(os.path.join(self.tmp.name, "usage.jsonl"))
        def record(cost, known=None, currency=None):
            ledger.record(UsageRecord(started_at=0, model="local", purpose="answer", prompt_tokens=10,
                completion_tokens=1, cached_tokens=0, cost=cost, latency_s=1, retries=0, usage_reported=True,
                cost_known=known, pricing={"currency": currency} if currency else {}))
        record(0)  # 旧账本，未知价格
        text = build_report(self.tmp.name, self.golds)
        self.assertIn("| 1 | 10 | 0 | 1 | 未知 |", text)
        self.assertIn("每题平均费用：未知", text)
        record(.1, True, "USD")
        record(.2, True, "CNY")
        text = build_report(self.tmp.name, self.golds)
        self.assertIn("未完整估算", text)
        self.assertIn("已知费用部分（USD）：0.1000", text)
        self.assertIn("已知费用部分（CNY）：0.2000", text)

    def test_explicit_free_cost_is_recorded_as_zero(self):
        UsageLedger(os.path.join(self.tmp.name, "usage.jsonl")).record(UsageRecord(
            started_at=0, model="local", purpose="embed", prompt_tokens=0, completion_tokens=0,
            cached_tokens=0, cost=0, latency_s=1, retries=0, usage_reported=False, cost_known=True,
            pricing={"input": 0, "output": 0}))
        text = build_report(self.tmp.name, self.golds)
        self.assertIn("| embed | 1 | 0 | 0 | 0 | 0.0000 |", text)
        self.assertIn("无法完整估算费用的调用：0", text)

    def test_write_coverage_uses_each_answered_source_store_once(self):
        self.write_rows("hypotheses.jsonl", [{"question_id": "local_one", "hypothesis": "local reply"}])
        coverage = {"rounds": 2, "extract_split_rounds": 1, "extract_truncated_rounds": 0,
                    "deferred": 0, "extract_failures": 0, "extract_max_input_tokens": 180}
        timing = {"question_id": "local_one", "context_tokens": 40, "history_tokens": 300,
                  "ingest_s": 0, "answer_s": 1, "cached_ingest": True, "write_coverage": coverage}
        self.write_rows("timings.jsonl", [timing, timing])
        text = build_report(self.tmp.name, self.golds)
        self.assertIn("有覆盖记录：1 / 1", text)
        self.assertIn("轮次：2；超长轮次分段：1；旧前缀模式截断：0", text)
        self.assertIn("最大估算 token：180", text)
        self.assertIn("不能作为本次实际调用或费用统计", text)

    def test_index_and_calculation_audits_are_not_scores_and_do_not_double_count(self):
        self.write_rows("hypotheses.jsonl", [{"question_id": "local_one", "hypothesis": "local reply"}])
        timing = {"question_id": "local_one", "context_tokens": 40, "history_tokens": 300,
                  "ingest_s": 0, "answer_s": 1,
                  "index_coverage": {"indexed_episodes": 3, "chunked_episodes": 2,
                                     "legacy_prefix_episodes": 1, "chunk_vectors": 8},
                  "aggregation": {"status": "checks_passed", "attempts": [{"status": "invalid"}]}}
        outside = {**timing, "question_id": "outside"}
        self.write_rows("timings.jsonl", [timing, timing, outside])
        text = build_report(self.tmp.name, self.golds)
        self.assertIn("分段索引轮次：2 / 3；旧前缀索引轮次：1；分段向量：8", text)
        self.assertIn("检查通过 1；未通过 0", text)
        self.assertIn("尝试修正 1", text)
        self.assertIn("不能作为语义正确率或证据完整率", text)


if __name__ == "__main__":
    unittest.main()
