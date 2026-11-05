#!/usr/bin/env python3
"""Score existing ingestions first, then evaluate a sealed sequence of small batches.

Scheduling revision only. Product behavior, input files, failed attempts and prior
successful answers remain unchanged. No score is used to select the next batch.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import run_oracle500 as full
import run_oracle500_recovery as recovery
import question_serial_eval as serial
from blind_eval_inputs import FILES
from check_webchat_reliability import credentials
from memsys.config import AppConfig
from memsys.llm import UsageLedger
from memsys.qdrant import Qdrant

read,rows,save,sha,now=recovery.read,recovery.rows,recovery.save,recovery.sha,recovery.now


def collect_writes(directory,old):
    for suffix in ('usage','prompts','responses','provider'):
        paths=sorted((old/'write-attempts').glob('*/LongMemEval-write-'+suffix+'.jsonl'))
        paths+=sorted((directory/'write-attempts').glob('*/LongMemEval-write-'+suffix+'.jsonl'))
        if paths:(directory/('LongMemEval-write-'+suffix+'.jsonl')).write_bytes(b''.join(p.read_bytes() for p in paths))


def fixed_batches(cases,ready,size):
    first=[c['index'] for c in cases if c['index'] in ready]
    remaining=[c['index'] for c in cases if c['index'] not in ready]
    return ([first] if first else [])+[remaining[i:i+size] for i in range(0,len(remaining),size)]


def prepare(args):
    source,out=args.source.resolve(),args.out.resolve()
    if out.exists():raise ValueError('Use a fresh scheduling revision directory')
    frozen,oldplan,cases=recovery.validate(source,originals=True)
    launch=read(source/'launcher.json')
    if subprocess.run(['ps','-p',str(launch['pid']),'-o','stat='],capture_output=True,text=True).stdout.strip():
        raise ValueError('Prior controller is still alive')
    oldstate=read(source/'execution.json')
    if any((source/c['directory']/'LongMemEval-answers.jsonl').exists() for c in cases):
        raise ValueError('Expected unattempted staged answers; never replace existing answers')
    out.mkdir(parents=True,mode=0o700)
    for name in ('selection.json','question-cases.json','config.frozen.json',*FILES.values()):
        shutil.copy2(source/name,out/name)
    initial={};ready=set()
    for case in cases:
        old=source/case['directory'];new=out/case['directory'];new.mkdir(parents=True)
        for name in case['files']:shutil.copy2(old/name,new/name)
        for path in old.glob('LongMemEval-write*'):
            if path.is_file() and path.suffix in ('.json','.jsonl'):shutil.copy2(path,new/path.name)
        if (old/'storage-audit.json').exists():shutil.copy2(old/'storage-audit.json',new/'storage-audit.json')
        collect_writes(new,old)
        attempts=sorted((old/'write-attempts').glob('*'))
        complete=recovery.complete_write(new)
        if complete:ready.add(case['index'])
        prior=oldstate['cases'].get(str(case['index']),{})
        initial[str(case['index'])]=dict(index=case['index'],question_id=case['question_id'],
            ingested=bool(prior.get('ingested')),prior_attempt_count=len(attempts),attempts=[],
            prior_directory=str(old),reused_complete_write=complete,
            **{k:prior[k] for k in ('write_sha256','audit_sha256') if k in prior})
    if not ready:raise ValueError('No completed ingestions available for first scoring batch')
    credentials(args.runtime);backend=Qdrant(AppConfig.load(out/'config.frozen.json').storage)
    live=serial.collections(backend)
    if not set(oldplan['external_collections'])<=live:raise ValueError('Unrelated collections disappeared')
    frozen=dict(frozen,protocol='User-authorized score-ready-first batching. First batch reuses every complete '
        'staged ingestion; later batches follow original order. Each batch is fully ingested/audited before '
        'answering, all its answers are fixed and its collections cleaned before independent judging. '
        'No next-batch writing until current batch scoring finishes.',
        evaluation_status='Infrastructure-recovery composite with staged reuse and disclosed batch scoring; '
                          'not a new blind evaluation or organizer official score.')
    frozen['tools']=dict(frozen['tools'],**{Path(__file__).name:sha(__file__)})
    save(out/'freeze.json',frozen);save(out/'freeze.sha256.json',dict(sha256=sha(out/'freeze.json')))
    protected={str(p.relative_to(source)):sha(p) for p in source.rglob('*')
               if p.is_file() and '__pycache__' not in p.parts}
    save(out/'staged-artifacts.sha256.json',protected)
    plan=dict(created_at=now(),staged_run=str(source),original_run=oldplan['original_run'],
        user_instruction='先评分，评分完再重新评测剩下的题目',
        selection_rule='All complete ingestions first, then all remaining executions in original release order; no grades consulted',
        batches=fixed_batches(cases,ready,args.batch_size),ready_count=len(ready),remaining_count=len(cases)-len(ready),
        batch_size=args.batch_size,workers=4,read_workers=1,max_write_attempts=oldplan['max_write_attempts'],
        retry_backoff_s=oldplan['retry_backoff_s'],initial_records=initial,
        initial_owned_collections=sorted(n for n in live if n.startswith(frozen['prefix']+'_')),
        external_collections=oldplan['external_collections'],embedding_runtime=oldplan['embedding_runtime'],
        original_runtime_revisions=oldplan['original_runtime_revisions'],
        coordinator_sha256=sha(__file__),staged_artifacts_manifest_sha256=sha(out/'staged-artifacts.sha256.json'),
        original_successes_retained=oldplan['original_successes_retained'],
        incomplete_write_policy='Preserve existing source markers and cumulative attempt numbers; no budget reset',
        next_batch_gate='All current batch answers fixed, owned collections cleaned and labels scored',
        infrastructure_commit=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True,check=True).stdout.strip())
    save(out/'batch-plan.json',plan);save(out/'batch-plan.sha256.json',dict(sha256=sha(out/'batch-plan.json')))
    save(out/'progress.json',dict(state='prepared',total=len(cases),first_batch=len(ready),remaining=len(cases)-len(ready)))
    shutil.copy2(__file__,out/Path(__file__).name)
    print(json.dumps(dict(prepared=len(cases),first_batch=len(ready),remaining=len(cases)-len(ready),later_batch_size=args.batch_size)))


def validate(out,*,preserved=False):
    frozen=full.validate(out)
    if sha(out/'freeze.json')!=read(out/'freeze.sha256.json')['sha256']:raise ValueError('Freeze changed')
    if sha(out/'batch-plan.json')!=read(out/'batch-plan.sha256.json')['sha256']:raise ValueError('Batch order changed')
    plan=read(out/'batch-plan.json')
    if sha(__file__)!=plan['coordinator_sha256']:raise ValueError('Coordinator changed')
    if sha(out/'staged-artifacts.sha256.json')!=plan['staged_artifacts_manifest_sha256']:
        raise ValueError('Staged artifact protection manifest changed')
    if preserved:
        staged=Path(plan['staged_run']);recovery.validate(staged,originals=True)
        for name,expected in read(out/'staged-artifacts.sha256.json').items():
            if sha(staged/name)!=expected:raise ValueError('Staged evidence changed: '+name)
    cases=serial.check_serial_freeze(out,frozen)
    ids=[i for batch in plan['batches'] for i in batch]
    if sorted(ids)!=[c['index'] for c in cases] or len(set(ids))!=len(cases):
        raise ValueError('Batch plan has missing or duplicate cases')
    return frozen,plan,cases


class BatchRun(recovery.Recovery):
    def __init__(self,args,frozen,plan,cases,**kwargs):
        super().__init__(args,frozen,plan,cases,check=lambda:validate(args.out),**kwargs)
        self.state['cases']=json.loads(json.dumps(plan['initial_records']))
        self.state['batches']=[]
        self.case_by_index={c['index']:c for c in cases}
        self.audit_lock=threading.Lock()

    def prepare_source(self,case):
        directory=self.out/case['directory'];record=self.state['cases'][str(case['index'])]
        if record.get('ingested'):
            if not recovery.complete_write(directory) or sha(directory/'LongMemEval-write.jsonl')!=record['write_sha256'] or \
                    sha(directory/'storage-audit.json')!=record['audit_sha256']:
                raise ValueError('Reused ingestion evidence changed')
            return
        if not recovery.complete_write(directory):
            old=Path(record['prior_directory'])
            for number in range(record['prior_attempt_count']+1,self.plan['max_write_attempts']+1):
                previous=(directory/'write-attempts'/str(number-1).zfill(2))
                if not previous.exists():previous=old/'write-attempts'/str(number-1).zfill(2)
                for value in rows(previous/'LongMemEval-write.jsonl'):
                    if any(s.get('error') and s.get('status')!='open' for s in value.get('sessions',[])):
                        raise ValueError('Non-open incomplete source; avoid duplicate history')
                attempt=directory/'write-attempts'/str(number).zfill(2);attempt.mkdir(parents=True,exist_ok=False)
                for name in ('freeze.json','selection.json','config.frozen.json',FILES['write']):
                    shutil.copy2(directory/name,attempt/name)
                proof=dict(number=number,directory=str(attempt.relative_to(self.out)),started_at=now(),complete=False)
                with self.lock:record['attempts'].append(proof);self.checkpoint()
                try:self.phase(case,attempt,'write')
                finally:collect_writes(directory,old)
                with self.lock:proof.update(complete=recovery.complete_write(attempt),finished_at=now());self.checkpoint()
                if proof['complete']:
                    for name in ('LongMemEval-write.jsonl','LongMemEval-write-summary.json'):
                        shutil.copy2(attempt/name,directory/name)
                    break
                if number<self.plan['max_write_attempts'] and self.halt.wait(self.plan['retry_backoff_s']):
                    raise RuntimeError('Stopped during backoff')
            if not recovery.complete_write(directory):raise ValueError('Ingestion retry budget exhausted; batch blocked')
        save(directory/'execution.json',dict(current='read',question_id=case['question_id']))
        if not (directory/'storage-audit.json').exists():
            with self.audit_lock:self.phase(case,directory,'audit')
        audit=read(directory/'storage-audit.json')
        if audit['episode_vectors'] or audit['episode_chunks'] or audit['source_sha256']!=self.frozen['source']['source_sha256']:
            raise ValueError('Storage audit failed')
        with self.lock:
            record.update(ingested=True,write_sha256=sha(directory/'LongMemEval-write.jsonl'),
                          audit_sha256=sha(directory/'storage-audit.json'))
            self.checkpoint()

    def selected_pool(self,cases,operation):
        with ThreadPoolExecutor(max_workers=self.plan['workers']) as pool:
            pending=iter(cases);active={}
            def submit():
                try:case=next(pending)
                except StopIteration:return
                active[pool.submit(operation,case)]=case
            for _ in range(self.plan['workers']):submit()
            while active:
                future=next(as_completed(active));active.pop(future)
                try:future.result()
                except BaseException:
                    self.halt.set()
                    for other in active:other.cancel()
                    raise
                submit()

    def barrier(self,cases,batch):
        proofs=[]
        for case in cases:
            d=self.out/case['directory'];r=self.state['cases'][str(case['index'])]
            if not r.get('ingested') or not recovery.complete_write(d):raise ValueError('Batch ingestion incomplete')
            if sha(d/'LongMemEval-write.jsonl')!=r['write_sha256'] or sha(d/'storage-audit.json')!=r['audit_sha256']:
                raise ValueError('Batch ingestion evidence changed')
            a=read(d/'storage-audit.json')
            if a['episode_vectors'] or a['episode_chunks']:raise ValueError('Raw vector audit failed')
            if (d/'LongMemEval-answers.jsonl').exists():raise ValueError('Refuse repeat answer')
            if not any(n.startswith(case['prefix']+'_') for n in serial.collections(self.backend)):
                raise ValueError('Staged question database missing')
            proofs.append(dict(index=case['index'],question_id=case['question_id'],write_sha256=r['write_sha256'],audit_sha256=r['audit_sha256']))
        path=self.out/'batches'/str(batch).zfill(4);path.mkdir(parents=True,exist_ok=False)
        save(path/'ingestion-barrier.json',dict(passed=True,checked_at=now(),cases=proofs))
        self.state['batch_barrier']=dict(path=str((path/'ingestion-barrier.json').relative_to(self.out)),sha256=sha(path/'ingestion-barrier.json'))
        self.checkpoint()

    def answer(self,case):
        proof=self.state['batch_barrier'];barrier=self.out/proof['path']
        if sha(barrier)!=proof['sha256']:raise ValueError('Batch barrier changed')
        if case['index'] not in {p['index'] for p in read(barrier)['cases']}:raise ValueError('Case not admitted by batch barrier')
        directory=self.out/case['directory'];record=self.state['cases'][str(case['index'])]
        self.phase(case,directory,'read')
        answer=rows(directory/'LongMemEval-answers.jsonl')
        if len(answer)!=1 or answer[0]['question_id']!=case['question_id']:raise ValueError('Invalid answer inventory')
        record.update(answer_status=answer[0]['status'],answer_sha256=sha(directory/'LongMemEval-answers.jsonl'))
        before=serial.collections(self.backend)
        siblings={n for n in before if not n.startswith(case['prefix']+'_')}
        serial.cleanup_question(self.backend,case['prefix'],siblings,record)
        self.checkpoint()

    def run(self):
        try:
            self.check()
            for index,indices in enumerate(self.plan['batches'],1):
                cases=[self.case_by_index[i] for i in indices]
                self.state.update(batch=index,stage='write',batch_started_at=now());self.checkpoint()
                self.selected_pool(cases,self.prepare_source)
                self.barrier(cases,index)
                self.state['stage']='read';self.checkpoint()
                for case in cases:self.answer(case)
                live=serial.collections(self.backend)
                if any(n.startswith(c['prefix']+'_') for c in cases for n in live):raise ValueError('Batch cleanup failed')
                fixed={c['question_id']:sha(self.out/c['directory']/'LongMemEval-answers.jsonl') for c in cases}
                save(self.out/'batches'/str(index).zfill(4)/'answers-fixed.json',dict(fixed_at=now(),hashes=fixed))
                self.state['stage']='judge';self.checkpoint()
                self.selected_pool(cases,self.judge)
                for c in cases:
                    directory=self.out/c['directory']
                    if sha(directory/'LongMemEval-answers.jsonl')!=fixed[c['question_id']]:raise ValueError('Answer changed during judging')
                    if any(not isinstance(g.get('correct'),bool) for g in rows(directory/'LongMemEval-grades.jsonl')):
                        raise ValueError('Batch scoring incomplete; no next batch may start')
                if max((self.out/c['directory']/'LongMemEval-answers.jsonl').stat().st_mtime_ns for c in cases) > \
                        min((self.out/c['directory']/'LongMemEval-grades.jsonl').stat().st_mtime_ns for c in cases):
                    raise ValueError('Scoring began before batch answers were fixed')
                with self.lock:
                    self.state['batches'].append(dict(index=index,cases=indices,started_at=self.state['batch_started_at'],
                        scored_at=now(),barrier_sha256=self.state['batch_barrier']['sha256'],
                        answers_fixed_sha256=sha(self.out/'batches'/str(index).zfill(4)/'answers-fixed.json')))
                    self.checkpoint()
                report(self.out)
            validate(self.out,preserved=True)
            if any(n.startswith(self.frozen['prefix']+'_') for n in serial.collections(self.backend)):
                raise ValueError('Owned collections remain after final batch')
            self.state.update(stage='complete',complete=True,finished_at=now())
        except BaseException as exc:
            self.halt.set();self.state.update(complete=False,error=dict(type=type(exc).__name__,message=str(exc)))
            raise
        finally:
            live=serial.collections(self.backend)
            self.state['cleanup']=dict(remaining_owned=sorted(n for n in live if n.startswith(self.frozen['prefix']+'_')),
                unrelated_collections_preserved=self.external<=live)
            if not self.state['cleanup']['unrelated_collections_preserved']:
                self.state.update(complete=False,error=dict(type='ValueError',message='Unrelated collection disappeared'))
            self.checkpoint()


def score(metas,answers,grades):
    correct=sum(grades.get(m['question_id'],{}).get('correct') is True for m in metas)
    labels=sum(isinstance(grades.get(m['question_id'],{}).get('correct'),bool) for m in metas)
    return dict(total=len(metas),correct=correct,answered=sum(answers.get(m['question_id'],{}).get('status')=='answered' for m in metas),
        attempted=sum(m['question_id'] in answers for m in metas),labels=labels,
        judged=sum(grades.get(m['question_id'],{}).get('status')=='graded' for m in metas),
        accuracy=correct/len(metas) if labels==len(metas) and metas else None)


def report(out):
    frozen,plan,cases=validate(out)
    state=read(out/'execution.json');selection=read(out/'selection.json')
    answers={};grades={};ledger=UsageLedger()
    for c in cases:
        directory=out/c['directory']
        ar=rows(directory/'LongMemEval-answers.jsonl');gr=rows(directory/'LongMemEval-grades.jsonl')
        if len(ar)>1 or len(gr)>1 or any(r['question_id']!=c['question_id'] for r in ar+gr):
            raise ValueError('Duplicate or foreign answer/grade record')
        for r in ar:answers[r['question_id']]=r
        # Reference text in native grade records is not used by the report.
        for r in gr:
            grades[r['question_id']]={k:r[k] for k in ('question_id','status','correct','error') if k in r}
        for path in directory.glob('LongMemEval-*-usage.jsonl'):
            for r in UsageLedger.from_jsonl(str(path)).records():ledger.record(r)
    local=score(selection,answers,grades)
    local.update(complete=state['complete'],batch_plan=plan['batches'],usage=ledger.summary(),usage_totals=ledger.totals(),
                 pricing_known=all(r.cost_known for r in ledger.records()),elapsed_s=state['elapsed_s'],evaluation_status=frozen['evaluation_status'])
    save(out/'metrics.json',local)
    completed={b['index']:b for b in state['batches']}
    batch_scores=[]
    for number,indices in enumerate(plan['batches'],1):
        subset=[selection[i-1] for i in indices];s=score(subset,answers,grades)
        s.update(batch=number,by_type={t:score([m for m in subset if m['category']==t],answers,grades) for t in full.TYPES},
                 abstention=score([m for m in subset if m['abstention']],answers,grades))
        s['task_averaged_accuracy']=statistics.mean(v['accuracy'] for v in s['by_type'].values() if v['total']) if s['labels']==s['total'] else None
        batch_scores.append(s)
        if number in completed:save(out/'batches'/str(number).zfill(4)/'metrics.json',s)
    save(out/'batch-scores.json',batch_scores)
    original=Path(plan['original_run']);original_metrics=read(original/'metrics.json')
    original_selection=read(original/'selection.json');composite_answers={};composite_grades={};provenance=[]
    for c in read(original/'question-cases.json'):
        path=original/c['directory'];old=rows(path/'LongMemEval-answers.jsonl')[0];qid=c['question_id']
        if old['status']=='answered':
            composite_answers[qid]=old
            g=rows(path/'LongMemEval-grades.jsonl')[0];composite_grades[qid]={k:g[k] for k in ('status','correct')}
            provenance.append(dict(question_id=qid,source='original',directory=str(path),answer_sha256=sha(path/'LongMemEval-answers.jsonl')))
        elif qid in answers:
            composite_answers[qid]=answers[qid]
            if qid in grades:composite_grades[qid]=grades[qid]
            case=next(c for c in cases if c['question_id']==qid);path=out/case['directory']
            provenance.append(dict(question_id=qid,source='batch_recovery',directory=str(path),answer_sha256=sha(path/'LongMemEval-answers.jsonl')))
    target=out/'composite500';target.mkdir(exist_ok=True)
    ready=full.export_answers(target,original_selection,list(composite_answers.values()))
    overall=score(original_selection,composite_answers,composite_grades)
    by_type={t:score([m for m in original_selection if m['category']==t],composite_answers,composite_grades) for t in full.TYPES}
    metrics=dict(overall,evaluation_kind='composite_after_batched_infrastructure_recovery',submission_ready=ready,
        complete=state['complete'],by_type=by_type,abstention=score([m for m in original_selection if m['abstention']],composite_answers,composite_grades),
        task_averaged_accuracy=statistics.mean(v['accuracy'] for v in by_type.values()) if overall['labels']==500 else None,
        original_successes_retained=plan['original_successes_retained'],batch_scores=batch_scores,
        original_first_pass=dict(accuracy=original_metrics['overall_accuracy'],task_averaged=original_metrics['task_averaged_accuracy'],answered=original_metrics['answered']),
        original_usage_totals=original_metrics['usage_totals'],supplement_usage_totals=ledger.totals(),pricing_known=local['pricing_known'],
        elapsed_s=state['elapsed_s'],runtime_revisions=dict(original=plan['original_runtime_revisions'],recovery=plan['embedding_runtime']),
        actual_answer_models=dict(Counter(a.get('actual_model') for a in composite_answers.values() if a['status']=='answered')),
        failures=[dict(question_id=q,error=a.get('error')) for q,a in answers.items() if a['status']!='answered'],
        judge_errors=[dict(question_id=q,error=g.get('error')) for q,g in grades.items() if g['status']=='judge_error'],
        limitation='Scores after infrastructure retries and staged reuse; not a fresh blind test. '
                   'Same-model independent-phase self-evaluation; official organizer reevaluation is separate.')
    save(target/'metrics.json',metrics);save(target/'answer-provenance.json',provenance)
    text=['# Oracle500 分批补跑报告','',f"有效回答覆盖 {overall['answered']}/500；已处理 {overall['attempted']}/500；有评分标签 {overall['labels']}/500。",'',
        f"Overall Accuracy：{overall['accuracy']}；Task-averaged：{metrics['task_averaged_accuracy']}。完整提交：{ready}。未完成时不把已评分子集当全量成绩。",'',
        f"原轮首遍结果保留：{original_metrics['correct']}/500（{original_metrics['overall_accuracy']:.2%}），有效回答{original_metrics['answered']}/500。",'',
        '| 批次 | 正确 | 有效回答 | 分母 | 准确率 |','|---|---:|---:|---:|---:|']
    for s in batch_scores:text.append(f"| {s['batch']} | {s['correct']} | {s['answered']} | {s['total']} | {s['accuracy']} |")
    text+=['','| 题型 | 正确 | 有效回答 | 分母 | 准确率 |','|---|---:|---:|---:|---:|']
    for name,s in by_type.items():text.append(f"| {name} | {s['correct']} | {s['answered']} | {s['total']} | {s['accuracy']} |")
    text+=['',f"拒答子集：{json.dumps(metrics['abstention'],ensure_ascii=False)}。",'',
        f"补跑用量：{json.dumps(ledger.totals(),ensure_ascii=False)}。货币成本未知，cost=0兼容值不代表免费。",'',
        f"本调度修订墙钟 {state['elapsed_s']/3600:.2f} 小时，含睡眠与暂停；此前入库用量已通过逐次账本纳入，失败请求的未回执用量可能缺失。",'',
        '按用户要求先对现成入库批次答题、清理并评分，再处理剩余题目，每批最多8题。批次顺序在评分前封印，绝不按成绩选择或调整后续题。', '',
        '原轮365份成功回答直接沿用；原始失败及入库重试保留。产品源码、模型参数、问题、提示词和BGE权重不变。原文/分块不建向量，每题仅使用其隔离事实库。', '',
        f"答题及独立判分模型：{frozen['judge_model']}。",'',
        '原轮首遍成绩与基础设施重试后的合成成绩分开披露；同模型独立阶段判分属于自测，正式项目组复评另算。', '',
        '元数据与逐题来源见metrics.json、answer-provenance.json、batch-plan.json；逐批评分见batches/。','']
    (out/'report.md').write_text('\n'.join(text));(target/'report.md').write_text('\n'.join(text))
    if state['complete']:
        if state.get('error') or state['cleanup']['remaining_owned'] or not state['cleanup']['unrelated_collections_preserved']:
            raise ValueError('Final cleanup verification failed')
        previous=None
        for batch in state['batches']:
            folder=out/'batches'/str(batch['index']).zfill(4)
            if sha(folder/'ingestion-barrier.json')!=batch['barrier_sha256'] or sha(folder/'answers-fixed.json')!=batch['answers_fixed_sha256']:
                raise ValueError('Batch stage evidence changed')
            if previous and batch['started_at']<previous:raise ValueError('Next batch began before prior scoring finished')
            previous=batch['scored_at']
            fixed=read(folder/'answers-fixed.json')['hashes']
            for i in batch['cases']:
                c=cases[i-1];d=out/c['directory'];a=read(d/'storage-audit.json')
                if a['episode_vectors'] or a['episode_chunks'] or sha(d/'LongMemEval-answers.jsonl')!=fixed[c['question_id']]:
                    raise ValueError('Fixed answer or zero-original-vector audit failed')
        export='hypotheses.jsonl' if ready else 'hypotheses.partial.jsonl'
        exported=rows(target/export)
        expected=[m['question_id'] for m in original_selection if composite_answers.get(m['question_id'],{}).get('status')=='answered']
        if [r['question_id'] for r in exported]!=expected or len(set(expected))!=len(exported):raise ValueError('Composite ID coverage failed')
        if any(r['hypothesis']!=composite_answers[r['question_id']]['hypothesis'] for r in exported):raise ValueError('Composite answer content changed')
        verification=dict(verified_at=now(),official_ids=500,valid_export_records=len(exported),complete_submission=ready,
            original_successes_unchanged=plan['original_successes_retained'],all_batch_order_and_answer_hashes_verified=True,
            all_question_cleanups_verified=True,original_vectors=0,chunk_vectors=0,
            original_and_staged_artifact_hashes_verified=True)
        save(out/'final-verification.json',verification)
        save(out/'delivery-manifest.json',dict(created_at=now(),files={str(p.relative_to(out)):dict(sha256=sha(p),bytes=p.stat().st_size)
            for p in [target/'report.md',target/'metrics.json',target/export,target/'answer-provenance.json',out/'final-verification.json']}))
    return metrics


def run(args):
    with (args.out/'batch-run.lock').open('a') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (args.out/'batch-started.json').exists():raise ValueError('Already started; no automatic restart')
        frozen,plan,cases=validate(args.out,preserved=True)
        proof=read(args.out/'preflight.json')
        if not proof.get('passed') or proof.get('config_sha256')!=frozen['config_sha256']:
            raise ValueError('Successful frozen-config preflight required')
        credentials(args.runtime)
        save(args.out/'batch-started.json',dict(pid=os.getpid(),started_at=now()))
        runner=BatchRun(args,frozen,plan,cases)
        try:runner.run()
        finally:report(args.out)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','preflight','run','report'))
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--source',type=Path)
    parser.add_argument('--batch-size',type=int,choices=range(1,9),default=8)
    parser.add_argument('--runtime',type=Path,default=Path.home()/'Library/Application Support/memsys/runtime.json')
    args=parser.parse_args();args.out=args.out.resolve()
    if args.action=='prepare':prepare(args)
    elif args.action=='preflight':validate(args.out,preserved=True);full.preflight(args)
    elif args.action=='run':run(args)
    else:report(args.out)


if __name__=='__main__':main()
