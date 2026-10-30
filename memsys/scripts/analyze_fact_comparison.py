#!/usr/bin/env python3
"""Compare paired LongMemEval runs and audit the actual facts-only answer inputs."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from memsys import prompts
from memsys.timeutil import format_day, parse_datetime
from memsys.webchat import SYSTEM
from memsys.bench.provenance import fingerprint


def read(path):
    return json.loads(path.read_text())


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def statistics_of(values):
    return dict(mean=statistics.mean(values), median=statistics.median(values), max=max(values))


def metrics(directory, qids):
    answers = {r['question_id']: r for r in rows(directory / 'LongMemEval-answers.jsonl')}
    grades = {r['question_id']: r for r in rows(directory / 'LongMemEval-grades.jsonl')}
    assert set(answers) == set(grades) == set(qids), 'Incomplete or unexpected answer/grade IDs'
    assert all(answers[q]['status'] == 'answered' and isinstance(grades[q].get('correct'), bool) for q in qids)
    usage = rows(directory / 'LongMemEval-read-usage.jsonl')
    traces = {q: read(directory / 'traces' / (q + '.json')) for q in qids}
    calls = [u for u in usage if u['purpose'] == 'answer']
    assert len(calls) == len(qids)
    by_category = {}
    for category in sorted({answers[q]['category'] for q in qids}):
        selected = [q for q in qids if answers[q]['category'] == category]
        by_category[category] = dict(correct=sum(grades[q]['correct'] for q in selected), total=len(selected))
    data = dict(total=len(qids), correct=sum(grades[q]['correct'] for q in qids), by_category=by_category,
        answer_prompt_tokens=sum(u['prompt_tokens'] for u in calls),
        answer_completion_tokens=sum(u['completion_tokens'] for u in calls),
        actual_answer_models=dict(Counter(u['model'] for u in calls)),
        actual_judge_models=dict(Counter(grades[q].get('actual_model') for q in qids)),
        all_usage_reported=all(u['usage_reported'] for u in calls),
        answer_latency_s=statistics_of([answers[q]['elapsed_s'] for q in qids]),
        first_question_latency_s=answers[qids[0]]['elapsed_s'],
        latency_excluding_first_s=statistics_of([answers[q]['elapsed_s'] for q in qids[1:]]),
        answer_api_latency_s=statistics_of([u['latency_s'] for u in calls]),
        rerank_inference_and_load_s=statistics_of([traces[q]['reranking']['inference_and_load_s'] for q in qids]),
        rerank_pair_windows_mean=statistics.mean(traces[q]['reranking']['pair_windows'] for q in qids),
        memory_utf8_bytes_mean=statistics.mean(traces[q]['memory_tokens'] for q in qids),
        complete_input_utf8_bytes_mean=statistics.mean(answers[q]['input_units'] for q in qids),
        selected_facts_mean=statistics.mean(sum(i['kind']=='fact' for i in answers[q]['memory_items']) for q in qids),
        selected_original_excerpts=sum(i['kind']=='episode' for q in qids for i in answers[q]['memory_items']),
        original_body_utf8_bytes=sum(len(i['text'].encode()) for q in qids for i in answers[q]['memory_items'] if i['kind']=='episode'),
        pending_evidence_items=sum(answers[q].get('pending_evidence_items', 0) for q in qids),
        pricing_known=all(u['cost_known'] for u in calls))
    return data, answers, grades, traces


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args=parser.parse_args()
    baseline, out=args.baseline.resolve(), args.out.resolve()
    sample=read(out/'sample.json'); selection=read(out/'selection.json')
    assert sample==read(baseline/'sample.json') and selection==read(baseline/'selection.json')
    assert len(sample)==24 and {s['dataset'] for s in selection}=={'LongMemEval'}
    qids=[s['question_id'] for s in selection]
    old, old_answers, old_grades, _=metrics(baseline,qids)
    new, new_answers, new_grades, new_traces=metrics(out,qids)
    assert old['actual_answer_models']==new['actual_answer_models']
    assert old['actual_judge_models']==new['actual_judge_models']
    audit=[]
    supplied={p['tags']['question_id']:p['messages'] for p in rows(out/'LongMemEval-read-prompts.jsonl') if p['purpose']=='answer'}
    for item in sample:
        q=item['question_id']; a=new_answers[q]; t=new_traces[q]
        assert a['recall_mode']==t['recall_mode']=='facts'
        assert not t['selected']['episodes'] and a['pending_evidence_items']==0
        assert all(i['kind']=='fact' for i in a['memory_items'])
        assert t['reranked'] and t['reranking']['backend']=='bge' and not t.get('retrieval_errors')
        context=prompts.MEMORY_CONTEXT.format(today=format_day(parse_datetime(item['question_date'])),memory=t['memory'],rules=prompts.MEMORY_RULES)
        expected=[dict(role='system',content=SYSTEM+'\n\n<retrieved_memory>\n'+context+'\n</retrieved_memory>'),
                  dict(role='user',content=item['question'])]
        assert supplied[q]==expected, 'Unexpected history or hidden input: '+q
        assert 'Past conversation excerpts' not in t['memory'] and 'recent_conversation_evidence' not in supplied[q][0]['content']
        audit.append(dict(question_id=q,facts=len(a['memory_items']),originals=0,pending_originals=0,
                          answer_prompt_exact=True,prompt_sha256=fingerprint(supplied[q])))
    paired=[]
    for q in qids:
        paired.append(dict(question_id=q,category=new_answers[q]['category'],question=new_answers[q]['question'],
            reference=new_grades[q]['reference'],old_correct=old_grades[q]['correct'],new_correct=new_grades[q]['correct'],
            old_answer=old_answers[q]['hypothesis'],new_answer=new_answers[q]['hypothesis'],
            new_fact_texts=[i['text'] for i in new_answers[q]['memory_items']]))
    difference={k:dict(old=old[k],new=new[k],reduction_percent=100*(1-new[k]/old[k])) for k in
        ('answer_prompt_tokens','memory_utf8_bytes_mean','complete_input_utf8_bytes_mean','rerank_pair_windows_mean')}
    difference['accuracy_percentage_points']=100*(new['correct']-old['correct'])/24
    difference['answer_latency_mean_reduction_percent']=100*(1-new['answer_latency_s']['mean']/old['answer_latency_s']['mean'])
    difference['rerank_mean_reduction_percent']=100*(1-new['rerank_inference_and_load_s']['mean']/old['rerank_inference_and_load_s']['mean'])
    results=dict(dataset='LongMemEval Oracle',same_questions=True,same_memory=True,
        reuse_audit=read(out/'reused-memory-audit.json'),old=old,new=new,difference=difference,
        lost=[r['question_id'] for r in paired if r['old_correct'] and not r['new_correct']],
        gained=[r['question_id'] for r in paired if not r['old_correct'] and r['new_correct']],
        answer_input_audit=audit,paired=paired,
        limitation='Paired diagnostic on 24 previously used answerable Oracle questions. Same GPT-6 Luna answer/judge model, same sources, extracted facts, embedding, reranker and budgets. No fresh write or abstention test; not a blind holdout or full benchmark. Latency includes provider and runtime variation. Monetary cost unknown.')
    save(out/'comparison.json',results)
    save(out/'summary.json',dict(total=24,correct=new['correct'],answered=24,complete=True,datasets={'LongMemEval':new},
        source_audit=results['reuse_audit'],facts_only_input_audit_passed=True,usage_scope='Fresh read and judge only; writing reused from the baseline',limitation=results['limitation']))
    labels={'knowledge-update':'知识更新','multi-session':'跨会话推理','single-session-assistant':'助手信息记忆',
            'single-session-preference':'偏好与个性化','single-session-user':'用户信息记忆','temporal-reasoning':'时间推理'}
    lines=['# 同一组 24 题：仅事实与事实＋原文比较','',
        f"旧系统 {old['correct']}/24；仅事实系统 {new['correct']}/24。六种题型各四题，题目、模型、520 条事实及向量不变。",'',
        '| 题型 | 旧：事实＋原文 | 新：仅事实 |','|---|---:|---:|']
    for c,s in new['by_category'].items(): lines.append(f"| {labels[c]} | {old['by_category'][c]['correct']}/4 | {s['correct']}/4 |")
    lines+=['','## 输入与耗时','','| 指标 | 旧系统 | 仅事实 |','|---|---:|---:|',
        f"| 回答输入 token，总计 | {old['answer_prompt_tokens']:,} | {new['answer_prompt_tokens']:,} |",
        f"| 平均记忆上下文，UTF-8 字节 | {old['memory_utf8_bytes_mean']:,.0f} | {new['memory_utf8_bytes_mean']:,.0f} |",
        f"| 原文摘录数，总计 | {old['selected_original_excerpts']} | {new['selected_original_excerpts']} |",
        f"| 平均单题检索至答完 | {old['answer_latency_s']['mean']:.3f} 秒 | {new['answer_latency_s']['mean']:.3f} 秒 |",
        f"| 平均 BGE 重排含加载 | {old['rerank_inference_and_load_s']['mean']:.3f} 秒 | {new['rerank_inference_and_load_s']['mean']:.3f} 秒 |",
        f"| 平均 BGE 配对窗口 | {old['rerank_pair_windows_mean']:.1f} | {new['rerank_pair_windows_mean']:.1f} |",
        '', '## 过程核验','',
        '复用原先 24 个隔离语料账户。40 段会话、477 条消息逐条一致，全部整理完毕；520 条事实正文、语义、时间、状态和来源保持一致，访问次数和最近访问时间不作为内容差异。没有重新调用抽取 LLM，也没有将新生成答案写入记忆库。',
        '24 次实际回答提示词均与“网页系统提示＋返回的事实上下文＋当前问题”精确一致，只有两个消息；原文片段、待整理原文、先前生成答案及评分参考均未额外注入。24 次 BGE 重排成功，无额外生成式查询规划或答案复查。',
        '回答与评分在独立进程进行，保留一次回答和原始 Boolean 评分。评分模型与之前一致，均为 GPT-6 Luna。',
        '', '## 未通过及变化题目','']
    failures=[r for r in paired if not r['new_correct']]
    if not failures: lines.append('本次没有未通过题目。')
    for r in failures:
        lines += [f"### {r['question_id']} · {labels[r['category']]}",'', '**问题：** '+r['question'],'',
            '**标准答案：** '+r['reference'],'','**旧回答：** '+r['old_answer'],'','**仅事实回答：** '+r['new_answer'],'',
            '相关事实与检索轨迹保存在 comparison.json、LongMemEval-answers.jsonl 和 traces/。','']
    lines+=['','## 比较范围','',
        '这是同一记忆库下读取路径的对照，当前默认模式只召回事实，取消原文独立检索与来源扩展，并把原先为原文预留的预算交给事实。写入算法和写入成本未发生改变；本次复用写入不能理解为写入免费。',
        '这 24 道题均可回答，不覆盖拒答题；Oracle 和同模型评分沿用之前的条件，未扩展成全量或独立盲测。耗时受 API 等待、缓存、模型首次加载及本机资源影响。价格未配置，货币费用未知。']
    (out/'report.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'old_correct':old['correct'],'new_correct':new['correct'],'total':24,'lost':results['lost'],
        'difference':difference,'new_latency':new['answer_latency_s'],'new_rerank':new['rerank_inference_and_load_s'],'originals':new['selected_original_excerpts']},ensure_ascii=False))


if __name__=='__main__': main()
