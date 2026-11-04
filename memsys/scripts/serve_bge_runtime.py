#!/usr/bin/env python3
"""Configure the frozen BGE backend without changing its model or package code."""
import argparse
from datetime import datetime, timezone
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import threading


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--state-dir', type=Path, required=True)
    result.add_argument('--host', default='127.0.0.1')
    result.add_argument('--port', type=int, default=11434)
    result.add_argument('--threads', type=int, default=4)
    result.add_argument('--batch-size', type=int, default=2)
    result.add_argument('--check', action='store_true', help='Load and check authored inputs, then exit.')
    return result


class DrainingServer(ThreadingHTTPServer):
    # Closing the listener must wait for accepted requests to finish.
    daemon_threads = False
    block_on_close = True


def make_server(address, backend, token, metadata):
    from memsys.embedding_server import Handler

    class RuntimeHandler(Handler):
        def do_GET(self):
            if self.path != '/healthz':
                return super().do_GET()
            ready = self.server.backend.files_ready
            self.reply(200 if ready else 503, dict(metadata,
                status='ready' if ready else 'weights_missing',
                model_loaded=self.server.backend.model is not None,
                model_files_ready=ready,
                batch_size=self.server.backend.batch_size,
                threads=self.server.backend.torch.get_num_threads()))

    server = DrainingServer(address, RuntimeHandler)
    server.backend, server.token = backend, token
    return server


def build_backend(args):
    if not 1 <= args.threads <= 256 or not 1 <= args.batch_size <= 256:
        raise ValueError('threads and batch-size must be between 1 and 256')
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
    server = make_server((args.host, args.port), backend, token, metadata)

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
