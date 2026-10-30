"""Freeze and execute a three-way, balanced LongMemEval comparison.

Uses audited completed stores from a frozen ingestion to compare baseline and
candidate readers without another extraction bill. Native Mem0 builds its own
stores from identical whitelisted sessions. Judge runs only after all answers.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from memsys.bench.provenance import source_manifest,prompt_fingerprint
from memsys.bench.longmemeval import load
from audit_frozen_ingestion import verify_store
from mem0_bridge import prohibit_mem0_execution

TYPES=('multi-session','knowledge-update','temporal-reasoning','single-session-user',
       'single-session-assistant','single-session-preference')
VARIANTS=('baseline','candidate','mem0')


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def record_stage(out,name,entry):
    """Merge independent process results; atomic replacement protects readers."""
    import fcntl
    with (out/'.comparison.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        state=read(out/'comparison.json')
        stages=state['variants'][name]['stages']
        prior=next((row for row in stages if row['stage']==entry['stage'] and row['log']==entry['log']),None)
        if prior is not None and prior['returncode']!=entry['returncode']:
            raise ValueError('Conflicting completion records for one immutable attempt')
        if prior is None:stages.append(entry)
        with tempfile.NamedTemporaryFile(mode='w',dir=out,prefix='comparison-',suffix='.json',delete=False) as stream:
            json.dump(state,stream,ensure_ascii=False,indent=2)
            stream.write('\n')
            temporary=Path(stream.name)
        temporary.replace(out/'comparison.json')


def package_hash(path):
    return {str(p.relative_to(path)):sha(p) for p in sorted(path.rglob('*.py'))}


def checked_sample(rows):
    ids=[row['question_id'] for row in rows]
    counts=Counter(row['question_type'] for row in rows)
    if len(ids)!=len(set(ids)) or set(counts)!=set(TYPES) or len(set(counts.values()))!=1:
        raise ValueError('Use unique IDs with identical counts across all six types')
    if set(counts.values()) not in ({4},{5}):
        raise ValueError('Diagnostic comparisons require four or five per type')
    return dict(counts)


def prepare(args):
    prohibit_mem0_execution()
    out=args.out.resolve()
    if (out/'comparison.json').exists():
        raise ValueError('Use a new comparison directory; existing freezes are immutable')
    reference=args.reference.resolve()
    seed=args.seed_iteration.resolve()
    baseline=read(reference/'freeze.json')
    preflight=read(args.mem0_preflight)
    if preflight.get('status')!='passed' or not preflight.get('bm25_active') or not preflight.get('entity_nlp_active'):
        raise ValueError('Native Mem0 SDK, BM25 and entity preflight must pass before comparison')
    seed_freeze=read(seed/'freeze.json')
    source=source_manifest()
    if (source['write_source_sha256']!=seed_freeze['source']['write_source_sha256']
            or baseline['source']['write_source_sha256']!=seed_freeze['source']['write_source_sha256']):
        raise ValueError('Writing code changed: use fresh ingestion instead of borrowed stores')
    if sha(reference/baseline['sample_file'])!=baseline['sample_sha256']:
        raise ValueError('Baseline sample changed')
    rows=read(reference/baseline['sample_file'])
    counts=checked_sample(rows)
    seed_rows={row['question_id']:row for row in read(seed/seed_freeze['sample_file'])}
    if any(seed_rows.get(row['question_id'])!=row for row in rows):
        raise ValueError('Every reused sample record must exactly match its frozen seed')
    config=read(reference/'config.frozen.json')
    seed_config=read(seed/'config.frozen.json')
    for role in ('memory','extract','resolve','embedding'):
        name=config['roles'].get(role,config['roles'].get('memory'))
        seed_name=seed_config['roles'].get(role,seed_config['roles'].get('memory'))
        if config['models'].get(name)!=seed_config['models'].get(seed_name):
            raise ValueError('Write/embedding profiles changed: '+role)
    # Match both readers' evidence path. Direct whole-history shortcuts are off.
    config['memory'].update(complete_episode_context=False,complete_context_skip_rerank=False,
                            complete_context_direct_query=False)
    # Mem0's native system prompt alone is 33,831 UTF-8 bytes at this revision.
    # Re-ingest both frameworks under an equal ceiling; never reuse a cache by
    # rewriting its identity to conceal this write-configuration change.
    config['memory']['memory_input_budget_tokens']=args.memory_input_budget
    config['fallbacks']={}
    out.mkdir(parents=True,exist_ok=True)
    sample=out/'sample.json'
    save(sample,rows)
    tools=out/'tools'
    tools.mkdir()
    tool_names=('evaluate_mem0.py','mem0_bridge.py','audit_frozen_ingestion.py','ingest_frozen_iteration.py')
    for name in tool_names:
        shutil.copy2(ROOT/'scripts'/name,tools/name)
    records={}
    for name in VARIANTS:
        folder=out/name
        folder.mkdir()
        origin=ROOT/'src' if name=='candidate' else reference/'frozen-src'
        shutil.copytree(origin/'memsys',folder/'frozen-src/memsys',ignore=shutil.ignore_patterns('__pycache__'))
        variant_config=json.loads(json.dumps(config))
        if name=='candidate':
            variant_config['memory']['selection_policy']='coverage'
        else:
            variant_config['memory'].pop('selection_policy',None)
        save(folder/'config.json',variant_config)
        save(folder/'config.frozen.json',variant_config)
        freeze=dict(source=source if name=='candidate' else baseline['source'],
            prompts=prompt_fingerprint() if name=='candidate' else baseline['prompts'],
            sample_file='../sample.json',sample_sha256=sha(sample),
            config_sha256=sha(folder/'config.frozen.json'))
        save(folder/'freeze.json',freeze)
        records[name]=dict(config_sha256=sha(folder/'config.json'),
            freeze_sha256=sha(folder/'freeze.json'),
            source_files=package_hash(folder/'frozen-src'),stages=[])
    state=dict(created_at=datetime.now(timezone.utc).isoformat(),status='prepared',counts=counts,
        sample_sha256=sha(sample),reference=str(reference),seed_iteration=str(seed),
        seed_write_source_sha256=seed_freeze['source']['write_source_sha256'],
        tools={name:sha(tools/name) for name in tool_names},
        variants=records,stores_copied=False,fresh_baseline_ingestion=True,
        mem0_preflight=preflight,
        protocol=dict(mode='whole_system_same_models_and_budgets',mem0_top_k=200,
            final_context_budget=config['memory']['context_budget_tokens'],
            answer_input_budget=config['memory']['answer_input_budget_tokens'],
            memory_input_budget=config['memory']['memory_input_budget_tokens'],
            budget_unit=config['memory']['budget_mode'],models=config['roles'],
            full_context_shortcuts=False,semantic_scoring='official LongMemEval prompts with the fixed local judge',
            limitations='Public development/regression sample; native framework pipelines differ; not a blind or full-500 score.'))
    save(out/'comparison.json',state)
    print(json.dumps(dict(status='prepared',counts=counts,source=source['source_sha256'])),flush=True)


def check(out,state):
    if sha(out/'sample.json')!=state['sample_sha256']:
        raise ValueError('Frozen sample changed')
    for name,expected in state['tools'].items():
        if sha(out/'tools'/name)!=expected:
            raise ValueError('Frozen comparison tool changed: '+name)
    for name,record in state['variants'].items():
        if sha(out/name/'config.json')!=record['config_sha256']:
            raise ValueError('Frozen configuration changed: '+name)
        if (sha(out/name/'config.frozen.json')!=record['config_sha256']
                or record.get('freeze_sha256') and sha(out/name/'freeze.json')!=record['freeze_sha256']):
            raise ValueError('Frozen ingestion configuration or identity changed: '+name)
        if package_hash(out/name/'frozen-src')!=record['source_files']:
            raise ValueError('Frozen source changed: '+name)


def copy_stores(out,state):
    if state['stores_copied']:
        return
    seed=out/'baseline' if state.get('fresh_baseline_ingestion') else Path(state['seed_iteration'])
    ingestion=read(seed/'ingestion.json')
    expected={item.question_id:item for item,_ in load(str(out/'sample.json'))}
    complete={row['question_id'] for row in ingestion['results']}
    if not set(expected)<=complete:
        raise ValueError('Seed ingestion is still incomplete; missing %d sampled stores'%(len(set(expected)-complete)))
    immutable=out/'store-seed'
    immutable.mkdir(exist_ok=True)
    audit=[]
    for qid,item in expected.items():
        source=seed/'current/stores'/(qid+'.sqlite')
        target=immutable/(qid+'.sqlite')
        if target.exists():
            raise ValueError('Partial store seed already exists; inspect before re-preparing')
        with sqlite3.connect('file:'+str(source.resolve())+'?mode=ro',uri=True) as original:
            with sqlite3.connect(target) as snapshot:
                original.backup(snapshot)
        audit.append(dict(question_id=qid,sha256=sha(target),**verify_store(target,item)))
    for name in ('baseline','candidate'):
        target=out/name/'current/stores'
        target.mkdir(parents=True,exist_ok=True)
        for row in audit:
            shutil.copy2(immutable/(row['question_id']+'.sqlite'),target/(row['question_id']+'.sqlite'))
    state.update(stores_copied=True,store_seed_audit=audit)
    save(out/'comparison.json',state)


def execute(args):
    prohibit_mem0_execution()
    out=args.out.resolve()
    state=read(out/'comparison.json')
    check(out,state)
    if args.variant!='mem0':
        if args.stage!='ingest':
            copy_stores(out,state)
    name=args.variant
    folder=out/name
    # Resolving a venv's Python symlink drops its pyvenv.cfg and optional SDKs.
    interpreter=args.mem0_python.absolute() if name=='mem0' else args.python.absolute()
    env=dict(os.environ,PYTHONPATH=str(folder/'frozen-src'),MEM0_TELEMETRY='false')
    if args.stage=='ingest':
        if name!='baseline':
            raise ValueError('Ingest baseline once; the read-only candidate reuses its audited stores')
        cmd=[str(interpreter),'-u',str(out/'tools/ingest_frozen_iteration.py'),
             '--iteration',str(folder),'--max-wall-s','7200']
    elif args.stage=='run':
        if name=='mem0':
            cmd=[str(interpreter),'-u',str(out/'tools/evaluate_mem0.py'),'--config',str(folder/'config.json'),
                 '--data',str(out/'sample.json'),'--out',str(folder/'current'),'--top-k','200','--max-wall-s','7200']
        else:
            cmd=[str(interpreter),'-u','-m','memsys','bench','run','--config',str(folder/'config.json'),
                 '--data',str(out/'sample.json'),'--out',str(folder/'current'),'--workers','1','--max-wall-s','3600']
    elif args.stage=='judge':
        # Finish every framework before exposing reference labels in this comparison.
        expected={row['question_id'] for row in read(out/'sample.json')}
        for variant in VARIANTS:
            path=out/variant/'current/hypotheses.jsonl'
            answered={json.loads(line)['question_id'] for line in path.read_text().splitlines() if line.strip()} if path.exists() else set()
            if answered!=expected:
                raise ValueError('Complete all three answer sets before judging: '+variant)
        cmd=[str(interpreter),'-u','-m','memsys','bench','judge','--config',str(folder/'config.json'),
             '--data',str(out/'sample.json'),'--hyp',str(folder/'current/hypotheses.jsonl'),'--workers','1']
    else:
        cmd=[str(interpreter),'-u','-m','memsys','bench','report','--data',str(out/'sample.json'),
             '--run',str(folder/'current')]
    attempt=1+sum(stage['stage']==args.stage for stage in state['variants'][name]['stages'])
    logfile=folder/('%s-%02d.log'%(args.stage,attempt))
    print('Starting %s %s; log=%s'%(name,args.stage,logfile),flush=True)
    started=time.monotonic()
    with logfile.open('w') as stream:
        done=subprocess.run(cmd,cwd=ROOT,env=env,stdout=stream,stderr=subprocess.STDOUT)
    check(out,state)
    if args.stage=='run' and name!='mem0':
        times=folder/'current/timings.jsonl'
        rows=[json.loads(line) for line in times.read_text().splitlines() if line.strip()] if times.exists() else []
        if any(not row.get('cached_ingest') for row in rows):
            raise ValueError('A paired reader unexpectedly rebuilt memories; comparison invalid')
    record_stage(out,name,dict(stage=args.stage,returncode=done.returncode,
        wall_s=round(time.monotonic()-started,3),log=logfile.name,interpreter=str(interpreter)))
    print('Finished %s %s: exit=%d'%(name,args.stage,done.returncode),flush=True)
    return done.returncode


def main():
    prohibit_mem0_execution()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--stage',choices=('prepare','ingest','run','judge','report'),required=True)
    parser.add_argument('--reference',type=Path)
    parser.add_argument('--seed-iteration',type=Path)
    parser.add_argument('--variant',choices=VARIANTS,default='candidate')
    parser.add_argument('--memory-input-budget',type=int,default=65536)
    parser.add_argument('--mem0-preflight',type=Path,default=ROOT/'runs/mem0-surpass-v1/sdk-preflight-v2/result.json')
    parser.add_argument('--python',type=Path,default=ROOT/'.venv/bin/python')
    parser.add_argument('--mem0-python',type=Path,default=ROOT/'runs/mem0-surpass-v1/venv/bin/python')
    args=parser.parse_args()
    from memsys.cli import load_dotenv
    load_dotenv(str(ROOT/'.env'))
    if args.stage=='prepare':
        if not args.reference or not args.seed_iteration:
            parser.error('prepare requires --reference and --seed-iteration')
        prepare(args)
        return 0
    return execute(args)


if __name__=='__main__':
    raise SystemExit(main())
