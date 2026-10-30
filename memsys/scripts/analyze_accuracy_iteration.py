"""Summarize completed iterations and preserve all errors and paired regressions."""
import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import json
from pathlib import Path
import re


def numeric_label_anomalies(records):
    """Flag simple numeric reference conflicts without changing judge labels.

    An explanation or allowed range is not a single numeric reference. Unit
    conversions and precision conventions still require source-level review.
    """
    literal = r'[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
    simple_reference = re.compile(r'\s*(' + literal + r')(?:\s*[%A-Za-z][A-Za-z /%-]*)?[.!]?\s*')
    number = re.compile(r'(?<![\w.])' + literal + r'(?!\w|[,\.]\d)')
    flagged = []
    for row in records:
        reference = simple_reference.fullmatch(str(row['reference']))
        if not row['correct'] or reference is None:
            continue
        expected = Decimal(reference.group(1).replace(',', ''))
        actual = [Decimal(match.group().replace(',', '')) for match in number.finditer(row['answer'])]
        if actual and expected not in actual:
            flagged.append({key: row[key] for key in ('question_id', 'question', 'reference', 'answer')})
    return flagged


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


def transport_errors(value):
    """Expose nested call failures without changing answers or judge labels."""
    errors = []
    if isinstance(value, dict):
        for key, child in value.items():
            if (key.endswith('error') and isinstance(child, str)
                    and re.search(r'HTTP\s*\d{3}', child)):
                errors.append(child)
            elif isinstance(child, (dict, list)):
                errors.extend(transport_errors(child))
    elif isinstance(value, list):
        for child in value:
            errors.extend(transport_errors(child))
    return list(dict.fromkeys(errors))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--paired-run', type=Path)
    args = p.parse_args()
    out = args.out.resolve(); run = out/'current'
    freeze = json.loads((out/'freeze.json').read_text())
    sample = json.loads((out/freeze.get('sample_file','sample100.json')).read_text())
    selection = json.loads((out/'selection.json').read_text())
    config = json.loads((out/'config.frozen.json').read_text())
    answers = {r['question_id']:r['hypothesis'] for r in lines(run/'hypotheses.jsonl')}
    log = sorted(run.glob('hypotheses.jsonl.eval-results-*'))
    if len(log) != 1:
        raise SystemExit('Expected exactly one fixed-judge result file.')
    labels = {r['question_id']:r['autoeval_label']['label'] for r in lines(log[0]) if 'autoeval_label' in r}
    if any(type(label) is not bool for label in labels.values()):
        raise SystemExit('Every judge label must be a completed Boolean judgment.')
    expected = {r['question_id'] for r in sample}
    if set(answers)!=expected or set(labels)!=expected:
        raise SystemExit('Complete every answer and judge result before analysis.')
    timings = {r['question_id']:r for r in lines(run/'timings.jsonl')}
    cached_reader = freeze.get('evaluation_mode') == 'paired_reader_on_identical_stores'
    if cached_reader and (set(timings) != expected or not all(r.get('cached_ingest') for r in timings.values())):
        raise SystemExit('A fixed-store reader comparison requires every sampled cache hit.')
    metadata = {r['question_id']:r for r in selection['selected']}
    previous = {}
    if args.paired_run:
        old = sorted(args.paired_run.glob('hypotheses.jsonl.eval-results-*'))
        if len(old)!=1: raise SystemExit('Paired run must have one judge file.')
        previous = {r['question_id']:r['autoeval_label']['label'] for r in lines(old[0]) if 'autoeval_label' in r}
        if any(type(label) is not bool for label in previous.values()):
            raise SystemExit('Every paired judge label must be a completed Boolean judgment.')
        paired_freeze = args.paired_run.parent/'freeze.json'
        paired_file = (json.loads(paired_freeze.read_text()).get('sample_file','sample100.json')
                       if paired_freeze.exists() else 'sample100.json')
        paired_sample = args.paired_run.parent/paired_file
        if paired_sample.exists():
            paired_expected = {r['question_id'] for r in json.loads(paired_sample.read_text())}
            if set(previous) != paired_expected:
                raise SystemExit('Finish every paired-sample judgment before reporting gains or regressions.')
    types = defaultdict(lambda:dict(correct=0,total=0))
    records=[]; statuses=Counter(); backend=Counter()
    for r in sample:
        qid=r['question_id']; label=bool(labels[qid]); typ=r['question_type']
        types[typ]['total']+=1; types[typ]['correct']+=int(label)
        trace=json.loads((run/'traces'/(qid+'.json')).read_text()); audit=trace.get('aggregation',{})
        statuses[audit.get('status','missing')]+=1
        rr=trace.get('reranking',{}); backend[rr.get('backend','unknown')]+=1
        details=dict(question_id=qid, question_type=typ, correct=label, **{k:metadata[qid][k] for k in ('previously_answered','paired','abstention')},
            question=r['question'], reference=r['answer'], answer=answers[qid], operation=trace['plan']['operation'],
            audit_status=audit.get('status'), error=audit.get('error') or audit.get('semantic_error'),
            pages_processed=audit.get('pages_processed'), pages_total=audit.get('pages_total'),
            validation_failures=audit.get('page_validation_failures',[]), coverage=trace.get('coverage'),
            read_transport=trace.get('read_transport', {}),
            audit_transport_errors=transport_errors(audit),
            memory_size=trace.get('memory_size'), timing=timings.get(qid), trace=str(run/'traces'/(qid+'.json')))
        if qid in previous: details['previous_correct']=bool(previous[qid])
        records.append(details)
    fresh=[r for r in records if not r['previously_answered']]
    paired=[r for r in records if 'previous_correct' in r]
    gained=[r['question_id'] for r in paired if r['correct'] and not r['previous_correct']]
    lost=[r['question_id'] for r in paired if not r['correct'] and r['previous_correct']]
    usage=lines(run/'usage.jsonl'); purposes=defaultdict(lambda:dict(calls=0,reported_tokens=0,seconds=0))
    for r in usage:
        u=purposes[r['purpose']];u['calls']+=1;u['reported_tokens']+=r.get('prompt_tokens',0)+r.get('completion_tokens',0);u['seconds']+=r.get('latency_s',0)
    summary=dict(overall=dict(correct=sum(bool(v) for v in labels.values()),total=len(expected)),types=dict(types),
        fresh=dict(correct=sum(r['correct'] for r in fresh),total=len(fresh)),
        paired=dict(before=sum(r['previous_correct'] for r in paired),after=sum(r['correct'] for r in paired),total=len(paired),gained=gained,lost=lost),
        internal_statuses=dict(statuses),reranking_backends=dict(backend),
        model_calls=dict(purposes),models=dict(Counter(r['model'] for r in usage if r['purpose'] not in ('embed','embed_query'))),
        errors=lines(run/'errors.jsonl'),freeze=freeze,
        roles=config['roles'],profiles={k:{field:v.get(field) for field in ('model','api','extra_body','output_token_headroom')}
                                      for k,v in config['models'].items() if k in set(config['roles'].values())},
        evaluation_mode=freeze.get('evaluation_mode','fresh_ingestion'),
        limitations=('Weighted oracle diagnostic sample; ' +
                     ('identical stored memories reused to isolate reading; generation can still vary. ' if cached_reader else
                      'freshly reingested memories can vary with model extraction. ') +
                     'Same answering/judging model, official judge prompts, not an official full benchmark result. Missing prices remain unknown.'))
    anomalies = numeric_label_anomalies(records)
    summary['automatic_label_anomalies'] = dict(total=len(anomalies), items=anomalies,
        status='Source-level review required; original automatic judgments remain unchanged.')
    unresolved_transport = [r['question_id'] for r in records if r['audit_transport_errors']
                            and r['audit_status'] in ('incomplete', 'invalid', 'failed')]
    summary['read_transport'] = dict(
        additional_retries=sum(r['read_transport'].get('additional_retries', 0) for r in records),
        retried_questions=[r['question_id'] for r in records if r['read_transport'].get('additional_retries')],
        audit_failure_questions=[r['question_id'] for r in records if r['audit_transport_errors']],
        unresolved_questions=unresolved_transport,
        scope='Trace audit only; all original answers, Boolean labels and the complete denominator are retained.')
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (out/'answers-and-audit.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n')
    report=['# 记忆正确率迭代结果','', '**%d/%d（%.1f%%）**。按题型固定抽样，完整分母如下。' % (summary['overall']['correct'],len(expected),100*summary['overall']['correct']/len(expected)),
        '', '| 题型 | 正确 | 总数 | 正确率 |','|---|---:|---:|---:|']
    for typ,t in types.items():report.append('| %s | %d | %d | %.1f%% |' % (typ,t['correct'],t['total'],100*t['correct']/t['total']))
    report += ['', '历史未答题：%d/%d。' % (summary['fresh']['correct'],summary['fresh']['total']),
        '自动评分为正确但与简单数字参考冲突的回答：%d题。原始标签保留；这些题须核对原文、单位及时间精度，不能直接作为达到目标的证明。' % len(anomalies)]
    if paired:
        report += ['', '完整同题对照组：%d/%d → %d/%d；修复%d题，回退%d题。' % (summary['paired']['before'],len(paired),summary['paired']['after'],len(paired),len(gained),len(lost)),
            '回退题号：'+(', '.join(lost) or '无')+('。固定同一批记忆库，回答生成仍可能波动。' if cached_reader else
                                            '。每题重新抽取建库，模型抽取差异也可能影响对照。')]
    if cached_reader:
        report += ['', '评估方式：固定上一轮对应的%d个记忆库，只评估新读取逻辑；%d/%d题确认缓存命中。原库副本、写入源码和模型指纹保存在 freeze.json。这不是一次重新抽取建库的测试。' % (len(expected),len(expected),len(expected))]
    report += ['', '运行记录：'+json.dumps(dict(statuses),ensure_ascii=False)+'。历史调用失败记录%d条，全部%d题已完成回答与评分。' % (len(summary['errors']),len(expected)),
        '读取附加重试%d次；最终凭据校验仍包含接口失败的题号：%s。嵌套的修复/校验接口失败单独列出，所有原始回答与评分仍计入完整分母。' %
        (summary['read_transport']['additional_retries'], ', '.join(unresolved_transport) or '无'),
        '本轮所有云端实际模型：'+', '.join(summary['models'])+'；重排后端：'+json.dumps(dict(backend))+'。价格缺失的费用为未知。',
        '固定角色配置：'+json.dumps(summary['roles'],ensure_ascii=False)+'。各档位的推理参数与输出余量保存在 summary.json 和 config.frozen.json。',
        '', '源码指纹：`'+freeze['source']['source_sha256']+'`。提示词指纹：`'+freeze['prompts']+'`。',
        '', '这是加权诊断样本，使用 oracle 历史，评分器与回答器相同；不能外推为完整基准或真实长历史干扰下的正确率。',
        '', '## 全部错题与回退题','']
    for r in records:
        if r['correct']:continue
        report += ['### '+r['question_id']+' · '+r['question_type']+(' · 同题回退' if r.get('previous_correct') else ''),'',
            '**问题**：'+r['question'],'','**实际回答**：'+r['answer'],'','**参考**：'+str(r['reference']),'',
            '**可观察的执行状态**：'+r['audit_status']+'；操作：'+r['operation']+'；错误：'+str(r['error'])+'。',
            '[逐题轨迹]('+r['trace']+')。','']
    (out/'assessment.md').write_text('\n'.join(report)+'\n')
    print(json.dumps({k:summary[k] for k in ('overall','types','fresh','paired','internal_statuses','reranking_backends')},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
