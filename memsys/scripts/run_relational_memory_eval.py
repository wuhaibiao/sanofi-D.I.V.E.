#!/usr/bin/env python3
"""One metadata-selected, physically separated LongMemEval evaluation after a build."""
from collections import Counter
from datetime import datetime, timezone
import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from blind_eval_inputs import FILES, materialize, load_phase
from memsys.bench.provenance import source_manifest
from memsys.config import AppConfig
from validate_fact_retention import summarize
from check_webchat_reliability import credentials
from question_serial_eval import freeze_cases, run_serial_evaluation

COUNTS = {'single-session-user': 4, 'single-session-assistant': 4,
          'single-session-preference': 4, 'knowledge-update': 4,
          'multi-session': 8, 'temporal-reasoning': 8}
SEED = 'working-relational-memory-v7-once-20261014'


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def base_id(value):
    value = str(value)
    if value.startswith('lme_'):
        value = value[4:]
    return value.removesuffix('_abs')


def exclusions():
    """Conservatively exclude old sample/selection IDs; never inspect question/label content."""
    command = ['rg', '--files', 'runs', '-g', '*.json', '-g', '*.jsonl',
               '-g', '!**/raw/**', '-g', '!**/frozen-src/**']
    found = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if found.returncode not in (0, 1):
        raise RuntimeError('Cannot enumerate previous evaluation metadata')
    ids, sources, unreadable = set(), [], []
    for name in sorted(found.stdout.splitlines()):
        path = ROOT / name
        try:
            data = ([json.loads(line) for line in path.read_text().splitlines() if line.strip()]
                    if path.suffix == '.jsonl' else read(path))
            observed = set()
            def collect(value):
                if isinstance(value, dict):
                    if 'question_id' in value:
                        observed.add(base_id(value['question_id']))
                    if 'source_question_id' in value:
                        observed.add(base_id(value['source_question_id']))
                    if 'qid' in value:
                        observed.add(base_id(value['qid']))
                    for child in value.values():
                        if isinstance(child, (dict, list)):
                            collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)
            collect(data)
            ids.update(observed)
            sources.append(dict(path=name, sha256=digest(path), observed_ids=len(observed)))
        except (OSError, ValueError, TypeError) as exc:
            unreadable.append(dict(path=name, error=type(exc).__name__))
    if unreadable:
        raise ValueError('Previous metadata could not be inspected safely: ' + str(unreadable))
    return ids, sources


def select(args):
    if args.out.exists():
        raise ValueError('Preserve previous runs; choose a fresh directory')
    requested_counts = getattr(args, 'counts', None) or COUNTS
    seed = getattr(args, 'seed', None) or SEED
    allow_exposed = getattr(args, 'allow_exposed', False)
    if (set(requested_counts) != set(COUNTS) or
            any(type(count) is not int or count <= 0 for count in requested_counts.values())):
        raise ValueError('Counts must give a positive integer for every supported question type')
    excluded, sources = exclusions()
    pool = read(args.data)
    counts = Counter(row['question_type'] for row in pool)
    if not set(requested_counts).issubset(counts):
        raise ValueError('Missing requested question types')
    chosen, selected, used = [], [], set()
    for category, count in requested_counts.items():
        candidates = [row for row in pool if row['question_type'] == category
                      and base_id(row['question_id']) not in (used if allow_exposed else excluded | used)]
        order = lambda row: hashlib.sha256((seed + ':' + category + ':' + str(row['question_id'])).encode()).hexdigest()
        abstentions = sorted((row for row in candidates if str(row['question_id']).endswith('_abs')), key=order)
        normal = sorted((row for row in candidates if not str(row['question_id']).endswith('_abs')), key=order)
        group = abstentions[:1]
        taken = {base_id(row['question_id']) for row in group}
        for row in normal:
            if len(group) >= count:
                break
            if base_id(row['question_id']) not in taken:
                group.append(row)
                taken.add(base_id(row['question_id']))
        if len(group) != count:
            raise ValueError('Not enough unused metadata-selected candidates for ' + category)
        for row in group:
            # Trusted preparation: only the mechanical whitelist sees full rows.
            clean = {key: row[key] for key in ('question_id', 'question_type', 'question', 'question_date',
                     'answer', 'haystack_dates', 'haystack_session_ids')}
            clean['haystack_sessions'] = [[{key: turn[key] for key in ('role', 'content')}
                                          for turn in session] for session in row['haystack_sessions']]
            original_id = str(clean['question_id'])
            clean['question_id'] = 'lme_' + original_id
            corpus = 'corpus-' + hashlib.sha256(json.dumps([clean['haystack_session_ids'], clean['haystack_dates'],
                clean['haystack_sessions']], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
            chosen.append(clean)
            selected.append(dict(question_id=clean['question_id'], source_question_id=original_id,
                dataset='LongMemEval', category=category, judge_type=category, corpus_id=corpus,
                abstention=original_id.endswith('_abs')))
            used.add(base_id(original_id))
    args.out.mkdir(parents=True)
    save(args.out / 'selection.json', selected)
    hashes = materialize(args.out, chosen, selected)
    # No combined sample file is created: each model phase has its own whitelist.
    save(args.out / 'selection-freeze.json', dict(seed=seed, selected_at=datetime.now(timezone.utc).isoformat(),
        counts=dict(Counter(row['category'] for row in selected)),
        abstentions=sum(row['abstention'] for row in selected),
        dataset_path=str(args.data), dataset_sha256=digest(args.data),
        excluded_base_ids=len(excluded), exclusion_sources=sources,
        selected_previous_overlap=len(used & excluded), development_regression=allow_exposed,
        selection_sha256=digest(args.out / 'selection.json'),
        blind_input_sha256=hashes, baseline_source=source_manifest(),
        exposure_limit=('Development regression: previously exposed public questions are permitted. '
                        'No fresh holdout or blind generalization claim.' if allow_exposed else
                       'Excludes identifiable previous sample, selection and answer IDs, including abstention siblings. '
                       'The public Oracle pool and earlier development have been used before; undocumented historical '
                       'exposure cannot be ruled out. This is a fresh selected cohort, not proof of blind generalization.')))
    print(json.dumps(dict(selected=len(selected), counts=requested_counts, abstentions=sum(row['abstention'] for row in selected),
                         previous_overlap=len(used & excluded), development_regression=allow_exposed,
                         excluded_base_ids=len(excluded)), ensure_ascii=False), flush=True)


def check_inputs(args):
    selection = read(args.out / 'selection-freeze.json')
    if digest(args.out / 'selection.json') != selection['selection_sha256']:
        raise ValueError('Frozen selection changed')
    for phase, filename in FILES.items():
        if digest(args.out / filename) != selection['blind_input_sha256'][phase]:
            raise ValueError('Frozen phase input changed: ' + phase)
    return selection


def freeze(args):
    if (args.out / 'freeze.json').exists():
        raise ValueError('Final source is already frozen; do not replace it')
    selection = check_inputs(args)
    selected_counts = dict(Counter(row['category'] for row in read(args.out / 'selection.json')))
    if 'counts' in selection and selection['counts'] != selected_counts:
        raise ValueError('Frozen selection counts disagree with metadata')
    cfg = AppConfig.load(args.config)
    if cfg.memory.recall_mode != 'facts' or cfg.memory.query_analysis or cfg.memory.query_policy != 'off':
        raise ValueError('Requires the default facts-only, non-generative retrieval path')
    shutil.copy2(args.config, args.out / 'config.frozen.json')
    os.chmod(args.out / 'config.frozen.json', 0o600)
    shutil.copytree(ROOT / 'src/memsys', args.out / 'frozen-src/memsys', ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('evaluate_public_types.py', 'blind_eval_inputs.py', 'question_serial_eval.py',
                 'capture_relational_eval_storage.py', Path(__file__).name):
        shutil.copy2(ROOT / 'scripts' / name, args.out / (name + '.frozen'))
    record = dict(selection, source=source_manifest(), runner_sha256=digest(ROOT / 'scripts/evaluate_public_types.py'),
        controller_sha256=digest(__file__), blind_input_builder_sha256=digest(ROOT / 'scripts/blind_eval_inputs.py'),
        config_sha256=digest(args.out / 'config.frozen.json'), prefix='memsys_v7_' + uuid.uuid4().hex[:12],
        namespace='longmemeval-v7-single-evaluation', counts={'LongMemEval': selected_counts},
        evaluation_status=selection['exposure_limit'],
        answer_mode='Question-local fresh history ingestion; facts and bounded relational indexing only; '
                    'one generation per original question; verified cleanup before the next question.',
        score_mode='Independent phase with the configured semantic judge; small unofficial Oracle evaluation.',
        method=('Metadata-only hash sampling with previously exposed questions allowed for development regression. '
                if selection.get('development_regression') else 'Metadata-only hash sampling with prior-ID exclusions. ') +
               'One abstention variant per available type. '
               'Each question is ingested, answered and cleaned before the next; independent judging follows all answers. '
               'No question edits, standard-answer-driven changes, resampling, selective retries or post-evaluation tuning.')
    for phase in FILES:
        _, items = load_phase(args.out, record, phase, 'LongMemEval')
        if len(items) != sum(selected_counts.values()):
            raise ValueError('Unexpected phase length')
    freeze_cases(args.out, record)
    save(args.out / 'freeze.json', record)
    print('FINAL SOURCE FROZEN ' + record['source']['source_sha256'], flush=True)


def report(args):
    frozen = read(args.out / 'freeze.json')
    result = summarize(args.out)
    result.update(source=frozen['source'], evaluation_status=frozen['evaluation_status'],
                  protocol=frozen['method'], requested_counts=frozen['counts']['LongMemEval'])
    traces = [read(path) for path in (args.out / 'traces').glob('*.json')]
    result['relations'] = dict(questions_expanded=sum(t.get('relations', {}).get('added_candidates', 0) > 0 for t in traces),
        added_candidates=sum(t.get('relations', {}).get('added_candidates', 0) for t in traces),
        coverage_supplemented=sum(t.get('coverage_supplement', {}).get('attempted', False) for t in traces))
    answers = {row['question_id']: row for row in
        [json.loads(line) for line in (args.out / 'LongMemEval-answers.jsonl').read_text().splitlines() if line]}
    grades = {row['question_id']: row for row in
        [json.loads(line) for line in (args.out / 'LongMemEval-grades.jsonl').read_text().splitlines() if line]}
    abstentions = [row for row in read(args.out / 'selection.json') if row['abstention']]
    result['abstentions'] = dict(total=len(abstentions),
        answered=sum(answers.get(row['question_id'], {}).get('status') == 'answered' for row in abstentions),
        correct=sum(grades.get(row['question_id'], {}).get('correct') is True for row in abstentions))
    save(args.out / 'summary.json', result)
    lines = [f"# LongMemEval {sum(frozen['counts']['LongMemEval'].values())}题评测", '', frozen['method'], '',
             '| 题型 | 正确 | 回答完成 |', '|---|---:|---:|']
    for category, score in result['by_category'].items():
        lines.append(f"| {category} | {score['correct']}/{score['total']} | {score['answered']}/{score['total']} |")
    lines += ['', f"总计：{result['correct']}/{result['total']}；拒答子集：{result['abstentions']['correct']}/{len(abstentions)}。", '',
              '## 流程与边界', '',
              '写入、固定原题回答和独立判分使用物理分离文件与哈希白名单。从历史重新建库；本轮每题只生成一次，失败原样计入。', '',
              ('执行顺序：按冻结题号逐题写入、回答、删除并验证该题专用集合，再进入下一题；全部回答结束后独立评分。'
               if frozen.get('execution_mode') == 'question_serial' else '历史执行顺序：全批写入后统一回答和评分；各题按corpus隔离。'), '',
              f"原文摘录：{result['original_excerpts']}；待处理原文：{result['pending_originals']}；历史写入失败会话：{result['failed_source_sessions']}。", '',
              f"关系扩展与覆盖补取：{json.dumps(result['relations'], ensure_ascii=False)}。", '',
              '检索使用 Oracle 文件提供的历史条件，不能据此推断完整历史干扰下的检索效果。新样本与此前24题不同，不作配对提升结论；同模型评分及小样本不能证明相对Mem0优劣。', '',
              frozen['evaluation_status'], '',
              '实际 token、调用和延迟见 summary.json 与阶段 usage.jsonl；未配置价格时费用未知，账本零费用不代表免费。', '',
              f"源码 SHA-256：{frozen['source']['source_sha256']}。冻结输入、提示、回答、错误、判分均保留。本轮结束后不按逐题结果修系统或重答。"]
    (args.out / 'report.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({key: result[key] for key in ('correct', 'total', 'answered', 'graded', 'complete')}, ensure_ascii=False), flush=True)


def checks(args):
    check_inputs(args)
    frozen = read(args.out / 'freeze.json')
    if source_manifest()['source_sha256'] != frozen['source']['source_sha256'] or digest(__file__) != frozen['controller_sha256']:
        raise ValueError('Final source or controller changed')
    targets = {args.out / 'config.frozen.json': frozen['config_sha256'],
        ROOT / 'scripts/evaluate_public_types.py': frozen['runner_sha256'],
        ROOT / 'scripts/blind_eval_inputs.py': frozen['blind_input_builder_sha256']}
    if frozen.get('execution_mode') == 'question_serial':
        targets.update({ROOT / 'scripts/question_serial_eval.py': frozen['serial_executor_sha256'],
            ROOT / 'scripts/capture_relational_eval_storage.py': frozen['storage_auditor_sha256'],
            args.out / 'question-cases.json': frozen['question_cases_sha256']})
    for path, expected in targets.items():
        if digest(path) != expected:
            raise ValueError('Frozen input or tool changed: ' + str(path))
    return frozen


def run(args):
    frozen = checks(args)
    credentials(args.runtime)
    config = AppConfig.load(args.out / 'config.frozen.json')
    config.storage = replace(config.storage, collection_prefix=frozen['prefix'], namespace=frozen['namespace'])
    run_serial_evaluation(args, frozen, config, lambda: checks(args))
    report(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('select', 'freeze', 'run', 'report'))
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--data', type=Path, default=ROOT / 'data/longmemeval_oracle.json')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--runtime', type=Path, default=Path.home() / 'Library/Application Support/memsys/runtime.json')
    parser.add_argument('--counts', type=json.loads, help='Selection only: JSON object of per-type question counts')
    parser.add_argument('--seed', help='Selection only: deterministic metadata sampling seed')
    parser.add_argument('--allow-exposed', action='store_true', help='Selection only: explicitly label exposed-cohort development regression')
    args = parser.parse_args()
    args.out, args.data, args.config = args.out.resolve(), args.data.resolve(), args.config.resolve()
    globals()[args.action](args)


if __name__ == '__main__':
    main()
