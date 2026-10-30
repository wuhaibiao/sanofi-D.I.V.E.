"""Question-local frozen inputs and write/read/cleanup orchestration.

This module never calls a model directly. Preparation only partitions already
frozen phase files; scoring still runs in separate processes after all answers.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from blind_eval_inputs import FILES, load_phase
from memsys.qdrant import Qdrant

ROOT = Path(__file__).resolve().parents[1]
MODE = 'question_serial'
MANIFEST = 'question-cases.json'
AUDITOR = ROOT / 'scripts/capture_relational_eval_storage.py'


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze_cases(out, frozen):
    """Mechanical partitioning in the trusted preparation phase, not selection."""
    out = Path(out)
    selection = read(out / 'selection.json')
    ids = [row['question_id'] for row in selection]
    if not ids or len(ids) != len(set(ids)) or any(row['dataset'] != 'LongMemEval' for row in selection):
        raise ValueError('Question-serial evaluation requires distinct LongMemEval IDs')
    inputs = {}
    for phase, filename in FILES.items():
        load_phase(out, frozen, phase, 'LongMemEval')
        rows = read(out / filename)
        if len(rows) != len(ids) or {row['question_id'] for row in rows} != set(ids):
            raise ValueError('Frozen question IDs disagree: ' + phase)
        inputs[phase] = {row['question_id']: row for row in rows}
        for meta in selection:
            row = inputs[phase][meta['question_id']]
            if any(row[key] != meta[key] for key in ('dataset', 'category', 'corpus_id')):
                raise ValueError('Question metadata disagrees: ' + phase)
    cases = []
    for index, meta in enumerate(selection, 1):
        relative = 'questions/' + str(index).zfill(4)
        case = out / relative
        case.mkdir(parents=True, exist_ok=False)
        save(case / 'selection.json', [meta])
        shutil.copy2(out / 'config.frozen.json', case / 'config.frozen.json')
        os.chmod(case / 'config.frozen.json', 0o600)
        hashes = {}
        for phase, filename in FILES.items():
            save(case / filename, [inputs[phase][meta['question_id']]])
            hashes[phase] = digest(case / filename)
        prefix = frozen['prefix'] + '_q' + str(index).zfill(4)
        child = dict(source=frozen['source'], config_sha256=frozen['config_sha256'],
            runner_sha256=frozen['runner_sha256'],
            blind_input_builder_sha256=frozen['blind_input_builder_sha256'],
            blind_input_sha256=hashes, prefix=prefix,
            namespace=frozen['namespace'] + '/question-' + str(index),
            execution_mode=MODE, question_id=meta['question_id'])
        save(case / 'freeze.json', child)
        names = ['freeze.json', 'selection.json', 'config.frozen.json', *FILES.values()]
        cases.append(dict(index=index, question_id=meta['question_id'], directory=relative,
            prefix=prefix, files={name: digest(case / name) for name in names}))
    save(out / MANIFEST, cases)
    frozen.update(execution_mode=MODE, question_cases_sha256=digest(out / MANIFEST),
        serial_executor_sha256=digest(__file__), storage_auditor_sha256=digest(AUDITOR))


def check_case(out, case):
    directory = Path(out) / case['directory']
    for name, expected in case['files'].items():
        if digest(directory / name) != expected:
            raise ValueError('Frozen question file changed: ' + str(directory / name))
    return directory


def check_serial_freeze(out, frozen):
    if frozen.get('execution_mode') != MODE:
        raise ValueError('Prepare a new question-serial run; preserve existing runs unchanged')
    for path, field in ((Path(__file__), 'serial_executor_sha256'),
                        (AUDITOR, 'storage_auditor_sha256'),
                        (Path(out) / MANIFEST, 'question_cases_sha256')):
        if digest(path) != frozen.get(field):
            raise ValueError('Frozen question-serial tool or plan changed: ' + str(path))
    cases = read(Path(out) / MANIFEST)
    if [c['question_id'] for c in cases] != [m['question_id'] for m in read(Path(out) / 'selection.json')]:
        raise ValueError('Question order changed')
    for index, case in enumerate(cases, 1):
        if case['directory'] != 'questions/' + str(index).zfill(4) or case['prefix'] != frozen['prefix'] + '_q' + str(index).zfill(4):
            raise ValueError('Unexpected question-local path or collection prefix')
        check_case(out, case)
    return cases


def collections(backend):
    return {row['name'] for row in backend.request('GET', '/collections')['collections']}


def cleanup_question(backend, prefix, before, record):
    cleanup = dict(deleted=[], verified_empty=False, unrelated_collections_preserved=False)
    record['cleanup'] = cleanup
    try:
        owned = {name for name in collections(backend) if name.startswith(prefix + '_')}
        for name in sorted(owned):
            backend.request('DELETE', '/collections/' + name)
            cleanup['deleted'].append(name)
        after = collections(backend)
        cleanup.update(remaining_owned=sorted(name for name in after if name.startswith(prefix + '_')),
            unrelated_collections_preserved=before <= after)
        cleanup['verified_empty'] = not cleanup['remaining_owned']
        if not cleanup['verified_empty'] or not cleanup['unrelated_collections_preserved']:
            raise RuntimeError('Question cleanup verification failed; do not start another question')
    except Exception as exc:
        cleanup['error'] = type(exc).__name__ + ': ' + str(exc)[:1000]
        raise


def invoke_phase(directory, phase, runtime):
    if phase == 'audit':
        command = [sys.executable, str(AUDITOR), '--out', str(directory), '--runtime', str(runtime)]
    else:
        command = [sys.executable, str(ROOT / 'scripts/evaluate_public_types.py'),
            '--out', str(directory), '--dataset', 'LongMemEval', '--phase', phase, '--runtime', str(runtime)]
    with (directory / ('LongMemEval-' + phase + '.log')).open('w') as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
        try:
            for line in process.stdout:
                log.write(line)
                log.flush()
                print(line, end='', flush=True)
            return process.wait()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        finally:
            process.stdout.close()


def aggregate_outputs(out, cases):
    """Rebuild compatibility views from original case artifacts, without scoring."""
    out = Path(out)
    directories = [out / case['directory'] for case in cases]
    names = sorted({p.name for directory in directories for p in directory.glob('LongMemEval-*.jsonl')})
    for name in names:
        with (out / name).open('wb') as target:
            for directory in directories:
                source = directory / name
                if source.exists():
                    data = source.read_bytes()
                    target.write(data)
                    if data and not data.endswith(b'\n'):
                        target.write(b'\n')
    for phase in ('write', 'read'):
        name = 'LongMemEval-' + phase + '-summary.json'
        summaries = [read(directory / name) for directory in directories if (directory / name).exists()]
        if summaries:
            save(out / name, dict(execution_mode=MODE,
                elapsed_s=sum(row['elapsed_s'] for row in summaries), by_question=summaries))
    traces = out / 'traces'
    for directory in directories:
        for source in (directory / 'traces').glob('*.json'):
            traces.mkdir(exist_ok=True)
            shutil.copy2(source, traces / source.name)
    audits = [read(directory / 'storage-audit.json') for directory in directories
              if (directory / 'storage-audit.json').exists()]
    if audits:
        totals = {key: sum(a[key] for a in audits) for key in
                  ('corpora', 'facts', 'entities', 'relations', 'orphan_relation_fact_ids')}
        for key in ('entity_types', 'relation_origins'):
            counts = Counter()
            for audit in audits:
                counts.update(audit[key])
            totals[key] = dict(counts)
        totals.update(phase='per_question_before_read', source_sha256=audits[0]['source_sha256'],
            by_corpus=[row for audit in audits for row in audit['by_corpus']],
            method='Sum of per-question read-only snapshots before each answer and cleanup; repeated histories counted per question.')
        save(out / 'storage-audit.json', totals)


def run_serial_evaluation(args, frozen, config, validate, *, backend=None, invoke=None):
    out = Path(args.out)
    cases = check_serial_freeze(out, frozen)
    marker = out / 'execution.json'
    if marker.exists() or any(out.glob('LongMemEval-*.jsonl')) or any(
            (out / c['directory'] / 'execution.json').exists() or
            any((out / c['directory']).glob('LongMemEval-*.jsonl')) for c in cases):
        raise ValueError('Evaluation already started; do not rerun or replace any answer')
    backend = backend if backend is not None else Qdrant(config.storage)
    invoke = invoke or invoke_phase
    before = collections(backend)
    if any(name.startswith(frozen['prefix'] + '_') for name in before):
        raise ValueError('Evaluation prefix is not empty; preserve existing collections')
    state = dict(started_at=datetime.now(timezone.utc).isoformat(), mode=MODE, cases=[], current=None, complete=False)
    save(marker, state)
    begin = time.monotonic()

    def phase(case, record, name):
        validate()
        directory = check_case(out, case)
        record['current'] = name
        state['current'] = dict(question_id=case['question_id'], phase=name)
        save(directory / 'execution.json', record)
        save(marker, state)
        code = invoke(directory, name, args.runtime)
        record['outcomes'].append(dict(phase=name, exit_code=code))
        save(directory / 'execution.json', record)
        if code:
            raise RuntimeError('Question phase failed without automatic retry: ' + name)

    try:
        for case in cases:
            directory = check_case(out, case)
            baseline = collections(backend)
            if any(name.startswith(frozen['prefix'] + '_') for name in baseline):
                raise RuntimeError('Previous question still has collections; stop before writing')
            record = dict(question_id=case['question_id'], index=case['index'], prefix=case['prefix'], outcomes=[])
            state['cases'].append(record)
            print('CASE ' + str(case['index']) + '/' + str(len(cases)) + ' ' + case['question_id'], flush=True)
            try:
                phase(case, record, 'write')
                # The auditor requires the read marker and never calls a model.
                record['current'] = 'read'
                save(directory / 'execution.json', record)
                validate()
                check_case(out, case)
                code = invoke(directory, 'audit', args.runtime)
                record['outcomes'].append(dict(phase='audit', exit_code=code))
                if code:
                    raise RuntimeError('Question storage audit failed without retry')
                phase(case, record, 'read')
            finally:
                try:
                    cleanup_question(backend, case['prefix'], baseline, record)
                finally:
                    record['current'] = None
                    save(directory / 'execution.json', record)
                    save(marker, state)
        # No source collection survives into the scoring stage.
        for case, record in zip(cases, state['cases']):
            phase(case, record, 'judge')
            record['current'] = None
            save(out / case['directory'] / 'execution.json', record)
        state['current'] = None
        state['complete'] = True
    except BaseException as exc:
        state['complete'] = False
        state['error'] = dict(type=type(exc).__name__, message=str(exc)[:1000])
        raise
    finally:
        state['elapsed_s'] = round(time.monotonic() - begin, 3)
        try:
            after = collections(backend)
            state['cleanup'] = dict(deleted=[name for r in state['cases'] for name in r.get('cleanup', {}).get('deleted', [])],
                remaining_owned=sorted(name for name in after if name.startswith(frozen['prefix'] + '_')),
                unrelated_collections_preserved=before <= after)
            aggregate_outputs(out, cases)
            if state['cleanup']['remaining_owned'] or not state['cleanup']['unrelated_collections_preserved']:
                state['complete'] = False
                if 'error' not in state:
                    state['error'] = dict(type='RuntimeError', message='Final collection isolation verification failed')
                    raise RuntimeError(state['error']['message'])
        finally:
            save(marker, state)
    return state
