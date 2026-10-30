"""Report complete paired results, original labels, costs and budget equivalence."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from memsys.bench.jsonl import read_jsonl
from memsys.budget import budget_counter
from memsys.config import AppConfig
from memsys.llm import UsageLedger
from memsys.read import effective_budget

from run_mem0_comparison import VARIANTS,TYPES,check,read,save


def unique_rows(path,key='question_id'):
    result={}
    for row in read_jsonl(str(path)):
        if key not in row:
            raise ValueError('Missing identity in '+str(path))
        if row[key] in result:
            raise ValueError('Duplicate identity in '+str(path))
        result[row[key]]=row
    return result


def complete_labels(folder,expected):
    files=list(folder.glob('hypotheses.jsonl.eval-results-*'))
    if len(files)!=1:
        raise ValueError('Expected exactly one frozen judge log in '+str(folder))
    rows=unique_rows(files[0])
    if set(rows)!=expected or any(type(row.get('autoeval_label',{}).get('label')) is not bool for row in rows.values()):
        raise ValueError('Complete every Boolean judge label before reporting accuracy')
    judges={row['autoeval_label']['model'] for row in rows.values()}
    if len(judges)!=1:
        raise ValueError('Mixed judges in one comparison')
    return {qid:row['autoeval_label']['label'] for qid,row in rows.items()},judges.pop()


def usage(paths):
    records=[record for path in paths if path.exists()
             for record in UsageLedger.from_jsonl(str(path)).records()]
    stages={}
    for record in records:
        stage='judge' if record.purpose.startswith('judge') else 'write' if record.purpose in (
            'extract','resolve','embed_memory','embed_fact','embed_episode','embed_chunk') else 'read'
        row=stages.setdefault(stage,dict(calls=0,prompt_tokens=0,completion_tokens=0,
            usage_missing=0,cost=0.0,cost_known=True))
        row['calls']+=1
        row['prompt_tokens']+=record.prompt_tokens
        row['completion_tokens']+=record.completion_tokens
        row['usage_missing']+=not record.usage_reported
        row['cost']+=record.cost
        row['cost_known']=row['cost_known'] and record.cost_known is True
    for row in stages.values():
        if not row['cost_known']:row['cost']=None
    return stages


def quantile(values,q):
    return sorted(values)[round(q*(len(values)-1))] if values else None


def paired(before,after):
    if set(before)!=set(after):
        raise ValueError('Paired scores require identical complete question sets')
    gained=sorted(qid for qid in before if not before[qid] and after[qid])
    lost=sorted(qid for qid in before if before[qid] and not after[qid])
    return dict(gained=gained,lost=lost,net=len(gained)-len(lost))


def summarize(out):
    state=read(out/'comparison.json')
    check(out,state)
    sample=read(out/'sample.json')
    types={row['question_id']:row['question_type'] for row in sample}
    expected=set(types)
    summaries={}
    labels={}
    answers={}
    judges=set()
    for name in VARIANTS:
        folder=out/name/'current'
        answers[name]=unique_rows(folder/'hypotheses.jsonl')
        timings=unique_rows(folder/'timings.jsonl')
        if set(answers[name])!=expected or set(timings)!=expected:
            raise ValueError('Finish all answers and timing records before comparison: '+name)
        labels[name],judge=complete_labels(folder,expected)
        judges.add(judge)
        if name!='mem0' and not all(row.get('cached_ingest') for row in timings.values()):
            raise ValueError('Reader comparison contains fresh ingestion: '+name)
        per_type={typ:dict(correct=sum(labels[name][qid] for qid in expected if types[qid]==typ),
            total=sum(types[qid]==typ for qid in expected)) for typ in TYPES}
        reads=[row['answer_s'] for row in timings.values()]
        # The candidate logically uses the same write stage. Report its shared
        # cost explicitly; never compare cached write=0 with Mem0's full ingestion.
        paths=[folder/'usage.jsonl']
        if name!='mem0':paths.append(out/'baseline/current/ingest-usage.jsonl')
        summaries[name]=dict(correct=sum(labels[name].values()),total=len(expected),per_type=per_type,
            read_p50_s=quantile(reads,.5),read_p95_s=quantile(reads,.95),
            usage=usage(paths),shared_write_stage=name!='mem0')
    if len(judges)!=1:
        raise ValueError('Use one identical judge across all variants')
    spec=importlib.util.spec_from_file_location('frozen_mem0_pack',out/'tools/evaluate_mem0.py')
    native=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    settings=AppConfig.load(str(out/'baseline/config.json')).memory
    counter=budget_counter(settings)
    budget_checks=[]
    for row in sample:
        qid=row['question_id']
        # Store.history_tokens counts each original turn independently.
        history=sum(counter.count(turn['content']) for session in row['haystack_sessions'] for turn in session)
        shared=effective_budget(settings,history,counter.count(row['question']))
        trace=read(out/'mem0/current/traces'/(qid+'.json'))
        repacked,selection=native.pack_memories(trace['memories'],counter,shared)
        identical=repacked==trace['memory']
        budget_checks.append(dict(question_id=qid,common_effective_budget=shared,
            actual_mem0_context_count=counter.count(trace['memory']),
            fits_common_budget=counter.count(trace['memory'])<=shared,
            ranking_and_packing_identical_at_common_budget=identical))
    budget_valid=all(row['fits_common_budget'] and row['ranking_and_packing_identical_at_common_budget']
                     for row in budget_checks)
    discordant=[]
    for left,right in (('baseline','candidate'),('candidate','mem0')):
        discordant.extend(dict(question_id=qid,left=left,right=right)
            for qid in expected if answers[left][qid]['hypothesis']==answers[right][qid]['hypothesis']
            and labels[left][qid]!=labels[right][qid])
    summary=dict(status='completed' if budget_valid else 'budget_equivalence_failed',
        scope='Balanced public development sample; whole-system pipelines; not a full-500 or blind score.',
        judge=judges.pop(),variants=summaries,
        candidate_vs_baseline=paired(labels['baseline'],labels['candidate']),
        candidate_vs_mem0=paired(labels['mem0'],labels['candidate']),
        budget_equivalent=budget_valid,budget_checks=budget_checks,
        identical_answer_judge_discordance=discordant,
        exceeds_mem0_on_this_sample=budget_valid and summaries['candidate']['correct']>summaries['mem0']['correct'],
        candidate_sample_100_percent=summaries['candidate']['correct']==len(expected),
        full_500_exceeds_mem0_proven=False,
        experimental_spend_note='Baseline and candidate share one physical write stage; logical end-to-end usage includes it for each. Prices and failed-call usage can be unknown.')
    save(out/'summary.json',summary)
    lines=['# Mem0 同题对照结果','',summary['scope'],'',
        '| 系统 | 正确 / 总数 | 读取 p50 | 读取 p95 |', '|---|---:|---:|---:|']
    for name,row in summaries.items():
        lines.append('| %s | %d/%d | %.2fs | %.2fs |'%(name,row['correct'],row['total'],row['read_p50_s'],row['read_p95_s']))
    lines+=['','| 题型 | baseline | candidate | Mem0 OSS |','|---|---:|---:|---:|']
    for typ in TYPES:
        lines.append('| %s | %s | %s | %s |'%(typ,*('%d/%d'%(summaries[name]['per_type'][typ]['correct'],
            summaries[name]['per_type'][typ]['total']) for name in VARIANTS)))
    lines+=['','候选相对原实现：修复 %d，回退 %d。'%(len(summary['candidate_vs_baseline']['gained']),len(summary['candidate_vs_baseline']['lost'])),
        '候选相对 Mem0：新增正确 %d，少正确 %d。'%(len(summary['candidate_vs_mem0']['gained']),len(summary['candidate_vs_mem0']['lost'])),
        '共同预算下 Mem0 排名与上下文重新装配一致：%s。'%budget_valid,
        '完全相同答案但 judge 标签不同：%d；原始标签保留。'%len(discordant),'',
        '实际分阶段调用/token、共享写入用量、未知价格和逐题预算检查见 summary.json。',
        '该批次结果不能外推为完整 500 题成绩；完整超越目标仍需验证。']
    (out/'assessment.md').write_text('\n'.join(lines)+'\n')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    summary=summarize(args.out.resolve())
    print(json.dumps({key:summary[key] for key in ('status','variants','candidate_vs_baseline','candidate_vs_mem0',
        'exceeds_mem0_on_this_sample','full_500_exceeds_mem0_proven')},ensure_ascii=False,indent=2))
    return 0 if summary['budget_equivalent'] else 1


if __name__=='__main__':
    raise SystemExit(main())
