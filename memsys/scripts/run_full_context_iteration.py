"""Run a frozen full-history diagnostic, separately from memory-system scores."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    run = out / 'current'
    state = json.loads((out / 'freeze.json').read_text())
    env = dict(os.environ, PYTHONPATH=str(out / 'frozen-src'))
    state.update(reingest=False, evaluation_mode='full_context_diagnostic',
                 diagnostic_runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                 limitations='Entire oracle history is directly supplied. No memory extraction, retrieval or source/support guards. This is a model diagnostic, not memory-system accuracy.')

    def save():
        (out / 'freeze.json').write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')

    def check():
        actual = json.loads(subprocess.check_output([sys.executable, '-c',
            'import json;from memsys.bench.provenance import source_manifest,prompt_fingerprint;print(json.dumps([source_manifest(),prompt_fingerprint()]))'],
            env=env, text=True))
        assert actual[0]['source_sha256'] == state['source']['source_sha256'] and actual[1] == state['prompts']
        for filename, key in [('sample100.json', 'sample_sha256'), ('config.frozen.json', 'config_sha256')]:
            assert hashlib.sha256((out / filename).read_bytes()).hexdigest() == state[key]

    save()
    check()
    base = [sys.executable, '-u', '-m', 'memsys', 'bench']
    commands = [
        ('run', base + ['run', '--mode', 'full-context', '--config', str(out / 'config.frozen.json'),
                       '--data', str(out / 'sample100.json'), '--out', str(run), '--workers', str(state['workers'])]),
        ('judge', base + ['judge', '--config', str(out / 'config.frozen.json'), '--data', str(out / 'sample100.json'),
                         '--hyp', str(run / 'hypotheses.jsonl'), '--workers', str(state['workers'])]),
        ('report', base + ['report', '--data', str(out / 'sample100.json'), '--run', str(run)]),
    ]
    expected = {r['question_id'] for r in json.loads((out / 'sample100.json').read_text())}
    for stage, command in commands:
        if any(s['stage'] == stage and s['returncode'] == 0 for s in state['stages']):
            continue
        for _ in range(3):
            if stage == 'judge':
                assert {json.loads(l)['question_id'] for l in (run / 'hypotheses.jsonl').read_text().splitlines()} == expected
            attempt = 1 + sum(s['stage'] == stage for s in state['stages'])
            log = out / ('%s-%02d.log' % (stage, attempt))
            print('Start full-context diagnostic', stage, attempt, flush=True)
            started = time.monotonic()
            with log.open('w') as stream:
                code = subprocess.run(command, env=env, stdout=stream, stderr=subprocess.STDOUT).returncode
            check()
            if stage == 'run' and code == 0:
                assert {json.loads(l)['question_id'] for l in (run / 'hypotheses.jsonl').read_text().splitlines()} == expected
            state['stages'].append(dict(stage=stage, attempt=attempt, returncode=code,
                wall_s=round(time.monotonic() - started, 3), log=log.name, identity_unchanged=True))
            save()
            if code == 0:
                break
            if 'HTTP 402' in log.read_text() or 'Insufficient account funds' in log.read_text():
                return code
            time.sleep(30)
        else:
            return code
    print('Completed 100 full-history diagnostic answers and labels.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
