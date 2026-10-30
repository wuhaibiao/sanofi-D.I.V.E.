"""命令行入口。

    memsys chat          多轮、跨会话的对话入口（带 /why、/mem、/time 等命令）
    memsys bench run     LongMemEval 回放跑分，输出官方格式的 JSONL
    memsys bench judge   用官方评分提示词在本地评分（调参用；正式成绩以官方复跑为准）
    memsys bench report  生成评估报告
    memsys bench load    把某道题的历史灌进对话入口的记忆库，当场追问它
    memsys bench scale   合成数据上测检索延迟随记忆规模的变化（不调模型）
    memsys bench leakcheck  检查提示词和代码里有没有评测数据的内容
    memsys reindex       换 embedding 模型后重建向量
    memsys rebuild       降级期间只存了原文的轮次，有了记忆模型后补抽事实

    接入别的 agent：
    memsys serve         OpenAI / Anthropic 兼容代理 + 记忆 HTTP 接口
    memsys mcp           MCP server（Claude Code、Claude Desktop、Cursor 等）
    memsys hook          Claude Code 的 hook：每轮自动记录对话、自动注入记忆
    memsys init          按当前环境找模型，生成 config.json
    memsys doctor        查看用的是哪些模型、从哪找到的、现在是哪一档

模型都可以不配：没配的角色按 配置 → 宿主借给的 → 环境变量 → 本机 Ollama 的顺序找，找不到就降级运行。
"现在"由这一层决定：对话用真实时钟或可手动推进的模拟时钟，评测用数据里的时间。
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Callable, List, Optional, TextIO

from .config import AppConfig, MemorySettings, Models
from .core import MemorySystem, SessionHandle, _safe_name
from .discover import HostModels, describe, resolve_models
from .fake import HashEmbedder, HeuristicChat
from .lifecycle import retention_table
from .llm import ModelError, UsageLedger
from .schema import Fact
from .store import EmbeddingMismatch, Store
from .timeutil import format_stamp, parse_datetime, shift

DEFAULT_CONFIG = "config.json"
DEFAULT_DATA_DIR = ".memsys"


def memsys_home() -> str:
    """接入别的 agent 时（serve / mcp / hook）记忆放在用户目录下，跨项目共用；环境变量 MEMSYS_HOME 可以改。"""
    return os.environ.get("MEMSYS_HOME") or os.path.join(os.path.expanduser("~"), ".memsys")


# ---------------------------------------------------------------- clocks
class RealClock:
    def now(self) -> datetime:
        return datetime.now().replace(microsecond=0)


class SimClock:
    """演示用的模拟时钟：/time +3w 就能"过去三周"，几分钟内演示几个月里发生的事。"""

    def __init__(self, start: datetime):
        self.current = start

    def now(self) -> datetime:
        return self.current


# ---------------------------------------------------------------- setup
def _fake_models() -> Models:
    chat = HeuristicChat()
    return Models(chat=chat, memory=chat, embedder=HashEmbedder(), embedding_name="hash-256",
                  names={"chat": "fake", "memory": "fake", "embedding": "fake"})


def _config_path(args: argparse.Namespace) -> Optional[str]:
    """--config → 环境变量 MEMSYS_CONFIG → 当前目录的 config.json → MEMSYS_HOME/config.json；都没有就不用配置文件。"""
    if getattr(args, "config", None):
        return args.config
    for path in (os.environ.get("MEMSYS_CONFIG"), DEFAULT_CONFIG, os.path.join(memsys_home(), DEFAULT_CONFIG)):
        if path and os.path.exists(path):
            return path
    return None


def _load(args: argparse.Namespace, ledger: UsageLedger, host: Optional[HostModels] = None):
    """返回 (配置或 None, 模型, 记忆参数)。--fake 时不联网。

    配置文件可以没有、也可以只写一部分角色：没写的按 宿主借给的 → 环境变量 → 本机 Ollama 去找，
    还找不到就降级运行（见 Models.tier）。"""
    config = None
    path = _config_path(args)
    args.config_path = path
    args.storage_settings = None
    if path:
        try:
            config = AppConfig.load(path)
        except (OSError, ValueError, TypeError) as exc:   # JSON 语法错误也是 ValueError
            raise SystemExit("配置文件 %s 有误：%s" % (path, exc)) from None
    args.storage_settings = config.storage if config else None
    if getattr(args, "fake", False):
        settings = config.memory if config else MemorySettings()
        settings = dataclasses.replace(settings, rerank_backend="fusion")
        return config, _fake_models(), settings
    try:
        models = config.build_models(ledger, host=host) if config else resolve_models(None, ledger, host=host)
    except ModelError as exc:     # 例如配置里写的 api_key_env 没有设置
        raise SystemExit("模型配置有误：%s" % exc) from None
    if config is not None:
        settings = config.memory
    else:
        # 没有配置文件：有记忆模型、但向量只有词面相似度时，让查询分析多给同义词补召回
        settings = MemorySettings(query_expansion=models.tier == "L1")
    return config, models, settings


def _need_chat(models: Models, what: str) -> None:
    if models.chat is None:
        raise SystemExit("%s需要一个回答模型，但没有找到。复制 config.example.json 为 config.json 并写好 roles.chat，"
                         "或者设置 OPENAI_API_KEY / DEEPSEEK_API_KEY 等环境变量，或者启动本机 Ollama；"
                         "也可以加 --fake 离线演练。memsys doctor 可以查看找到了什么。" % what)


# ------------------------------------------------------------------ chat
class ChatApp:
    def __init__(self, system: MemorySystem, user: str, clock, config: Optional[AppConfig],
                 ledger: UsageLedger, out: Callable[[str], None] = print, idle_minutes: float = 0):
        self.system = system
        self.user = user
        self.clock = clock
        self.config = config
        self.ledger = ledger
        self.out = out
        self.idle = timedelta(minutes=idle_minutes) if idle_minutes > 0 else None
        self.session: Optional[SessionHandle] = None
        self.last_active: Optional[datetime] = None
        self.last_trace: Optional[dict] = None

    def start(self) -> None:
        try:
            for report in self.system.recover(self.user):
                self.out("[恢复] 补整理了上次没关闭的会话 %s。%s" % (report.session_id, report.summary()))
        except ModelError as exc:
            # 原文都在日志里，会话保持未关闭，下次启动再补；不影响这次正常对话
            self.out("[恢复] 补整理上次的会话失败，下次启动再试：%s" % exc)
        self._maintain()
        self.out("记忆系统已就绪（用户：%s，现在：%s，档位 %s）。输入 /help 查看命令。" % (
            self.user, format_stamp(self.clock.now()), self.system.models.tier))
        backlog = self.system.store(self.user).backlog_count()
        if backlog and self.system.models.tier != "L0":
            self.out("  有 %d 轮原文是在没有记忆模型时存下的，运行 memsys rebuild --user %s 补抽事实。" % (backlog, self.user))

    def handle(self, line: str) -> bool:
        """处理一行输入；返回 False 表示退出。任何一条输入出错都不让程序退出。"""
        line = line.strip()
        if not line:
            return True
        try:
            if line.startswith("/"):
                return self._command(line)
            self._message(line)
        except (ModelError, EmbeddingMismatch, ValueError, KeyError) as exc:
            self.out("出错了：%s" % exc)
        except KeyboardInterrupt:
            self.out("[已取消这一步]")
        except Exception as exc:   # 意料之外的错误也只影响这一句，记忆库的写入都是事务，不会留下半截
            self.out("出错了（%s）：%s" % (type(exc).__name__, exc))
        return True

    def _message(self, text: str) -> None:
        now = self.clock.now()
        if self.session is not None and self.idle and self.last_active and now - self.last_active > self.idle:
            self.out("[空闲超过 %d 分钟，上一个会话已结束]" % (self.idle.total_seconds() // 60))
            self.end_session()
        if self.session is None:
            self.session = self.system.open_session(self.user, now)
            self.out("[会话 %s · %s]" % (self.session.session_id, format_stamp(now)))
        self.last_active = now
        from .agent_adapter import AgentAdapter
        reply = AgentAdapter(self.system).respond(self.user, text, now, session=self.session)
        self.last_active = self.clock.now()
        self.last_trace = reply.trace
        self.out("助手 > " + reply.text)
        if reply.trace.get("flushed"):
            self.out("[会话较长，已先整理一部分] " + reply.trace["flushed"])

    def _maintain(self) -> None:
        """距离上次整理过了一段时间（重新打开程序、模拟时钟推进），跑一次遗忘策略。"""
        summary = self.system.maintain(self.user, self.clock.now()).summary()
        if summary:
            self.out("  " + summary)

    def shutdown(self) -> None:
        """退出时结束会话。整理失败也照样退出：原文已在日志里，下次启动会自动补整理。"""
        try:
            self.end_session()
        except (Exception, KeyboardInterrupt) as exc:
            self.out("结束会话时出错（%s），原文已保存，下次启动会自动补整理。" % (str(exc)[:200] or type(exc).__name__))

    def end_session(self) -> None:
        if self.session is None:
            return
        report, lifecycle = self.system.close_session(self.session, self.clock.now())
        self.session = None
        self.out(report.summary())
        store = self.system.store(self.user)
        for old_id, new_id in report.updates:
            old, new = store.fact(old_id), store.fact(new_id)
            self.out("  取代：F%d「%s」→ F%d「%s」" % (old_id, old.text, new_id, new.text))
        for item in self.system.pending(self.user):
            if item["id"] in report.pending:
                self.out("  待确认 #%d：F%d「%s」是否取代 F%d「%s」？用 /confirm %d yes|no 回答" % (
                    item["id"], item["new"].id, item["new"].text, item["old"].id, item["old"].text, item["id"]))
        if lifecycle.summary():
            self.out("  " + lifecycle.summary())

    def _command(self, line: str) -> bool:
        name, _, arg = line[1:].partition(" ")
        name, arg = name.lower(), arg.strip()
        if name in ("quit", "exit", "q"):
            self.shutdown()
            return False
        handlers = {"help": self._help, "end": lambda _: self.end_session(), "new": self._new,
                    "time": self._time, "why": self._why, "mem": self._mem, "hist": self._hist,
                    "forget": self._forget, "stats": self._stats, "model": self._model,
                    "confirm": self._confirm}
        if name not in handlers:
            self.out("未知命令 /%s，输入 /help 查看命令。" % name)
            return True
        handlers[name](arg)
        return True

    def _help(self, _: str) -> None:
        self.out("\n".join([
            "/end                结束当前会话，触发写入并打印写入报告",
            "/new                结束当前会话，下一句话开启新会话",
            "/time +3w | /time 2026-12-01   推进模拟时钟（需要 --clock sim），会先结束当前会话",
            "/why                上一个回答的检索轨迹",
            "/mem [关键词]       列出当前有效的事实，或检索记忆",
            "/hist F12 | 关键词   某条事实的版本链（给关键词时取最相关的事实）",
            "/forget F12|E5      遗忘一条记忆（软删除），失去依据的原文或事实一并遗忘",
            "/stats              记忆规模、各状态数量、上下文/历史比、用量",
            "/model 名字         切换回答用的模型（配置里定义的名字）",
            "/confirm [编号 yes|no]  查看或处理待确认的冲突",
            "/quit               结束会话并退出"]))

    def _new(self, _: str) -> None:
        self.end_session()
        self.out("下一句话将开启新会话。")

    def _time(self, arg: str) -> None:
        if not isinstance(self.clock, SimClock):
            self.out("真实时钟不能调整时间，用 --clock sim 启动才能使用 /time。")
            return
        if not arg:
            self.out("现在：%s" % format_stamp(self.clock.now()))
            return
        target = shift(self.clock.now(), arg) if arg[0] in "+-" else parse_datetime(arg)
        if target < self.clock.now():
            self.out("时间不能倒流：现在已经是 %s。" % format_stamp(self.clock.now()))
            return
        self.end_session()
        self.clock.current = target
        self.out("现在：%s" % format_stamp(target))
        self._maintain()

    def _why(self, _: str) -> None:
        if not self.last_trace:
            self.out("还没有回答过问题。")
            return
        self.out(format_trace(self.last_trace))

    def _mem(self, arg: str) -> None:
        if not arg:
            facts = sorted(self.system.facts(self.user), key=lambda f: (f.when().start, f.id))
            if not facts:
                self.out("还没有任何事实记忆。")
            for fact in facts[-40:]:
                self.out(_fact_line(fact))
            return
        result = self.system.search(self.user, arg)
        if not result.ranked:
            self.out("没有找到相关记忆。")
        for cand in result.ranked[:12]:
            item = result.items[cand.key]
            text = item.text if isinstance(item, Fact) else item.text().replace("\n", " / ")
            self.out("%-5s %.4f %s %s" % (cand.key, cand.score, item.status, text[:120]))

    def _hist(self, arg: str) -> None:
        """参数可以是事实编号（F12），也可以是关键词：取最相关的那条事实，显示它的版本链。"""
        key = arg.strip()
        if not re.match(r"^[Ff]\d+$", key):
            result = self.system.search(self.user, key or "?")
            facts = [c.key for c in result.ranked if c.kind == "fact"]
            if not facts:
                self.out("没有找到相关的事实。")
                return
            key = facts[0]
        for fact in self.system.history(self.user, key):
            self.out(_fact_line(fact))

    def _forget(self, arg: str) -> None:
        keys = self.system.forget(self.user, arg)
        line = "已遗忘 %s（软删除：不再参与检索，数据仍保留以便追溯）。" % keys[0]
        if len(keys) > 1:
            line += "连带遗忘：%s（它们的依据只剩被遗忘的条目）。" % ", ".join(keys[1:])
        self.out(line)

    def _stats(self, _: str) -> None:
        stats = self.system.stats(self.user)
        counts = stats["counts"]
        self.out("事实：%s" % json.dumps(counts.get("fact", {}), ensure_ascii=False))
        self.out("原文轮次：%s" % json.dumps(counts.get("episode", {}), ensure_ascii=False))
        self.out("会话：%s · 待确认冲突 %d 条" % (json.dumps(counts.get("session", {}), ensure_ascii=False),
                                          stats["pending"]))
        self.out("历史总量约 %d token · embedding：%s" % (stats["history_tokens"], stats["embedding_model"]))
        index = stats["index_coverage"]
        self.out("原文分段索引 %d / %d 轮 · 分段向量 %d 条 · 旧前缀索引 %d 轮" % (
            index["chunked_episodes"], index["indexed_episodes"], index["chunk_vectors"], index["legacy_prefix_episodes"]))
        if self.last_trace:
            self.out("上一个回答的上下文：%d token / 历史 %d token" % (
                self.last_trace["context_tokens"], self.last_trace["history_tokens"]))
        scores = retention_table(self.system.store(self.user), self.clock.now(), self.system.settings)
        if scores:
            lowest = sorted(scores.items(), key=lambda kv: kv[1])[:5]
            self.out("保留分最低的 5 条：%s" % ", ".join("%s %.2f" % kv for kv in lowest))
        totals = self.ledger.totals()
        records = self.ledger.records()
        known = all(r.cost_known is True or r.cost_known is None and r.cost > 0 for r in records)
        currencies = {r.pricing.get("currency") for r in records if r.cost}
        cost = "%.4f" % totals["cost"] if known and len(currencies) <= 1 else "未完整估算"
        self.out("本次运行的模型用量：%d 次调用 · 输入 %d · 输出 %d token · 费用 %s" % (
            totals["calls"], totals["prompt_tokens"], totals["completion_tokens"], cost))

    def _model(self, arg: str) -> None:
        if self.config is None:
            self.out("--fake 模式下没有可切换的模型。")
            return
        if arg not in self.config.models:
            self.out("可用模型：%s" % ", ".join(sorted(self.config.models)))
            return
        self.system.models = dataclasses.replace(self.system.models, chat=self.config.client(arg, self.ledger),
                                                 names=dict(self.system.models.names, chat=arg))
        self.out("回答模型已切换为 %s；记忆数据不受影响。" % arg)

    def _confirm(self, arg: str) -> None:
        parts = arg.split()
        if not parts:
            items = self.system.pending(self.user)
            if not items:
                self.out("没有待确认的冲突。")
            for item in items:
                self.out("#%d F%d「%s」是否取代 F%d「%s」？" % (
                    item["id"], item["new"].id, item["new"].text, item["old"].id, item["old"].text))
            return
        if len(parts) != 2 or parts[1].lower() not in ("yes", "no", "y", "n"):
            self.out("用法：/confirm 编号 yes|no")
            return
        self.out(self.system.confirm(self.user, int(parts[0]), parts[1].lower().startswith("y")))


def _fact_line(fact: Fact) -> str:
    status = {"current": "当前", "superseded": "已被取代", "archived": "已归档", "forgotten": "已遗忘"}.get(
        fact.status, fact.status)
    extra = " → F%d" % fact.superseded_by if fact.superseded_by else ""
    flag = " · 存疑" if fact.uncertain else ""
    return "%-5s [%s · %s%s%s · 置信 %.1f] %s" % (fact.key, fact.when().label(), status, extra, flag,
                                                fact.confidence, fact.text)


def format_trace(trace: dict) -> str:
    plan = trace["plan"]
    lines = ["查询计划（%s）：" % plan["source"],
             "  检索语句：%s" % " | ".join(plan["queries"]),
             "  关键词：%s" % (", ".join(plan["keywords"]) or "—"),
             "  时间范围：%s · 需要全部召回：%s · 关注：%s" % (
                 " ~ ".join(plan["time_range"]) if plan["time_range"] else "无",
                 "是" if plan["needs_all"] else "否", plan["focus"])]
    if trace.get("used_archive"):
        lines.append("  （相关记忆太少，已到归档记忆里兜底检索）")
    lines.append("候选（前 10，分数 = 向量和关键词排名融合后再加权）：")
    for row in trace["candidates"][:10]:
        lines.append("  %-5s %.4f 相似度 %.2f%s [%s] %s" % (
            row["key"], row["score"], row["sim"], " lexical#%d" % row["bm25"] if row["bm25"] else "",
            "+".join(row["via"]), row["preview"]))
        if row.get("matched_chunks"):
            lines.append("        命中片段：" + "; ".join("%s[%d:%d] 相似度 %.2f" % (
                c["role"], c["start"], c["end"], c["sim"]) for c in row["matched_chunks"]))
    lines.append("选入上下文：事实 %s；原文 %s" % (", ".join(trace["selected"]["facts"]) or "无",
                                              ", ".join(trace["selected"]["episodes"]) or "无"))
    omitted = trace.get("omitted", {})
    if omitted.get("facts") or omitted.get("episodes"):
        lines.append("未选入的条目：事实 %s；原文 %s（预算、版本或条数限制）" % (
            ", ".join(omitted.get("facts", [])) or "无", ", ".join(omitted.get("episodes", [])) or "无"))
    budget = "预算 %d" % trace.get("budget", trace.get("effective_budget", 0))
    if trace.get("effective_budget") and trace["effective_budget"] < trace["budget"]:
        budget += "，历史较短，这次最多 %d" % trace["effective_budget"]
    unit = trace.get("budget_counter", {}).get("unit", "tokens")
    lines.append("上下文 %d %s / 历史 %d %s（%s）" % (trace.get("context_tokens", trace["memory_tokens"]), unit, trace.get("history_tokens", 0), unit, budget))
    if trace.get("notes"):
        lines.append("模型笔记：")
        lines += ["  " + line for line in trace["notes"].splitlines()]
    if trace.get("aggregation"):
        audit = trace["aggregation"]
        lines.append("聚合核验：%s（仅核对引用、单位、显式重复与算术，完整性和事件归属仍需模型判断）" % audit["status"])
        if audit.get("errors"):
            lines += ["  " + error for error in audit["errors"]]
        if audit.get("repair_error"):
            lines.append("  修正调用失败：" + audit["repair_error"])
    return "\n".join(lines)


def cmd_chat(args: argparse.Namespace) -> int:
    ledger = UsageLedger(args.usage_log)
    config, models, settings = _load(args, ledger)
    _need_chat(models, "对话入口")
    settings = dataclasses.replace(settings, confirm_conflicts=True)   # 对话模式才有人可问
    system = MemorySystem(models, settings, data_dir=args.data_dir, storage=getattr(args, "storage_settings", None),
                          trace_dir=os.path.join(args.data_dir, "traces"))
    if args.at and args.clock != "sim":
        raise SystemExit("--at 只对模拟时钟有效，请同时加 --clock sim。")
    clock = SimClock(parse_datetime(args.at) if args.at else RealClock().now()) if args.clock == "sim" else RealClock()
    # 空闲超时只对真实时钟有意义；模拟时钟用 /time 推进时会先结束会话
    idle = args.idle_minutes if args.clock == "real" else 0
    app = ChatApp(system, args.user, clock, config, ledger, idle_minutes=idle)
    app.start()
    try:
        if args.script:
            with open(args.script, encoding="utf-8") as fh:
                for line in fh:
                    line = line.rstrip("\n")
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    print("你 > " + line)
                    if not app.handle(line):
                        break
                else:
                    app.shutdown()
        else:
            _repl(app, sys.stdin)
    finally:
        system.close()
    return 0


def _repl(app: ChatApp, stream: TextIO) -> None:
    while True:
        try:
            line = input("你 > ") if stream.isatty() else stream.readline()
        except (EOFError, KeyboardInterrupt):
            print()
            app.shutdown()
            return
        if not stream.isatty():
            if not line:
                app.shutdown()
                return
            print("你 > " + line.rstrip("\n"))
        if not app.handle(line):
            return


# ----------------------------------------------------------------- bench
def _items(args: argparse.Namespace):
    from .bench.longmemeval import load
    pairs = load(args.data)
    if getattr(args, "ids", None):
        wanted = set(args.ids.split(","))
        pairs = [p for p in pairs if p[0].question_id in wanted]
    if getattr(args, "limit", None):
        pairs = pairs[:args.limit]
    return pairs


def _data_info(path: str) -> dict:
    """数据版本：文件名和内容哈希，写进 run_config，报告按手册要求披露。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return {"file": os.path.basename(path), "sha256": digest.hexdigest()[:16]}


def cmd_bench_run(args: argparse.Namespace) -> int:
    from .bench.runner import ConfigChanged, Runner
    os.makedirs(args.out, exist_ok=True)
    ledger = UsageLedger(os.path.join(args.out, "usage.jsonl"))
    _, models, settings = _load(args, ledger)
    _need_chat(models, "评测")
    print("模型：" + "；".join("%s=%s（%s）" % (role, models.names.get(role) or "—", models.sources.get(role, "none"))
                             for role in ("chat", "memory", "embedding")) + "；档位 %s" % models.tier)
    pairs = _items(args)
    if args.mode != "memory":
        print("注意：%s 是对照组，结果只能用于对比，不能作为提交。" % args.mode)
    runner = Runner(models, settings, args.out, mode=args.mode, reingest=args.reingest, progress=print,
                    data_info=_data_info(args.data), allow_config_change=args.allow_config_change,
                    max_wall_s=args.max_wall_s, stop_after_service_errors=args.stop_after_service_errors)
    try:
        counts = runner.run([item for item, _ in pairs], workers=args.workers)
    except ConfigChanged as exc:
        print(exc)
        return 2
    if runner.aborted:
        print("已停止：执行预算、连续服务故障或账户错误，剩下 %d 题没有开始。" % counts["not_started"])
        print("  " + runner.aborted)
        print("处理好之后用同样的命令续跑。")
    print("完成：新答 %(answered)d 题，出错 %(errors)d 题，跳过已有答案 %(skipped)d 题。" % counts)
    print("答案：%s" % os.path.join(args.out, "hypotheses.jsonl"))
    if counts["errors"]:
        print("出错的题记在 errors.jsonl，修好后用同样的命令续跑即可。")
    return 0 if counts["errors"] == 0 and not runner.aborted else 1


def cmd_bench_judge(args: argparse.Namespace) -> int:
    from .bench.judge import judge
    run_dir = os.path.dirname(os.path.abspath(args.hyp))
    ledger = UsageLedger(os.path.join(run_dir, "usage.jsonl"))
    golds = {gold.question_id: gold for _, gold in _items(args)}
    if args.offline:
        from .bench.judge import judge_offline
        out = judge_offline(args.hyp, golds)
        print("近似评分结果（离线规则，偏好题不评，不是官方口径）：%s" % out)
        return 0
    if args.fake:
        model, name = HeuristicChat(), "fake"
    else:
        config, _, _ = _load(args, ledger)
        name = args.judge or config.roles.get("judge")
        if not name:
            raise SystemExit("请用 --judge 指定评分模型，或在配置的 roles 里设置 judge。")
        model = config.client(name, ledger)
    from .bench.judge import JudgeIncomplete
    try:
        out = judge(args.hyp, golds, model, name, workers=args.workers)
    except JudgeIncomplete as exc:
        print("评分结果：%s" % exc.out_path)
        print("%s。用同样的命令重跑即可补上，已评过的题不会重复评分。" % exc)
        return 1
    print("评分结果：%s" % out)
    print("提示：正式成绩以官方 evaluate_qa.py + GPT-4o 统一复跑为准。")
    return 0


def cmd_bench_report(args: argparse.Namespace) -> int:
    from .bench.report import build_report
    golds = {gold.question_id: gold for _, gold in _items(args)}
    text = build_report(args.run, golds, args.eval_log)
    out = args.out or os.path.join(args.run, "report.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    print("\n报告已保存到 %s" % out)
    return 0


def cmd_bench_load(args: argparse.Namespace) -> int:
    """把一道题的历史按时间灌进对话入口的记忆库，之后用 memsys chat --user <题号> 当场追问。"""
    ledger = UsageLedger(args.usage_log)
    _, models, settings = _load(args, ledger)
    pairs = _items(args)
    if not pairs:
        raise SystemExit("数据里没有题号 %s" % args.ids)
    item = pairs[0][0]
    user = args.user or item.question_id
    system = MemorySystem(models, settings, data_dir=args.data_dir, storage=getattr(args, "storage_settings", None))
    try:
        system.recover(user)    # 上次载入到一半中断的会话先补整理
        loaded = set(system.store(user).external_ids())
        for session in item.sessions:
            if session.session_id in loaded:   # 重复执行不会把同一段历史灌两遍
                print("%s %s：已载入过，跳过" % (format_stamp(session.date), session.session_id))
                continue
            handle = system.open_session(user, session.date, session.session_id)
            for turn in session.turns:
                system.add_turn(handle, "user" if turn.role == "user" else "assistant", turn.content, session.date)
            report, _ = system.close_session(handle, session.date)
            print("%s %s：%s" % (format_stamp(session.date), session.session_id, report.summary()))
    finally:
        system.close()
    print('已载入。追问：memsys chat --user %s --clock sim --at "%s"' % (
        user, item.question_date.strftime("%Y-%m-%d %H:%M")))
    print("原问题：%s" % item.question)
    return 0


def cmd_bench_scale(args: argparse.Namespace) -> int:
    from .bench.scale import measure, to_markdown
    sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
    if not sizes or min(sizes) < 2:
        raise SystemExit("--sizes 要写成逗号分隔的条数，每个至少 2，例如 100,1000,5000")
    rows = measure(sizes, dim=args.dim, queries=args.queries, progress=print)
    text = to_markdown(rows, args.dim, args.queries)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("已保存到 %s" % args.out)
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    ledger = UsageLedger(args.usage_log)
    _, models, settings = _load(args, ledger)
    if models.tier == "L0":
        raise SystemExit("还是没有记忆模型，补不了事实。memsys doctor 可以查看找到了什么。")
    system = MemorySystem(models, settings, data_dir=args.data_dir, storage=getattr(args, "storage_settings", None))
    try:
        before = system.store(args.user).backlog_count()
        reports = system.rebuild(args.user)
        for report in reports:
            print("%s：%s" % (report.session_id, report.summary()))
        print("补抽完成：%d 轮原文，%d 个会话。" % (before, len(reports)))
    finally:
        system.close()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ledger = UsageLedger()
    _, models, settings = _load(args, ledger)
    failed = False
    print("配置文件：%s" % (args.config_path or "没有（全部自动查找）"))
    for line in describe(models):
        print(line)
    print("检索：普通角色分块 · 重排 %s" % settings.rerank_backend)
    if settings.rerank_backend == "bge":
        print("  本地模型 %s @ %s · %s/%s · batch %d · strict=%s" % (
            settings.rerank_model, settings.rerank_revision, settings.rerank_device,
            settings.rerank_dtype, settings.rerank_batch_size, settings.rerank_strict))
    if args.check:
        print("实际调用检查：")
        for role, model in (("memory", models.memory), ("chat", models.chat)):
            if model is None:
                continue
            try:
                reply = model.chat([{"role": "user", "content": "Reply with the single word OK."}],
                                   purpose="check", max_tokens=16).text
                print("  %-9s 正常（回复：%s）" % (role, reply.strip()[:40]))
            except Exception as exc:
                failed = True
                print("  %-9s 失败：%s" % (role, str(exc)[:200]))
        try:
            vector = models.embedder.embed(["hello"], purpose="check")[0]
            print("  %-9s 正常（%d 维）" % ("embedding", len(vector)))
        except Exception as exc:
            failed = True
            print("  %-9s 失败：%s" % ("embedding", str(exc)[:200]))
        if settings.rerank_backend == "bge":
            try:
                from .reranking import RerankDocument, local_reranker
                _, trace = local_reranker(settings).rank("Which mineral is in tray A?", [
                    RerankDocument("check", "The mineral in tray A is fluorite.")])
                print("  %-9s 正常（%s · %d 配对窗口）" % ("rerank", trace['device'], trace['pair_windows']))
            except Exception as exc:
                failed = True
                print("  %-9s 失败：%s" % ("rerank", str(exc)[:200]))
    print("主存储：Qdrant（地址与命名空间由 storage 配置决定）；旧 .sqlite 文件不会自动加载。")
    return 1 if failed else 0


def cmd_init(args: argparse.Namespace) -> int:
    from .discover import config_from_discovery, discovered_configs
    out = args.out
    if os.path.exists(out) and not args.force:
        raise SystemExit("%s 已经存在；要覆盖就加 --force。" % out)
    data = config_from_discovery(discovered_configs())
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("已写入 %s。" % out)
    config = AppConfig.from_dict(data)
    for line in describe(config.build_models(UsageLedger(), probe=False)):
        print(line)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .serve import Borrowed, BorrowedChat, MemoryProxy, MemoryService
    ledger = UsageLedger(args.usage_log)
    borrowed, host = None, None
    if args.upstream and not args.no_borrow:
        borrowed = Borrowed(args.upstream, model=args.memory_model)
        chat = BorrowedChat(borrowed, ledger)
        host = HostModels(memory=chat, names={"memory": chat.name, "chat": chat.name}, source="upstream")
    _, models, settings = _load(args, ledger, host=host)
    service = MemoryService(models, settings, args.data_dir, idle_minutes=args.idle_minutes, storage=args.storage_settings)
    server = MemoryProxy((args.host, args.port), service, args.upstream, borrowed, default_user=args.user)
    base = "http://%s:%d" % (args.host, server.server_address[1])
    print("memsys serve 已启动：%s（Qdrant 主存储，本地目录仅放轨迹 %s）" % (base, args.data_dir))
    for line in describe(models):
        print(line)
    if args.upstream:
        print("上游：%s" % args.upstream)
        print("  OpenAI 兼容的 agent：把 base_url 改成 %s/v1" % base)
        print("  Anthropic 接口的 agent：把 base URL（例如 ANTHROPIC_BASE_URL）改成 %s" % base)
    print("记忆接口：POST %s/memsys/recall | /memsys/turns | /memsys/end | /memsys/forget；GET /memsys/status" % base)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("\n正在整理还没结束的会话……")
        service.shutdown()
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp import McpServer
    ledger = UsageLedger(args.usage_log)

    def build(sampling):
        host = None
        if sampling is not None:
            host = HostModels(memory=sampling, names={"memory": "mcp-sampling", "chat": "mcp-sampling"},
                              source="mcp-sampling")
        _, models, settings = _load(args, ledger, host=host)
        return MemorySystem(models, settings, data_dir=args.data_dir, storage=getattr(args, "storage_settings", None),
                            trace_dir=os.path.join(args.data_dir, "traces"))

    McpServer(build, args.user, RealClock().now, use_sampling=not args.no_sampling).serve()
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    """Claude Code 的 hook。出任何错都只往 stderr 写一句、照常退出 0：记忆不能挡住 Claude Code。"""
    from .hook import handle
    try:
        event = json.loads(sys.stdin.read() or "{}")
        ledger = UsageLedger(args.usage_log)
        _, models, settings = _load(args, ledger)
        system = MemorySystem(models, settings, data_dir=args.data_dir, storage=getattr(args, "storage_settings", None),
                              trace_dir=os.path.join(args.data_dir, "traces"))
        try:
            out = handle(event, system, args.user, RealClock().now())
        finally:
            system.close()
    except (Exception, SystemExit) as exc:
        print("memsys hook: %s" % exc, file=sys.stderr)
        return 0
    if out:
        sys.stdout.write(out + "\n")
    return 0


def cmd_bench_leakcheck(args: argparse.Namespace) -> int:
    from .bench.leakcheck import check, project_files, to_text
    root = args.root or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    leaks = check(args.data, project_files(root))
    print(to_text(leaks))
    return 1 if leaks else 0


def cmd_bench_behavior(args: argparse.Namespace) -> int:
    from .bench.behavior import run_behavior
    if args.fake:
        raise SystemExit("回答行为回归需要真实回答模型；离线替身只能验证流程，不能评估生成策略。")
    os.makedirs(args.out, exist_ok=True)
    ledger = UsageLedger(os.path.join(args.out, "usage.jsonl"))
    _, models, _ = _load(args, ledger)
    _need_chat(models, "回答行为回归")
    report = run_behavior(models.chat)
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("自拟回答回归：回答 %d / %d，正确 %d，答错 %d，调用失败 %d，未开始 %d（%s）。" % (
        report["answered"], report["total"], report["passed"], report["incorrect"], report["errors"],
        report["unstarted"], report["status"]))
    for row in report["results"]:
        if row.get("error"):
            print("  %s: %s" % (row["case"], row["error"]))
    return 1 if report["failed"] or report["unstarted"] else 0


def cmd_bench_mechanisms(args: argparse.Namespace) -> int:
    from .bench.mechanisms import run_mechanisms
    if args.fake:
        raise SystemExit("记忆机制回归需要真实记忆与回答模型；替身不能证明抽取、更新或回答效果。")
    os.makedirs(args.out, exist_ok=True)
    ledger = UsageLedger(os.path.join(args.out, "usage.jsonl"))
    _, models, settings = _load(args, ledger)
    report = run_mechanisms(models, settings, args.out)
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("自拟记忆机制回归：回答 %d / %d，正确 %d，答错 %d，调用失败 %d，未开始 %d（%s）。" % (
        report["answered"], report["total"], report["passed"], report["incorrect"], report["errors"],
        report["unstarted"], report["status"]))
    for row in report["results"]:
        if row.get("error"):
            print("  %s (%s): %s" % (row["case"], row["stage"], row["error"]))
    return 1 if report["incorrect"] or report["errors"] or report["unstarted"] else 0


def cmd_reindex(args: argparse.Namespace) -> int:
    ledger = UsageLedger(args.usage_log)
    _, models, settings = _load(args, ledger)
    system = MemorySystem(models, settings, data_dir=args.data_dir, storage=getattr(args, "storage_settings", None))
    try:
        if getattr(args, "rollback", False):
            generation = system.rollback_index(args.user, getattr(args, "generation", None))
            print("已回滚检索索引至代 %d；请同时恢复该代对应配置。" % generation)
            return 0
        count = system.reindex(args.user)
        coverage = system.stats(args.user)["index_coverage"]
        print("已重建 %d 条记忆的向量；原文分段向量 %d 条。" % (count, coverage["chunk_vectors"]))
        print("索引代：%s" % json.dumps(system.store(args.user).index_generations(), ensure_ascii=False))
    finally:
        system.close()
    return 0


def cmd_maintenance(args: argparse.Namespace) -> int:
    from .maintenance import apply_cleanup, cleanup_plan, queue_repair, repair_plan
    path = _config_path(args)
    from .qdrant import QdrantSettings
    storage = AppConfig.load(path).storage if path else QdrantSettings.environment()
    store = Store(user_id=args.user, settings=storage, max_bytes=getattr(args, "max_bytes", None))
    try:
        if getattr(args, "plan", None):
            with open(args.plan, encoding="utf-8") as stream:
                plan = json.load(stream)
                plan = plan.get("plan", plan)
        elif args.command == "repair":
            plan = repair_plan(store, args.session, args.failure_log)
        else:
            plan = cleanup_plan(store, args.archive_before)
        result = {"mode": "preview", "plan": plan}
        if args.apply:
            if args.command == "repair":
                result = {"mode": "queued", "plan": plan, "episodes": queue_repair(store, plan),
                          "next": "run memsys rebuild with this tenant's user and storage configuration"}
            else:
                backup = args.backup or os.path.join(memsys_home(), _safe_name(args.user) + ".qdrant-backup-") + datetime.now().strftime("%Y%m%dT%H%M%S%f")
                os.makedirs(os.path.dirname(os.path.abspath(backup)), exist_ok=True)
                result = {"mode": "applied", "plan": plan, "result": apply_cleanup(store, plan, backup, offline=args.offline)}
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as stream:
                stream.write(text + "\n")
        print(text)
        return 0 if result.get("result", {}).get("cap_met", True) else 1
    finally:
        store.close()


def cmd_migrate(args):
    from .migrate import import_sqlite
    from .qdrant import QdrantSettings
    path = _config_path(args)
    storage = AppConfig.load(path).storage if path else QdrantSettings.environment()
    if args.namespace:
        storage.namespace = args.namespace
    store = Store(user_id=args.user, settings=storage)
    try:
        print(json.dumps(import_sqlite(args.source, store), ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memsys", description="长期交互式 AI 记忆系统")
    home, default_user = memsys_home(), os.environ.get("MEMSYS_USER", "default")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="配置文件（默认读当前目录的 config.json）")
    common.add_argument("--fake", action="store_true", help="用离线替身模型演练流程，不需要 API key")
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser("migrate-sqlite", parents=[common], help="显式只读导入旧 SQLite；不抽取、不重算向量")
    migrate.add_argument("--source", required=True)
    migrate.add_argument("--user", required=True)
    migrate.add_argument("--namespace")
    migrate.set_defaults(func=cmd_migrate)

    repair = sub.add_parser("repair", parents=[common], help="预览或排队旧版本遗漏的抽取任务，不调用模型")
    repair.add_argument("--user", required=True)
    repair.add_argument("--session", action="append", default=[])
    repair.add_argument("--failure-log")
    repair.add_argument("--apply", action="store_true", help="明确将所选原文排队，随后用 rebuild 补抽")
    repair.add_argument("--out")
    repair.set_defaults(func=cmd_maintenance)

    cleanup = sub.add_parser("cleanup", parents=[common], help="预览物理清理；显式应用时先备份，再清理和压缩")
    cleanup.add_argument("--user", required=True)
    cleanup.add_argument("--offline", action="store_true", help="确认所有客户端已停机后执行物理清理")
    cleanup.add_argument("--archive-before")
    cleanup.add_argument("--max-bytes", type=int)
    cleanup.add_argument("--plan", help="已复核的 cleanup 计划 JSON（plan 对象）")
    cleanup.add_argument("--backup")
    cleanup.add_argument("--apply", action="store_true")
    cleanup.add_argument("--out")
    cleanup.set_defaults(func=cmd_maintenance)

    chat = sub.add_parser("chat", parents=[common], help="对话入口")
    chat.add_argument("--user", default="default")
    chat.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    chat.add_argument("--clock", choices=("real", "sim"), default="real")
    chat.add_argument("--at", help="模拟时钟的起点，例如 2026-10-01 或 \"2026-10-01 09:00\"")
    chat.add_argument("--script", help="按行读取输入（含命令），用于可重复的演示")
    chat.add_argument("--idle-minutes", type=float, default=30,
                      help="真实时钟下空闲超过这么久，下一句话前先结束上一个会话（0 表示不自动结束）")
    chat.add_argument("--usage-log", help="把模型用量追加写入这个 JSONL")
    chat.set_defaults(func=cmd_chat)

    reindex = sub.add_parser("reindex", parents=[common], help="换 embedding 模型后重建向量")
    reindex.add_argument("--user", default="default")
    reindex.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    reindex.add_argument("--usage-log")
    reindex.add_argument("--rollback", action="store_true", help="恢复上一检索索引代；同时恢复该代配置")
    reindex.add_argument("--generation", type=int, help="要恢复的索引代；与 --rollback 同用")
    reindex.set_defaults(func=cmd_reindex)

    rebuild = sub.add_parser("rebuild", parents=[common], help="有了记忆模型后，给降级期间存下的原文补抽事实")
    rebuild.add_argument("--user", default="default")
    rebuild.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    rebuild.add_argument("--usage-log")
    rebuild.set_defaults(func=cmd_rebuild)

    doctor = sub.add_parser("doctor", parents=[common], help="查看用的是哪些模型、从哪找到的、现在是哪一档")
    doctor.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    doctor.add_argument("--check", action="store_true", help="真的调用一次各个模型，确认能用")
    doctor.set_defaults(func=cmd_doctor)

    init = sub.add_parser("init", help="按当前环境（环境变量里的 key、本机 Ollama）生成 config.json")
    init.add_argument("--out", default=DEFAULT_CONFIG)
    init.add_argument("--force", action="store_true", help="覆盖已有的配置文件")
    init.set_defaults(func=cmd_init)

    serve = sub.add_parser("serve", parents=[common], help="OpenAI / Anthropic 兼容代理 + 记忆 HTTP 接口")
    serve.add_argument("--upstream", help="原来的模型接口地址，例如 https://api.deepseek.com/v1、"
                                          "https://api.anthropic.com、http://localhost:11434/v1；不给就只提供 /memsys 接口")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8848)
    serve.add_argument("--user", default=default_user, help="请求里没带用户身份时用这个")
    serve.add_argument("--data-dir", default=home)
    serve.add_argument("--memory-model", help="记忆模型用上游的哪个模型；默认跟 agent 用同一个")
    serve.add_argument("--no-borrow", action="store_true", help="不借上游的模型和 key，只用配置、环境变量或 Ollama")
    serve.add_argument("--idle-minutes", type=float, default=30, help="空闲多久算会话结束（0 表示只靠 /memsys/end）")
    serve.add_argument("--usage-log")
    serve.set_defaults(func=cmd_serve)

    mcp = sub.add_parser("mcp", parents=[common], help="MCP server（标准输入输出）")
    mcp.add_argument("--user", default=default_user)
    mcp.add_argument("--data-dir", default=home)
    mcp.add_argument("--no-sampling", action="store_true", help="不借客户端的模型（MCP sampling）")
    mcp.add_argument("--usage-log")
    mcp.set_defaults(func=cmd_mcp)

    hook = sub.add_parser("hook", parents=[common], help="Claude Code 的 hook（事件 JSON 从标准输入读）")
    hook.add_argument("--user", default=default_user)
    hook.add_argument("--data-dir", default=home)
    hook.add_argument("--usage-log")
    hook.set_defaults(func=cmd_hook)

    bench = sub.add_parser("bench", help="LongMemEval 评测").add_subparsers(dest="bench_command", required=True)
    behavior = bench.add_parser("behavior", parents=[common], help="用自拟固定记忆检查回答行为（真实模型调用）")
    behavior.add_argument("--out", required=True, help="回归报告与用量账本的输出目录")
    behavior.set_defaults(func=cmd_bench_behavior)
    mechanisms = bench.add_parser("mechanisms", parents=[common], help="用自拟原始会话检查记忆机制（真实模型调用）")
    mechanisms.add_argument("--out", required=True, help="机制回归报告、轨迹与用量账本的输出目录")
    mechanisms.set_defaults(func=cmd_bench_mechanisms)
    run = bench.add_parser("run", parents=[common], help="回放跑分")
    run.add_argument("--data", required=True, help="longmemeval_oracle.json")
    run.add_argument("--out", required=True, help="输出目录，例如 runs/oracle-v1")
    run.add_argument("--mode", choices=("memory", "full-context", "no-memory"), default="memory")
    run.add_argument("--workers", type=int, default=1)
    run.add_argument("--max-wall-s", type=float, default=3600, help="整批执行预算；冒烟用600，整批用3600")
    run.add_argument("--stop-after-service-errors", type=int, default=2, help="连续服务故障达到此数量即停止")
    run.add_argument("--limit", type=int)
    run.add_argument("--ids", help="只跑这些题号，逗号分隔")
    run.add_argument("--reingest", action="store_true", help="忽略写入缓存，重新整理记忆")
    run.add_argument("--allow-config-change", action="store_true",
                     help="续跑时允许配置和已有答案不同（旧配置记进 run_config.json 的 history）")
    run.set_defaults(func=cmd_bench_run)

    judge = bench.add_parser("judge", parents=[common], help="本地评分（官方提示词）")
    judge.add_argument("--data", required=True)
    judge.add_argument("--hyp", required=True, help="hypotheses.jsonl")
    judge.add_argument("--judge", help="评分模型名（配置里的名字），默认用 roles.judge")
    judge.add_argument("--workers", type=int, default=4)
    judge.add_argument("--offline", action="store_true", help="不调用模型、按规则近似评分（没有评分模型可用时救急）")
    judge.set_defaults(func=cmd_bench_judge)

    report = bench.add_parser("report", help="生成评估报告")
    report.add_argument("--data", required=True)
    report.add_argument("--run", required=True, help="bench run 的输出目录")
    report.add_argument("--eval-log", help="评分结果文件；默认找 hypotheses.jsonl.eval-results-*")
    report.add_argument("--out", help="报告路径，默认 <run>/report.md")
    report.set_defaults(func=cmd_bench_report)

    load = bench.add_parser("load", parents=[common], help="把一道题的历史灌进对话记忆库")
    load.add_argument("--data", required=True)
    load.add_argument("--ids", required=True, help="题号")
    load.add_argument("--user", help="灌进哪个用户，默认用题号")
    load.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    load.add_argument("--usage-log")
    load.set_defaults(func=cmd_bench_load)

    scale = bench.add_parser("scale", help="合成数据上测检索延迟随规模的变化（不调模型）")
    scale.add_argument("--sizes", default="100,1000,5000,10000", help="记忆条数，逗号分隔")
    scale.add_argument("--dim", type=int, default=1024, help="向量维度，和实际用的 embedding 模型一致（bge-m3 为 1024）")
    scale.add_argument("--queries", type=int, default=30, help="每个规模检索多少次")
    scale.add_argument("--out", help="把 Markdown 表格另存到这个文件")
    scale.set_defaults(func=cmd_bench_scale)

    leak = bench.add_parser("leakcheck", help="检查提示词和代码里有没有评测数据的内容（题号、题目、答案、对话里的名称）")
    leak.add_argument("--data", required=True)
    leak.add_argument("--root", help="项目根目录，默认是这份代码所在的仓库")
    leak.set_defaults(func=cmd_bench_leakcheck)
    return parser


def load_dotenv(path: str = ".env") -> None:
    """读取 KEY=VALUE 形式的 .env（已在 .gitignore 里），不覆盖已经存在的环境变量。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def main(argv: Optional[List[str]] = None) -> int:
    load_dotenv()
    load_dotenv(os.path.join(memsys_home(), ".env"))   # hook / mcp 在别的目录下运行时也能读到 key
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
