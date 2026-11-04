"""Native runtime settings and HTTP compatibility, without model inference."""
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location('bge_runtime_launcher',
    Path(__file__).resolve().parents[1] / 'scripts/serve_bge_runtime.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


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

    def test_invalid_runtime_settings_fail_before_model_loading(self):
        for flag, value in [('--threads', '0'), ('--batch-size', '0'), ('--batch-size', '257')]:
            args = launcher.parser().parse_args(['--state-dir', '/nonexistent', flag, value])
            with self.assertRaisesRegex(ValueError, 'between 1 and 256'):
                launcher.build_backend(args)


if __name__ == '__main__':
    unittest.main()
