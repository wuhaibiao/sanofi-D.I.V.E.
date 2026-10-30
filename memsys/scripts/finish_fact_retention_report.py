#!/usr/bin/env python3
"""Summarize existing authorized repair runs; this script makes no model calls."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from memsys.bench.provenance import source_manifest
from validate_fact_retention import read, rows, save, summarize


def main():
    parent = ROOT / 'runs/fact-retention-20261013'
    names = ('read-fix-24', 'rebuilt-24', 'compact-24', 'compact-final-unseen-24')
    results = {}
    for name in names:
        out = parent / name
        result = summarize(out)
        assert result['complete'], name + ' is unfinished'
        traces = [read(out / 'traces' / (a['question_id'] + '.json'))
                  for a in rows(out / 'LongMemEval-answers.jsonl')]
        measured_bge = [t['reranking']['inference_and_load_s'] for t in traces
                        if 'inference_and_load_s' in t['reranking']]
        result['mean_bge_inference_and_load_s'] = statistics.mean(measured_bge) if measured_bge else None
        result['source_freeze'] = read(out / 'freeze.json')['source']['source_sha256']
        if (out / 'restoration-audit.json').exists():
            audit = read(out / 'restoration-audit.json')
            result['restoration_llm_calls'] = audit['restoration_llm_calls']
            result['restored_facts'] = sum(r['added'] for r in audit['rows'])
            result['removed_retry_literals'] = len(audit.get('retry_literals_removed', []))
        save(out / 'summary.json', result)
        results[name] = result
    old = read(ROOT / 'runs/longmemeval-facts-only-four-20261013/comparison.json')
    current = results['compact-24']
    comparisons = {}
    for name, baseline in (('facts_only_before', old['new']), ('facts_and_sources_before', old['old'])):
        pairs = dict(answer_prompt_tokens=(baseline['answer_prompt_tokens'], current['usage']['answer']['prompt_tokens']),
            memory_utf8_bytes_mean=(baseline['memory_utf8_bytes_mean'], current['mean_memory_utf8_bytes']),
            answer_elapsed_s_mean=(baseline['answer_latency_s']['mean'], current['mean_answer_elapsed_s']))
        comparisons[name] = {key: dict(before=a, after=b, change_percent=(b / a - 1) * 100)
                             for key, (a, b) in pairs.items()}
    # Clones contain inherited write ledgers. Deduplicate identical ledger FILES,
    # rather than charging repeated copies as new calls or dropping real repeats.
    unique_ledgers = {}
    actual_usage = defaultdict(lambda: dict(calls=0, prompt_tokens=0, completion_tokens=0, latency_s=0))
    for path in sorted(parent.glob('*/LongMemEval-*-usage.jsonl')):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in unique_ledgers:
            unique_ledgers[digest]['copies'].append(str(path.relative_to(parent)))
            continue
        unique_ledgers[digest] = dict(path=str(path.relative_to(parent)), copies=[])
        for row in rows(path):
            u = actual_usage[row['purpose']]
            u['calls'] += 1
            for key in ('prompt_tokens', 'completion_tokens', 'latency_s'):
                u[key] += row.get(key, 0)
    test_log = (parent / 'implementation-tests.log').read_text()
    test_count = int(re.search(r'Ran (\d+) tests', test_log).group(1))
    assert re.search(r'^OK$', test_log, re.M)
    retry_usage = [row for row in rows(parent / 'retained-unseen-24/LongMemEval-restore-usage.jsonl')
                   if row['purpose'] == 'extract']
    data = dict(approved_scope='Read fields, structured write and compression repair, original 24-question regression and preselected extra 24. No optional ablations or Oracle-500.',
        implementation_tests=test_count, current_source=source_manifest(),
        results=results, comparisons=comparisons, selection=read(parent / 'unseen-24/selection-plan.json'),
        baseline=old, local_deployment=read(parent / 'local-deployment-audit.json'),
        unseen_operational_retry_usage=retry_usage, actual_work_usage=dict(actual_usage),
        unique_usage_ledgers=list(unique_ledgers.values()), pricing_known=False,
        usage_scope='actual_work_usage deduplicates identical inherited ledger files; cohort usage includes its reused original write ledger plus this cohort restoration/read/judge. The unseen extraction retry is separately disclosed. No prices were configured.')
    save(parent / 'validation-summary.json', data)
    labels = {'read-fix-24': '读取修复，复用原事实库', 'rebuilt-24': '结构化抽取，从历史重新写入',
              'compact-24': '紧凑结构回补，原24题回归', 'compact-final-unseen-24': '紧凑结构回补，预选额外24题'}
    lines = ['# 事实保真修复与验证', '',
        '已执行读取字段修复、结构化写入及紧凑结构保留，并完成原24题回归和预选额外24题的回答、评分及来源审计。依用户批注，不执行可选消融实验或Oracle 500题正式验收。', '',
        '| 阶段 | 正确 | 有效回答 | 原文摘录 |', '|---|---:|---:|---:|',
        '| 修复前仅事实 | 21/24（87.5%） | 24/24 | 0 |']
    for name, r in results.items():
        lines.append(f"| {labels[name]} | {r['correct']}/{r['total']}（{r['correct']/r['total']:.1%}） | {r['answered']}/{r['total']} | {r['original_excerpts']} |")
    lines += ['', '## 已落实的措施', '',
        '- 统一事实正文与完整value的展示；值、实体、限定条件不再被固定字符上限静默截断。数值0和false保持有效。预算不足时移除整条事实并记录遗漏。',
        '- 事实值进入稠密与词法索引、重排、HTTP条目及分页；索引新增内容格式指纹，旧的只含正文的向量需要重建。',
        '- 列表、表格、动作序列采用紧凑JSON；相同摘要下不同值、条件或事件分别保留，完整事实字段一致才关联来源去重。',
        '- 同一次写入用通用解析器保留明确的助手列表名称、原编号、短动作及表格单元格，不保留列表解释段落。长的无标签条目标注跳过位置；覆盖字段明确提示并非保留全部细节。来源片段只用于溯源。',
        '- 正常每批一次抽取LLM调用；结构保留和历史结构回补不增加抽取调用，仍需Embedding。默认回答记忆只含事实，原文补充和来源展开均为0。',
        f'- {test_count}项相关测试通过，涵盖全字段、索引升级、原子失败、来源、遗忘边界、结构回补、网页和HTTP。git diff --check通过。', '',
        '## 原24题的变化及资源开销', '',
        '原来三道失败题（菜名、棋步、排班）现均正确；厨房个性化建议出现一次退步，故最终为23/24。原事实库的读取修复已修复排班映射，但不会凭空恢复从未抽取或已被截断的内容。', '',
        '| 指标 | 修复前仅事实 | 当前原24题回归 | 以前事实＋原文 |', '|---|---:|---:|---:|',
        f"| 回答输入tokens，总计 | {old['new']['answer_prompt_tokens']:,} | {current['usage']['answer']['prompt_tokens']:,} | {old['old']['answer_prompt_tokens']:,} |",
        f"| 记忆UTF-8字节，均值 | {old['new']['memory_utf8_bytes_mean']:.0f} | {current['mean_memory_utf8_bytes']:.0f} | {old['old']['memory_utf8_bytes_mean']:.0f} |",
        f"| 完整读取及回答耗时，均值 | {old['new']['answer_latency_s']['mean']:.2f}s | {current['mean_answer_elapsed_s']:.2f}s | {old['old']['answer_latency_s']['mean']:.2f}s |", '',
        f"相对精简事实版，正确率提高8.3个百分点，但输入tokens增加{comparisons['facts_only_before']['answer_prompt_tokens']['change_percent']:.1f}%，耗时增加{comparisons['facts_only_before']['answer_elapsed_s_mean']['change_percent']:.1f}%。相对历史混合原文版，输入tokens降低{-comparisons['facts_and_sources_before']['answer_prompt_tokens']['change_percent']:.1f}%。保真度提升已有代价，重复结构和无关建议仍需压缩。耗时包含模型加载、检索与网络影响，仅代表本次本机运行。", '',
        '## 分题型结果', '']
    for name in ('compact-24', 'compact-final-unseen-24'):
        r = results[name]
        lines += ['### ' + labels[name], '', '| 题型 | 正确 |', '|---|---:|']
        for kind, score in r['by_category'].items():
            lines.append(f"| {kind} | {score['correct']}/{score['total']} |")
        lines += ['', f"历史会话{r['source_sessions']}个、消息{r['source_messages']}条，原文逐条一致；处理失败会话为{r['failed_source_sessions']}。最终存储事实{r['stored_facts']}条，回补新增{r['restored_facts']}条，回补抽取LLM调用为0。", '']
    # Case diagnoses are report data, never source-code constants or model input.
    lines += ['## 失败分析与下一步', ''] + read(parent / 'failure-analysis.json') + ['',
        '下一阶段优先级：先减少重复结构与无关助手建议，同时保留完整事实值；再完善事件的时间锚点、完成状态与计算边界。继续使用现有小样本回归，尚未在本轮实施这些后续优化。', '',
        '## 验证口径与限制', '',
        '- 数据集沿用本地LongMemEval Oracle。原24题为六类各4题；扫描既有本地评测记录后排除了461个已用题号，余39题只含用户信息、时间推理和1道拒答。额外24题在写入修复前冻结为11道用户信息、12道时间推理和1道拒答，不代表六类均衡样本。这里“未用”指本地记录，不能证明模型预训练未见过。',
        '- 答案及评分沿用GPT-6 Luna，BGE配置保持一致；问题和标准答案不进入记忆构建，生成的评测答案不回写事实库。失败、截断和中间版本均保留，无选择性改分。',
        '- 读取字段修复阶段的棋局回答有一次输出length错误，保留原响应并计入24题分母；因此该阶段有效回答为23题，没有选择性重答。',
        '- 读取修复复用了520条原事实。结构化抽取版本重新从477条历史消息写入400条事实，得分22/24。最终原24题为这些模型事实加352条紧凑结构回补；这属于修复/回补验证，不能标为完整V5重新抽取的端到端成绩。',
        '- 额外题首次写入有1个6000输出上限导致的整批失败；回答前以12000上限整批重试一次，原错误保留。该次重试包含21条旧格式较长字面结构，第一次compact-unseen-24副本未移除，故21/24只作为中间结果。最终compact-final-unseen-24按重试审计删除这些21条后，从历史重建紧凑结构，保留模型抽取事实；没有新增抽取调用。',
        '- 最终原24题冻结源码早于时间/URL分隔符的边界修复，最终额外题包含该修复；两组质量评测都早于随后新增的回补并发保护。这些边界由相关单元测试验证。两批结果按各自freeze.json溯源，不将它们包装成当前全部代码的全新端到端评测。',
        '- 只有1道额外拒答题，无法估计全面拒答能力；没有同条件Mem0运行，不能推断相对优劣。价格未配置，货币费用未知，usage中cost=0不代表免费。各阶段用量及重试成本见validation-summary.json；复制的历史写入账本只计一次。', '',
        '## 本机上线', '',
        '只重载记忆API。现有12条事实、13轮原文及原始消息在重载前后指纹一致；API召回4条事实且含完整值和语义字段，原文摘录为0，排队/重试/失败任务均为0。没有重新抽取私人历史，也没有调用回答LLM。详见local-deployment-audit.json。', '',
        '中间版本、失败日志、冻结源码、提示词、逐题回答和评分保存在各目录。最终审阅入口为compact-24、compact-final-unseen-24，以及validation-summary.json。']
    (parent / 'report.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({name: {'correct': r['correct'], 'total': r['total'],
        'original_excerpts': r['original_excerpts']} for name, r in results.items()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
