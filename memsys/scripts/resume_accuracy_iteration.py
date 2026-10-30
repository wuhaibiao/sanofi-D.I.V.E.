"""Resume an immutable iteration after its current process exits.

Successful stages and answers are retained. Failed requests stay in their audit
logs. Account errors stop the retry; other interruptions have a bounded retry.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def account_error(error):
    """The gateway also reports a transient unavailable upstream as HTTP 401."""
    return ('HTTP 402' in error or 'Insufficient account funds' in error
            or ('HTTP 401' in error and 'Endpoint is unavailable' not in error))


def stage_done(directory, name):
    try:
        stages = json.loads((directory / 'freeze.json').read_text())['stages']
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return any(stage['stage'] == name and stage['returncode'] == 0 for stage in stages)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--after-pid', type=int)
    parser.add_argument('--after-run', type=Path, action='append', default=[])
    parser.add_argument('--seed-run', type=Path)
    parser.add_argument('--paired-run', type=Path)
    args = parser.parse_args()
    if args.after_pid is not None:
        while True:
            try:
                os.kill(args.after_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(15)
    while not all(stage_done(directory, 'run') for directory in args.after_run):
        time.sleep(15)
    for attempt in range(3):
        stages = json.loads((args.out / 'freeze.json').read_text())['stages']
        if all(any(s['stage'] == name and s['returncode'] == 0 for s in stages)
               for name in ('run', 'judge', 'report')):
            break
        print('Resume attempt', attempt + 1, str(args.out), flush=True)
        if args.seed_run:
            command = [sys.executable, '-u', 'scripts/run_cached_read_iteration.py',
                       '--out', str(args.out), '--seed-run', str(args.seed_run)]
        else:
            command = [sys.executable, '-u', 'scripts/run_accuracy_iteration.py',
                       '--out', str(args.out), '--stage', 'all']
        result = subprocess.run(command)
        if result.returncode == 0:
            break
        path = args.out / 'current/errors.jsonl'
        errors = [json.loads(line).get('error', '') for line in path.read_text().splitlines()[-6:]] if path.exists() else []
        if any(account_error(error) for error in errors):
            return result.returncode
        time.sleep(30)
    else:
        return 1
    command = [sys.executable, 'scripts/analyze_accuracy_iteration.py', '--out', str(args.out)]
    if args.paired_run:
        parent = args.paired_run.parent
        if (parent / 'freeze.json').exists():
            while not stage_done(parent, 'judge'):
                time.sleep(15)
        command += ['--paired-run', str(args.paired_run)]
    return subprocess.run(command).returncode


if __name__ == '__main__':
    raise SystemExit(main())
