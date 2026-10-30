"""Freeze a weighted iteration, rebuild memories and judge after answering.

Sample selection uses types and IDs only. Legacy weighted comparisons retain
the complete prior sample. Balanced24/30 modes take four/five per module.
Frozen sources permit a interrupted run to resume without mixing versions.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from memsys.config import AppConfig
from memsys.bench.provenance import source_manifest, prompt_fingerprint

COUNTS = {'multi-session': 40, 'knowledge-update': 20, 'temporal-reasoning': 20,
          'single-session-user': 8, 'single-session-assistant': 6, 'single-session-preference': 6}
SMALL_COUNTS = {'multi-session': 16, 'knowledge-update': 8, 'temporal-reasoning': 8,
                'single-session-user': 3, 'single-session-assistant': 2, 'single-session-preference': 3}
MODULE_COUNTS = {name: 4 for name in SMALL_COUNTS}
FIVE_COUNTS = {name: 5 for name in SMALL_COUNTS}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def prepare(args):
    out = args.out.resolve()
    if (out / 'freeze.json').exists():
        raise SystemExit('A freeze already exists. Use --stage all to resume, or a fresh directory.')
    out.mkdir(parents=True, exist_ok=True)
    rows = json.loads(args.data.read_text())
    by_id = {r['question_id']: r for r in rows}
    fixed = json.loads(args.paired.read_text()) if args.paired else []
    excluded = {r['question_id'] for r in json.loads(args.exclude_sample.read_text())} if args.exclude_sample else set()
    if len({r['question_id'] for r in fixed}) != len(fixed) or any(r['question_id'] not in by_id for r in fixed):
        raise SystemExit('Paired IDs must be unique and belong to the dataset.')
    used = set()
    prior_sources = []
    for p in sorted((ROOT / 'runs').rglob('hypotheses.jsonl')):
        if out in p.parents or any(part in ('before', 'frozen-src', 'frozen-sources') for part in p.parts):
            continue
        try:
            ids = {r['question_id'] for line in p.read_text().splitlines() if line.strip()
                   for r in [json.loads(line)] if r.get('question_id') in by_id and 'hypothesis' in r}
        except (ValueError, UnicodeError):
            continue
        used.update(ids)
        if ids:
            prior_sources.append(dict(path=str(p.relative_to(ROOT)), count=len(ids), sha256=sha(p)))
    rng = random.Random(args.seed)
    balanced = args.sample_size in (24, 30)
    counts = MODULE_COUNTS if args.sample_size == 24 else FIVE_COUNTS if args.sample_size == 30 else SMALL_COUNTS if args.sample_size == 40 else COUNTS
    selected = []
    paired_ids = {r['question_id'] for r in fixed}
    if paired_ids & excluded:
        raise SystemExit('Paired and excluded samples must not overlap.')
    for typ, count in counts.items():
        base = sorted([by_id[qid] for qid in paired_ids if by_id[qid]['question_type'] == typ], key=lambda r: r['question_id'])
        if len(base) > count:
            if not balanced:
                raise SystemExit('Paired sample exceeds a type quota.')
            base = sorted(rng.sample(base,count),key=lambda r:r['question_id'])
        pool = sorted([r for r in rows if r['question_type'] == typ and r['question_id'] not in paired_ids | excluded], key=lambda r: r['question_id'])
        fresh = pool if balanced else [r for r in pool if r['question_id'] not in used]
        need = count - len(base)
        picks = rng.sample(fresh, min(need, len(fresh)))
        taken = {r['question_id'] for r in picks}
        picks += rng.sample([r for r in pool if r['question_id'] not in taken], need-len(picks))
        selected.extend(base + picks)
    assert len(selected) == len({r['question_id'] for r in selected}) == args.sample_size
    sample_file = 'sample%d.json' % args.sample_size
    save(out / sample_file, selected)
    records = [dict(question_id=r['question_id'], question_type=r['question_type'],
        previously_answered=r['question_id'] in used, paired=r['question_id'] in paired_ids,
        abstention=r['question_id'].endswith('_abs'), sessions=len(r['haystack_sessions'])) for r in selected]
    save(out / 'selection.json', dict(seed=args.seed, counts=counts, total=args.sample_size,
        method=(('Four' if args.sample_size==24 else 'Five')+' per module: seeded random reduction of oversized paired modules, retaining all smaller modules and filling deficits uniformly from eligible IDs. Only types and IDs guide selection; answers, grades, write completion and latency are not used.' if balanced else
                'All paired sample IDs retained without reference to correctness; seeded random sampling per type, preferring IDs with no prior recorded hypothesis. No gold answers or correctness used.'),
        fresh=sum(not r['previously_answered'] for r in records), paired=sum(r['paired'] for r in records),
        paired_input_count=len(paired_ids),
        excluded_sample=dict(path=str(args.exclude_sample), sha256=sha(args.exclude_sample), count=len(excluded)) if args.exclude_sample else None,
        selected=records, prior_sources=prior_sources,
        limitations=('Balanced' if balanced else 'Weighted')+' diagnostic oracle sample; fresh means no prior recorded hypothesis, not a blind test. Not a full benchmark score.'))
    active = AppConfig.load(str(args.config))
    raw = json.loads(args.config.read_text())
    roles = {k:v for k,v in raw['roles'].items() if not k.startswith('_')}
    config = dict(models={k: {a:b for a,b in raw['models'][k].items() if not a.startswith('_')}
                          for k in sorted(set(roles.values()))}, roles=roles,
                  memory=active.memory.to_dict(), fallbacks={})
    save(out / 'config.frozen.json', config)
    shutil.copytree(ROOT / 'src/memsys', out / 'frozen-src/memsys', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(Path(__file__), out / 'runner.py')
    save(out / 'freeze.json', dict(created_at=datetime.now(timezone.utc).isoformat(), source=source_manifest(),
        prompts=prompt_fingerprint(), config_sha256=sha(out/'config.frozen.json'),
        sample_sha256=sha(out/sample_file), sample_file=sample_file,
        sample_size=args.sample_size, selection_sha256=sha(out/'selection.json'),
        data_sha256=sha(args.data), runner_sha256=sha(out/'runner.py'),
        models_fixed=True, reingest=True, workers=args.workers, stages=[]))
    print('Frozen %d questions: ' % args.sample_size + json.dumps(dict(Counter(r['question_type'] for r in selected))), flush=True)


def execute(args, stage):
    out = args.out.resolve()
    state = json.loads((out / 'freeze.json').read_text())
    sample_file = state.get('sample_file', 'sample100.json')
    env = dict(os.environ, PYTHONPATH=str(out/'frozen-src'))
    def check():
        for file, key in [('config.frozen.json','config_sha256'),(sample_file,'sample_sha256'),('selection.json','selection_sha256'),('runner.py','runner_sha256')]:
            if sha(out/file) != state[key]:
                raise SystemExit('Frozen file changed: ' + file)
        actual = json.loads(subprocess.check_output([sys.executable, '-c',
            'import json; from memsys.bench.provenance import source_manifest,prompt_fingerprint; print(json.dumps([source_manifest(),prompt_fingerprint()]))'],
            cwd=ROOT, env=env, text=True))
        if actual[0]['source_sha256'] != state['source']['source_sha256'] or actual[1] != state['prompts']:
            raise SystemExit('Frozen source or prompts changed.')
    check()
    sample, config, run = out/sample_file, out/'config.frozen.json', out/'current'
    expected = {r['question_id'] for r in json.loads(sample.read_text())}
    def complete():
        p = run/'hypotheses.jsonl'
        return {json.loads(x)['question_id'] for x in p.read_text().splitlines() if x.strip() and 'hypothesis' in json.loads(x)} if p.exists() else set()
    base = [sys.executable, '-u', '-m', 'memsys', 'bench']
    if stage == 'run':
        cmd = base + ['run', '--config', str(config), '--data', str(sample), '--out', str(run), '--workers', str(state['workers']), '--reingest']
    elif stage == 'judge':
        if complete() != expected:
            raise SystemExit('Complete every sampled answer before presenting gold labels to the judge.')
        cmd = base + ['judge', '--config', str(config), '--data', str(sample), '--hyp', str(run/'hypotheses.jsonl'), '--workers', str(state['workers'])]
    elif stage == 'report':
        cmd = base + ['report', '--data', str(sample), '--run', str(run)]
    else:
        raise SystemExit('Unknown stage')
    attempt = sum(s['stage']==stage for s in state['stages']) + 1
    log = out/('%s-%02d.log' % (stage, attempt))
    started = time.monotonic()
    print('Starting %s, log=%s' % (stage, log), flush=True)
    with log.open('w') as stream:
        process = subprocess.run(cmd, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
    check()
    code = process.returncode
    if stage == 'run' and complete() != expected:
        code = code or 1
    state['stages'].append(dict(stage=stage, attempt=attempt, returncode=code, log=log.name,
        wall_s=round(time.monotonic()-started,3), finished_at=datetime.now(timezone.utc).isoformat(), identity_unchanged=True))
    save(out/'freeze.json', state)
    print('Finished %s: exit=%d, completed_answers=%d' % (stage, code, len(complete())), flush=True)
    return code


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--config', type=Path, default=ROOT/'config.json')
    p.add_argument('--data', type=Path, default=ROOT/'data/longmemeval_oracle.json')
    p.add_argument('--paired', type=Path)
    p.add_argument('--exclude-sample', type=Path, help='Exclude all IDs from a previous sample before seeded selection.')
    p.add_argument('--seed', default='memory-accuracy-20261010-v21')
    p.add_argument('--workers', type=int, default=1)
    p.add_argument('--sample-size', type=int, choices=(24, 30, 40, 100), default=24,
                   help='24/30: four/five per module;40/100 retained for historical runs.')
    p.add_argument('--stage', choices=['prepare','run','judge','report','all'], default='all')
    args = p.parse_args()
    if args.workers < 1:
        p.error('--workers must be positive')
    if args.stage == 'prepare' or not (args.out/'freeze.json').exists():
        prepare(args)
    stages = ['run','judge','report'] if args.stage == 'all' else [] if args.stage=='prepare' else [args.stage]
    for stage in stages:
        code = execute(args, stage)
        if code:
            return code
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
