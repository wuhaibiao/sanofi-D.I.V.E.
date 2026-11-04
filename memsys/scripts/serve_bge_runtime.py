#!/usr/bin/env python3
"""Configure the frozen BGE backend without changing its model or package code."""
import argparse
from collections import Counter, OrderedDict, deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from datetime import datetime, timezone
import errno
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import select
import socket
import threading
import time


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--state-dir', type=Path, required=True)
    result.add_argument('--host', default='127.0.0.1')
    result.add_argument('--port', type=int, default=11434)
    result.add_argument('--threads', type=int, default=4)
    result.add_argument('--batch-size', type=int, default=2)
    result.add_argument('--max-pending', type=int, default=16)
    result.add_argument('--metrics-path', type=Path)
    result.add_argument('--wait-for-port', type=float, default=0.)
    result.add_argument('--check', action='store_true', help='Load and check authored inputs, then exit.')
    return result


class DrainingServer(ThreadingHTTPServer):
    # Closing the listener must wait for accepted requests to finish.
    daemon_threads = False
    block_on_close = True

    def server_close(self):
        try:
            super().server_close()
        finally:
            if isinstance(getattr(self, 'backend', None), InferenceQueue):
                self.backend.close()


class ClientDisconnected(RuntimeError):
    pass


def disconnected(connection):
    """Detect EOF without consuming another HTTP request or waiting for input."""
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        return bool(readable) and connection.recv(1, socket.MSG_PEEK) == b''
    except (OSError, ValueError):
        return True


class InferenceQueue:
    """One persistent inference thread, bounded admission and idempotent retries.

    The short replay cache contains only vectors and an input digest, never text
    or conversation state. All requests use the same immutable embedding model.
    """
    def __init__(self, backend, max_pending=16, replay_s=30., max_replay=32,
                 metrics_path=None):
        self.backend, self.max_pending = backend, max_pending
        self.replay_s, self.max_replay = replay_s, max_replay
        self.metrics_path = Path(metrics_path) if metrics_path else None
        self.condition = threading.Condition()
        self.jobs, self.replay = {}, OrderedDict()
        self.queues = (deque(), deque())
        self.stats, self.local = Counter(), threading.local()
        self.closed, self.active, self.query_streak = False, None, 0
        self.startup_error = None
        self.ready = threading.Event()
        self.worker = threading.Thread(target=self._work, name='bge-inference', daemon=True)
        self.worker.start()
        if not self.ready.wait(15):
            raise RuntimeError('Inference worker did not initialize')
        if getattr(self, 'startup_error', None):
            raise RuntimeError('Inference worker initialization failed') from self.startup_error

    def __getattr__(self, name):
        if name in ('files_ready', 'model', 'batch_size', 'torch', 'device'):
            return getattr(self.backend, name)
        raise AttributeError(name)

    def embed(self, texts):
        key = hashlib.sha256(json.dumps(list(texts), ensure_ascii=False,
            separators=(',', ':')).encode()).hexdigest()
        with self.condition:
            self.stats['requests'] += 1
            now = time.monotonic()
            for old in [k for k, (until, _) in self.replay.items() if until <= now]:
                del self.replay[old]
            if key in self.replay:
                self.stats['replayed_results'] += 1
                return self.replay[key][1]
            if self.closed:
                raise RuntimeError('Inference queue is shutting down')
            job = self.jobs.get(key)
            if job is None:
                if len(self.jobs) >= self.max_pending:
                    self.stats['rejected_full'] += 1
                    raise RuntimeError('Inference queue is full; retry later')
                job = dict(key=key, texts=list(texts), future=Future(), queued_at=now,
                    running=False, waiters=0, characters=sum(map(len, texts)), count=len(texts))
                self.jobs[key] = job
                # Short single-input requests can pass bulk writes; two in a row
                # is the limit so queued bulk work cannot starve.
                priority = 0 if len(texts) == 1 and len(texts[0]) <= 4096 else 1
                self.queues[priority].append(job)
                self.stats['submitted_jobs'] += 1
            else:
                self.stats['coalesced_requests'] += 1
            job['waiters'] += 1
            self.condition.notify()
        try:
            check = getattr(self.local, 'disconnected', None)
            while True:
                if check and check():
                    raise ClientDisconnected('Embedding client disconnected')
                try:
                    return job['future'].result(timeout=.2)
                except FutureTimeout:
                    if job['future'].done():
                        return job['future'].result()
                    continue
        finally:
            with self.condition:
                job['waiters'] -= 1
                if not job['waiters'] and not job['running'] and not job['future'].done():
                    job['future'].cancel()
                    job['texts'] = []
                    if self.jobs.get(key) is job:
                        del self.jobs[key]
                    for queued in self.queues:
                        try:
                            queued.remove(job)
                        except ValueError:
                            pass
                    self.stats['cancelled_queued_jobs'] += 1

    def _work(self):
        try:
            self.worker_threads = self.backend.torch.get_num_threads()
            self.backend.torch.set_num_threads(self.worker_threads)
        except BaseException as exc:
            self.startup_error = exc
            self.ready.set()
            return
        self.ready.set()
        while True:
            with self.condition:
                while not any(self.queues):
                    if self.closed:
                        return
                    self.condition.wait()
                priority = 0 if self.queues[0] and (not self.queues[1] or self.query_streak < 2) else 1
                job = self.queues[priority].popleft()
                if job['future'].cancelled():
                    continue
                self.query_streak = self.query_streak + 1 if priority == 0 else 0
                job['running'], self.active = True, job
                began = time.monotonic()
            cpu = time.process_time()
            error = None
            try:
                value = self.backend.embed(job['texts'])
            except BaseException as exc:
                error = exc
            ended = time.monotonic()
            cpu = time.process_time() - cpu
            job['texts'] = []
            with self.condition:
                if self.jobs.get(job['key']) is job:
                    del self.jobs[job['key']]
                if error is None:
                    self.replay[job['key']] = (ended + self.replay_s, value)
                    while len(self.replay) > self.max_replay:
                        self.replay.popitem(last=False)
                    job['future'].set_result(value)
                    self.stats['completed_jobs'] += 1
                else:
                    job['future'].set_exception(error)
                    self.stats['failed_jobs'] += 1
                self.active = None
            self._record(dict(at=datetime.now(timezone.utc).isoformat(),
                input_digest=job['key'], texts=job['count'], characters=job['characters'],
                queue_s=began-job['queued_at'], inference_s=ended-began, process_cpu_s=cpu,
                prompt_tokens=value[1] if error is None else None,
                error_type=type(error).__name__ if error else None))

    def _record(self, value):
        if self.metrics_path is None:
            return
        try:
            self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.metrics_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a') as stream:
                stream.write(json.dumps(value) + '\n')
        except OSError:
            with self.condition:
                self.stats['metrics_write_errors'] += 1

    def info(self):
        with self.condition:
            return dict(inference_dispatch='single_persistent_thread',
                inference_thread_id=self.worker.ident, inference_threads=self.worker_threads,
                queued_jobs=sum(not job['running'] for job in self.jobs.values()),
                running_jobs=int(self.active is not None), max_pending=self.max_pending,
                replay_s=self.replay_s, replay_entries=len(self.replay), counters=dict(self.stats))

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.worker.join()


def make_server(address, backend, token, metadata, *, max_pending=16, metrics_path=None):
    from memsys.embedding_server import Handler

    class RuntimeHandler(Handler):
        def reply(self, code, value):
            try:
                return super().reply(code, value)
            except (BrokenPipeError, ConnectionResetError):
                # A departed client must not trigger a second response/write.
                with self.server.backend.condition:
                    self.server.backend.stats['write_disconnects'] += 1

        def do_POST(self):
            self.server.backend.local.disconnected = lambda: disconnected(self.connection)
            try:
                return super().do_POST()
            finally:
                del self.server.backend.local.disconnected

        def do_GET(self):
            if self.path != '/healthz':
                return super().do_GET()
            ready = self.server.backend.files_ready
            self.reply(200 if ready else 503, dict(metadata,
                status='ready' if ready else 'weights_missing',
                model_loaded=self.server.backend.model is not None,
                model_files_ready=ready,
                batch_size=self.server.backend.batch_size,
                threads=self.server.backend.worker_threads,
                inference=self.server.backend.info()))

    server = DrainingServer(address, RuntimeHandler)
    try:
        server.backend = InferenceQueue(backend, max_pending=max_pending, metrics_path=metrics_path)
    except BaseException:
        server.server_close()
        raise
    server.token = token
    return server


def bind_server(args, backend, token, metadata):
    deadline = time.monotonic() + args.wait_for_port
    while True:
        try:
            return make_server((args.host, args.port), backend, token, metadata,
                max_pending=args.max_pending,
                metrics_path=args.metrics_path or args.state_dir / 'logs/embedding-runtime-events.jsonl')
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE or time.monotonic() >= deadline:
                raise
            time.sleep(.2)


def build_backend(args):
    if not all(1 <= v <= 256 for v in (args.threads, args.batch_size, args.max_pending)):
        raise ValueError('threads, batch-size and max-pending must be between 1 and 256')
    if not 0 <= args.wait_for_port <= 600:
        raise ValueError('wait-for-port must be between 0 and 600 seconds')
    if args.host not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('this native runtime launcher binds to loopback only')
    runtime = json.loads((args.state_dir / 'runtime.json').read_text())
    os.environ['OMP_NUM_THREADS'] = str(args.threads)
    os.environ['MKL_NUM_THREADS'] = str(args.threads)
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    import torch
    from memsys import embedding_server
    torch.set_num_threads(args.threads)
    backend = embedding_server.BGEEmbedding(runtime['embedding_snapshot'],
        device=runtime.get('embedding_device', 'cpu'), batch_size=args.batch_size)
    backend._load()
    metadata = dict(pid=os.getpid(), started_at=datetime.now(timezone.utc).isoformat(),
        model='bge-m3', device=backend.device, dtype='float32',
        model_revision=Path(runtime['embedding_snapshot']).name,
        backend_sha256=hashlib.sha256(Path(embedding_server.__file__).read_bytes()).hexdigest(),
        runtime_launcher_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    return backend, runtime['tokens']['embedding'], metadata


def main(argv=None):
    args = parser().parse_args(argv)
    backend, token, metadata = build_backend(args)
    if args.check:
        import math
        vectors, tokens = backend.embed(['A blue calibration instrument is stored in cabinet A.',
            'Cabinet B contains three sealed spare instruments.'])
        if len(vectors) != 2 or any(len(v) != 1024 or
                not all(math.isfinite(x) for x in v) or
                abs(sum(x*x for x in v) - 1.) > 1e-4 for v in vectors):
            raise ValueError('authored semantic embedding check failed')
        print(json.dumps(dict(metadata, checked=True, dimensions=1024,
            threads=backend.torch.get_num_threads(), batch_size=backend.batch_size,
            texts=len(vectors), prompt_tokens=tokens)), flush=True)
        return
    print(json.dumps(dict(metadata, backend_ready=True,
        threads=backend.torch.get_num_threads(), batch_size=backend.batch_size)), flush=True)
    server = bind_server(args, backend, token, metadata)

    def shutdown(signum, frame):
        # shutdown() must be called outside serve_forever's main thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, shutdown)
    print(json.dumps(dict(metadata, listening=list(server.server_address),
        threads=backend.torch.get_num_threads(), batch_size=backend.batch_size)), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
