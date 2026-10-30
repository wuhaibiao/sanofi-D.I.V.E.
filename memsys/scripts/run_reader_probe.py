"""Bounded reader validation on immutable historical stores; not fresh ingestion."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
import os
sys.path.insert(0, os.environ.get('MEMSYS_PROBE_SOURCE', str(ROOT/'src')))
from memsys.bench.jsonl import append_jsonl, read_jsonl
from memsys.bench.judge import judge
from memsys.bench.longmemeval import load
from memsys.bench.provenance import source_manifest, model_manifest, prompt_fingerprint
from memsys.cli import load_dotenv
from memsys.config import AppConfig
from memsys.core import MemorySystem
from memsys.llm import UsageLedger
from memsys.read_budget import is_read_service_error
from memsys.read_retry import ReadTransportChat


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--sample', type=Path, required=True)
    parser.add_argument('--seed-run', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--max-wall-s', type=float, default=600)
    parser.add_argument('--stage', choices=('read','judge'), default='read')
    args = parser.parse_args()
    load_dotenv(str(ROOT/'.env'))
    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out/'probe.json'
    config = AppConfig.load(str(args.config))
    ledger = UsageLedger(str(args.out/'usage.jsonl'))
    models = config.build_models(ledger)
    pairs = load(str(args.sample))
    seed_state = json.loads((args.seed_run/'freeze.json').read_text())
    seed_rows = json.loads((args.seed_run/seed_state.get('sample_file','sample40.json')).read_text())
    by_id = {r['question_id']:r for r in seed_rows}
    sample_rows = json.loads(args.sample.read_text())
    if any(by_id.get(r['question_id']) != r for r in sample_rows):
        raise SystemExit('Probe sample differs from historical seed input.')
    identity = dict(source=source_manifest()['source_sha256'], prompts=prompt_fingerprint(),
                    config_sha256=sha(args.config), sample_sha256=sha(args.sample),
                    seed_freeze_sha256=sha(args.seed_run/'freeze.json'),
                    model_settings=model_manifest(models), runner_sha256=sha(Path(__file__)))
    report = dict(identity=identity, scope='Reader-only probe on historical stores; no Step ingestion claim.',
                  expected=len(pairs), seed_run=str(args.seed_run.resolve()))
    if manifest_path.exists():
        prior = json.loads(manifest_path.read_text())
        if prior['identity'] != identity:
            raise SystemExit('Probe identity changed; use a new directory.')
        report = prior
    hyp = args.out/'hypotheses.jsonl'
    done = {r['question_id'] for r in read_jsonl(str(hyp))}
    started = time.monotonic()
    stop = threading.Event()
    timer = threading.Timer(args.max_wall_s, stop.set)
    timer.daemon = True
    timer.start()
    failures, previous, consecutive = [], None, 0
    try:
        if args.stage == 'read':
            for item, _ in pairs:
                if item.question_id in done:
                    continue
                if stop.is_set() or consecutive >= 2:
                    break
                stores = args.out/'stores'
                stores.mkdir(exist_ok=True)
                seed = args.seed_run/'current/stores'/(item.question_id+'.sqlite')
                immutable = args.out/'store-seed'
                immutable.mkdir(exist_ok=True)
                if not (immutable/seed.name).exists():
                    shutil.copy2(seed, immutable/seed.name)
                if sha(immutable/seed.name) != sha(seed):
                    raise SystemExit('Historical store seed changed.')
                shutil.copy2(immutable/seed.name, stores/seed.name)
                system = MemorySystem(models, config.memory, data_dir=str(stores), trace_dir=str(args.out/'traces'))
                t0 = time.monotonic()
                try:
                    reply = system.respond(item.question_id, item.question, item.question_date,
                        tags={'question_id':item.question_id}, cancel_event=stop,
                        read_timeout_s=min(config.memory.read_timeout_s,args.max_wall_s-(t0-started)))
                    append_jsonl(str(hyp),dict(question_id=item.question_id,hypothesis=reply.text))
                    append_jsonl(str(args.out/'timings.jsonl'),dict(question_id=item.question_id,
                        answer_s=round(time.monotonic()-t0,3),read_transport=reply.trace['read_transport'],
                        aggregation=reply.trace['aggregation'],reranking=reply.trace['reranking'],
                        index_coverage=reply.trace['index_coverage'],historical_store_sha256=sha(immutable/seed.name)))
                    done.add(item.question_id)
                    consecutive,previous=0,None
                    print(json.dumps(dict(question_id=item.question_id,status='answered',wall_s=round(time.monotonic()-t0,3))),flush=True)
                except Exception as exc:
                    kind=(getattr(exc,'read_state','error'),getattr(exc,'status',None))
                    consecutive=consecutive+1 if kind==previous else 1
                    previous=kind
                    row=dict(question_id=item.question_id,error=str(exc),read_state=kind[0],status=kind[1],
                             read_transport=getattr(exc,'read_transport',None),elapsed_s=round(time.monotonic()-t0,3))
                    append_jsonl(str(args.out/'errors.jsonl'),row)
                    failures.append(row)
                    print(json.dumps({k:v for k,v in row.items() if k!='read_transport'}),flush=True)
                finally:
                    system.close()
        else:
            if done != {i.question_id for i,_ in pairs}:
                raise SystemExit('Complete probe answers before grading.')
            judge_name=config.roles['judge']
            model=config.client(judge_name,ledger)
            for item,gold in pairs:
                if stop.is_set() or consecutive >= 2:
                    break
                try:
                    path=judge(str(hyp),{item.question_id:gold},ReadTransportChat(model),judge_name,
                               workers=1,progress=lambda x:print(x,flush=True))
                    consecutive,previous=0,None
                except Exception as exc:
                    kind=(type(exc).__name__,getattr(exc,'status',None))
                    consecutive=consecutive+1 if kind==previous else 1
                    previous=kind
                    failures.append(dict(question_id=item.question_id,stage='judge',error=str(exc)))
    finally:
        timer.cancel()
        report.update(completed_answers=len(done),last_stage=args.stage,last_stage_failures=failures,
                      wall_s=round(time.monotonic()-started,3),stopped=stop.is_set() or consecutive>=2,
                      usage=ledger.totals())
        eval_path=Path(str(hyp)+'.eval-results-'+config.roles['judge'])
        if eval_path.exists():
            grades=read_jsonl(str(eval_path))
            report['graded']=len(grades)
            report['correct']=sum(r['autoeval_label']['label'] is True for r in grades)
        manifest_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    complete = (len(done)==len(pairs) and not failures and
                (args.stage != 'judge' or report.get('graded',0)==len(pairs)))
    return 0 if complete else 1


if __name__ == '__main__':
    raise SystemExit(main())
