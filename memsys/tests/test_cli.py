from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest

from memsys.cli import main
from memsys.config import AppConfig

from helpers import isolated_env

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mini_oracle.json")

SCRIPT = """# 演示脚本：注释和空行会被跳过
I just moved to Boston for a new job at a biotech startup.
/end
/time +3w
I adopted a cat named Luna yesterday.
/mem
/hist F1
/hist Boston
/hist zzzz
/why
/time +1w
Where did I move for my job?
/why
/stats
/confirm
/forget F2
/model gpt
/oops
/time 2020-01-01
/quit
"""


def run_cli(*argv):
    """在空的临时目录里运行，避免读到仓库里的 config.json、.env、~/.memsys 和环境里的 key。"""
    out = io.StringIO()
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as empty:
        os.chdir(empty)
        try:
            with isolated_env(os.path.join(empty, "home")), contextlib.redirect_stdout(out):
                code = main(list(argv))
        finally:
            os.chdir(cwd)
    return code, out.getvalue()


class CliTest(unittest.TestCase):
    def test_scripted_chat_with_simulated_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "demo.txt")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(SCRIPT)
            code, out = run_cli("chat", "--fake", "--user", "demo", "--data-dir", os.path.join(tmp, "data"),
                                "--clock", "sim", "--at", "2026-10-01 09:00", "--script", script)
        self.assertEqual(code, 0)
        for expected in ("[会话 s1 · 2026/10/01 (Thursday) 09:00]", "写入：1 轮原文", "现在：2026/10/22 (Thursday) 09:00",
                         "[会话 s2 · 2026/10/22", "F1    [2026-10-01 · 当前 · 置信 0.8] I just moved to Boston",
                         "查询计划（llm）", "选入上下文：", "助手 > I just moved to Boston",
                         "事实：", "没有待确认的冲突", "已遗忘 F2", "--fake 模式下没有可切换的模型",
                         "未知命令 /oops", "时间不能倒流", "没有找到相关的事实"):
            self.assertIn(expected, out)

    def test_bench_run_judge_report_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = os.path.join(tmp, "run")
            code, out = run_cli("bench", "run", "--fake", "--data", FIXTURE, "--out", run_dir, "--limit", "2")
            self.assertEqual(code, 0)
            self.assertIn("新答 2 题", out)
            code, out = run_cli("bench", "judge", "--fake", "--data", FIXTURE,
                                "--hyp", os.path.join(run_dir, "hypotheses.jsonl"))
            self.assertIn("eval-results-fake", out)
            code, out = run_cli("bench", "report", "--data", FIXTURE, "--run", run_dir)
            self.assertIn("# 评估报告：run", out)
            self.assertTrue(os.path.exists(os.path.join(run_dir, "report.md")))
            code, out = run_cli("bench", "load", "--fake", "--data", FIXTURE, "--ids", "ku_city",
                                "--data-dir", os.path.join(tmp, "data"))
            self.assertIn("已载入。追问：memsys chat --user ku_city --clock sim --at \"2023-07-01 10:00\"", out)

    def test_bench_load_twice_does_not_duplicate_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "data")
            run_cli("bench", "load", "--fake", "--data", FIXTURE, "--ids", "ku_city", "--data-dir", data)
            code, out = run_cli("bench", "load", "--fake", "--data", FIXTURE, "--ids", "ku_city", "--data-dir", data)
            self.assertEqual(code, 0)
            self.assertEqual(out.count("已载入过，跳过"), 2)

    def test_bench_scale_and_config_change_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = os.path.join(tmp, "scale.md")
            code, out = run_cli("bench", "scale", "--sizes", "20,40", "--dim", "32", "--queries", "2", "--out", table)
            self.assertEqual(code, 0)
            self.assertIn("| 40 | 20 | 20 |", out)
            self.assertTrue(os.path.exists(table))

            run_dir = os.path.join(tmp, "run")
            run_cli("bench", "run", "--fake", "--data", FIXTURE, "--out", run_dir, "--limit", "1")
            config = os.path.join(tmp, "config.json")
            with open(config, "w", encoding="utf-8") as fh:
                fh.write('{"models": {"m": {"model": "x", "base_url": "local:hash"}}, '
                         '"roles": {"chat": "m", "memory": "m", "embedding": "m"}, '
                         '"memory": {"context_budget_tokens": 2000}}')
            code, out = run_cli("bench", "run", "--fake", "--config", config, "--data", FIXTURE, "--out", run_dir)
            self.assertEqual(code, 2)
            self.assertIn("memory.context_budget_tokens: 3500 → 2000", out)

    def test_broken_config_file_is_explained(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "config.json")
            with open(config, "w", encoding="utf-8") as fh:
                fh.write('{"models": {}, ')
            with self.assertRaises(SystemExit) as ctx:
                run_cli("chat", "--fake", "--config", config)
            self.assertIn("配置文件", str(ctx.exception))

    def test_example_config_loads(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config = AppConfig.load(os.path.join(root, "config.example.json"))
        self.assertEqual(config.roles["embedding"], "bge-m3")
        self.assertEqual(config.models["deepseek-chat"].api_key_env, "DEEPSEEK_API_KEY")
        self.assertEqual(config.memory.context_budget_tokens, 3500)

    def test_missing_config_is_explained(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                run_cli("bench", "run", "--data", FIXTURE, "--out", os.path.join(tmp, "x"))
            self.assertIn("config.example.json", str(ctx.exception))

    def test_dotenv_does_not_override_existing_variables(self):
        from memsys.cli import load_dotenv
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w") as fh:
                fh.write("# comment\nexport MEMSYS_DOTENV_A='from-file'\nMEMSYS_DOTENV_B=from-file\n")
            os.environ["MEMSYS_DOTENV_B"] = "already-set"
            self.addCleanup(os.environ.pop, "MEMSYS_DOTENV_A", None)
            self.addCleanup(os.environ.pop, "MEMSYS_DOTENV_B", None)
            load_dotenv(path)
            self.assertEqual(os.environ["MEMSYS_DOTENV_A"], "from-file")
            self.assertEqual(os.environ["MEMSYS_DOTENV_B"], "already-set")


if __name__ == "__main__":
    unittest.main()
