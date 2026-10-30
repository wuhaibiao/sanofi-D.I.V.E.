#!/usr/bin/env python3
"""Fresh V7 regression on the exact previously frozen 24 LongMemEval questions."""
import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from blind_eval_inputs import FILES, load_phase, materialize
from check_webchat_reliability import credentials
from memsys.bench.provenance import source_manifest
from memsys.config import AppConfig
from question_serial_eval import freeze_cases, run_serial_evaluation
from validate_fact_retention import summarize


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def checks(args):
    frozen = read(args.out / 'freeze.json')
    if source_manifest()['source_sha256'] != frozen['source']['source_sha256']:
        raise ValueError('Product source changed after freezing')
    targets = {args.out / 'selection.json': frozen['selection_sha256'],
               args.out / 'config.frozen.json': frozen['config_sha256'],
               Path(__file__): frozen['controller_sha256'],
               ROOT / 'scripts/evaluate_public_types.py': frozen['runner_sha256'],
               ROOT / 'scripts/blind_eval_inputs.py': frozen['blind_input_builder_sha256'],
               ROOT / 'scripts/capture_relational_eval_storage.py': frozen['storage_auditor_sha256']}
    targets.update({args.out / name: frozen['blind_input_sha256'][phase]
                    for phase, name in FILES.items()})
    if frozen.get('execution_mode') == 'question_serial':
        targets[ROOT / 'scripts/question_serial_eval.py'] = frozen['serial_executor_sha256']
        targets[args.out / 'question-cases.json'] = frozen['question_cases_sha256']
    for path, expected in targets.items():
        if digest(path) != expected:
            raise ValueError('Frozen input or tool changed: ' + str(path))
    return frozen


def prepare(args):
    if args.out.exists():
        raise ValueError('Preserve prior results; choose a new directory')
    selection = read(args.cohort / 'selection.json')
    counts = Counter(row['category'] for row in selection)
    if len(selection) != 24 or len(counts) != 6 or set(counts.values()) != {4}:
        raise ValueError('Expected the original six types by four')
    if not all(row['dataset'] == 'LongMemEval' for row in selection):
        raise ValueError('Unexpected dataset')
    # Trusted mechanical preparation only. No model receives this combined file.
    sample = read(args.cohort / 'sample.json')
    if {row['question_id'] for row in selection} != {row['question_id'] for row in sample}:
        raise ValueError('Source sample and fixed selection disagree')
    cfg = AppConfig.load(args.config)
    if cfg.memory.recall_mode != 'facts' or cfg.memory.query_analysis or cfg.memory.query_policy != 'off':
        raise ValueError('Requires deployed default facts retrieval')
    args.out.mkdir(parents=True)
    save(args.out / 'selection.json', selection)
    inputs = materialize(args.out, sample, selection)
    shutil.copy2(args.config, args.out / 'config.frozen.json')
    os.chmod(args.out / 'config.frozen.json', 0o600)
    shutil.copytree(ROOT / 'src/memsys', args.out / 'frozen-src/memsys',
                    ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('evaluate_public_types.py', 'blind_eval_inputs.py', 'question_serial_eval.py',
                 'capture_relational_eval_storage.py', Path(__file__).name):
        shutil.copy2(ROOT / 'scripts' / name, args.out / (name + '.frozen'))
    frozen = dict(source=source_manifest(), controller_sha256=digest(__file__),
        runner_sha256=digest(ROOT / 'scripts/evaluate_public_types.py'),
        blind_input_builder_sha256=digest(ROOT / 'scripts/blind_eval_inputs.py'),
        storage_auditor_sha256=digest(ROOT / 'scripts/capture_relational_eval_storage.py'),
        blind_input_sha256=inputs, selection_sha256=digest(args.out / 'selection.json'),
        config_sha256=digest(args.out / 'config.frozen.json'),
        original_selection_sha256=digest(args.cohort / 'selection.json'),
        original_sample_sha256=digest(args.cohort / 'sample.json'), cohort=str(args.cohort),
        prefix='memsys_v7reg24_' + uuid.uuid4().hex[:12],
        namespace='longmemeval-v7-development-regression24',
        counts={'LongMemEval': dict(counts)}, selected_at=datetime.now(timezone.utc).isoformat(),
        method='Exact previously frozen 24 questions, six types by four. Fresh history ingestion, '
               'one question at a time: ingest, answer, verify collection cleanup, then advance. '
               'Independent judging follows all answers. No resampling, '
               'question rewriting, selective answer retries or product changes.',
        evaluation_status='Previously exposed development regression; not a held-out generalization estimate.',
        answer_mode='Fresh conversation; stored facts and bounded relational expansion; no original excerpts '
                    'or previously generated benchmark answers.',
        score_mode='Independent phase with configured semantic Boolean judge; small unofficial Oracle regression.')
    for phase in FILES:
        _, items = load_phase(args.out, frozen, phase, 'LongMemEval')
        if len(items) != 24:
            raise ValueError('Unexpected frozen phase length')
    freeze_cases(args.out, frozen)
    save(args.out / 'freeze.json', frozen)
    print('FROZEN exact original 24; source ' + frozen['source']['source_sha256'], flush=True)


def comparison(args, result):
    baselines = {}
    new = {row['question_id']: row.get('correct') for row in rows(args.out / 'LongMemEval-grades.jsonl')}
    for label, folder in (
        ('v6_first_run', ROOT / 'runs/longmemeval-v6-small-24-20261014'),
        ('retention_restored_facts', ROOT / 'runs/fact-retention-20261013/compact-24')):
        baseline = read(folder / 'summary.json')
        old = {row['question_id']: row.get('correct') for row in rows(folder / 'LongMemEval-grades.jsonl')}
        if new.keys() != old.keys():
            raise ValueError('Cannot compare different question sets')
        baselines[label] = dict(path=str(folder), correct=baseline['correct'], total=baseline['total'],
            by_category=baseline['by_category'], usage=baseline['usage'],
            mean_memory_utf8_bytes=baseline['mean_memory_utf8_bytes'],
            mean_answer_elapsed_s=baseline['mean_answer_elapsed_s'],
            summary_sha256=digest(folder / 'summary.json'),
            grades_sha256=digest(folder / 'LongMemEval-grades.jsonl'),
            paired_changes=dict(improved=sum(old[q] is False and value is True for q, value in new.items()),
                regressed=sum(old[q] is True and value is False for q, value in new.items()),
                same=sum(old[q] is value for q, value in new.items())))
    result['baselines'] = baselines
    result['comparison_limit'] = ('Fresh stochastic write/read/judge on identical development questions. '
        'The restored-facts baseline reused and repaired facts. The V6 first-run baseline includes a source '
        'service failure; its separately authorized retry must not replace its original score. '
        'This is not a controlled attribution experiment or a comparison with Mem0.')


def report(args):
    frozen = checks(args)
    result = summarize(args.out)
    traces = [read(path) for path in (args.out / 'traces').glob('*.json')]
    result.update(source=frozen['source'], evaluation_status=frozen['evaluation_status'],
        protocol=frozen['method'], relations=dict(
            questions_expanded=sum(t.get('relations', {}).get('added_candidates', 0) > 0 for t in traces),
            added_candidates=sum(t.get('relations', {}).get('added_candidates', 0) for t in traces),
            coverage_supplemented=sum(t.get('coverage_supplement', {}).get('attempted', False) for t in traces)))
    comparison(args, result)
    save(args.out / 'summary.json', result)
    v6 = result['baselines']['v6_first_run']
    restored = result['baselines']['retention_restored_facts']
    lines = ['# V7 原24题开发回归', '',
        '原先冻结的六类各4题，从历史重新写入，固定原题各回答一次，随后独立评分。评测期间未调整产品代码、提示或检索规则。', '',
        ('执行顺序：按冻结题号逐题写入、回答、删除并验证该题专用集合，再进入下一题；全部回答结束后独立评分。'
         if frozen.get('execution_mode') == 'question_serial' else '历史执行顺序：全批写入后统一回答和评分；各题按corpus隔离。'), '',
        '| 版本 | 正确 | 条件 |', '|---|---:|---|',
        f"| V6首次评测 | {v6['correct']}/24 | 包含1题写入服务失败，保留首次结果 |",
        f"| 历史事实保真修复 | {restored['correct']}/24 | 复用并修复过的事实库 |",
        f"| 当前V7 | {result['correct']}/{result['total']} | 本次从历史重新写入 |", '',
        '| 题型 | V6首次 | 当前V7 |', '|---|---:|---:|']
    for category, score in result['by_category'].items():
        old = v6['by_category'][category]
        lines.append(f"| {category} | {old['correct']}/{old['total']} | {score['correct']}/{score['total']} |")
    lines += ['', f"相对V6逐题变化：{json.dumps(v6['paired_changes'], ensure_ascii=False)}。", '',
        f"完成回答{result['answered']}/{result['total']}，评分{result['graded']}/{result['total']}；写入失败会话{result['failed_source_sessions']}。", '',
        f"原文摘录{result['original_excerpts']}，待处理原文{result['pending_originals']}；历史会话{result['source_sessions']}个、消息{result['source_messages']}条；原文一致={result['all_originals_exact']}。", '',
        f"关系召回与覆盖补取：{json.dumps(result['relations'], ensure_ascii=False)}。", '',
        f"回答输入token：{result['usage'].get('answer', {}).get('prompt_tokens', 0)}；记忆UTF-8字节均值：{result['mean_memory_utf8_bytes']:.0f}；读取加回答平均：{result['mean_answer_elapsed_s']:.2f}秒。", '',
        '写入文件不含评测问题和答案，回答文件不含标准答案，只有独立评分进程读取参考答案。输入、源码、配置与工具哈希均冻结。每题只生成一次，未选择性补答。', '',
        '本集合属于已使用的开发回归。Oracle文件提供限定历史，小样本与同模型评分不能代表完整历史条件下的总体准确率或相对Mem0表现。', '',
        result['comparison_limit'], '',
        'V6此前另行授权的失败题补答结果单独保存，本报告不覆盖其首次成绩。价格未配置，费用未知，账本零费用不代表免费。', '',
        f"源码 SHA-256：{frozen['source']['source_sha256']}。实际提示、回答、错误与评分均保留；storage-audit.json保存清理前的只读索引计数。"]
    (args.out / 'report.md').write_text('\n'.join(lines) + '\n')
    print('RESULT ' + json.dumps({k: result[k] for k in ('correct', 'total', 'answered', 'graded', 'complete')}, ensure_ascii=False), flush=True)


def run(args):
    frozen = checks(args)
    credentials(args.runtime)
    config = AppConfig.load(args.out / 'config.frozen.json')
    config.storage = replace(config.storage, collection_prefix=frozen['prefix'], namespace=frozen['namespace'])
    run_serial_evaluation(args, frozen, config, lambda: checks(args))
    report(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'run', 'report'))
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cohort', type=Path, default=ROOT / 'runs/longmemeval-facts-only-four-20261013')
    parser.add_argument('--runtime', type=Path, default=Path.home() / 'Library/Application Support/memsys/runtime.json')
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    args.out, args.cohort = args.out.resolve(), args.cohort.resolve()
    args.config = args.config or Path(read(args.runtime)['config_path'])
    globals()[args.action](args)


if __name__ == '__main__':
    main()
