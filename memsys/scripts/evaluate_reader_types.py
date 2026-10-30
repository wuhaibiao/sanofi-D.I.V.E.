"""Evaluate fixed four-per-type subsets on audited frozen memsys readers only.

Selection depends on type, never answers or labels. Completed response replay
is an explicit quality control; neither replay latency nor skipped calls are
production performance measurements. No ingestion or Mem0 execution exists.
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

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from clone_frozen_reader_seeds import identity
from run_memsys_comparison import MODULE_COUNTS,sha,read,save


def select_types(rows,types):
    if not types or len(set(types))!=len(types) or not set(types)<=set(MODULE_COUNTS):
        raise ValueError('Select unique supported question types.')
    selected=[row for row in rows if row['question_type'] in types]
    counts=Counter(row['question_type'] for row in selected)
    if (counts!={kind:4 for kind in types}
            or len({r['question_id'] for r in selected})!=len(selected)):
        raise ValueError('Use all four unique frozen questions of each chosen type.')
    return selected


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iteration',type=Path,required=True)
    parser.add_argument('--types',nargs='+',choices=tuple(MODULE_COUNTS),required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--controls',type=Path,required=True)
    parser.add_argument('--response-seed',type=Path,required=True)
    parser.add_argument('--max-wall-s',type=float,default=600)
    args=parser.parse_args()
    if not math.isfinite(args.max_wall_s) or args.max_wall_s<=0:
        parser.error('Use a finite positive wall deadline.')
    root,out=args.iteration.resolve(),args.out.resolve()
    state=read(root/'freeze.json')
    actual,prompts,_,_=identity(root)
    if (actual['source_sha256']!=state['source']['source_sha256'] or prompts!=state['prompts']
            or sha(root/'config.frozen.json')!=state['config_sha256']
            or sha(root/state['sample_file'])!=state['sample_sha256']):
        raise ValueError('Frozen reader identity changed.')
    lineage=read(root/'reader-seed-lineage.json')
    if sha(root/'reader-seed-lineage.json')!=state['reader_seed_lineage_sha256']:
        raise ValueError('Frozen store lineage changed.')
    rows=select_types(read(root/state['sample_file']),args.types)
    stores={r['question_id']:r['store_sha256'] for r in lineage['stores']}
    for row in rows:
        qid=row['question_id']
        if sha(root/'store-seed'/(qid+'.sqlite'))!=stores[qid]:
            raise ValueError('Original pre-answer store changed: '+qid)
    if out.exists():
        raise ValueError('Use a fresh diagnostic output directory; do not replace old results.')
    out.mkdir(parents=True)
    save(out/'sample.json',rows)
    tools=out/'tools'
    tools.mkdir()
    for name in ('evaluate_retrieval_variant.py','run_bounded_command.py','evaluate_reader_types.py'):
        shutil.copy2(ROOT/'scripts'/name,tools/name)
    controls=args.controls.resolve()
    shutil.copytree(controls,out/'controls')
    cache=out/'response-cache'
    cache.mkdir()
    # Copy only completed cached values. Historical cache and lock files stay
    # unchanged; new requests append new entries in this experiment alone.
    seed_cache=args.response_seed.resolve()
    for path in seed_cache.glob('*.json'):
        shutil.copy2(path,cache/path.name)
    control_proof=dict(scope='fixed four-per-selected-type reader regression, not a full24 score',
        counts=dict(Counter(r['question_type'] for r in rows)),source=actual,
        sample_sha256=sha(out/'sample.json'),config_sha256=sha(root/'config.frozen.json'),
        pre_answer_store_sha256={r['question_id']:stores[r['question_id']] for r in rows},
        plans_sha256=sha(out/'controls/plans.json'),
        response_seed_sha256={p.name:sha(p) for p in cache.glob('*.json')},
        helper_sha256={p.name:sha(p) for p in tools.glob('*.py')},mem0_execution=False,
        timing_scope='completed-response/verified-rerank replay; not online latency')
    save(out/'control.json',control_proof)
    env=dict(os.environ,PYTHONPATH=str(root/'frozen-src'))
    command=[sys.executable,'-u',str(tools/'run_bounded_command.py'),'--out',str(out/'supervisor'),
        '--timeout-s',str(args.max_wall_s),'--',sys.executable,'-u',str(tools/'evaluate_retrieval_variant.py'),
        '--source',str(root/'frozen-src'),'--config',str(root/'config.frozen.json'),
        '--sample',str(out/'sample.json'),'--stores',str(root/'store-seed'),
        '--out',str(out/'current'),'--workers','1','--plans',str(out/'controls/plans.json'),
        '--rerank-traces',str(out/'controls/rerank-traces'),'--response-cache',str(cache),
        '--sample-status','fixed previously used four-per-selected-type regression']
    print('Starting fixed memsys subset: '+json.dumps(control_proof['counts']),flush=True)
    return subprocess.run(command,cwd=ROOT,env=env).returncode


if __name__=='__main__':
    raise SystemExit(main())
