"""Native runtime settings and HTTP compatibility, without model inference."""
import importlib.util
import errno
import json
import socket
from pathlib import Path
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location('bge_runtime_launcher',
    Path(__file__).resolve().parents[1] / 'scripts/serve_bge_runtime.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def wait_for(check, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if check():
            return
        time.sleep(.01)
    raise AssertionError('Expected concurrent state did not arrive')


class InferenceQueueTests(unittest.TestCase):
    def backend(self, function):
        value = Mock()
        value.torch.get_num_threads.return_value = 8
        value.embed.side_effect = function
        return value

    def test_distinct_requests_use_one_persistent_inference_thread(self):
        ids = []
        def embed(texts):
            ids.append(threading.get_ident())
            return [[float(len(texts[0]))]], 1
        backend = self.backend(embed)
        broker = launcher.InferenceQueue(backend)
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda i: broker.embed([str(i)]), range(8)))
            self.assertEqual(len(results), 8)
            self.assertEqual(len(set(ids)), 1)
            self.assertEqual(broker.info()['counters']['completed_jobs'], 8)
        finally:
            broker.close()

    def test_inflight_and_completed_transport_retries_do_not_recompute(self):
        entered, release = threading.Event(), threading.Event()
        def embed(texts):
            entered.set()
            release.wait(3)
            return [[1.]], 1
        backend = self.backend(embed)
        broker = launcher.InferenceQueue(backend)
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(broker.embed, ['Same authored vector input.']) for _ in range(4)]
                self.assertTrue(entered.wait(2))
                wait_for(lambda: broker.info()['counters'].get('coalesced_requests') == 3)
                release.set()
                self.assertTrue(all(f.result(timeout=2) == ([[1.]], 1) for f in futures))
            self.assertEqual(broker.embed(['Same authored vector input.']), ([[1.]], 1))
            backend.embed.assert_called_once()
            self.assertEqual(broker.info()['counters']['replayed_results'], 1)
        finally:
            release.set()
            broker.close()

    def test_disconnected_queued_request_is_not_computed(self):
        entered, release, departed = threading.Event(), threading.Event(), threading.Event()
        def embed(texts):
            entered.set()
            release.wait(3)
            return [[1.]], 1
        backend = self.backend(embed)
        broker = launcher.InferenceQueue(backend)
        def queued():
            broker.local.disconnected = departed.is_set
            return broker.embed(['This queued client will depart.'])
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                active = pool.submit(broker.embed, ['Active work.'])
                self.assertTrue(entered.wait(2))
                abandoned = pool.submit(queued)
                wait_for(lambda: broker.info()['queued_jobs'] == 1)
                departed.set()
                with self.assertRaises(launcher.ClientDisconnected):
                    abandoned.result(timeout=2)
                release.set()
                active.result(timeout=2)
            backend.embed.assert_called_once_with(['Active work.'])
            self.assertEqual(broker.info()['counters']['cancelled_queued_jobs'], 1)
        finally:
            release.set()
            broker.close()

    def test_backend_timeout_is_propagated_instead_of_becoming_an_infinite_wait(self):
        def fail(texts):
            raise TimeoutError('authored backend failure')
        broker = launcher.InferenceQueue(self.backend(fail))
        try:
            with self.assertRaisesRegex(TimeoutError, 'authored backend failure'):
                broker.embed(['Failure fixture.'])
            self.assertEqual(broker.info()['counters']['failed_jobs'], 1)
            self.assertEqual(broker.info()['queued_jobs'], 0)
        finally:
            broker.close()

    def test_completion_at_the_poll_timeout_boundary_returns_the_real_result(self):
        class PollRace(Future):
            injected = False
            def result(self, timeout=None):
                value = super().result(timeout)
                if not self.injected:
                    self.injected = True
                    raise FutureTimeout()
                return value
        broker = launcher.InferenceQueue(self.backend(lambda texts: ([[1.]], 1)))
        try:
            with patch.object(launcher, 'Future', PollRace):
                self.assertEqual(broker.embed(['Completion boundary fixture.']), ([[1.]], 1))
        finally:
            broker.close()

    def test_admission_is_bounded_and_short_queries_do_not_starve_bulk(self):
        entered, release = threading.Event(), threading.Event()
        order = []
        def embed(texts):
            order.append(texts[0])
            if texts[0] == 'first':
                entered.set()
                release.wait(3)
            return [[1.] for _ in texts], len(texts)
        broker = launcher.InferenceQueue(self.backend(embed), max_pending=5)
        try:
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [pool.submit(broker.embed, ['first', 'bulk'])]
                self.assertTrue(entered.wait(2))
                for text in ['q1', 'q2', 'q3']:
                    futures.append(pool.submit(broker.embed, [text]))
                futures.append(pool.submit(broker.embed, ['second', 'bulk']))
                wait_for(lambda: broker.info()['queued_jobs'] == 4)
                with self.assertRaisesRegex(RuntimeError, 'queue is full'):
                    broker.embed(['excess'])
                release.set()
                for future in futures:
                    future.result(timeout=2)
            self.assertEqual(order, ['first', 'q1', 'q2', 'second', 'q3'])
        finally:
            release.set()
            broker.close()


class RuntimeLauncherTests(unittest.TestCase):
    def setUp(self):
        backend = Mock(files_ready=True, model=object(), batch_size=8)
        backend.torch.get_num_threads.return_value = 8
        backend.embed.return_value = ([[1.] + [0.] * 1023, [0., 1.] + [0.] * 1022], 12)
        self.backend = backend
        self.server = launcher.make_server(('127.0.0.1', 0), backend, 'authored-token',
            dict(model='bge-m3', dtype='float32', model_revision='frozen-revision'))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = 'http://127.0.0.1:%d' % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, body=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        req = urllib.request.Request(self.url + path,
            json.dumps(body).encode() if body is not None else None, headers)
        try:
            with urllib.request.urlopen(req, timeout=3) as result:
                return result.status, json.load(result)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_health_reports_actual_runtime_without_credentials(self):
        code, data = self.request('/healthz')
        self.assertEqual(code, 200)
        self.assertEqual((data['threads'], data['batch_size']), (8, 8))
        self.assertEqual(data['model_revision'], 'frozen-revision')
        self.assertNotIn('authored-token', json.dumps(data))
        self.backend.embed.assert_not_called()

    def test_frozen_http_validation_authentication_and_vector_order(self):
        body = dict(model='bge-m3', input=['Cabinet A is blue.', 'Cabinet B is green.'])
        self.assertEqual(self.request('/v1/embeddings', body)[0], 401)
        self.backend.embed.assert_not_called()
        code, data = self.request('/v1/embeddings', body, 'authored-token')
        self.assertEqual(code, 200)
        self.assertEqual([r['index'] for r in data['data']], [0, 1])
        self.assertEqual(data['data'][0]['embedding'][0], 1.)
        self.assertEqual(data['data'][1]['embedding'][1], 1.)
        self.backend.embed.assert_called_once_with(body['input'])

    def test_http_retry_after_disconnect_reuses_running_inference(self):
        entered, release = threading.Event(), threading.Event()
        value = self.backend.embed.return_value
        def delayed(texts):
            entered.set()
            release.wait(3)
            return value
        self.backend.embed.side_effect = delayed
        body = dict(model='bge-m3', input=['Blue calibration item.', 'Green spare item.'])
        payload = json.dumps(body).encode()
        connection = socket.create_connection(self.server.server_address, timeout=2)
        connection.sendall((
            'POST /v1/embeddings HTTP/1.0\r\n'
            'Content-Type: application/json\r\n'
            'Authorization: Bearer authored-token\r\n'
            f'Content-Length: {len(payload)}\r\n\r\n').encode() + payload)
        try:
            self.assertTrue(entered.wait(2))
            connection.close()
            with ThreadPoolExecutor(max_workers=1) as pool:
                retry = pool.submit(self.request, '/v1/embeddings', body, 'authored-token')
                wait_for(lambda: self.server.backend.info()['counters'].get('coalesced_requests') == 1)
                release.set()
                self.assertEqual(retry.result(timeout=2)[0], 200)
            self.backend.embed.assert_called_once_with(body['input'])
        finally:
            connection.close()
            release.set()

    def test_invalid_runtime_settings_fail_before_model_loading(self):
        for flag, value in [('--threads', '0'), ('--batch-size', '0'), ('--batch-size', '257')]:
            args = launcher.parser().parse_args(['--state-dir', '/nonexistent', flag, value])
            with self.assertRaisesRegex(ValueError, 'between 1 and 256'):
                launcher.build_backend(args)

    def test_preloaded_backend_is_reused_when_waiting_for_the_old_listener(self):
        args = launcher.parser().parse_args(['--state-dir', '/authored', '--wait-for-port', '5'])
        server = object()
        with patch.object(launcher, 'make_server', side_effect=[OSError(errno.EADDRINUSE, 'busy'), server]) as make, \
                patch.object(launcher.time, 'sleep'):
            self.assertIs(launcher.bind_server(args, self.backend, 'token', {}), server)
        self.assertEqual(make.call_count, 2)
        self.assertIs(make.call_args.args[1], self.backend)

    def test_listener_permission_errors_are_not_retried(self):
        args = launcher.parser().parse_args(['--state-dir', '/authored', '--wait-for-port', '5'])
        with patch.object(launcher, 'make_server', side_effect=PermissionError(errno.EACCES, 'denied')) as make:
            with self.assertRaises(PermissionError):
                launcher.bind_server(args, self.backend, 'token', {})
        self.assertEqual(make.call_count, 1)


if __name__ == '__main__':
    unittest.main()
