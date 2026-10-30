"""Freeze one implementation, then replay and judge two preselected samples."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from memsys.bench.provenance import prompt_fingerprint, source_manifest


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(config, samples):
    return {'source': source_manifest(), 'prompts': prompt_fingerprint(),
            'config_sha256': digest(config),
            'samples': {name: {'path': str(path), 'sha256': digest(path)}
                        for name, path in samples.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--fixed', required=True, type=Path)
    parser.add_argument('--holdout', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    samples = {'fixed34': args.fixed.resolve(), 'holdout': args.holdout.resolve()}
    config = args.config.resolve()
    if json.loads(config.read_text()).get('fallbacks'):
        raise SystemExit('Frozen evaluation requires explicit models without automatic fallbacks.')
    ids = {}
    for name, path in samples.items():
        entries = json.loads(path.read_text())
        ids[name] = [entry['question_id'] for entry in entries]
        if len(ids[name]) != len(set(ids[name])):
            raise SystemExit('Repeated question IDs in ' + name)
    if len(ids['fixed34']) != 34 or not ids['holdout'] or set(ids['fixed34']) & set(ids['holdout']):
        raise SystemExit('Expected 34 fixed questions and a separate nonempty holdout.')
    args.out.mkdir(parents=True, exist_ok=True)
    frozen = identity(config, samples)
    state = {'frozen_at': datetime.now(timezone.utc).isoformat(), 'identity': frozen,
             'counts': {name: len(keys) for name, keys in ids.items()}, 'stages': []}
    manifest = args.out / 'freeze.json'
    if manifest.exists():
        raise SystemExit('Use a fresh evaluation directory; an existing freeze cannot be overwritten.')

    def save():
        manifest.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')

    save()
    for name, sample in samples.items():
        run = args.out / name
        commands = [
            ['run', '--config', str(config), '--data', str(sample), '--out', str(run),
             '--workers', str(args.workers), '--reingest'],
            ['judge', '--config', str(config), '--data', str(sample),
             '--hyp', str(run / 'hypotheses.jsonl'), '--workers', str(args.workers)],
            ['report', '--data', str(sample), '--run', str(run)],
        ]
        for command in commands:
            if identity(config, samples) != frozen:
                raise SystemExit('Source, prompt, config or sample changed after the freeze.')
            stage = name + '-' + command[0]
            print('Starting ' + stage, flush=True)
            with (args.out / (stage + '.log')).open('w') as stream:
                completed = subprocess.run([sys.executable, '-u', '-m', 'memsys', 'bench'] + command,
                    cwd=str(ROOT), stdout=stream, stderr=subprocess.STDOUT)
            state['stages'].append({'stage': stage, 'returncode': completed.returncode,
                                   'log': stage + '.log'})
            save()
            print('Finished %s (exit %s)' % (stage, completed.returncode), flush=True)
    state['identity_unchanged'] = identity(config, samples) == frozen
    save()
    return 0 if state['identity_unchanged'] and all(s['returncode'] == 0 for s in state['stages']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
