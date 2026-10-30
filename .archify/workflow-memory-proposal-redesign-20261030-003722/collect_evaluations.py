"""Read aggregate historical results without grading, model calls, or label edits."""
import hashlib
import json
from pathlib import Path

ROOT = Path('/Users/OleniusBlanc227/Documents/sanofi')
HERE = Path(__file__).resolve().parent
SPECS = [
    ('memsys/runs/memory-regression40-20261025', '10月25日 列式抽取初版'),
    ('memsys/runs/memory-regression40-objects-20261025', '10月25日 对象格式修复'),
    ('memsys/runs/memory-regression40-semantic-20261025', '10月25日 语义边界修复'),
    ('memsys/runs/memory-control30-semantic-20261025', '10月25日 30题对照版本'),
    ('memsys/runs/memory-regression30-contracts-20261026', '10月26日 硬校验版本'),
    ('memsys/runs/memory-regression7-system-flow-20261026', '10月26日 七道流程失败题复测'),
    ('memsys/runs/memory-regression3-audit-only-20261026', '10月26日 移除硬规则 三题复测'),
    ('memsys/runs/memory-regression20-audit-only-20261026', '10月26日 校验仅作诊断'),
    ('memsys/runs/memory-regression2-fidelity-20261026', '10月26日 记忆保真 两题复测'),
    ('memsys/runs/memory-regression20-fidelity-20261026', '10月26日 记忆保真 完整复测'),
    ('memsys/runs/memory-regression2-task-context-20261027', '10月27日 抽取容错 两题复测'),
    ('memsys/runs/memory-regression20-no-answer-audit-20261027', '10月27日 移除回答审计'),
    ('memsys/runs/memory-additional20-no-answer-audit-20261027', '10月27日 主干 另一组20题'),
    ('.worktrees/memsys-evidence-selection-experiment/runs/evidence-selection-regression20-20261028', '10月28日 模型事实筛选分支'),
    ('.worktrees/memsys-compact-ranked-refactor/runs/compact-ranked-regression20-20261028', '10月28日 紧凑写入 Top 10'),
    ('.worktrees/memsys-top20-refinement/runs/top20-refinement-regression20-20261028', '10月28日 来源优先 Top 20'),
    ('.worktrees/memsys-top20-refinement/runs/top20-answer-context-regression20-20261028', '10月28日 Top 20 返回与表格改进'),
    ('memsys/runs/return-layer-regression20-20261029', '10月29日 返回层压缩分支'),
    ('memsys/runs/main-additional30-20261029', '10月29日 主干 另一组30题'),
    ('memsys/runs/raw-chunk-regression20-20261029', '10月29日 原文分块初版'),
    ('memsys/runs/raw-chunk-context20-20261029', '10月29日 原文分块 选择与返回改进'),
]
files = {}


def read(path):
    files[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return path.read_text()


def question_ids(path):
    return tuple(json.loads(line)['question_id'] for line in read(path).splitlines() if line.strip())


cohorts = {}
for rel, label in [
    ('memsys/runs/memory-regression40-20261025', 'A40'),
    ('memsys/runs/memory-control30-semantic-20261025', 'B30'),
    ('memsys/runs/memory-regression20-audit-only-20261026', 'C20'),
    ('memsys/runs/memory-additional20-no-answer-audit-20261027', 'D20'),
    ('memsys/runs/main-additional30-20261029', 'E30'),
]:
    cohorts[question_ids(ROOT/rel/'LongMemEval-answers.jsonl')] = label

rows = []
for rel, label in SPECS:
    run = ROOT/rel
    summary = run/'summary.json'
    if not summary.exists():
        summary = run/'evaluation-summary.json'
    data = json.loads(read(summary))
    ids = question_ids(run/'LongMemEval-answers.jsonl')
    source = data.get('candidate', data)
    if 'by_purpose' in source:
        purposes = source['by_purpose']
        usage = {name: dict(calls=value['calls'], prompt_tokens=value['input_tokens'],
                 completion_tokens=value['output_tokens']) for name, value in purposes.items()}
        mean_elapsed = source['read_answer_elapsed_s']['mean']
    else:
        usage = source['usage']
        mean_elapsed = source.get('mean_answer_elapsed_s')
    production = [v for name, v in usage.items() if name not in ('judge', 'embed', 'embed_query')]
    answer = usage['answer']
    complete = source['answered'] == source['total'] and answer['calls'] == source['total']
    assert len(ids) == source['total'] and len(set(ids)) == len(ids)
    report = run/'report.md'
    read(report)
    rows.append(dict(
        label=label, path=str(run), report=str(report), summary=str(summary),
        cohort=cohorts.get(ids, f'定向{len(ids)}'),
        cohort_sha256=hashlib.sha256(json.dumps(ids).encode()).hexdigest(),
        total=source['total'], correct=source['correct'], answered=source['answered'],
        production_calls=sum(v['calls'] for v in production),
        production_input_tokens=sum(v['prompt_tokens'] for v in production),
        production_output_tokens=sum(v['completion_tokens'] for v in production),
        answer_calls=answer['calls'], answer_input_tokens=answer['prompt_tokens'],
        mean_answer_input_tokens=answer['prompt_tokens']/source['total'] if complete else None,
        mean_read_answer_s=mean_elapsed if complete else None,
        successful_production_purposes={name: value['calls'] for name, value in usage.items()
            if name not in ('judge', 'embed', 'embed_query')},
        complete_delivery=complete,
    ))

payload = dict(scope='2026-10-25 through 2026-10-29 architecture-related real LongMemEval runs retained in this discussion',
               includes_branch_experiments=True, model_calls=0, relabeling=False,
               cohort_meaning='Same group means exactly the same question IDs and order, not identical implementation or runtime conditions.',
               metrics_meaning='Successful provider operation ledger; extraction, answer and selector only. Judging and local embedding/reranking excluded. Failed-request usage may be unknown.',
               rows=rows, source_files_sha256=files)
(HERE/'evaluation-catalog.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2))
print('Historical runs:', len(rows))
for row in rows:
    print(row['cohort'], row['label'], f"{row['correct']}/{row['total']}",
          f"delivered={row['answered']}/{row['total']}", f"calls={row['production_calls']}")
