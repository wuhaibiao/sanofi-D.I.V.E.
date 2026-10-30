"""Run a foreground evaluation with a wall deadline; stop its whole process group."""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout-s', type=float, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or not math.isfinite(args.timeout_s) or args.timeout_s <= 0:
        parser.error('a command and a finite positive timeout are required')
    args.out.mkdir(parents=True, exist_ok=True)
    state_path = args.out/'supervisor.json'
    if state_path.exists():
        parser.error('use a new output directory to retain earlier attempts')
    started = time.monotonic()
    interrupted = []
    def stop(signum, frame):
        interrupted.append(signum)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    state = dict(command=command, timeout_s=args.timeout_s, status='running')
    with (args.out/'command.log').open('w') as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        state['pid'] = process.pid
        state_path.write_text(json.dumps(state, indent=2)+'\n')
        reason = None
        while process.poll() is None:
            if interrupted or time.monotonic()-started >= args.timeout_s:
                reason = 'cancelled' if interrupted else 'timeout'
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                break
            try:
                process.wait(timeout=min(0.25, max(0.001, args.timeout_s-(time.monotonic()-started))))
            except subprocess.TimeoutExpired:
                pass
    # Reap any remaining children in the evaluation's group as well.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    code = 130 if reason == 'cancelled' else 124 if reason == 'timeout' else process.returncode
    state.update(status=reason or ('completed' if code == 0 else 'failed'),
                 returncode=code, child_returncode=process.returncode,
                 wall_s=round(time.monotonic()-started, 3), process_group_stopped=True)
    state_path.write_text(json.dumps(state, indent=2)+'\n')
    print(json.dumps(state, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
