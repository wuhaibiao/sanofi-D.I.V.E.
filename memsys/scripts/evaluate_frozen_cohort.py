"""Run one memsys-only balanced cohort after immutable ingestion has been audited.

Live query plans are not a locked-plan causal ablation. Exact completed response
replay is optional and never presented as online latency or an independent run.
"""
import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

from clone_frozen_reader_seeds import identity
from run_memsys_comparison import audit_seed,read,save,sha,MODULE_COUNTS

ROOT=Path(__file__).resolve().parents[1]


def validate_cohort(rows):
    counts=Counter(r['question_type'] for r in rows)
    if (len({r['question_id'] for r in rows})!=len(rows) or set(counts)!=set(MODULE_COUNTS)
            or len(set(counts.values()))!=1 or next(iter(counts.values()),0) not in (4,5)):
        raise ValueError('Require exactly four or five unique questions of every supported type.')
    return dict(counts)


def validate_reader(root):
    state=read(root/'freeze.json')
    actual,prompts,_,_=identity(root)
    if (actual['source_sha256']!=state['source']['source_sha256'] or prompts!=state['prompts']
            or sha(root/'config.frozen.json')!=state['config_sha256']
            or sha(root/state['sample_file'])!=state['sample_sha256']):
        raise ValueError('Frozen reader identity changed before execution.')
    config=read(root/'config.frozen.json')
    if any(config['memory'].get(k) for k in ('complete_episode_context',
            'complete_context_skip_rerank','complete_context_direct_query')):
        raise ValueError('Whole-history short paths are disabled for this cohort.')
    if config.get('fallbacks'):
        raise ValueError('Use fixed models without automatic fallback.')
    rows=read(root/state['sample_file'])
    counts=validate_cohort(rows)
    audit=audit_seed(root,rows)
    return state,actual,counts,audit


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--iteration',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--response-seed',type=Path)
    p.add_argument('--max-wall-s',type=float,default=1800)
    args=p.parse_args()
    if not math.isfinite(args.max_wall_s) or args.max_wall_s<=0:
        p.error('Use a finite positive deadline.')
    root,out=args.iteration.resolve(),args.out.resolve()
    state,source,counts,audit=validate_reader(root)
    if out.exists():
        raise ValueError('Use a fresh output directory; do not replace completed answers.')
    out.mkdir(parents=True)
    helpers=out/'tools';helpers.mkdir()
    for name in ('evaluate_retrieval_variant.py','run_bounded_command.py','evaluate_frozen_cohort.py'):
        shutil.copy2(ROOT/'scripts'/name,helpers/name)
    cache=out/'response-cache';cache.mkdir()
    if args.response_seed:
        for path in args.response_seed.resolve().glob('*.json'):
            shutil.copy2(path,cache/path.name)
    proof=dict(scope='balanced memsys cohort; live query plans, not locked-plan causal attribution',
        counts=counts,source=source,sample_sha256=state['sample_sha256'],
        config_sha256=state['config_sha256'],source_audit_sha256=sha(root/'source-audit.json'),
        ingest_freeze_sha256=sha(root/'ingest-freeze.json'),
        audited_stores={r['question_id']:r['sha256'] for r in audit},
        helpers={path.name:sha(path) for path in helpers.glob('*.py')},
        response_seed={path.name:sha(path) for path in cache.glob('*.json')},
        mem0_execution=False,reingest=False,
        timing_scope='strict completed-response replay must be excluded from live performance claims')
    save(out/'control.json',proof)
    command=[sys.executable,'-u',str(helpers/'run_bounded_command.py'),
        '--timeout-s',str(args.max_wall_s),'--out',str(out/'supervisor'),'--',
        sys.executable,'-u',str(helpers/'evaluate_retrieval_variant.py'),
        '--source',str(root/'frozen-src'),'--config',str(root/'config.frozen.json'),
        '--sample',str(root/state['sample_file']),'--stores',str(root/'store-seed'),
        '--out',str(out/'current'),'--workers','1','--response-cache',str(cache),
        '--sample-status','previously used development cohort, four/five per type; not blind']
    print('Starting own-system cohort: '+json.dumps(counts),flush=True)
    return subprocess.run(command,cwd=ROOT,env=dict(os.environ,PYTHONPATH=str(root/'frozen-src'))).returncode


if __name__=='__main__':
    raise SystemExit(main())
