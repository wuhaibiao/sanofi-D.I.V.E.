#!/usr/bin/env python3
"""One fresh end-to-end regression of an existing frozen LongMemEval cohort.

Preparation splits the source mechanically; product phases use only their
whitelisted files. No resampling, answer retries, product tuning or other sets.
"""
from collections import Counter
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from blind_eval_inputs import materialize, load_phase
from memsys.bench.provenance import source_manifest
from memsys.config import AppConfig
from validate_fact_retention import summarize


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(args):
    if args.out.exists():
        raise ValueError('Preserve existing runs; choose a new output directory')
    selection = read(args.cohort / 'selection.json')
    counts = Counter(row['category'] for row in selection)
    assert len(selection) == 24 and len(counts) == 6 and set(counts.values()) == {4}
    assert all(row['dataset'] == 'LongMemEval' for row in selection)
    sample = read(args.cohort / 'sample.json')
    assert {row['question_id'] for row in selection} == {row['question_id'] for row in sample}
    cfg = AppConfig.load(args.config)
    assert cfg.memory.recall_mode == 'facts' and not cfg.memory.query_analysis
    assert cfg.memory.query_policy == 'off'
    args.out.mkdir(parents=True)
    save(args.out / 'selection.json', selection)
    blind_hashes = materialize(args.out, sample, selection)
    shutil.copy2(args.config, args.out / 'config.frozen.json')
    os.chmod(args.out / 'config.frozen.json', 0o600)
    shutil.copytree(ROOT / 'src/memsys', args.out / 'frozen-src/memsys', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(ROOT / 'scripts/evaluate_public_types.py', args.out / 'runner.frozen.py')
    shutil.copy2(ROOT / 'scripts/blind_eval_inputs.py', args.out / 'blind-input-builder.frozen.py')
    shutil.copy2(Path(__file__), args.out / 'controller.frozen.py')
    freeze = dict(source=source_manifest(), runner_sha256=digest(ROOT / 'scripts/evaluate_public_types.py'),
        controller_sha256=digest(__file__), blind_input_builder_sha256=digest(ROOT / 'scripts/blind_eval_inputs.py'),
        blind_input_sha256=blind_hashes, config_sha256=digest(args.out / 'config.frozen.json'),
        selection_sha256=digest(args.out / 'selection.json'), cohort=str(args.cohort),
        original_sample_sha256=digest(args.cohort / 'sample.json'),
        original_selection_sha256=digest(args.cohort / 'selection.json'),
        prefix='memsys_v6small_' + uuid.uuid4().hex[:12], namespace='longmemeval-v6-development-regression',
        counts={'LongMemEval': dict(counts)}, selected_at=datetime.now(timezone.utc).isoformat(),
        method='Identical previously used frozen 24, six types by four. Fresh history ingestion, one answer per fixed original question, independent subsequent judging. No resampling or product changes.',
        evaluation_status='Development regression; these public questions have previously informed development. Not a held-out generalization estimate.',
        answer_mode='Facts-only fresh conversation; no pending sources, original excerpts or prior generated benchmark answers.',
        score_mode='Same configured answer model used for semantic Boolean judging; unofficial small regression.',
        baseline=str(args.baseline), memory_configuration=dict(recall_mode=cfg.memory.recall_mode,
            context_budget_tokens=cfg.memory.context_budget_tokens,
            answer_input_budget_tokens=cfg.memory.answer_input_budget_tokens,
            extract_max_tokens=cfg.memory.extract_max_tokens, answer_max_tokens=cfg.memory.answer_max_tokens))
    # All phase files must load through the actual whitelist/hash gate before work.
    for phase in ('write', 'read', 'judge'):
        _, items = load_phase(args.out, freeze, phase, 'LongMemEval')
        assert len(items) == 24
    save(args.out / 'freeze.json', freeze)
    print('FROZEN 24 LongMemEval regression questions; isolated fresh storage; source ' + freeze['source']['source_sha256'], flush=True)


def finish(args):
    freeze = read(args.out / 'freeze.json')
    assert source_manifest()['source_sha256'] == freeze['source']['source_sha256']
    assert digest(__file__) == freeze['controller_sha256']
    result = summarize(args.out)
    baseline = read(args.baseline / 'summary.json')
    before = read(args.cohort / 'comparison.json')
    traces = [read(p) for p in (args.out / 'traces').glob('*.json')]
    writes = [json.loads(line) for line in (args.out / 'LongMemEval-write.jsonl').read_text().splitlines() if line]
    result.update(evaluation_status=freeze['evaluation_status'], source=freeze['source'],
        covered_literal_structures=sum((s.get('report') or {}).get('covered_literal_structures', 0)
            for w in writes for s in w['sessions']),
        event_status_counts=dict(Counter(f.get('semantics', {}).get('event_status', 'unknown')
            for w in writes for f in w['facts'])),
        advice_operations=sum(t['plan']['operation'] == 'advice' for t in traces),
        baseline=dict(correct=baseline['correct'], total=baseline['total'],
            answer_prompt_tokens=baseline['usage']['answer']['prompt_tokens'],
            mean_memory_utf8_bytes=baseline['mean_memory_utf8_bytes'],
            mean_answer_elapsed_s=baseline['mean_answer_elapsed_s'], by_category=baseline['by_category']),
        earlier_facts_only=dict(correct=before['new']['correct'], total=24),
        initial_mixed=dict(correct=before['old']['correct'], total=24),
        comparison_limit='Same questions, fresh V6 extraction versus historical repaired/restored facts. Extraction and generation are stochastic; not a controlled component comparison or Mem0 comparison.')
    # Pair only grading outcomes; no reference answers are read by this reporter.
    old_grades = {r['question_id']: r.get('correct') for r in
        [json.loads(line) for line in (args.baseline / 'LongMemEval-grades.jsonl').read_text().splitlines() if line]}
    new_grades = {r['question_id']: r.get('correct') for r in
        [json.loads(line) for line in (args.out / 'LongMemEval-grades.jsonl').read_text().splitlines() if line]}
    result['paired_changes'] = dict(
        improved=sum(old_grades.get(q) is False and label is True for q, label in new_grades.items()),
        regressed=sum(old_grades.get(q) is True and label is False for q, label in new_grades.items()),
        same=sum(old_grades.get(q) is label for q, label in new_grades.items()))
    save(args.out / 'summary.json', result)
    lines = ['# V6 LongMemEval 24题开发回归', '',
        '沿用原先冻结的六类各4题，独立存储从历史重新写入，再答固定原题，最后评分。没有改题、按得分重选、补答失败题或本轮产品调参。', '',
        '| 版本 | 正确 | 说明 |', '|---|---:|---|',
        f"| 早期事实＋原文 | {before['old']['correct']}/24 | 历史混合上下文结果 |",
        f"| 早期仅事实 | {before['new']['correct']}/24 | 历史精简事实结果 |",
        f"| 上次事实保真修复 | {baseline['correct']}/24 | 复用抽取事实并进行结构回补 |",
        f"| 当前V6 | {result['correct']}/24 | 本轮重新写入、读取和评分 |", '',
        '| 题型 | 上次 | 当前V6 |', '|---|---:|---:|']
    for category, score in result['by_category'].items():
        old = baseline['by_category'][category]
        lines.append(f"| {category} | {old['correct']}/{old['total']} | {score['correct']}/{score['total']} |")
    u = result['usage']
    lines += ['', '## 完整性与效率', '',
        f"完成回答{result['answered']}/{result['total']}，确定评分{result['graded']}/{result['total']}；回答错误{len(result['answer_errors'])}，评分错误{len(result['judge_errors'])}。原文摘录{result['original_excerpts']}，待处理原文{result['pending_originals']}。", '',
        f"历史会话{result['source_sessions']}个、消息{result['source_messages']}条，原文逐条一致={result['all_originals_exact']}，失败写入会话{result['failed_source_sessions']}。写入只读不含问题/答案的历史文件；答题只读固定原题和事实；评分独立进程才读取标签。", '',
        f"存储事实{result['stored_facts']}条；精确覆盖省去重复字面结构{result['covered_literal_structures']}条；显式事件状态分布{result['event_status_counts']}。这些数量不代表语义信息全部保留。", '',
        '| 指标 | 上次 | 当前V6 |', '|---|---:|---:|',
        f"| 回答输入tokens合计 | {baseline['usage']['answer']['prompt_tokens']} | {u.get('answer',{}).get('prompt_tokens',0)} |",
        f"| 记忆UTF-8字节均值 | {baseline['mean_memory_utf8_bytes']:.0f} | {result.get('mean_memory_utf8_bytes',0):.0f} |",
        f"| 读取加回答秒数均值 | {baseline['mean_answer_elapsed_s']:.2f} | {result.get('mean_answer_elapsed_s',0):.2f} |", '',
        '## 解读边界', '', freeze['evaluation_status'], '', result['comparison_limit'], '',
        '模型沿用配置并独立会话调用；小样本与同模型评分不能证明总体准确率或相对Mem0优劣。每轮抽取、回答的模型差异和网络/加载时间会影响结果，不能把所有变化归因于V6压缩。', '',
        f"实际调用与token见summary.json：{json.dumps(u,ensure_ascii=False)}。价格未配置，费用未知；账本cost=0不代表免费。", '',
        '冻结源码、配置哈希、原题分离文件、实际提示词、回答、评分和失败日志保留在本目录。生产源码从冻结至完成保持一致，本轮不根据逐题反馈修复系统。']
    (args.out / 'report.md').write_text('\n'.join(lines) + '\n')
    print('RESULT ' + json.dumps({k: result[k] for k in ('correct', 'total', 'answered', 'graded', 'complete', 'original_excerpts', 'failed_source_sessions')}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cohort', type=Path, default=ROOT / 'runs/longmemeval-facts-only-four-20261013')
    parser.add_argument('--baseline', type=Path, default=ROOT / 'runs/fact-retention-20261013/compact-24')
    parser.add_argument('--runtime', type=Path, default=Path.home() / 'Library/Application Support/memsys/runtime.json')
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    args.out, args.cohort, args.baseline = args.out.resolve(), args.cohort.resolve(), args.baseline.resolve()
    args.config = args.config or Path(read(args.runtime)['config_path'])
    prepare(args)
    start = time.monotonic()
    execution = dict(outcomes=[], started_at=read(args.out / 'freeze.json')['selected_at'])
    for phase in ('write', 'read', 'judge'):
        print('START ' + phase, flush=True)
        execution.update(current=phase)
        save(args.out / 'execution.json', execution)
        with (args.out / ('LongMemEval-' + phase + '.log')).open('w') as log:
            command = [sys.executable, str(ROOT / 'scripts/evaluate_public_types.py'), '--out', str(args.out),
                '--dataset', 'LongMemEval', '--phase', phase, '--runtime', str(args.runtime)]
            process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in process.stdout:
                log.write(line)
                log.flush()
                print(line, end='', flush=True)
            code = process.wait()
        execution['outcomes'].append(dict(phase=phase, exit_code=code))
        execution['elapsed_s'] = round(time.monotonic() - start, 3)
        save(args.out / 'execution.json', execution)
        if code:
            raise RuntimeError(phase + ' process failed; original logs preserved')
    execution['current'] = None
    save(args.out / 'execution.json', execution)
    finish(args)


if __name__ == '__main__':
    main()
