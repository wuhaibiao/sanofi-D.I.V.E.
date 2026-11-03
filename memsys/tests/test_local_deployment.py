"""Local deployment and HTTP contracts only; no model loading or inference."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import stat
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from memsys.embedding_server import Handler


class Backend:
    files_ready = True
    model = None

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(texts)
        return [[1.] + [0.] * 1023 for _ in texts], len(texts)


class EmbeddingHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.backend, self.server.token = Backend(), "synthetic-token"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, path, data=None, auth=False):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer synthetic-token"
        request = urllib.request.Request(self.url + path, json.dumps(data).encode() if data is not None else None, headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_health_and_model_metadata_do_not_load_or_infer(self):
        self.assertEqual(self.request("/healthz")[0], 200)
        self.assertFalse(self.request("/healthz")[1]["model_loaded"])
        self.assertEqual(self.request("/v1/models")[0], 401)
        self.assertEqual(self.request("/v1/models", auth=True)[0], 200)
        self.assertEqual(self.server.backend.calls, [])

    def test_authorization_and_invalid_input_reject_before_encoding(self):
        body = dict(model="bge-m3", input="synthetic")
        self.assertEqual(self.request("/v1/embeddings", body)[0], 401)
        self.assertEqual(self.request("/v1/embeddings", dict(model="other", input="x"), True)[0], 400)
        self.assertEqual(self.request("/v1/embeddings", dict(model="bge-m3", input=[123]), True)[0], 400)
        self.assertEqual(self.request("/v1/embeddings", dict(model="bge-m3", input="x", dimensions=3), True)[0], 400)
        self.assertEqual(self.server.backend.calls, [])

    def test_synthetic_openai_embedding_contract(self):
        code, body = self.request("/v1/embeddings", dict(model="bge-m3", input=["one", "two"]), True)
        self.assertEqual(code, 200)
        self.assertEqual([item["index"] for item in body["data"]], [0, 1])
        self.assertEqual([len(item["embedding"]) for item in body["data"]], [1024, 1024])
        self.assertEqual(body["usage"]["total_tokens"], 2)


class LocalConfigTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "deploy/local.py"
        spec = importlib.util.spec_from_file_location("memsys_local_deploy", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_dotenv_is_data_without_shell_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("export KEY='literal $HOME `command`'\nEMPTY=\nIGNORED=hello # note\n")
            self.assertEqual(self.module.dotenv(path), {"KEY": "literal $HOME `command`", "EMPTY": "", "IGNORED": "hello"})

    def test_inference_device_matches_mac_architecture(self):
        for architecture, expected in (("x86_64", "cpu"), ("arm64", "mps")):
            with self.subTest(architecture=architecture), patch.object(self.module.platform, "machine", return_value=architecture):
                self.assertEqual(self.module.inference_device(), expected)

    def test_credentials_file_is_private_and_other_projects_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            self.module.private_json(path, dict(project_root="/different-project", tokens={"user": "synthetic"}))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(ValueError):
                self.module.profile(path.parent)

    def test_background_runtime_and_credentials_avoid_protected_project_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            project = home / "Downloads/project"
            project.mkdir(parents=True)
            project.joinpath("config.json").write_text(json.dumps({
                "models": {"test": {"model": "test", "api_key_env": "PROVIDER_TEST_KEY"}},
                "roles": {"memory": "test"}}))
            project.joinpath(".env").write_text("PROVIDER_TEST_KEY=synthetic-key\nUNRELATED_SECRET=do-not-copy\n")
            state = home / "Library/Application Support/memsys"
            state.joinpath("bin").mkdir(parents=True)
            state.joinpath("bin/qdrant").touch()
            python = state / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            agents = home / "Library/LaunchAgents"
            agents.mkdir(parents=True)
            # Existing deployments in Downloads migrate without changing their owner.
            for label in self.module.LABELS.values():
                agents.joinpath(label + ".plist").write_bytes(plistlib.dumps({"WorkingDirectory": str(project)}))
            with patch.object(self.module, "ROOT", project), patch.object(Path, "home", return_value=home):
                self.module.prepare(state, Path("unused"))
            credentials = state / "provider-credentials.json"
            self.assertEqual(json.loads(credentials.read_text()), {"PROVIDER_TEST_KEY": "synthetic-key"})
            self.assertEqual(stat.S_IMODE(credentials.stat().st_mode), 0o600)
            self.assertTrue(state.joinpath("local-service.py").is_file())
            for label in self.module.LABELS.values():
                path = agents / (label + ".plist")
                definition = plistlib.loads(path.read_bytes())
                self.assertEqual(definition["WorkingDirectory"], str(state))
                self.assertEqual(definition["ProgramArguments"][:2], [str(python), str(state / "local-service.py")])
                self.assertEqual(definition["EnvironmentVariables"]["MEMSYS_SOURCE_ROOT"], str(project))
                self.assertNotIn(b"synthetic-key", path.read_bytes())

    def test_stop_waits_for_unregistration_before_restart_can_proceed(self):
        completed = MagicMock(returncode=1)
        removing = MagicMock(returncode=0)
        replies = [completed, removing, completed, completed, completed, completed, completed]
        sock = MagicMock()
        sock.__enter__.return_value.connect_ex.return_value = 1
        with patch.object(self.module, "profile", return_value={}), \
             patch.object(self.module.subprocess, "run", side_effect=replies) as run, \
             patch.object(self.module.socket, "socket", return_value=sock), \
             patch.object(self.module.time, "sleep") as sleep:
            self.module.stop(Path("/synthetic-state"))
        self.assertEqual(sleep.call_count, 1)
        commands = [call.args[0][1] for call in run.call_args_list]
        self.assertEqual(commands, ["bootout", "print", "print", "bootout", "print", "bootout", "print"])
