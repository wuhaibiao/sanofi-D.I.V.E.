#!/usr/bin/env python3
"""Full Oracle replay using the existing physically isolated question executor.

Preparation mechanically partitions all rows; no question or label is printed.
Only this evaluation adapter records telemetry. Product behavior is unchanged.
"""
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import evaluate_public_types as worker
import question_serial_eval as serial
from blind_eval_inputs import FILES, materialize
from check_webchat_reliability import credentials
from memsys.bench.provenance import model_manifest, prompt_fingerprint, source_manifest
from memsys.config import AppConfig
from memsys.llm import UsageLedger
from memsys.qdrant import Qdrant

TYPES = ('single-session-user', 'single-session-assistant', 'single-session-preference',
         'multi-session', 'knowledge-update', 'temporal-reasoning')
TOOLS = ('run_oracle500.py', 'evaluate_public_types.py', 'blind_eval_inputs.py',
         'question_serial_eval.py', 'capture_relational_eval_storage.py', 'check_webchat_reliability.py')


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()] if Path(path).exists() else []


def save(path, value):
    worker.save(Path(path), value)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def partition(pool, expected=500):
    """Keep original IDs/order, including abstention siblings; never sample."""
    ids = [row['question_id'] for row in pool]
    if len(ids) != expected or any(not isinstance(q, str) or not q for q in ids) or len(set(ids)) != expected:
        raise ValueError('Require exact unique original question IDs')
    selection = []
    for index, row in enumerate(pool, 1):
        if row['question_type'] not in TYPES:
            raise ValueError('Unsupported question type')
        selection.append(dict(index=index, question_id=row['question_id'], source_question_id=row['question_id'],
            dataset='LongMemEval', category=row['question_type'], judge_type=row['question_type'],
            abstention=row['question_id'].endswith('_abs'), corpus_id='oracle-case-' + str(index).zfill(4)))
    return selection


def validate(out):
    out = Path(out)
    frozen = read(out / 'freeze.json')
    if source_manifest()['source_sha256'] != frozen['source']['source_sha256']:
        raise ValueError('Product source changed during frozen evaluation')
    for name, sha in frozen['tools'].items():
        if digest(ROOT / 'scripts' / name) != sha:
            raise ValueError('Evaluation tool changed: ' + name)
    for name, sha in frozen['files'].items():
        if digest(out / name) != sha:
            raise ValueError('Frozen input changed: ' + name)
    return frozen


def prepare(args):
    if args.out.exists():
        raise ValueError('Use a fresh run directory; never replace prior artifacts')
    pool = read(args.data)
    selection = partition(pool)
    if sum(m['abstention'] for m in selection) != 30:
        raise ValueError('Expected all 30 Oracle abstention instances')
    cfg = AppConfig.load(args.config)
    if cfg.fallbacks or cfg.memory.recall_mode != 'facts' or cfg.memory.query_policy != 'off':
        raise ValueError('Freeze current facts retrieval with no automatic model fallback')
    if not cfg.roles.get('embedding') or cfg.models[cfg.roles['embedding']].base_url == 'local:hash':
        raise ValueError('Explicit semantic embedding configuration required')
    args.out.mkdir(parents=True, mode=0o700)
    shutil.copy2(args.config, args.out / 'config.frozen.json')
    (args.out / 'config.frozen.json').chmod(0o600)
    save(args.out / 'selection.json', selection)
    phase_hashes = materialize(args.out, pool, selection)
    frozen = dict(selected_at=datetime.now(timezone.utc).isoformat(), source=source_manifest(),
        prompt_sha256=prompt_fingerprint(), config_sha256=digest(args.out / 'config.frozen.json'),
        selection_sha256=digest(args.out / 'selection.json'),
        runner_sha256=digest(ROOT / 'scripts/evaluate_public_types.py'),
        blind_input_builder_sha256=digest(ROOT / 'scripts/blind_eval_inputs.py'),
        blind_input_sha256=phase_hashes, prefix='memsys_oracle500_' + uuid.uuid4().hex[:12],
        namespace='oracle500-' + uuid.uuid4().hex, total=len(selection),
        counts=dict(Counter(m['category'] for m in selection)), abstentions=30,
        indexing=dict(index_originals=cfg.memory.index_originals,
            episode_chunk_vectors=cfg.memory.episode_chunk_vectors,
            extract_context_memories=cfg.memory.extract_context_memories),
        data=dict(path=str(args.data), sha256=digest(args.data),
            url='https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json'),
        protocol='All original IDs in released order; chronological history per question; new isolated collections; '
            'write/audit/read/verified cleanup per question; judging only after all answers; '
            'no answer-based sampling, selective re-answering, product tuning or model fallback.',
        evaluation_status='Full public Oracle development evaluation; historical exposure disclosed; '
            'independent phase and session isolation; local semantic judge, not organizer official score.',
        judge_model=cfg.models[cfg.roles.get('judge', cfg.roles['chat'])].model,
        tools={name: digest(ROOT / 'scripts' / name) for name in TOOLS})
    serial.freeze_cases(args.out, frozen)
    frozen['files'] = {name: digest(args.out / name) for name in
        ('config.frozen.json', 'selection.json', 'question-cases.json', *FILES.values())}
    shutil.copytree(ROOT / 'src/memsys', args.out / 'frozen-src/memsys', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT / 'scripts', args.out / 'frozen-scripts', ignore=shutil.ignore_patterns('__pycache__'))
    frozen['packages'] = {name: importlib.metadata.version(name) for name in
        ('torch', 'transformers', 'numpy', 'tokenizers', 'huggingface-hub')}
    save(args.out / 'freeze.json', frozen)
    save(args.out / 'freeze.sha256.json', dict(sha256=digest(args.out / 'freeze.json')))
    save(args.out / 'progress.json', dict(state='prepared', total=500, answered=0, graded=0))
    print(json.dumps(dict(prepared=500, abstentions=30, counts=frozen['counts'],
        source_sha256=frozen['source']['source_sha256']), ensure_ascii=False), flush=True)
    report(args)


class CapturedChat(worker.CapturedChat):
    def __init__(self, client, path, session_scope=None):
        super().__init__(client, path, session_scope)
        self.response_path = path.with_name(path.name.replace('-prompts.jsonl', '-responses.jsonl'))
        self.provider_path = path.with_name(path.name.replace('-prompts.jsonl', '-provider.jsonl'))
        self.fatal_path = path.parent / 'provider-fatal.json'
        original = getattr(client, '_post', None)
        if original:
            def captured_post(endpoint, body, tags=None, **kwargs):
                began = time.time()
                event = dict(started_at=began, endpoint=endpoint, request=body, tags=tags, purpose=kwargs.get('purpose'))
                try:
                    data, stats = original(endpoint, body, tags, **kwargs)
                    event.update(response=data, stats=asdict(stats))
                    return data, stats
                except Exception as exc:
                    event.update(error_type=type(exc).__name__, error=str(exc), status=getattr(exc, 'status', None))
                    if getattr(exc, 'account_problem', False) or getattr(exc, 'quota_or_rate_limit', False):
                        save(self.fatal_path, {k: v for k, v in event.items() if k != 'request'})
                    raise
                finally:
                    event['elapsed_s'] = time.time() - began
                    worker.append(self.provider_path, event)
            client._post = captured_post

    def chat(self, messages, **kwargs):
        try:
            result = super().chat(messages, **kwargs)
        except Exception as exc:
            worker.append(self.response_path, dict(purpose=kwargs['purpose'], tags=kwargs.get('tags'),
                error_type=type(exc).__name__, error=str(exc)))
            raise
        worker.append(self.response_path, dict(purpose=kwargs['purpose'], tags=kwargs.get('tags'), result=asdict(result)))
        return result


def phase(args):
    """Only worker.load_phase opens phase data; telemetry cannot read labels."""
    worker.CapturedChat = CapturedChat
    base_system = worker.MemorySystem
    class TimedMemorySystem(base_system):
        def recall(self, *values, **kwargs):
            begin = time.monotonic()
            try:
                result = super().recall(*values, **kwargs)
                return result
            finally:
                worker.append(args.out / 'retrieval-timings.jsonl', dict(
                    question_id=(kwargs.get('tags') or {}).get('question_id'), elapsed_s=time.monotonic() - begin))
    worker.MemorySystem = TimedMemorySystem
    args.dataset = 'LongMemEval'
    {'write': worker.write, 'read': worker.answer, 'judge': worker.judge}[args.phase](args)
    if (args.out / 'provider-fatal.json').exists():
        raise RuntimeError('Account/quota failure retained; stopping before another question')


def preflight(args):
    """Verify real dependencies using authored input before any benchmark call."""
    frozen = validate(args.out)
    credentials(args.runtime)
    cfg = AppConfig.load(args.out / 'config.frozen.json')
    directory = args.out / 'preflight' / uuid.uuid4().hex[:12]
    directory.mkdir(parents=True)
    ledger = UsageLedger(str(directory / 'usage.jsonl'))
    models = cfg.build_models(ledger, env={}, probe=False)
    checks = {}
    began = time.monotonic()
    def check(name, operation):
        try:
            checks[name] = dict(passed=True, result=operation())
        except Exception as exc:
            checks[name] = dict(passed=False, error_type=type(exc).__name__, error=str(exc),
                status=getattr(exc, 'status', None))
        print('PREFLIGHT ' + name + ' ' + str(checks[name]['passed']), flush=True)
    check('qdrant', lambda: dict(collections=len(Qdrant(cfg.storage).request('GET', '/collections')['collections'])))
    def embedding():
        vector = models.embedder.embed(['Self-authored preflight: the fixture is blue.'])[0]
        if not models.semantic_embedding or len(vector) != 1024:
            raise ValueError('Expected deployed semantic BGE-M3 1024-dimensional embedding')
        return dict(dimensions=len(vector), semantic=True)
    check('embedding', embedding)
    def chat():
        client = CapturedChat(models.chat, directory / 'preflight-prompts.jsonl', frozen['prefix'] + ':preflight')
        reply = client.chat([dict(role='user', content='Reply with OK only.')], purpose='preflight', max_tokens=32)
        if not reply.text.strip() or reply.finish_reason not in ('stop', 'end_turn'):
            raise ValueError('Preflight reply did not complete')
        return dict(model=reply.model, finish_reason=reply.finish_reason)
    check('answer_model', chat)
    def rerank():
        from memsys.reranking import local_reranker, RerankDocument
        scores, _ = local_reranker(cfg.memory).rank('What color is the authored fixture?',
            [RerankDocument('authored', 'The authored fixture is blue.')])
        if len(scores) != 1:
            raise ValueError('Incomplete local reranker preflight')
        return dict(backend='bge', device=cfg.memory.rerank_device)
    check('reranker', rerank)
    proof = dict(passed=all(c['passed'] for c in checks.values()), checks=checks,
        config_sha256=frozen['config_sha256'], source_sha256=frozen['source']['source_sha256'],
        checked_at=datetime.now(timezone.utc).isoformat(), elapsed_s=time.monotonic()-began,
        directory=str(directory.relative_to(args.out)), models=model_manifest(models))
    save(directory / 'result.json', proof)
    save(args.out / 'preflight.json', proof)
    if not proof['passed']:
        raise RuntimeError('Preflight failed; no benchmark question was run')


def invoke(directory, phase_name, runtime):
    if phase_name == 'audit':
        return serial.invoke_phase(directory, phase_name, runtime)
    command = [sys.executable, str(Path(__file__).resolve()), 'phase', '--out', str(directory),
        '--phase', phase_name, '--runtime', str(runtime)]
    with (directory / ('LongMemEval-' + phase_name + '.log')).open('x') as log:
        return subprocess.call(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)


def export_answers(out, selection, answers):
    ids = [m['question_id'] for m in selection]
    if len({a['question_id'] for a in answers}) != len(answers) or set(a['question_id'] for a in answers) - set(ids):
        raise ValueError('Duplicate or foreign answers; refuse submission export')
    by_id = {a['question_id']: a for a in answers}
    complete = (len(by_id) == len(ids) and all(a.get('status') == 'answered' and
        isinstance(a.get('hypothesis'), str) and a['hypothesis'].strip() for a in answers))
    target = out / ('hypotheses.jsonl' if complete else 'hypotheses.partial.jsonl')
    if not complete and (out / 'hypotheses.jsonl').exists():
        raise ValueError('Refuse to replace an existing complete submission with incomplete results')
    contents = ''.join(json.dumps(dict(question_id=q, hypothesis=by_id[q]['hypothesis']), ensure_ascii=False) + '\n'
        for q in ids if q in by_id and by_id[q].get('status') == 'answered')
    target.write_text(contents)
    return complete


def statistics_row(values):
    if not values:
        return {}
    ordered = sorted(values)
    return dict(count=len(values), mean=statistics.mean(values), median=statistics.median(values),
        p95=ordered[min(round(.95 * (len(values)-1)), len(values)-1)], total=sum(values))


def report(args):
    """Read completed artifacts and metadata only; no gold input or models."""
    out = args.out
    frozen = read(out / 'freeze.json')
    selection = read(out / 'selection.json')
    cases = read(out / 'question-cases.json')
    directories = [out / c['directory'] for c in cases]
    answers = [row for d in directories for row in rows(d / 'LongMemEval-answers.jsonl')]
    grades = [row for d in directories for row in rows(d / 'LongMemEval-grades.jsonl')]
    if (len({g['question_id'] for g in grades}) != len(grades) or
            set(g['question_id'] for g in grades) - {m['question_id'] for m in selection}):
        raise ValueError('Duplicate or foreign judge records')
    answer_by_id = {a['question_id']: a for a in answers}
    grade_by_id = {g['question_id']: g for g in grades}
    ready = export_answers(out, selection, answers)
    labels_complete = len(grades) == len(selection) and all(isinstance(g.get('correct'), bool) for g in grades)
    def score(metas):
        correct = sum(grade_by_id.get(m['question_id'], {}).get('correct') is True for m in metas)
        return dict(total=len(metas), correct=correct,
            answered=sum(answer_by_id.get(m['question_id'], {}).get('status') == 'answered' for m in metas),
            judged=sum(grade_by_id.get(m['question_id'], {}).get('status') == 'graded' for m in metas),
            accuracy=correct / len(metas) if labels_complete and len(metas) else None)
    by_type = {t: score([m for m in selection if m['category'] == t]) for t in TYPES}
    abstention = score([m for m in selection if m['abstention']])
    ledger = UsageLedger()
    for directory in directories:
        for path in directory.glob('LongMemEval-*-usage.jsonl'):
            for record in UsageLedger.from_jsonl(str(path)).records():
                ledger.record(record)
    records = ledger.records()
    timing = [r['elapsed_s'] for d in directories for r in rows(d / 'retrieval-timings.jsonl')]
    execution = read(out / 'execution.json') if (out / 'execution.json').exists() else {}
    index_audits = [read(d / 'storage-audit.json') for d in directories if (d / 'storage-audit.json').exists()]
    index_totals = {key: sum(a.get(key, 0) for a in index_audits)
        for key in ('fact_vectors', 'source_episodes', 'episode_vectors', 'episode_chunks')}
    complete = len(answers) == len(selection) and labels_complete
    result = dict(total=len(selection), answered=sum(a['status'] == 'answered' for a in answers),
        attempted=len(answers), correct=sum(g.get('correct') is True for g in grades),
        graded=sum(g.get('status') == 'graded' for g in grades), by_type=by_type, abstention=abstention,
        submission_ready=ready, complete=complete, overall_accuracy=None, task_averaged_accuracy=None,
        answer_errors=[dict(question_id=a['question_id'], error=a.get('error')) for a in answers if a['status'] != 'answered'],
        judge_errors=[dict(question_id=g['question_id'], error=g.get('error')) for g in grades if g['status'] == 'judge_error'],
        usage=ledger.summary(), usage_totals=ledger.totals(),
        usage_missing_calls=sum(not r.usage_reported for r in records),
        cost_unknown_calls=sum(not r.cost_known for r in records),
        pricing_known=bool(records) and all(r.cost_known for r in records),
        retrieval_s=statistics_row(timing), answer_s=statistics_row([a['elapsed_s'] for a in answers]),
        elapsed_s=execution.get('elapsed_s'), cleanup=execution.get('cleanup'),
        indexing=dict(policy=frozen.get('indexing'), audited_questions=len(index_audits), **index_totals),
        updated_at=datetime.now(timezone.utc).isoformat(), evaluation_status=frozen['evaluation_status'])
    if complete:
        result['overall_accuracy'] = result['correct'] / len(selection)
        result['task_averaged_accuracy'] = statistics.mean(v['accuracy'] for v in by_type.values() if v['total'])
    save(out / 'metrics.json', result)
    text = ['# LongMemEval Oracle 500 题评测', '',
        f"状态：已回答 {result['answered']}/500；已尝试 {len(answers)}/500；已评分 {result['graded']}/500。", '',
        '完整提交文件：hypotheses.jsonl' if ready else '尚未形成完整提交，当前仅有 hypotheses.partial.jsonl。', '',
        f"Overall Accuracy：{result['overall_accuracy']}；Task-averaged Accuracy：{result['task_averaged_accuracy']}。", '',
        '未结束前不将已完成子集分数标为全量成绩；流程失败与未评分分别报告。', '',
        '| 题型 | 正确 | 已回答 | 已评分 | 总数 |', '|---|---:|---:|---:|---:|']
    for t, v in by_type.items():
        text.append(f"| {t} | {v['correct']} | {v['answered']} | {v['judged']} | {v['total']} |")
    text += ['', f"拒答子集：{abstention['correct']}/{abstention['total']}；accuracy={abstention['accuracy']}。", '',
        '## 用量与耗时', '', '| 用途 | 调用 | 输入 token | 输出 token | 模型耗时秒 |', '|---|---:|---:|---:|---:|']
    for purpose, u in result['usage'].items():
        text.append(f"| {purpose} | {u['calls']} | {u['prompt_tokens']} | {u['completion_tokens']} | {u['latency_s']:.2f} |")
    text += ['', f"总量：{json.dumps(result['usage_totals'])}", '',
        f"货币费用：{'见 usage 原始账本' if result['pricing_known'] else '未知：尚无完整可核实单价，不能把零值解释为免费'}。", '',
        f"检索完整路径秒（含查询向量、检索、BGE及上下文组装）：{json.dumps(result['retrieval_s'])}", '',
        f"读取加回答秒：{json.dumps(result['answer_s'])}；运行墙钟秒：{result['elapsed_s']}。", '',
        '## 配置与复现', '', f"判分模型：{frozen['judge_model']}（独立进程和会话；项目组正式成绩另行统一复评）。", '',
        f"源码 SHA-256：{frozen['source']['source_sha256']}；提示词 SHA-256：{frozen['prompt_sha256']}。", '',
        f"数据：{json.dumps(frozen['data'], ensure_ascii=False)}", '',
        '完整模型、参数、依赖与工具指纹见 config.frozen.json、freeze.json；实际模型及调用见逐阶段 summary/usage/provider 记录。', '',
        f"索引策略和逐题存储审计：{json.dumps(result['indexing'], ensure_ascii=False)}。", '',
        '## 完整性与局限', '', frozen['protocol'], '', frozen['evaluation_status'], '',
        '每题冻结输入、写入事实、检索轨迹、实际请求、原始提供方响应、用量、错误及清理证明在 questions/。', '',
        '标准答案只由评分阶段读取。报告不读 judge-inputs.json；不按错题反馈修产品或替换回答。', '',
        'Oracle 已裁至相关历史，不代表完整长历史干扰下的效果；同模型评分有偏差风险。', '',
        f"回答失败：{json.dumps(result['answer_errors'], ensure_ascii=False)}", '',
        f"评分失败：{json.dumps(result['judge_errors'], ensure_ascii=False)}", '']
    (out / 'report.md').write_text('\n'.join(text))
    return result


def run(args):
    frozen = validate(args.out)
    if digest(args.out / 'freeze.json') != read(args.out / 'freeze.sha256.json')['sha256']:
        raise ValueError('Freeze manifest changed')
    proof = read(args.out / 'preflight.json')
    if not proof.get('passed') or proof.get('config_sha256') != frozen['config_sha256']:
        raise ValueError('Successful preflight of this exact frozen configuration required')
    credentials(args.runtime)
    cfg = AppConfig.load(args.out / 'config.frozen.json')
    cfg.storage = replace(cfg.storage, collection_prefix=frozen['prefix'], namespace=frozen['namespace'])
    began = time.monotonic()
    def invoke_and_record(directory, name, runtime):
        code = invoke(directory, name, runtime)
        if name in ('read', 'judge'):
            result = report(args)
            save(args.out / 'progress.json', dict(state='running', phase=name, elapsed_s=time.monotonic()-began,
                answered=result['answered'], attempted=result['attempted'], graded=result['graded'], total=500,
                current_index=int(directory.name), updated_at=datetime.now(timezone.utc).isoformat()))
            print(f"PROGRESS answered={result['answered']}/500 graded={result['graded']}/500 phase={name}", flush=True)
        return code
    try:
        serial.run_serial_evaluation(args, frozen, cfg, lambda: validate(args.out), invoke=invoke_and_record)
    finally:
        result = report(args)
        execution = read(args.out / 'execution.json') if (args.out / 'execution.json').exists() else {}
        save(args.out / 'progress.json', dict(state='complete' if execution.get('complete') and result['complete'] else 'stopped',
            answered=result['answered'], attempted=result['attempted'], graded=result['graded'], total=500,
            elapsed_s=time.monotonic()-began, error=execution.get('error'), updated_at=datetime.now(timezone.utc).isoformat()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'preflight', 'run', 'report', 'phase'))
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--data', type=Path, default=ROOT / 'data/longmemeval_oracle.json')
    parser.add_argument('--runtime', type=Path, default=Path.home() / 'Library/Application Support/memsys/runtime.json')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--phase', choices=('write', 'read', 'judge'))
    args = parser.parse_args()
    args.out, args.data = args.out.resolve(), args.data.resolve()
    if args.config is None:
        args.config = Path(read(args.runtime)['config_path'])
    os.umask(0o077)
    globals()[args.action](args)


if __name__ == '__main__':
    main()
