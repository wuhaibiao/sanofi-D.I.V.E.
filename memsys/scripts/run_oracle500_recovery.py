#!/usr/bin/env python3
"""User-authorized infrastructure recovery: ingest every missing case before answering.

The original run and successful answers remain immutable. This is a disclosed
supplement, selected only by execution status, never by grades or answer content.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import run_oracle500 as full
import run_oracle500_parallel as parallel
import question_serial_eval as serial
from blind_eval_inputs import FILES
from check_webchat_reliability import credentials
from memsys.config import AppConfig
from memsys.llm import UsageLedger
from memsys.qdrant import Qdrant
from memsys.store import Store


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    return full.rows(Path(path))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def save(path, value):
    """Atomic status publication, including during multi-worker execution."""
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp-' + str(threading.get_ident()))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    os.replace(temporary, path)


def incomplete_cases(source):
    """Status-only selection; no grade or question text is inspected."""
    chosen = []
    for case in read(source / 'question-cases.json'):
        directory = source / case['directory']
        answer = rows(directory / 'LongMemEval-answers.jsonl')
        if len(answer) != 1 or answer[0]['question_id'] != case['question_id']:
            raise ValueError('Original answer inventory is incomplete')
        if answer[0]['status'] == 'answered':
            continue
        writes = rows(directory / 'LongMemEval-write.jsonl')
        chosen.append(dict(case, original_ingestion_complete=bool(writes) and all(r.get('complete') for r in writes)))
    return chosen


def prepare(args):
    source, out = args.source.resolve(), args.out.resolve()
    if out.exists():
        raise ValueError('Recovery requires a new directory; original evidence cannot be replaced')
    execution = read(source / 'execution.json')
    if not execution.get('complete') or execution.get('error'):
        raise ValueError('Original controller must finish first')
    launch = read(source / 'launcher.json')
    state = subprocess.run(['ps', '-p', str(launch['pid']), '-o', 'stat='],
                           capture_output=True, text=True).stdout.strip()
    if state and 'Z' not in state:
        raise ValueError('Original controller is still running')
    original, _ = parallel.validate_plan(source)
    serial.check_serial_freeze(source, original)
    chosen = incomplete_cases(source)
    if not chosen:
        raise ValueError('No failed answer records need recovery')
    selection_by_id = {m['question_id']: m for m in read(source / 'selection.json')}
    out.mkdir(parents=True, mode=0o700)
    selection = [selection_by_id[c['question_id']] for c in chosen]
    save(out / 'selection.json', selection)
    shutil.copy2(source / 'config.frozen.json', out / 'config.frozen.json')
    os.chmod(out / 'config.frozen.json', 0o600)
    phase_hashes = {}
    # Trusted mechanical partition only; standard-answer contents are not logged.
    for phase, filename in FILES.items():
        values = []
        for case in chosen:
            values.extend(read(source / case['directory'] / filename))
        save(out / filename, values)
        phase_hashes[phase] = sha(out / filename)
    prefix = 'oracle_recovery_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    frozen = dict(original, prefix=prefix, namespace=prefix,
                  blind_input_sha256=phase_hashes,
                  protocol='User-authorized recovery of all failed executions; all selected sources must be '
                           'fully ingested and audited before any recovered answer. Original successes retained. '
                           'Separate question collections and phase processes; all cleanup before judging.',
                  evaluation_status='Disclosed infrastructure-recovery supplement to an exposed public Oracle '
                                    'development evaluation, not an independent first-pass or official score.')
    frozen['counts'] = dict(Counter(m['category'] for m in selection))
    serial.freeze_cases(out, frozen)
    frozen['tools'] = dict(original['tools'], **{Path(__file__).name: sha(__file__)})
    frozen['files'] = {name: sha(out / name) for name in
                       ('config.frozen.json', 'selection.json', 'question-cases.json', *FILES.values())}
    save(out / 'freeze.json', frozen)
    save(out / 'freeze.sha256.json', dict(sha256=sha(out / 'freeze.json')))
    credentials(args.runtime)
    backend = Qdrant(AppConfig.load(out / 'config.frozen.json').storage)
    external = sorted(serial.collections(backend))
    if any(n.startswith(prefix + '_') for n in external):
        raise ValueError('New recovery prefix is not empty')
    protected = {str(p.relative_to(source)): sha(p) for p in sorted(source.rglob('*'))
                 if p.is_file() and '__pycache__' not in p.parts}
    save(out / 'original-artifacts.sha256.json', protected)
    plan = dict(created_at=now(), original_run=str(source), total=len(chosen), official_total=500,
                selection_rule='All original status != answered, without consulting correctness or labels',
                original_successes_retained=500-len(chosen),
                original_ingestion_incomplete=sum(not c['original_ingestion_complete'] for c in chosen),
                original_ingestion_complete_answer_failed=sum(c['original_ingestion_complete'] for c in chosen),
                original_cases=[dict(index=c['index'], question_id=c['question_id'], directory=c['directory'],
                                     original_ingestion_complete=c['original_ingestion_complete']) for c in chosen],
                workers=args.workers, read_workers=1, max_write_attempts=args.max_write_attempts,
                retry_backoff_s=30, external_collections=external, coordinator_sha256=sha(__file__),
                original_artifacts_manifest_sha256=sha(out / 'original-artifacts.sha256.json'),
                original_freeze_sha256=sha(source / 'freeze.json'),
                original_runtime_revisions=read(source / 'metrics.json').get('runtime_revisions'),
                answer_gate='Every selected case ingestion complete, zero raw vectors, all frozen inputs valid',
                stopped_process_policy='Never restart automatically; preserve staged databases and evidence',
                failed_write_policy='Up to three separately recorded attempts; successful stored sessions skipped',
                answer_policy='One answer execution per recovery case, only after the global ingestion barrier',
                infrastructure_commit=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,
                                                     capture_output=True,text=True,check=True).stdout.strip())
    import urllib.request
    with urllib.request.urlopen('http://127.0.0.1:11434/healthz',timeout=10) as response:
        health=json.load(response)
    if health.get('status')!='ready' or health.get('threads')!=8 or health.get('batch_size')!=8 or \
            health.get('inference',{}).get('inference_dispatch')!='single_persistent_thread':
        raise ValueError('Expected the user-approved repaired BGE runtime')
    plan['embedding_runtime']=health
    save(out / 'recovery-plan.json', plan)
    save(out / 'recovery-plan.sha256.json', dict(sha256=sha(out / 'recovery-plan.json')))
    save(out / 'progress.json', dict(state='prepared', stage='write', total=len(chosen), ingested=0, answered=0))
    shutil.copytree(ROOT / 'src/memsys', out / 'frozen-src/memsys', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT / 'scripts', out / 'frozen-scripts', ignore=shutil.ignore_patterns('__pycache__'))
    print(json.dumps({k:plan[k] for k in ('total','original_successes_retained',
                     'original_ingestion_incomplete','original_ingestion_complete_answer_failed')},ensure_ascii=False))


def validate(out, *, originals=False):
    frozen = full.validate(out)
    if sha(out / 'freeze.json') != read(out / 'freeze.sha256.json')['sha256']:
        raise ValueError('Recovery freeze changed')
    if sha(out / 'recovery-plan.json') != read(out / 'recovery-plan.sha256.json')['sha256']:
        raise ValueError('Recovery plan changed')
    plan = read(out / 'recovery-plan.json')
    if sha(__file__) != plan['coordinator_sha256']:
        raise ValueError('Recovery coordinator changed')
    if sha(out / 'original-artifacts.sha256.json') != plan['original_artifacts_manifest_sha256']:
        raise ValueError('Original protection manifest changed')
    if originals:
        source = Path(plan['original_run'])
        for name, expected in read(out / 'original-artifacts.sha256.json').items():
            if sha(source / name) != expected:
                raise ValueError('Original artifact changed: ' + name)
    cases = serial.check_serial_freeze(out, frozen)
    return frozen, plan, cases


def complete_write(directory):
    values = rows(directory / 'LongMemEval-write.jsonl')
    return bool(values) and all(r.get('complete') is True and not r.get('pending_messages') and
        r.get('sessions') and all(s.get('source_exact') is True and s.get('error') is None and
            s.get('status') == 'closed' and s.get('raw_messages') == s.get('processed_messages')
            for s in r['sessions']) for r in values)


class Recovery:
    def __init__(self, args, frozen, plan, cases, *, backend=None, invoke=None, check=None):
        self.args, self.out, self.frozen, self.plan, self.cases = args, args.out, frozen, plan, cases
        self.config = AppConfig.load(args.out / 'config.frozen.json') if backend is None else None
        self.backend = backend or Qdrant(self.config.storage)
        self.invoke = invoke or full.invoke
        self.check = check or (lambda: validate(self.out))
        self.lock = threading.RLock()
        self.halt = threading.Event()
        self.state = dict(started_at=now(), stage='write', complete=False, cases={}, current={}, error=None)
        self.external = set(plan['external_collections'])

    def checkpoint(self):
        with self.lock:
            self.state['elapsed_s']=(datetime.now(timezone.utc)-datetime.fromisoformat(self.state['started_at'])).total_seconds()
            save(self.out / 'execution.json', self.state)
            records = list(self.state['cases'].values())
            save(self.out / 'progress.json', dict(state='complete' if self.state['complete'] else
                ('blocked' if self.state['error'] else 'running'), stage=self.state['stage'], total=len(self.cases),
                ingested=sum(r.get('ingested') is True for r in records),
                answer_attempted=sum(r.get('answer_status') is not None for r in records),
                answered=sum(r.get('answer_status') == 'answered' for r in records),
                graded=sum(r.get('grade_status') == 'graded' for r in records),
                current=list(self.state['current'].values()), error=self.state['error'], updated_at=now()))

    def phase(self, case, directory, name):
        if self.halt.is_set():
            raise RuntimeError('Recovery halted; no new phase may start')
        self.check()
        serial.check_case(self.out, case)
        with self.lock:
            self.state['current'][str(case['index'])] = dict(index=case['index'], question_id=case['question_id'],phase=name)
            self.checkpoint()
        try:
            code = self.invoke(directory, name, self.args.runtime)
            if code:
                raise RuntimeError('Phase subprocess failed: '+name+' exit='+str(code))
            if (directory / 'provider-fatal.json').exists():
                raise RuntimeError('Provider account/quota failure; evidence retained')
        finally:
            with self.lock:
                self.state['current'].pop(str(case['index']), None)
                self.checkpoint()

    def write(self, case):
        directory = self.out / case['directory']
        record = dict(index=case['index'],question_id=case['question_id'],ingested=False,attempts=[])
        with self.lock:
            self.state['cases'][str(case['index'])] = record
            self.checkpoint()
        # Original DB was already cleaned. Recovery gets a fresh question prefix.
        if any(n.startswith(case['prefix']+'_') for n in serial.collections(self.backend)):
            raise ValueError('Recovery question prefix is not empty at first attempt')
        for number in range(1,self.plan['max_write_attempts']+1):
            if number>1:
                prior=directory/'write-attempts'/str(number-1).zfill(2)
                for report in rows(prior/'LongMemEval-write.jsonl'):
                    if any(s.get('error') is not None and s.get('status')!='open' for s in report.get('sessions',[])):
                        raise ValueError('Incomplete source has a non-open session; preserve it instead of duplicating history')
            attempt = directory / 'write-attempts' / str(number).zfill(2)
            attempt.mkdir(parents=True, exist_ok=False)
            for name in ('freeze.json','selection.json','config.frozen.json',FILES['write']):
                shutil.copy2(directory / name,attempt / name)
            # The writer's stored session-completion markers make retries skip all
            # sessions already completed in this recovery; logs never overwrite.
            self.phase(case, attempt, 'write')
            proof = dict(number=number,directory=str(attempt.relative_to(self.out)),
                         complete=complete_write(attempt),finished_at=now())
            with self.lock:
                record['attempts'].append(proof)
                self.checkpoint()
            if proof['complete']:
                shutil.copy2(attempt/'LongMemEval-write.jsonl',directory/'LongMemEval-write.jsonl')
                shutil.copy2(attempt/'LongMemEval-write-summary.json',directory/'LongMemEval-write-summary.json')
                # Include all attempts in the usage view, not just the final one.
                for suffix in ('usage','prompts','responses','provider'):
                    paths = sorted((directory/'write-attempts').glob('*/LongMemEval-write-'+suffix+'.jsonl'))
                    if paths:
                        (directory/('LongMemEval-write-'+suffix+'.jsonl')).write_bytes(b''.join(p.read_bytes() for p in paths))
                save(directory/'execution.json',dict(current='read',question_id=case['question_id']))
                self.phase(case,directory,'audit')
                audit = read(directory/'storage-audit.json')
                if audit['episode_vectors'] or audit['episode_chunks']:
                    raise ValueError('Original or chunk vectors detected')
                with self.lock:
                    record.update(ingested=True,ingested_at=now(),write_sha256=sha(directory/'LongMemEval-write.jsonl'),
                                  audit_sha256=sha(directory/'storage-audit.json'))
                    self.checkpoint()
                return
            if number < self.plan['max_write_attempts']:
                if self.halt.wait(self.plan['retry_backoff_s']):
                    raise RuntimeError('Recovery halted during backoff')
        # No read/answer is allowed for any case while even one remains incomplete.
        self.checkpoint()

    def pool(self, operation):
        with ThreadPoolExecutor(max_workers=self.plan['workers']) as pool:
            pending = iter(self.cases)
            active = {}
            def submit():
                try:
                    case = next(pending)
                except StopIteration:
                    return
                active[pool.submit(operation,case)] = case
            for _ in range(self.plan['workers']):
                submit()
            while active:
                future = next(as_completed(active))
                active.pop(future)
                try:
                    future.result()
                except BaseException:
                    self.halt.set()
                    for other in active:
                        other.cancel()
                    raise
                submit()

    def barrier(self):
        if len(self.state['cases']) != len(self.cases) or any(
                not self.state['cases'][str(c['index'])].get('ingested') for c in self.cases):
            raise ValueError('Ingestion barrier blocked: not every recovery case was fully ingested')
        self.check()
        proofs = []
        for case in self.cases:
            directory = self.out/case['directory'];record=self.state['cases'][str(case['index'])]
            if not complete_write(directory) or sha(directory/'LongMemEval-write.jsonl') != record['write_sha256']:
                raise ValueError('Completed write evidence changed')
            if sha(directory/'storage-audit.json') != record['audit_sha256']:
                raise ValueError('Storage audit evidence changed')
            if self.config is not None:
                frozen=read(directory/'freeze.json')
                settings=replace(self.config.storage,collection_prefix=frozen['prefix'],namespace=frozen['namespace'])
                for source in read(directory/FILES['write']):
                    store=Store(user_id=source['corpus_id'],settings=settings,client=self.backend)
                    try:
                        for session in source['sessions']:
                            marker=store.record('benchmark_source',session['session_id'])
                            if not marker or marker.get('finished') is not True:
                                raise ValueError('Stored source is incomplete at global barrier')
                    finally:
                        store.close()
            proofs.append(dict(question_id=case['question_id'],write_sha256=record['write_sha256'],
                               audit_sha256=record['audit_sha256']))
        existing=serial.collections(self.backend)
        if not self.external <= existing:
            raise ValueError('Unrelated collection disappeared')
        save(self.out/'ingestion-barrier.json',dict(passed=True,checked_at=now(),total=len(proofs),cases=proofs,
             no_recovery_answer_files=not any((self.out/c['directory']/'LongMemEval-answers.jsonl').exists() for c in self.cases)))
        if not read(self.out/'ingestion-barrier.json')['no_recovery_answer_files']:
            raise ValueError('Answer existed before ingestion barrier')
        self.state['ingestion_barrier_sha256']=sha(self.out/'ingestion-barrier.json')
        self.checkpoint()

    def answer(self,case):
        directory=self.out/case['directory'];record=self.state['cases'][str(case['index'])]
        if not read(self.out/'ingestion-barrier.json').get('passed') or \
                sha(self.out/'ingestion-barrier.json')!=self.state.get('ingestion_barrier_sha256'):
            raise ValueError('Answer requires global ingestion barrier')
        try:
            self.phase(case,directory,'read')
            values=rows(directory/'LongMemEval-answers.jsonl')
            if len(values)!=1 or values[0]['question_id']!=case['question_id']:
                raise ValueError('Missing or duplicate recovery answer')
            with self.lock:
                record.update(answer_status=values[0]['status'],answer_sha256=sha(directory/'LongMemEval-answers.jsonl'))
        finally:
            # Only this question's prefix is deleted; staged siblings remain.
            serial.cleanup_question(self.backend,case['prefix'],self.external,record)
            self.checkpoint()

    def judge(self,case):
        directory=self.out/case['directory'];record=self.state['cases'][str(case['index'])]
        if sha(directory/'LongMemEval-answers.jsonl')!=record['answer_sha256']:
            raise ValueError('Answer changed before scoring')
        self.phase(case,directory,'judge')
        values=rows(directory/'LongMemEval-grades.jsonl')
        if len(values)!=1 or values[0]['question_id']!=case['question_id']:
            raise ValueError('Missing or duplicate recovery grade')
        with self.lock:
            record['grade_status']=values[0]['status']
            self.checkpoint()

    def run(self):
        try:
            self.check()
            self.pool(self.write)
            self.barrier()
            self.state['stage']='read';self.checkpoint()
            # The model/reranker read path keeps the prior concurrency of one.
            for case in self.cases:
                self.answer(case)
            if any(n.startswith(self.frozen['prefix']+'_') for n in serial.collections(self.backend)):
                raise ValueError('Question collections remain before scoring')
            if any(not r.get('cleanup',{}).get('verified_empty') for r in self.state['cases'].values()):
                raise ValueError('Question cleanup proof missing')
            self.state['stage']='judge';self.checkpoint()
            self.pool(self.judge)
            self.state.update(stage='complete',complete=True,finished_at=now())
        except BaseException as exc:
            self.halt.set()
            self.state.update(error=dict(type=type(exc).__name__,message=str(exc)),complete=False)
            raise
        finally:
            existing=serial.collections(self.backend)
            self.state['cleanup']=dict(remaining_owned=sorted(n for n in existing if n.startswith(self.frozen['prefix']+'_')),
                                       unrelated_collections_preserved=self.external<=existing)
            if not self.state['cleanup']['unrelated_collections_preserved']:
                self.state['complete']=False
                self.state['error']=dict(type='ValueError',message='Unrelated collection disappeared')
            self.checkpoint()


def report(out):
    frozen,plan,cases=validate(out,originals=True)
    execution=read(out/'execution.json')
    if not execution.get('complete') or execution.get('error') or execution['cleanup']['remaining_owned'] or \
            not execution['cleanup']['unrelated_collections_preserved']:
        raise ValueError('Recovery did not complete; do not publish a complete composite')
    credentials(Path.home()/'Library/Application Support/memsys/runtime.json')
    live=serial.collections(Qdrant(AppConfig.load(out/'config.frozen.json').storage))
    if any(n.startswith(frozen['prefix']+'_') for n in live) or not set(plan['external_collections'])<=live:
        raise ValueError('Live collection cleanup verification failed')
    answer_times=[];grade_times=[];write_times=[]
    for case in cases:
        directory=out/case['directory'];record=execution['cases'][str(case['index'])]
        if not record.get('cleanup',{}).get('verified_empty'):
            raise ValueError('Missing question cleanup verification')
        if sha(directory/'LongMemEval-answers.jsonl')!=record['answer_sha256']:
            raise ValueError('Fixed recovery answer changed')
        audit=read(directory/'storage-audit.json')
        if audit['episode_vectors'] or audit['episode_chunks']:
            raise ValueError('Raw-vector audit failed')
        write_times.append((directory/'storage-audit.json').stat().st_mtime_ns)
        answer_times.append((directory/'LongMemEval-answers.jsonl').stat().st_mtime_ns)
        grade_times.append((directory/'LongMemEval-grades.jsonl').stat().st_mtime_ns)
    if max(write_times)>min(answer_times) or max(answer_times)>min(grade_times):
        raise ValueError('Global stage ordering verification failed')
    if sha(out/'ingestion-barrier.json')!=execution['ingestion_barrier_sha256']:
        raise ValueError('Global ingestion barrier changed')
    verification=dict(verified_at=now(),original_artifacts_unchanged=True,all_ingestion_before_any_answer=True,
        all_answers_fixed_before_judging=True,all_question_cleanups_verified=True,live_remaining_owned=[],
        original_vectors=0,chunk_vectors=0,storage_audits=len(cases),
        protected_original_files=len(read(out/'original-artifacts.sha256.json')))
    save(out/'final-verification.json',verification)
    serial.aggregate_outputs(out,cases)
    supplement=full.report(argparse.Namespace(out=out))
    supplement.update(evaluation_kind='user_authorized_infrastructure_recovery',original_run=plan['original_run'],
                      ingestion_barrier=read(out/'ingestion-barrier.json'),recovery_plan_sha256=sha(out/'recovery-plan.json'),
                      write_attempts=sum(len(r['attempts']) for r in execution['cases'].values()),
                      incomplete_write_attempts=sum(not a['complete'] for r in execution['cases'].values() for a in r['attempts']),
                      embedding_runtime=plan['embedding_runtime'])
    save(out/'metrics.json',supplement)
    source=Path(plan['original_run'])
    original=read(source/'metrics.json')
    selection=read(source/'selection.json')
    original_cases=read(source/'question-cases.json')
    recovered={c['question_id']:out/c['directory'] for c in cases}
    answers,grades,provenance={}, {}, []
    for case in original_cases:
        old=source/case['directory'];a=rows(old/'LongMemEval-answers.jsonl')[0]
        directory=old if a['status']=='answered' else recovered[case['question_id']]
        answer=rows(directory/'LongMemEval-answers.jsonl')[0]
        grade=rows(directory/'LongMemEval-grades.jsonl')[0]
        qid=case['question_id'];answers[qid]=answer;grades[qid]=grade
        provenance.append(dict(question_id=qid,source_run='original' if directory==old else 'recovery',
                               directory=str(directory),answer_sha256=sha(directory/'LongMemEval-answers.jsonl')))
    composite=out/'composite500';composite.mkdir(exist_ok=True)
    ready=full.export_answers(composite,selection,list(answers.values()))
    labels_complete=all(isinstance(g.get('correct'),bool) for g in grades.values())
    def score(metas):
        correct=sum(grades[m['question_id']].get('correct') is True for m in metas)
        return dict(total=len(metas),correct=correct,answered=sum(answers[m['question_id']]['status']=='answered' for m in metas),
                    judged=sum(grades[m['question_id']]['status']=='graded' for m in metas),
                    accuracy=correct/len(metas) if labels_complete and metas else None)
    overall=score(selection)
    by_type={t:score([m for m in selection if m['category']==t]) for t in full.TYPES}
    abstention=score([m for m in selection if m['abstention']])
    metric=dict(evaluation_kind='composite_after_infrastructure_recovery',official_total=500,
        original_valid_answers_retained=plan['original_successes_retained'],recovery_cases=plan['total'],
        **overall,submission_ready=ready,labels_complete=labels_complete,by_type=by_type,abstention=abstention,
        task_averaged_accuracy=statistics.mean(v['accuracy'] for v in by_type.values()) if labels_complete else None,
        original_first_pass=dict(overall_accuracy=original['overall_accuracy'],task_averaged_accuracy=original['task_averaged_accuracy'],
                                 answered=original['answered'],answer_failures=len(original['answer_errors'])),
        original_run=str(source),recovery_run=str(out),created_at=now(),
        original_usage_totals=original['usage_totals'],recovery_usage_totals=supplement['usage_totals'],
        original_elapsed_s=original['elapsed_s'],recovery_elapsed_s=execution['elapsed_s'],
        verification=verification,embedding_runtime=plan['embedding_runtime'],
        runtime_revisions=dict(original=plan['original_runtime_revisions'],recovery=plan['embedding_runtime']),
        cost='Unknown; zero compatibility ledger values do not mean free',
        answer_errors=[dict(question_id=q,error=a.get('error')) for q,a in answers.items() if a['status']!='answered'],
        judge_errors=[dict(question_id=q,error=g.get('error')) for q,g in grades.items() if g['status']=='judge_error'],
        limitation='Retried infrastructure failures after exposure; not an independent first-pass score. '
                   'All original successful answers retained; no grade-based selection or product tuning. '
                   'Same-model independent-phase judging; official organizer reevaluation is separate.')
    save(composite/'metrics.json',metric);save(composite/'answer-provenance.json',provenance)
    lines=['# Oracle500 入库优先补跑与合成结果','',
        f"原轮保留{plan['original_successes_retained']}份成功回答；独立补跑{plan['total']}个原失败记录。",'',
        f"补跑先全部入库并验证，再统一答题，全部清理后独立评分。补跑有效回答{supplement['answered']}/{plan['total']}。",'',
        f"合成有效回答{overall['answered']}/500；完整提交：{ready}；正确{overall['correct']}/500；准确率{overall['accuracy']}；Task-averaged {metric['task_averaged_accuracy']}。",'',
        f"原轮首遍成绩保留：Overall {original['overall_accuracy']}，Task-averaged {original['task_averaged_accuracy']}；原轮{original['answered']}/500份有效回答。",'',
        '| 题型 | 正确 | 有效回答 | 总数 | 准确率 |','|---|---:|---:|---:|---:|']
    for name,v in by_type.items():
        lines.append(f"| {name} | {v['correct']} | {v['answered']} | {v['total']} | {v['accuracy']} |")
    lines+=['',f"拒答子集：{abstention['correct']}/{abstention['total']}，准确率{abstention['accuracy']}。",'',
        '成功回答没有重答或按得分挑选。补跑按执行失败状态纳入，最多三次入库尝试均留存；已完成会话由持久化标记跳过。', '',
        '原文不建向量；抽取、模型、参数、问题和提示词沿用冻结版本。补跑使用已修复BGE服务（8线程、batch8）。', '',
        '这是披露基础设施重试后的合成结果，不能替代原轮首遍成绩或宣称独立盲测。Qwen同模型独立阶段评分属于自测，项目组正式复评另算。', '',
        'metrics.json 包含六题型、拒答、分母、原轮/补跑用量和失败；货币成本未知。逐题来源见answer-provenance.json。', '',
        f"原轮输入/输出token：{original['usage_totals']['prompt_tokens']}/{original['usage_totals']['completion_tokens']}；补跑输入/输出token：{supplement['usage_totals']['prompt_tokens']}/{supplement['usage_totals']['completion_tokens']}。",'',
        f"原轮墙钟{original['elapsed_s']/3600:.2f}小时；补跑墙钟{execution['elapsed_s']/3600:.2f}小时（含暂停、睡眠与重试等待）。",'',
        '完整性：原轮保护文件哈希、补跑冻结输入、全量入库门槛、原文/分块零向量、答题固定和清理均核验。', '']
    (composite/'report.md').write_text('\n'.join(lines))
    (out/'report.md').write_text('\n'.join(lines))
    export='hypotheses.jsonl' if ready else 'hypotheses.partial.jsonl'
    exported=rows(composite/export)
    expected=[m['question_id'] for m in selection if answers[m['question_id']]['status']=='answered']
    if [r['question_id'] for r in exported]!=expected or len(set(expected))!=len(exported):
        raise ValueError('Composite IDs do not match actual original IDs and answers')
    if any(r['hypothesis']!=answers[r['question_id']]['hypothesis'] for r in exported):
        raise ValueError('Composite answer content differs from fixed answer artifacts')
    save(out/'delivery-manifest.json',dict(created_at=now(),complete_submission=ready,
        files={str(p.relative_to(out)):dict(sha256=sha(p),bytes=p.stat().st_size)
               for p in [composite/'report.md',composite/'metrics.json',composite/export,
                          composite/'answer-provenance.json',out/'final-verification.json']}))
    print(json.dumps(dict(complete=execution['complete'],composite=metric),ensure_ascii=False))


def run(args):
    with (args.out/'recovery-run.lock').open('a') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (args.out/'recovery-started.json').exists():
            raise ValueError('Recovery already started; no automatic restart or replacement')
        frozen,plan,cases=validate(args.out,originals=True)
        preflight=read(args.out/'preflight.json')
        if not preflight.get('passed') or preflight.get('config_sha256')!=frozen['config_sha256']:
            raise ValueError('Successful frozen-config preflight required')
        credentials(args.runtime)
        save(args.out/'recovery-started.json',dict(pid=os.getpid(),started_at=now()))
        runner=Recovery(args,frozen,plan,cases)
        runner.run()
        report(args.out)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','preflight','run','report'))
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--runtime',type=Path,default=Path.home()/'Library/Application Support/memsys/runtime.json')
    parser.add_argument('--workers',type=int,choices=(1,2,3,4),default=4)
    parser.add_argument('--max-write-attempts',type=int,choices=(1,2,3),default=3)
    args=parser.parse_args();args.out=args.out.resolve()
    if args.action=='prepare':
        if args.source is None:parser.error('--source required for prepare')
        prepare(args)
    elif args.action=='preflight':
        validate(args.out,originals=True);full.preflight(args)
    elif args.action=='run':run(args)
    else:report(args.out)


if __name__=='__main__':
    main()
