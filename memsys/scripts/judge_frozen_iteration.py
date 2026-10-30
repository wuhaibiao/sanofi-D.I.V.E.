"""Grade complete frozen answers with the shared per-question read budget."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import threading
import time


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--iteration',type=Path,required=True)
    p.add_argument('--max-wall-s',type=float,default=3600)
    args=p.parse_args()
    if args.max_wall_s<=0:p.error('grading wall budget must be positive')
    root=args.iteration.resolve()
    sys.path.insert(0,str(root/'frozen-src'))
    from memsys.bench.judge import _grade, _hypotheses, anscheck_prompt
    from memsys.bench.jsonl import append_jsonl,read_jsonl
    from memsys.bench.longmemeval import load
    from memsys.bench.provenance import source_manifest,prompt_fingerprint
    from memsys.cli import load_dotenv
    from memsys.config import AppConfig
    from memsys.llm import UsageLedger
    from memsys.read_budget import ReadBudget,is_read_service_error
    from memsys.read_retry import ReadTransportChat
    freeze=json.loads((root/'freeze.json').read_text())
    sample=root/freeze['sample_file'];config=root/'config.frozen.json';run=root/'current'
    if (source_manifest()['source_sha256']!=freeze['source']['source_sha256'] or
            prompt_fingerprint()!=freeze['prompts'] or sha(sample)!=freeze['sample_sha256'] or
            sha(config)!=freeze['config_sha256']):
        raise SystemExit('Frozen evaluation identity changed')
    pairs=load(str(sample));golds={g.question_id:g for _,g in pairs}
    hyp=run/'hypotheses.jsonl';answers={r['question_id']:r for r in _hypotheses(str(hyp))}
    if set(answers)!=set(golds):raise SystemExit('Complete every answer before loading references into the judge')
    timings=read_jsonl(str(run/'timings.jsonl'))
    if {r['question_id'] for r in timings}!=set(golds) or any(not r.get('cached_ingest') for r in timings):
        raise SystemExit('Every answer must use the verified frozen ingestion cache')
    load_dotenv(str(Path(__file__).resolve().parents[1]/'.env'))
    c=AppConfig.load(str(config));name=c.roles['judge'];model=c.client(name,UsageLedger(str(run/'judge-usage.jsonl')))
    out=Path(str(hyp)+'.eval-results-'+name)
    done={r['question_id'] for r in read_jsonl(str(out)) if isinstance(r.get('autoeval_label',{}).get('label'),bool)}
    started=time.monotonic();stop=threading.Event();timer=threading.Timer(args.max_wall_s,stop.set);timer.daemon=True;timer.start()
    previous,consecutive=None,0;errors=[];reason=None
    try:
        for item,gold in pairs:
            if item.question_id in done:continue
            if stop.is_set() or consecutive>=2:
                reason='batch wall budget exhausted' if stop.is_set() else 'two consecutive service failures'
                break
            s=c.memory
            budget=ReadBudget(min(s.read_timeout_s,max(0.001,args.max_wall_s-(time.monotonic()-started))),
                              s.read_retries_per_call,s.read_retries_total,s.read_retry_wait_s,stop)
            try:
                prompt=anscheck_prompt(gold.question_type,gold.question,gold.answer,
                                       str(answers[item.question_id]['hypothesis']),gold.is_abstention)
                label,actual,attempts=_grade(ReadTransportChat(model,budget),prompt,item.question_id)
                append_jsonl(str(out),dict(answers[item.question_id],autoeval_label=dict(model=name,label=label,
                    actual_model=actual,generation_attempts=attempts),read_transport=budget.snapshot()))
                done.add(item.question_id);previous,consecutive=None,0
                print(json.dumps(dict(question_id=item.question_id,label=label)),flush=True)
            except Exception as exc:
                service=is_read_service_error(exc)
                kind=(getattr(exc,'read_state',type(exc).__name__),getattr(exc,'status',None))
                consecutive=(consecutive+1 if previous==kind else 1) if service else 0
                previous=kind if service else None
                row=dict(question_id=item.question_id,error=str(exc),kind=kind,read_transport=budget.snapshot(),
                         raw_outputs=budget.outputs)
                errors.append(row);append_jsonl(str(run/'judge-errors.jsonl'),row)
                if getattr(exc,'account_problem',False):
                    reason='account access failure'
                    break
    finally:
        timer.cancel()
        state=dict(status='completed' if done==set(golds) else 'incomplete',expected=len(golds),graded=len(done),
                   failures=errors,wall_s=round(time.monotonic()-started,3),max_wall_s=args.max_wall_s,
                   runner_sha256=sha(__file__),judge=name,stopped=stop.is_set() or consecutive>=2 or reason is not None,
                   reason=reason)
        path=root/'judge-execution.json'
        if path.exists():
            old=json.loads(path.read_text());state['previous_attempts']=old.get('previous_attempts',[])+[{k:v for k,v in old.items() if k!='previous_attempts'}]
        path.write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n')
    return 0 if done==set(golds) else 1


if __name__=='__main__':
    raise SystemExit(main())
