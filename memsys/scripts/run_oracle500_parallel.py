#!/usr/bin/env python3
"""Continue frozen question cases concurrently without changing product inputs.

The serial controller is handed over only after its active read subprocess exits.
Its answers, failures, inputs and raw responses remain immutable. This module is
a separately sealed scheduling revision, outside the original frozen tools.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import run_oracle500 as full
import question_serial_eval as serial
from check_webchat_reliability import credentials
from memsys.config import AppConfig
from memsys.qdrant import Qdrant

PLAN = 'parallel-plan.json'


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    """Ignore only an unfinished final append while another worker is active."""
    if not Path(path).exists():
        return []
    lines = Path(path).read_bytes().splitlines(keepends=True)
    result = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines)-1 and not line.endswith(b'\n'):
                break
            raise
    return result


def processes():
    output = subprocess.run(['ps', '-ax', '-o', 'pid=,ppid=,stat=,command='],
        capture_output=True, text=True, check=True).stdout
    return {int(p[0]): dict(ppid=int(p[1]), state=p[2], command=p[3])
        for line in output.splitlines() if len(p := line.strip().split(None, 3)) == 4}


def protected_files(out, cases):
    return {str(p.relative_to(out)): full.digest(p) for c in cases
        for p in sorted((out / c['directory']).rglob('*'))
        if p.is_file() and p.name != 'execution.json'}


def validate_plan(out, *, preserved=True):
    frozen = full.validate(out)
    plan = read(out / PLAN)
    if full.digest(__file__) != plan['scheduler_sha256'] or full.digest(out / 'freeze.json') != plan['original_freeze_sha256']:
        raise ValueError('Parallel scheduler or original freeze changed')
    if full.digest(out / PLAN) != read(out / 'parallel-plan.sha256.json')['sha256']:
        raise ValueError('Parallel handoff plan changed')
    if preserved:
        for name, sha in plan['preserved_files'].items():
            if full.digest(out / name) != sha:
                raise ValueError('Previously completed artifact changed: ' + name)
    return frozen, plan


def adopt_finished_read(out, frozen, state, backend):
    """Validate completion of the already-running read, then clean only its DB."""
    cases = serial.check_serial_freeze(out, frozen)
    record = state['cases'][-1]
    case = cases[record['index']-1]
    directory = serial.check_case(out, case)
    answer = rows(directory / 'LongMemEval-answers.jsonl')
    if (len(answer) != 1 or answer[0]['question_id'] != case['question_id'] or
            answer[0].get('status') not in ('answered', 'error') or
            not (directory / 'LongMemEval-read-summary.json').exists() or
            (directory / 'provider-fatal.json').exists()):
        raise ValueError('Existing read did not finish cleanly; preserve it without replay')
    if any(o['phase'] == 'read' for o in record['outcomes']):
        raise ValueError('Read already accounted for; refuse duplicate adoption')
    owned = {n for n in serial.collections(backend) if n.startswith(frozen['prefix'] + '_')}
    if any(not n.startswith(case['prefix'] + '_') for n in owned):
        raise ValueError('Unexpected source collections at handoff')
    external = serial.collections(backend) - owned
    serial.cleanup_question(backend, case['prefix'], external, record)
    record['outcomes'].append(dict(phase='read', exit_code=0, adopted_existing_process=True))
    record['current'] = None
    full.save(directory / 'execution.json', record)
    return cases, record['index'], external


def takeover(args):
    out = args.out
    with (out / 'parallel-handoff.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (out / PLAN).exists():
            raise ValueError('Handoff already exists; do not repeat it')
        frozen = full.validate(out)
        launch = read(out / 'launcher.json')
        parent = launch['pid']
        deadline = time.monotonic() + 1800
        paused = False
        try:
            print('Waiting for the existing question read; no question will be replayed', flush=True)
            while time.monotonic() < deadline:
                ps = processes()
                controller = ps.get(parent)
                if not controller or str(out) not in controller['command']:
                    raise RuntimeError('Original controller absent; no automatic restart')
                state = read(out / 'execution.json')
                current = state.get('current') or {}
                if current.get('phase') == 'read':
                    directory = out / 'questions' / str(state['cases'][-1]['index']).zfill(4)
                    workers = [(pid, p) for pid, p in ps.items() if p['ppid'] == parent and
                        'run_oracle500.py phase' in p['command'] and '--phase read' in p['command'] and
                        str(directory) in p['command'] and not p['state'].startswith('Z')]
                    if len(workers) == 1:
                        child = workers[0][0]
                        os.kill(parent, signal.SIGSTOP)
                        paused = True
                        time.sleep(.15)
                        stable = read(out / 'execution.json')
                        if stable.get('current') == current and stable['cases'][-1]['index'] == state['cases'][-1]['index']:
                            state = stable
                            break
                        os.kill(parent, signal.SIGCONT)
                        paused = False
                time.sleep(1)
            else:
                raise TimeoutError('No safe read handoff window; serial run remains intact')
            print('Draining existing read for case ' + str(state['cases'][-1]['index']), flush=True)
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                worker = processes().get(child)
                if not worker or worker['state'].startswith('Z'):
                    break
                time.sleep(1)
            else:
                raise TimeoutError('Read is still active; resume original controller')
            full.validate(out)
            credentials(args.runtime)
            cfg = AppConfig.load(out / 'config.frozen.json')
            backend = Qdrant(cfg.storage)
            archive = out / 'serial-handoff'
            archive.mkdir(exist_ok=False)
            for name in ('execution.json', 'progress.json', 'launcher.json', 'metrics.json', 'report.md'):
                shutil.copy2(out / name, archive / name)
            shutil.copy2(directory / 'execution.json', archive / 'last-case-execution.json')
            cases, completed, external = adopt_finished_read(out, frozen, state, backend)
            for case in cases[:completed]:
                record = read(out / case['directory'] / 'execution.json')
                if not record.get('cleanup', {}).get('verified_empty') or len(rows(out / case['directory'] / 'LongMemEval-answers.jsonl')) != 1:
                    raise ValueError('Earlier case incomplete; do not replay it')
            for case in cases[completed:]:
                directory = out / case['directory']
                if (directory / 'execution.json').exists() or any(directory.glob('LongMemEval-*.jsonl')):
                    raise ValueError('Unexpected started case beyond handoff')
            state.update(mode='question_parallel', current=[], complete=False,
                scheduling=dict(workers=args.workers, read_workers=1, serial_completed=completed),
                external_collections=sorted(external))
            plan = dict(created_at=datetime.now(timezone.utc).isoformat(),
                scheduler_sha256=full.digest(__file__), original_freeze_sha256=full.digest(out / 'freeze.json'),
                workers=args.workers, read_workers=1, serial_completed=completed,
                old_controller_pid=parent, drained_read_pid=child, external_collections=sorted(external),
                preserved_files=protected_files(out, cases[:completed]),
                protocol='User-authorized scheduling handoff only. Existing outcomes, including failures, retained byte-for-byte; '
                    'only never-started cases admitted concurrently; independent per-case databases and prompts; '
                    'read/BGE concurrency one; judging only after all cases are answered and cleaned.')
            full.save(out / PLAN, plan)
            full.save(out / 'parallel-plan.sha256.json', dict(sha256=full.digest(out / PLAN)))
            # No read or model process is running, and its DB is already clean.
            os.kill(parent, signal.SIGKILL)
            paused = False
            full.save(out / 'execution.json', state)
            full.save(out / 'progress.json', dict(state='handoff_ready', total=len(cases),
                attempted=completed, answered=sum(rows(out / c['directory'] / 'LongMemEval-answers.jsonl')[0]['status']=='answered' for c in cases[:completed]),
                graded=0, workers=args.workers, read_workers=1))
            print(json.dumps(dict(handoff_ready=True, serial_completed=completed, workers=args.workers)), flush=True)
        finally:
            if paused:
                os.kill(parent, signal.SIGCONT)


class ParallelRun:
    def __init__(self, args, frozen, plan, *, backend=None, invoke=None, validate=None, publish=None):
        self.args, self.out, self.frozen, self.plan = args, args.out, frozen, plan
        self.cases = serial.check_serial_freeze(self.out, frozen)
        self.state = read(self.out / 'execution.json')
        self.backend = backend if backend is not None else Qdrant(AppConfig.load(self.out / 'config.frozen.json').storage)
        self.invoke = invoke or full.invoke
        self.validate = validate or (lambda: validate_plan(self.out, preserved=False))
        self.publish_hook = publish
        self.lock, self.report_lock = threading.RLock(), threading.Lock()
        self.read_slot = threading.Semaphore(1)
        self.stop = threading.Event()
        self.active = {}
        self.external = set(plan['external_collections'])

    def checkpoint(self):
        with self.lock:
            self.state['current'] = [dict(index=i, **v) for i, v in sorted(self.active.items())]
            self.state['elapsed_s'] = (datetime.now(timezone.utc)-datetime.fromisoformat(self.state['started_at'])).total_seconds()
            self.state['elapsed_basis'] = 'wall_including_sleep'
            full.save(self.out / 'execution.json', self.state)

    def publish(self):
        with self.report_lock:
            self.checkpoint()
            if self.publish_hook:
                self.publish_hook()
                return
            result = full.report(self.args)
            result['scheduling'] = {k:self.plan[k] for k in ('workers', 'read_workers', 'serial_completed', 'protocol')}
            full.save(self.out / 'metrics.json', result)
            with self.lock:
                active = [dict(index=i, **v) for i,v in sorted(self.active.items())]
            full.save(self.out / 'progress.json', dict(state='complete' if self.state['complete'] else
                'stopped' if self.state.get('error') else 'running', total=len(self.cases),
                answered=result['answered'], attempted=result['attempted'], graded=result['graded'],
                workers=self.plan['workers'], read_workers=1, active=active,
                elapsed_s=self.state['elapsed_s'], error=self.state.get('error'), updated_at=datetime.now(timezone.utc).isoformat()))
            path = self.out / 'report.md'
            text = path.read_text()
            note = (f"\n执行调度：前 {self.plan['serial_completed']} 题完成后，按用户要求接管为 "
                f"{self.plan['workers']} 个题目 worker；读取/BGE 并发为1。已有回答、失败、模型、产品代码与冻结输入均保留。"
                "原 freeze.json 描述初始串行流程，本次调度修订及保留文件哈希见 parallel-plan.json。\n")
            path.write_text(text.replace('\n', '\n'+note, 1))

    def phase(self, case, record, name):
        self.validate()
        directory = serial.check_case(self.out, case)
        with self.lock:
            record['current'] = 'read' if name == 'audit' else name
            self.active[case['index']] = dict(question_id=case['question_id'], phase=name)
            full.save(directory / 'execution.json', record)
            self.checkpoint()
        code = self.invoke(directory, name, self.args.runtime)
        with self.lock:
            record['outcomes'].append(dict(phase=name, exit_code=code))
            full.save(directory / 'execution.json', record)
        if code:
            raise RuntimeError(f"Case {case['index']} {name} failed; no retry")

    def question(self, case):
        directory = serial.check_case(self.out, case)
        if (directory / 'execution.json').exists() or any(directory.glob('LongMemEval-*.jsonl')):
            raise ValueError('Refuse to repeat started question')
        record = dict(question_id=case['question_id'], index=case['index'], prefix=case['prefix'], outcomes=[])
        with self.lock:
            self.state['cases'].append(record)
            self.state['cases'].sort(key=lambda r:r['index'])
        try:
            self.phase(case, record, 'write')
            with self.read_slot:
                self.phase(case, record, 'audit')
                self.phase(case, record, 'read')
        except BaseException:
            self.stop.set()
            raise
        finally:
            # Only this prefix is deleted. Other worker collections are expected.
            try:
                serial.cleanup_question(self.backend, case['prefix'], self.external, record)
            except BaseException:
                self.stop.set()
                raise
            finally:
                with self.lock:
                    record['current'] = None
                    self.active.pop(case['index'], None)
                    full.save(directory / 'execution.json', record)
                    self.checkpoint()
            self.publish()

    def judge(self, case):
        record = next(r for r in self.state['cases'] if r['index']==case['index'])
        try:
            self.phase(case, record, 'judge')
        except BaseException:
            self.stop.set()
            raise
        finally:
            with self.lock:
                record['current'] = None
                self.active.pop(case['index'], None)
                full.save(self.out / case['directory'] / 'execution.json', record)
            self.publish()

    def pool(self, cases, operation):
        todo = iter(cases)
        failures = []
        with ThreadPoolExecutor(max_workers=self.plan['workers']) as executor:
            futures = {}
            def submit():
                if self.stop.is_set():
                    return
                case = next(todo, None)
                if case is not None:
                    futures[executor.submit(operation, case)] = case
            for _ in range(self.plan['workers']):
                submit()
            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    case = futures.pop(future)
                    try:
                        future.result()
                    except BaseException as exc:
                        failures.append((case['index'], exc))
                        self.stop.set()
                if not self.stop.is_set():
                    for _ in done:
                        submit()
        if failures:
            raise RuntimeError('Concurrent case failed: ' + str(failures[0][0])) from failures[0][1]

    def run(self):
        try:
            self.validate()
            owned = [n for n in serial.collections(self.backend) if n.startswith(self.frozen['prefix']+'_')]
            if owned:
                raise ValueError('Source DBs remain at start; no automatic replay')
            self.pool(self.cases[self.plan['serial_completed']:], self.question)
            if len(self.state['cases']) != len(self.cases) or any(not r.get('cleanup',{}).get('verified_empty') for r in self.state['cases']):
                raise ValueError('Judging requires all question cleanups')
            if any(n.startswith(self.frozen['prefix']+'_') for n in serial.collections(self.backend)):
                raise ValueError('Source collections remain before judging')
            if self.publish_hook is None:
                validate_plan(self.out)
            self.pool(self.cases, self.judge)
            if self.publish_hook is None:
                validate_plan(self.out)
            self.state['complete'] = True
        except BaseException as exc:
            self.state['complete'] = False
            self.state['error'] = dict(type=type(exc).__name__, message=str(exc))
            raise
        finally:
            after = serial.collections(self.backend)
            self.state['cleanup'] = dict(remaining_owned=sorted(n for n in after if n.startswith(self.frozen['prefix']+'_')),
                unrelated_collections_preserved=self.external <= after)
            if self.state['cleanup']['remaining_owned'] or not self.state['cleanup']['unrelated_collections_preserved']:
                self.state['complete'] = False
                self.state.setdefault('error', dict(type='RuntimeError', message='Final isolation check failed'))
            self.publish()


def run(args):
    with (args.out / 'parallel-run.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        frozen, plan = validate_plan(args.out)
        if (args.out / 'parallel-started.json').exists():
            raise ValueError('Parallel run already started; no automatic restart')
        credentials(args.runtime)
        # Frozen report inputs may be appended by another case process.
        full.rows = rows
        original_read = full.read
        def stable_read(path):
            for attempt in range(5):
                try:
                    return original_read(path)
                except json.JSONDecodeError:
                    if attempt == 4:
                        raise
                    time.sleep(.03)
        full.read = stable_read
        full.save(args.out / 'parallel-started.json', dict(pid=os.getpid(), started_at=datetime.now(timezone.utc).isoformat()))
        ParallelRun(args, frozen, plan).run()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('takeover', 'run'))
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=2, choices=(2,3,4))
    parser.add_argument('--runtime', type=Path, default=Path.home()/'Library/Application Support/memsys/runtime.json')
    args = parser.parse_args()
    args.out = args.out.resolve()
    os.umask(0o077)
    globals()[args.action](args)


if __name__ == '__main__':
    main()
