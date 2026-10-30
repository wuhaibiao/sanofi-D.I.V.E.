#!/usr/bin/env python3
"""Report completed evaluation artifacts only; never read judge input labels or call a model."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from memsys.bench.provenance import source_manifest

NAMES = {'single-session-user':'单会话用户事实', 'single-session-assistant':'单会话助手信息',
         'single-session-preference':'偏好与建议', 'knowledge-update':'知识更新',
         'multi-session':'跨会话', 'temporal-reasoning':'时间推理'}


def read(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    out = args.out
    summary, freeze, execution = (read(out / name) for name in ('summary.json', 'freeze.json', 'execution.json'))
    if execution.get('current') is not None or len(execution['outcomes']) != 3:
        raise ValueError('Report only after the single write/read/judge execution has ended')
    integrity = dict(source_unchanged=source_manifest()['source_sha256'] == freeze['source']['source_sha256'],
        frozen_config_unchanged=digest(out / 'config.frozen.json') == freeze['config_sha256'],
        selection_unchanged=digest(out / 'selection.json') == freeze['selection_sha256'])
    for phase in ('write','read','judge'):
        integrity[phase + '_input_unchanged'] = digest(out / (phase + '-inputs.json')) == freeze['blind_input_sha256'][phase]
    if not all(integrity.values()):
        raise ValueError('Frozen integrity check failed')
    storage = read(out / 'storage-audit.json')
    traces = [read(path) for path in (out / 'traces').glob('*.json')]
    writes = [json.loads(line) for line in (out / 'LongMemEval-write.jsonl').read_text().splitlines() if line]
    stats = dict(storage=storage, integrity=integrity, cleanup=execution.get('cleanup'),
        relation_warnings=sum(len((session.get('report') or {}).get('relation_warnings',[])) for row in writes for session in row['sessions']),
        expanded_questions=sum(t.get('relations',{}).get('added_candidates',0) > 0 for t in traces),
        added_candidates=sum(t.get('relations',{}).get('added_candidates',0) for t in traces),
        graph_limited_questions=sum(bool(t.get('relations',{}).get('limited')) for t in traces),
        coverage_supplemented=sum(bool(t.get('coverage_supplement',{}).get('attempted')) for t in traces),
        coverage_incomplete=sum(t.get('plan',{}).get('needs_all') and not t.get('coverage',{}).get('context_complete') for t in traces),
        wall_elapsed_s=execution['elapsed_s'])
    (out / 'build-evaluation-audit.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2)+'\n')
    lines = ['# V7 工作记忆与实体关系记忆：唯一一轮32题评测', '',
        f"正确 **{summary['correct']}/{summary['total']}（{100*summary['correct']/summary['total']:.2f}%）**；回答完成 {summary['answered']}/{summary['total']}，确定判分 {summary['graded']}/{summary['total']}。", '',
        '| 题型 | 正确 | 回答完成 |', '|---|---:|---:|']
    for category in NAMES:
        score = summary['by_category'][category]
        lines.append(f"| {NAMES[category]} | {score['correct']}/{score['total']} | {score['answered']}/{score['total']} |")
    abstentions=summary['abstentions']
    lines += ['', f"拒答变体子集：{abstentions['correct']}/{abstentions['total']}，已计入上表。", '',
        '## 本轮构建', '',
        '- 完整轮工作窗口：最多12轮，独立上下文预算；淘汰前检查事实写入水位，失败保留消息。',
        '- 一次抽取同时生成事实和关系；显式别名独立验证来源，旧实体描述用于维持跨会话身份。',
        '- Qdrant 继续统一存储；关系引用事实，不作为第二份向量候选。最多两跳扩展并按事实ID去重。',
        '- 首次回答前按剩余输入预算补入遗漏事实，仍不足时给出已知子集边界；保持一次最终生成。', '',
        '## 数据与机制证据', '',
        f"从原始历史重新写入 {summary['source_sessions']} 段会话、{summary['source_messages']} 条消息；原文逐条一致={summary['all_originals_exact']}，写入失败会话={summary['failed_source_sessions']}。", '',
        f"存储事实 {storage['facts']} 条、实体 {storage['entities']} 个、关系索引 {storage['relations']} 条。其中模型抽取且通过来源校验的关系 {storage['relation_origins'].get('extracted',0)} 条，已有字段的弱关联 {storage['relation_origins'].get('legacy_key',0)} 条；关系悬空事实引用 {storage['orphan_relation_fact_ids']}。数量不代表语义完整或每条关系均正确。", '',
        f"关联检索新增候选出现在 {stats['expanded_questions']} 道题，共 {stats['added_candidates']} 个事实候选；{stats['graph_limited_questions']} 道触及图检索上限。{stats['coverage_supplemented']} 道在首次生成前扩大事实装载预算，{stats['coverage_incomplete']} 道最终仍标记汇总覆盖不足。", '',
        f"结构／关系来源警告：{stats['relation_warnings']} 条；不合格关系被排除，原始事实和警告保留。答题原文摘录={summary['original_excerpts']}，待处理原文={summary['pending_originals']}。", '',
        '## 调用、token与耗时', '',
        '| 用途 | 已记录调用 | 输入tokens | 输出tokens |', '|---|---:|---:|---:|']
    for purpose, label in [('extract','事实与关系抽取'),('answer','回答'),('judge','独立判分')]:
        usage=summary['usage'].get(purpose,{})
        lines.append(f"| {label} | {usage.get('calls',0)} | {usage.get('prompt_tokens',0)} | {usage.get('completion_tokens',0)} |")
    lines += ['', f"读取加回答平均 {summary.get('mean_answer_elapsed_s',0):.2f} 秒；记忆上下文平均 {summary.get('mean_memory_utf8_bytes',0):.0f} UTF-8字节；整轮 {execution['elapsed_s']:.1f} 秒。字节预算不是模型实际token数，后者来自usage账本。", '',
        '未配置可核实价格，费用未知；账本零费用不代表免费。', '',
        '## 完整性与解读范围', '',
        freeze['method'], '', freeze['evaluation_status'], '',
        '只执行一轮32题，未重答失败题或按逐题反馈改动生产系统。写入、固定原题回答和判分文件物理分离，标准答案只供独立判分。此报告只读取已生成结果及元数据，不调用模型或读取judge-inputs标签。', '',
        f"全部冻结校验通过={all(integrity.values())}；源码SHA-256：{freeze['source']['source_sha256']}。临时评测集合已清理，其他集合保留={execution.get('cleanup',{}).get('unrelated_collections_preserved')}。", '',
        '本轮使用目录内LongMemEval Oracle提供的历史条件，不能据此推断全历史干扰下的效果。题目和权重与此前24题不同，不能作配对提升或Mem0优劣结论。同模型语义判分和小样本也不构成总体准确率证明。', '',
        '当前关系索引最多两跳，实体识别、关系和状态仍依赖抽取质量；图索引不能恢复未写入的语义，也不能保证复杂身份消歧或所有时序更新正确。旧私人事实的部署升级仅构建已有字段的弱关联，没有重新抽取私人历史。']
    if (out / 'report.md').exists() and not (out / 'report.controller.md').exists():
        shutil.copy2(out / 'report.md', out / 'report.controller.md')
    (out / 'report.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps(dict(correct=summary['correct'],total=summary['total'],integrity=integrity,
        storage={key:storage[key] for key in ('facts','entities','relations')},expanded_questions=stats['expanded_questions'],
        coverage_supplemented=stats['coverage_supplemented']),ensure_ascii=False))


if __name__ == '__main__':
    main()
