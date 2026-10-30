"""Evaluate a frozen reader on identical completed memory stores.

The normal iteration runner remains the fresh-ingestion reference. This mode
requires an identical write-source fingerprint, write settings, input sample,
memory model and embedding configuration; every answered item must confirm a
cache hit before judging. Immutable store seeds make the comparison auditable.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def validate_seed_records(sample_rows, source_rows):
    source_by_id = {row['question_id']: row for row in source_rows}
    if (len(source_by_id) != len(source_rows)
            or len({row['question_id'] for row in sample_rows}) != len(sample_rows)
            or any(row != source_by_id.get(row['question_id']) for row in sample_rows)):
        raise SystemExit('Every cached sample must exactly match its unique complete seed record.')
    return source_by_id


def check_local_embedding(config):
    """Catch a stopped local service before paying for query-model requests."""
    profile = config['models'][config['roles']['embedding']]
    endpoint = urlsplit(profile.get('base_url') or '')
    if endpoint.hostname not in ('localhost', '127.0.0.1', '::1'):
        return
    port = endpoint.port or (443 if endpoint.scheme == 'https' else 80)
    try:
        with socket.create_connection((endpoint.hostname, port), timeout=3):
            pass
    except OSError as exc:
        raise SystemExit('Local embedding service is not reachable at %s:%d; restore it before reader evaluation.'
                         % (endpoint.hostname, port)) from exc
    print('Local embedding port is reachable: %s:%d' % (endpoint.hostname, port), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--seed-run', type=Path, required=True)
    args = parser.parse_args()
    out, seed = args.out.resolve(), args.seed_run.resolve()
    state = read(out / 'freeze.json')
    source_state = read(seed / 'freeze.json')
    sample_file = state.get('sample_file', 'sample100.json')
    source_sample_file = source_state.get('sample_file', 'sample100.json')
    sample_rows = read(out / sample_file)
    source_rows = read(seed / source_sample_file)
    source_by_id = validate_seed_records(sample_rows, source_rows)
    if (sha(seed / source_sample_file) != source_state['sample_sha256']
            or sha(seed / 'config.frozen.json') != source_state['config_sha256']):
        raise SystemExit('Frozen seed sample or configuration changed.')
    env = dict(os.environ, PYTHONPATH=str(out / 'frozen-src'))

    def check():
        for filename, key in [('config.frozen.json', 'config_sha256'), (sample_file, 'sample_sha256'),
                              ('selection.json', 'selection_sha256'), ('runner.py', 'runner_sha256')]:
            if sha(out / filename) != state[key]:
                raise SystemExit('Frozen artifact changed: ' + filename)
        if sha(out / 'read-runner.py') != state['read_runner_sha256']:
            raise SystemExit('Frozen read runner changed.')
        actual = json.loads(subprocess.check_output([sys.executable, '-c',
            'import json; from memsys.bench.provenance import source_manifest,prompt_fingerprint; print(json.dumps([source_manifest(),prompt_fingerprint()]))'],
            cwd=ROOT, env=env, text=True))
        if actual[0]['source_sha256'] != state['source']['source_sha256'] or actual[1] != state['prompts']:
            raise SystemExit('Frozen reader source changed.')

    if 'read_runner_sha256' not in state:
        shutil.copy2(Path(__file__), out / 'read-runner.py')
        state.update(read_runner_sha256=sha(out / 'read-runner.py'), reingest=False,
                     evaluation_mode='paired_reader_on_identical_stores', seed_run=str(seed))
        save(out / 'freeze.json', state)
    if state['seed_run'] != str(seed):
        raise SystemExit('Cannot change a frozen store seed.')
    check()
    if state['source']['write_source_sha256'] != source_state['source']['write_source_sha256']:
        raise SystemExit('Writing code differs; use fresh ingestion.')
    config, source_config = read(out / 'config.frozen.json'), read(seed / 'config.frozen.json')
    for role in ('memory', 'embedding'):
        if config['models'][config['roles'][role]] != source_config['models'][source_config['roles'][role]]:
            raise SystemExit('Writing or embedding model differs; use fresh ingestion.')
    # Check settings through the frozen implementation, rather than guessing
    # which configuration fields affect ingestion and indexing.
    settings_check = subprocess.check_output([sys.executable, '-c',
        'import sys; from memsys.config import AppConfig; a,b=[AppConfig.load(p).memory for p in sys.argv[1:]]; print(a.write_fingerprint()==b.write_fingerprint())',
        str(out / 'config.frozen.json'), str(seed / 'config.frozen.json')], cwd=ROOT, env=env, text=True).strip()
    if settings_check != 'True':
        raise SystemExit('Writing settings differ; use fresh ingestion.')

    print('Waiting for the complete seed run; only unchanged sampled records are reused.', flush=True)
    while True:
        source_state = read(seed / 'freeze.json')
        runs = [s for s in source_state['stages'] if s['stage'] == 'run']
        if runs and runs[-1]['returncode'] == 0:
            break
        time.sleep(15)
    expected = {r['question_id'] for r in sample_rows}
    if len(expected) != len(sample_rows):
        raise SystemExit('Sample IDs must be unique.')
    hypotheses = [json.loads(line) for line in (seed / 'current/hypotheses.jsonl').read_text().splitlines() if line.strip()]
    if {r['question_id'] for r in hypotheses} != set(source_by_id) or len(hypotheses) != len(source_by_id):
        raise SystemExit('Store seed has incomplete question coverage.')
    run = out / 'current'
    immutable = out / 'store-seed'
    if 'store_seed' not in state:
        immutable.mkdir()
        stores = run / 'stores'
        stores.mkdir(parents=True, exist_ok=True)
        records = []
        for qid in sorted(expected):
            name = qid + '.sqlite'
            original = seed / 'current/stores' / name
            shutil.copy2(original, immutable / name)
            shutil.copy2(original, stores / name)
            records.append(dict(question_id=qid, file=name, sha256=sha(immutable / name)))
        state['store_seed'] = dict(source_manifest=source_state['source'], records=records,
                                  source_freeze_sha256=sha(seed / 'freeze.json'))
        save(out / 'freeze.json', state)
    for record in state['store_seed']['records']:
        if sha(immutable / record['file']) != record['sha256']:
            raise SystemExit('Immutable seed changed: ' + record['file'])
    for stage in ('run', 'judge', 'report'):
        if any(s['stage'] == stage and s['returncode'] == 0 for s in state['stages']):
            continue
        check()
        base = [sys.executable, '-u', '-m', 'memsys', 'bench']
        sample, config_path = out / sample_file, out / 'config.frozen.json'
        if stage == 'run':
            check_local_embedding(config)
            command = base + ['run', '--config', str(config_path), '--data', str(sample), '--out', str(run),
                              '--workers', str(state['workers'])]
        elif stage == 'judge':
            command = base + ['judge', '--config', str(config_path), '--data', str(sample),
                              '--hyp', str(run / 'hypotheses.jsonl'), '--workers', str(state['workers'])]
        else:
            command = base + ['report', '--data', str(sample), '--run', str(run)]
        attempt = 1 + sum(s['stage'] == stage for s in state['stages'])
        log = out / ('%s-%02d.log' % (stage, attempt))
        print('Starting %s: %s' % (stage, log), flush=True)
        started = time.monotonic()
        with log.open('w') as stream:
            code = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT).returncode
        check()
        if stage == 'run':
            hypothesis_file, timing_file = run / 'hypotheses.jsonl', run / 'timings.jsonl'
            actual = {json.loads(x)['question_id'] for x in hypothesis_file.read_text().splitlines() if x.strip()} if hypothesis_file.exists() else set()
            timings = [json.loads(x) for x in timing_file.read_text().splitlines() if x.strip()] if timing_file.exists() else []
            if actual != expected or {x['question_id'] for x in timings if x.get('cached_ingest')} != expected:
                code = code or 1
            state['cache_validation'] = dict(expected=len(expected), cache_hits=sum(bool(x.get('cached_ingest')) for x in timings),
                                            completed_answers=len(actual))
        state['stages'].append(dict(stage=stage, attempt=attempt, returncode=code, log=log.name,
                                    wall_s=round(time.monotonic() - started, 3), identity_unchanged=True))
        save(out / 'freeze.json', state)
        print('Finished %s: exit=%d' % (stage, code), flush=True)
        if code:
            return code
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
