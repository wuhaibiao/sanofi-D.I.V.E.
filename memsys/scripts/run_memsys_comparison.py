"""Compare memsys evidence selectors only; never import or execute Mem0.

Reuse audited pre-answer stores, keeping models, sources and budgets identical.
Existing freeze, store-clone, reader, judge and reporting helpers remain the
single implementation of the respective steps. No extraction is permitted.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import fields
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
sys.path.insert(0, str(ROOT/'scripts'))
from audit_frozen_ingestion import verify_store
from clone_frozen_reader_seeds import clone
from run_accuracy_iteration import prepare as freeze_reader, MODULE_COUNTS
from memsys.bench.longmemeval import load
from memsys.config import MemorySettings
from memsys.operation_validation import validate_operation
from memsys.queryops import complete_search, operation_for
from memsys.read import QueryPlan

VARIANTS = ('ranked', 'coverage')
TOOLS = ('run_accuracy_iteration.py', 'clone_frozen_reader_seeds.py',
         'evaluate_retrieval_variant.py', 'compare_retrieval_variants.py',
         'run_bounded_command.py', 'run_memsys_comparison.py')


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def package_files(path):
    return {str(p.relative_to(path)):sha(p) for p in sorted(path.rglob('*.py'))}


def validate_sample(rows):
    counts = Counter(row['question_type'] for row in rows)
    if len({row['question_id'] for row in rows}) != len(rows) or counts != MODULE_COUNTS:
        raise ValueError('This comparison requires exactly four unique questions per each of six types.')
    return dict(counts)


def audit_seed(seed, rows):
    """Check completed immutable originals before even constructing a client."""
    state, binding = read(seed/'freeze.json'), read(seed/'ingest-freeze.json')
    ingestion, audit = read(seed/'ingestion.json'), read(seed/'source-audit.json')
    if (sha(seed/state['sample_file']) != state['sample_sha256']
            or sha(seed/'config.frozen.json') != state['config_sha256']):
        raise ValueError('Frozen seed sample or configuration changed.')
    if (not binding.get('immutable_seed') or ingestion.get('status') != 'completed'
            or ingestion.get('failures') or audit.get('status') != 'passed'):
        raise ValueError('Completed audited pre-answer stores are required.')
    for name, key in (('ingestion.json','ingestion_sha256'),('source-audit.json','source_audit_sha256')):
        if sha(seed/name) != binding[key]:
            raise ValueError('Ingestion proof changed: '+name)
    stores = {row['question_id']:row['store_sha256'] for row in binding['stores']}
    if len(stores) != len(binding['stores']) or set(stores) != {r['question_id'] for r in rows}:
        raise ValueError('Incomplete or duplicate immutable store coverage.')
    report = []
    for item, _ in load(str(seed/state['sample_file'])):
        path = seed/'store-seed'/(item.question_id+'.sqlite')
        if sha(path) != stores[item.question_id]:
            raise ValueError('Pre-answer store changed: '+item.question_id)
        report.append(dict(question_id=item.question_id, sha256=stores[item.question_id],
                           **verify_store(path,item)))
    return report


def freeze_controls(reference, sample, out, settings):
    """Borrow query output, never prior answers or gold; bind exact inputs."""
    reference=reference.resolve()
    origin=read(reference/'freeze.json')
    original_sample=reference/origin['sample_file']
    if (sha(original_sample)!=origin['sample_sha256']
            or sha(reference/'config.frozen.json')!=origin['config_sha256']):
        raise ValueError('Query-plan reference sample or configuration changed.')
    originals={item.question_id:item for item,_ in load(str(original_sample))}
    items=[item for item,_ in load(str(sample))]
    for item in items:
        if originals.get(item.question_id)!=item:
            raise ValueError('Query-plan source input differs: '+item.question_id)
    directory=out/'controls'
    directory.mkdir()
    rerank_directory=directory/'rerank-traces'
    rerank_directory.mkdir()
    plans,trace_hashes,corrections={}, {}, []
    allowed={field.name for field in fields(QueryPlan)}
    for item in items:
        path=reference/'current/traces'/(item.question_id+'.json')
        trace=read(path)
        if trace.get('message')!=item.question or trace.get('at')!=item.question_date.isoformat():
            raise ValueError('Recorded query input or clock differs: '+item.question_id)
        plan={key:deepcopy(value) for key,value in trace['plan'].items() if key in allowed}
        prior_error=plan.get('error')
        plan['error']=None  # Historical fallback is provenance, not a new outage.
        plan['source']='frozen_shared_query_plan'
        plan['trigger']='controlled_input'
        if settings.operation_semantics:
            selected,correction=validate_operation(item.question,plan['operation'],operation_for(item.question))
            plan['operation']=selected
            plan['operation_validation']=correction
            plan['needs_all']=bool(plan['needs_all'] or complete_search(selected))
            if correction:
                corrections.append(dict(question_id=item.question_id,**correction))
        plans[item.question_id]=dict(question=item.question,question_date=item.question_date.isoformat(),
            plan=plan,prior_planner_error=prior_error)
        # Only measured reranking inputs/scores enter the replay directory.
        # Do not copy old answer text, answer notes, labels or model responses.
        if trace.get('reranking',{}).get('backend')=='bge':
            save(rerank_directory/(item.question_id+'.json'),
                 dict(message=trace['message'],reranking=trace['reranking']))
        trace_hashes[item.question_id]=sha(path)
    save(directory/'plans.json',plans)
    return dict(reference=str(reference),reference_freeze_sha256=sha(reference/'freeze.json'),
        source_trace_sha256=trace_hashes,plans_sha256=sha(directory/'plans.json'),
        rerank_trace_sha256={p.name:sha(p) for p in sorted(rerank_directory.glob('*.json'))},
        operation_corrections=corrections,
        response_control='first explicitly completed response per identical model/generation/messages request',
        timing_scope='quality control with verified replay; not a live latency comparison')


def prepare(args):
    out, seed = args.out.resolve(), args.seed_run.resolve()
    if out.exists():
        raise ValueError('Use a new output directory; frozen comparisons are immutable.')
    seed_state = read(seed/'freeze.json')
    sample = seed/seed_state['sample_file']
    rows = read(sample)
    counts = validate_sample(rows)
    audit = audit_seed(seed, rows)
    config = read(seed/'config.frozen.json')
    if config.get('fallbacks'):
        raise ValueError('Use fixed model roles without automatic fallbacks.')
    # Only read-only settings change. Do not enlarge write or answer budgets.
    config['memory'].update(complete_episode_context=False,
        complete_context_skip_rerank=False, complete_context_direct_query=False)
    out.mkdir(parents=True)
    save(out/'source-audit.json', dict(status='passed', seed=str(seed), results=audit))
    controls=(freeze_controls(args.plans_from,sample,out,MemorySettings.from_dict(config['memory']))
              if getattr(args,'plans_from',None) else None)
    tools = out/'tools'
    tools.mkdir()
    for name in TOOLS:
        shutil.copy2(ROOT/'scripts'/name, tools/name)
    records = {}
    for name in VARIANTS:
        folder = out/name
        profile = json.loads(json.dumps(config))
        profile['memory']['selection_policy'] = name
        profile_path = out/(name+'-input-config.json')
        save(profile_path, profile)
        freeze_reader(SimpleNamespace(out=folder, data=sample, paired=sample, exclude_sample=None,
            seed='memsys-evidence-selection-four-per-class', sample_size=24,
            config=profile_path, workers=args.workers))
        clone(seed, folder)
        state = read(folder/'freeze.json')
        records[name] = dict(source=state['source'], prompts=state['prompts'],
            config_sha256=sha(folder/'config.frozen.json'),
            sample_sha256=sha(folder/state['sample_file']), sample_file=state['sample_file'],
            source_files=package_files(folder/'frozen-src'), stages=[])
    if records['ranked']['source'] != records['coverage']['source']:
        raise ValueError('Source changed while freezing; no clients were called. Create a fresh comparison.')
    state = dict(created_at=datetime.now(timezone.utc).isoformat(), status='prepared',
        scope='memsys-only read-policy ablation on identical audited pre-answer stores; no fresh ingestion',
        sample_status='previously used development/regression sample, not a blind holdout',
        mem0_execution=False, seed_run=str(seed), counts=counts, workers=args.workers,
        budgets={k:config['memory'][k] for k in ('budget_mode','memory_input_budget_tokens',
                    'answer_input_budget_tokens','context_budget_tokens')},
        tool_sha256={name:sha(tools/name) for name in TOOLS},
        source_audit_sha256=sha(out/'source-audit.json'), stores={r['question_id']:r['sha256'] for r in audit},
        variants=records,controls=controls)
    save(out/'comparison.json',state)
    check(out,state)
    print('Frozen memsys-only selectors on 24 audited stores; extraction calls allowed: 0.',flush=True)


def check(out, state):
    """Reject changed sources, budgets, tools or store contents before calls."""
    if state.get('mem0_execution') is not False or set(state['variants']) != set(VARIANTS):
        raise ValueError('Only memsys ranked/coverage variants may execute.')
    if sha(out/'source-audit.json') != state['source_audit_sha256']:
        raise ValueError('Frozen source audit changed.')
    for name, digest in state['tool_sha256'].items():
        if sha(out/'tools'/name) != digest:
            raise ValueError('Frozen helper changed: '+name)
    controls=state.get('controls')
    if controls:
        if sha(out/'controls/plans.json')!=controls['plans_sha256']:
            raise ValueError('Frozen shared query plans changed.')
        actual={p.name:sha(p) for p in sorted((out/'controls/rerank-traces').glob('*.json'))}
        if actual!=controls['rerank_trace_sha256']:
            raise ValueError('Frozen reranking replay inputs changed.')
    profiles = {}
    for name, variant in state['variants'].items():
        folder = out/name
        if (sha(folder/'config.frozen.json') != variant['config_sha256']
                or sha(folder/variant['sample_file']) != variant['sample_sha256']
                or package_files(folder/'frozen-src') != variant['source_files']):
            raise ValueError('Frozen source, sample or configuration changed: '+name)
        for qid, digest in state['stores'].items():
            if sha(folder/'store-seed'/(qid+'.sqlite')) != digest:
                raise ValueError('Immutable input store changed: '+qid)
        profiles[name] = read(folder/'config.frozen.json')
    for name in VARIANTS:
        if profiles[name]['memory'].pop('selection_policy') != name:
            raise ValueError('Unexpected selector policy.')
        if any(profiles[name]['memory'][k] for k in ('complete_episode_context',
                    'complete_context_skip_rerank','complete_context_direct_query')):
            raise ValueError('Whole-history bypass is forbidden in this ablation.')
    if profiles['ranked'] != profiles['coverage']:
        raise ValueError('Models, settings or budgets differ; not a policy ablation.')


def execute(args):
    out = args.out.resolve()
    state = read(out/'comparison.json')
    check(out,state)
    names = VARIANTS if args.variant=='all' else (args.variant,)
    for name in names:
        variant, folder = state['variants'][name], out/name
        if any(row['returncode']==0 for row in variant['stages']):
            continue
        attempt = len(variant['stages'])+1
        command = [sys.executable, '-u', str(out/'tools/run_bounded_command.py'),
            '--out', str(folder/('supervisor-%02d'%attempt)), '--timeout-s', str(args.max_wall_s),
            '--', sys.executable, '-u', str(out/'tools/evaluate_retrieval_variant.py'),
            '--source', str(folder/'frozen-src'), '--config', str(folder/'config.frozen.json'),
            '--sample', str(folder/variant['sample_file']), '--stores', str(folder/'store-seed'),
            '--out', str(folder/'current'), '--workers', str(state['workers']),
            '--sample-status', state['sample_status']]
        if state.get('controls'):
            command += ['--plans',str(out/'controls/plans.json'),
                '--rerank-traces',str(out/'controls/rerank-traces'),
                '--response-cache',str(out/'response-cache')]
        print('Starting memsys '+name+'; log='+str(folder/('supervisor-%02d/command.log'%attempt)),flush=True)
        env = dict(os.environ, PYTHONPATH=str(folder/'frozen-src'))
        done = subprocess.run(command,cwd=ROOT,env=env)
        check(out,state)
        variant['stages'].append(dict(attempt=attempt,returncode=done.returncode,
            supervisor='supervisor-%02d/supervisor.json'%attempt))
        save(out/'comparison.json',state)
        if done.returncode:
            return done.returncode
    if all(any(row['returncode']==0 for row in v['stages']) for v in state['variants'].values()):
        report(out,state)
    return 0


def report(out, state):
    check(out,state)
    # All scoring helpers are read-only here. Never relabel existing answers.
    sys.path.insert(0,str(out/'tools'))
    from compare_retrieval_variants import summarize
    rows = read(out/'ranked'/state['variants']['ranked']['sample_file'])
    golds = {row['question_id']:row for row in rows}
    results = {name:summarize(out/name/'current',golds) for name in VARIANTS}
    if results['ranked'][0]['frozen_fact_stores'] != results['coverage'][0]['frozen_fact_stores']:
        raise ValueError('Reader seeds differ.')
    changes, discordant = [], []
    for qid in golds:
        old, new = results['ranked'][1][qid], results['coverage'][1][qid]
        if old != new:
            entry = dict(question_id=qid,question_type=golds[qid]['question_type'],
                         direction='improved' if new else 'regressed')
            changes.append(entry)
            if results['ranked'][2][qid] == results['coverage'][2][qid]:
                discordant.append(entry)
    controlled=bool(state.get('controls'))
    replay_stats={}
    matching_plans=0
    if controlled:
        expected=read(out/'controls/plans.json')
        for qid in golds:
            actual=[read(out/name/'current/traces'/(qid+'.json'))['plan'] for name in VARIANTS]
            if actual[0]!=actual[1] or actual[0]!=expected[qid]['plan']:
                raise ValueError('Shared query plan not reproduced: '+qid)
            matching_plans+=1
        for name in VARIANTS:
            path=out/name/'current/response-events.jsonl'
            events=[json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            replay_stats[name]=dict(replayed=sum(bool(r['replayed']) for r in events),
                live=sum(not r['replayed'] for r in events),
                by_purpose={purpose:dict(replayed=sum(r['replayed'] for r in events if r['purpose']==purpose),
                    live=sum(not r['replayed'] for r in events if r['purpose']==purpose))
                    for purpose in sorted({r['purpose'] for r in events})})
    summary = dict(scope=state['scope'],sample_status=state['sample_status'],mem0_execution=False,
        variants={name:values[0] for name,values in results.items()},paired_changes=changes,
        paired_improved=sum(c['direction']=='improved' for c in changes),
        paired_regressed=sum(c['direction']=='regressed' for c in changes),
        identical_answer_judge_disagreements=discordant,
        controlled_query_plans=controlled,matching_shared_plans=matching_plans,
        response_replay=replay_stats,live_latency_comparison_valid=not controlled,
        achieved_full_benchmark_superiority=False,achieved_full_benchmark_100_percent=False)
    save(out/'results.json',summary)
    lines=['# memsys 证据选取冻结比较','',
        '范围：已使用的开发回归样本；仅比较自研读取策略，不运行 Mem0，不代表完整 500 题。','',
        '| 类别 | ranked | coverage |','| --- | ---: | ---: |']
    for kind in MODULE_COUNTS:
        values=[summary['variants'][name]['by_type'][kind] for name in VARIANTS]
        lines.append('| %s | %d/%d | %d/%d |'%(kind,values[0]['correct'],values[0]['total'],
                                                           values[1]['correct'],values[1]['total']))
    lines += ['', '总体：'+', '.join('%s %d/%d'%(name,results[name][0]['correct'],len(golds)) for name in VARIANTS)+'.',
        '配对改善 %d，回退 %d；相同答案评分分歧 %d，原始标签保持不变。'%(summary['paired_improved'],
            summary['paired_regressed'],len(discordant)), '',
        '写入和索引身份相同，无新增抽取/裁决调用。成本缺少价格时为 unknown，不记作零。', '',
        '两边均关闭完整历史短路径；不能将该结果与 v78 短路径 19/24 直接称为同条件净提升。']
    if controlled:
        lines += ['', '共享查询计划逐题重现 %d/%d；只回放完全相同请求的首个明确完成响应。'%(matching_plans,len(golds)),
            '本轮为质量控制实验；缓存命中耗时和调用减少不作为线上性能或成本提升。',
            '响应回放：'+json.dumps(replay_stats,ensure_ascii=False)]
    (out/'report.md').write_text('\n'.join(lines)+'\n')
    state['status']='completed'
    save(out/'comparison.json',state)
    print('memsys-only results: '+', '.join('%s=%d/24'%(n,results[n][0]['correct']) for n in VARIANTS),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--seed-run',type=Path)
    parser.add_argument('--plans-from',type=Path,
        help='Frozen reference reader supplying query plans and measured rerank inputs, never answers')
    parser.add_argument('--stage',choices=('prepare','run','report','all'),default='all')
    parser.add_argument('--variant',choices=(*VARIANTS,'all'),default='all')
    parser.add_argument('--workers',type=int,default=1)
    parser.add_argument('--max-wall-s',type=float,default=1800)
    args=parser.parse_args()
    if args.workers!=1 or not math.isfinite(args.max_wall_s) or args.max_wall_s<=0:
        parser.error('Use a single worker and a positive bounded wall time.')
    if args.stage=='prepare' or not (args.out/'comparison.json').exists():
        if args.seed_run is None:
            parser.error('Prepare requires a completed audited --seed-run.')
        prepare(args)
    if args.stage=='prepare':
        return 0
    if args.stage=='report':
        report(args.out.resolve(),read(args.out/'comparison.json'))
        return 0
    return execute(args)


if __name__=='__main__':
    raise SystemExit(main())
