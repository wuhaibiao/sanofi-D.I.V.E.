#!/usr/bin/env python3
"""Run only the approved repair checks: two small facts-only LongMemEval cohorts."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]


def read(path):return json.loads(path.read_text())


def rows(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def summarize(out):
    selection=read(out/'selection.json')
    answers={r['question_id']:r for r in rows(out/'LongMemEval-answers.jsonl')}
    grades={r['question_id']:r for r in rows(out/'LongMemEval-grades.jsonl')}
    writes=rows(out/'LongMemEval-write.jsonl')
    categories=defaultdict(lambda:dict(total=0,correct=0,answered=0,graded=0))
    for m in selection:
        a=answers.get(m['question_id'],{});g=grades.get(m['question_id'],{})
        score=categories[m['category']];score['total']+=1
        score['correct']+=g.get('correct') is True
        score['answered']+=a.get('status')=='answered'
        score['graded']+=isinstance(g.get('correct'),bool)
    usage=defaultdict(lambda:dict(calls=0,prompt_tokens=0,completion_tokens=0,latency_s=0))
    for file in out.glob('LongMemEval-*-usage.jsonl'):
        for r in rows(file):
            u=usage[r['purpose']];u['calls']+=1
            for key in ('prompt_tokens','completion_tokens','latency_s'):u[key]+=r.get(key,0)
    traces=[read(out/'traces'/(q+'.json')) for q in answers if (out/'traces'/(q+'.json')).exists()]
    original_count=sum(len(t['selected']['episodes']) for t in traces)
    pending_count=sum(a.get('pending_evidence_items',0) for a in answers.values())
    assert original_count==pending_count==0, 'Original evidence entered a repair check'
    for t in traces:assert t['recall_mode']=='facts'
    data=dict(total=len(selection),correct=sum(s['correct'] for s in categories.values()),
        answered=sum(s['answered'] for s in categories.values()),graded=sum(s['graded'] for s in categories.values()),
        by_category=dict(categories),usage=dict(usage),original_excerpts=original_count,pending_originals=pending_count,
        answer_errors=[{'question_id':q,'error':a.get('error')} for q,a in answers.items() if a['status']!='answered'],
        judge_errors=[{'question_id':q,'error':g.get('error')} for q,g in grades.items() if not isinstance(g.get('correct'),bool)],
        failed_source_sessions=sum(s['error'] is not None or s['processed_messages']!=s['raw_messages'] for w in writes for s in w['sessions']),
        source_sessions=sum(len(w['sessions']) for w in writes),source_messages=sum(s['raw_messages'] for w in writes for s in w['sessions']),
        all_originals_exact=all(s['source_exact'] for w in writes for s in w['sessions']),
        structure_warnings=sum(len((s.get('report') or {}).get('structure_warnings',[])) for w in writes for s in w['sessions']),
        stored_facts=sum(len(w['facts']) for w in writes),
        actual_answer_models=dict(Counter(a.get('actual_model') for a in answers.values() if a.get('actual_model'))),
        actual_judge_models=dict(Counter(g.get('actual_model') for g in grades.values() if g.get('actual_model'))),
        lost_questions=[q for q,g in grades.items() if g.get('correct') is False],pricing_known=False)
    if answers:data['mean_answer_elapsed_s']=statistics.mean(a['elapsed_s'] for a in answers.values())
    if traces:data['mean_memory_utf8_bytes']=statistics.mean(t['memory_tokens'] for t in traces)
    data['complete']=len(answers)==len(grades)==len(selection) and not data['judge_errors']
    save(out/'summary.json',data)
    return data


def report(parent):
    previous=ROOT/'runs/longmemeval-facts-only-four-20261013/summary.json'
    all_results={name:summarize(parent/name) for name in ('read-fix-24','rebuilt-24','unseen-24')}
    data=dict(previous_facts_only=read(previous)['correct'],results=all_results,
        approved_scope='Read-field repair, structured write/compression repair, fixed 24-question regression and preselected unused local questions. No optional ablations or Oracle-500 evaluation.',
        implementation_tests=106,selection_limitations=read(parent/'unseen-24/selection-plan.json'),
        validator_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    save(parent/'validation-summary.json',data)
    lines=['# 事实保真修复与验证','',
        '范围：读取字段修复、结构化写入与压缩、原24题回归及额外未使用题。未执行可选消融实验或500题正式验收。','',
        '| 阶段 | 正确 | 有效回答 | 回答错误 | 原文摘录 |','|---|---:|---:|---:|---:|',
        '| 修复前仅事实 | 21/24 | 24/24 | 0 | 0 |']
    labels={'read-fix-24':'读取字段修复，同一事实库','rebuilt-24':'结构修复，重新写入原24题','unseen-24':'结构修复，额外未使用24题'}
    for name,r in all_results.items():
        lines.append(f"| {labels[name]} | {r['correct']}/{r['total']} | {r['answered']}/{r['total']} | {len(r['answer_errors'])} | {r['original_excerpts']} |")
    lines += ['', '## 实现','',
        '- 统一输出事实正文、完整值、时间及条件；重复的字面值不再再次附加。',
        '- 结构化列表、表格和动作序列使用紧凑JSON；停止对值、实体名和限定条件的静默截断。',
        '- 同一句摘要但值、条件或事件不同的事实分别保留；字段完全一致时才关联来源去重。',
        '- 事实值进入稠密/词法检索、重排和分页；索引带内容格式指纹，旧事实向量需重建。',
        '- 表格/列表未保留结构时记录诊断警告；警告不证明语义覆盖，也不会触发第二次LLM抽取。',
        '- 保持原文零输入、每批一次抽取、追加事实、无独立答案复查。相关106项测试通过。','',
        '## 验证口径','',
        '读取修复复用原事实库；写入修复两组均在隔离命名空间从历史重新写入。题目和标准答案不进入写入提示，新生成答案不保存到事实库。写入、回答与评分分别在独立进程进行，模型沿用GPT-6 Luna与本机BGE。',
        '本地既有样本/答题记录覆盖461个题号，剩余39题只有用户信息、时间推理和1道拒答。额外24题在写入修复前冻结：用户信息11题、时间推理12题、拒答1题，不代表六类题型或独立于模型预训练的盲测。',
        '读取修复的棋局题发生一次输出length错误，原始错误保留并计入分母，未选择性重答。','',
        '## 分题型结果','']
    for name,r in all_results.items():
        lines += ['### '+labels[name],'','| 题型 | 正确 |','|---|---:|']
        for kind,s in r['by_category'].items():lines.append(f"| {kind} | {s['correct']}/{s['total']} |")
        lines += ['',f"写入失败会话：{r['failed_source_sessions']}；结构诊断警告：{r['structure_warnings']}。",'']
    lines += ['## 用量与限制','','完整用量、延迟和错误记录见各阶段summary.json与usage.jsonl。价格未配置，货币费用未知；读取修复复用了写入，不能将其视为写入免费。']
    (parent/'report.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({name:{k:r[k] for k in ('correct','total','answered','failed_source_sessions')} for name,r in all_results.items()},ensure_ascii=False),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();parent=args.out.resolve();started=time.monotonic()
    status=dict(phases=[],current=None)
    for name in ('rebuilt-24','unseen-24'):
        for phase in ('write','read','judge'):
            status['current']=dict(cohort=name,phase=phase)
            save(parent/'execution.json',status)
            print('START '+name+' '+phase,flush=True)
            command=[sys.executable,str(ROOT/'scripts/evaluate_public_types.py'),'--out',str(parent/name),
                     '--dataset','LongMemEval','--phase',phase]
            with (parent/name/('LongMemEval-'+phase+'.log')).open('a') as log:
                process=subprocess.Popen(command,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
                for line in process.stdout:
                    log.write(line);log.flush();print(line,end='',flush=True)
                code=process.wait()
            status['phases'].append(dict(cohort=name,phase=phase,exit_code=code))
            status['elapsed_s']=round(time.monotonic()-started,3)
            save(parent/'execution.json',status)
            if code:raise RuntimeError(name+' '+phase+' failed; logs preserved')
    status['current']=None;save(parent/'execution.json',status);report(parent)


if __name__=='__main__':main()
