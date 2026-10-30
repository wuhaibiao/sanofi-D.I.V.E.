"""Synthetic shared-gateway removal checks; no Docker or remote operations."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


class CloudRemovalTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "scripts/remove_cloud_deployment.py"
        spec = importlib.util.spec_from_file_location("cloud_removal", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.blog = "whb.vin {\n    reverse_proxy whb_wordpress_app:80\n}\n"
        self.site = ("# BEGIN memsys memory.whb.vin\n"
                     "memory.whb.vin {\n    reverse_proxy memsys-api:8848\n}\n"
                     "# END memsys memory.whb.vin\n")

    def test_removal_preserves_blog_and_is_idempotent(self):
        original = self.blog + "\n" + self.site
        candidate = self.module.managed_site_removed(original)
        self.assertEqual(candidate, self.blog + "\n")
        self.assertEqual(self.module.managed_site_removed(candidate), candidate)

    def test_unmanaged_or_incomplete_site_is_not_changed(self):
        for content in (self.blog + "memory.whb.vin { }\n",
                        self.blog + self.site.split("# END")[0],
                        self.blog + self.site + self.site):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    self.module.managed_site_removed(content)

    def container_result(self, directory="/opt/memsys/deploy", service="qdrant", project="memsys"):
        return {"Id": "synthetic-id", "Name": "/memsys-qdrant-1", "Config": {
            "Labels": {"com.docker.compose.project": project,
                       "com.docker.compose.service": service,
                       "com.docker.compose.project.working_dir": directory},
            "Env": ["QDRANT__SERVICE__API_KEY=synthetic-private-key"],
        }}

    def test_compose_file_directory_and_explicit_root_directory_are_accepted(self):
        for directory in ("/opt/memsys/deploy", "/opt/memsys"):
            with self.subTest(directory=directory):
                item = self.container_result(directory=directory)
                replies = [subprocess.CompletedProcess([], 0, stdout="synthetic-id\n"),
                           subprocess.CompletedProcess([], 0, stdout=json.dumps([item]))]
                with patch.object(self.module, "run", side_effect=replies) as mocked:
                    self.assertEqual(self.module.containers(), [item])
                    self.assertEqual([call.args[0][:2] for call in mocked.call_args_list],
                                     [["docker", "ps"], ["docker", "inspect"]])

    def test_other_directory_project_or_service_is_rejected_without_exposing_keys(self):
        for fields in (dict(directory="/opt/other-memsys"), dict(service="wordpress"), dict(project="whb_blog")):
            with self.subTest(fields=fields):
                item = self.container_result(**fields)
                replies = [subprocess.CompletedProcess([], 0, stdout="synthetic-id\n"),
                           subprocess.CompletedProcess([], 0, stdout=json.dumps([item]))]
                with patch.object(self.module, "run", side_effect=replies) as mocked:
                    with self.assertRaises(ValueError) as caught:
                        self.module.containers()
                    message = str(caught.exception)
                    self.assertIn("working_dir", message)
                    self.assertIn("service", message)
                    self.assertNotIn("synthetic-private-key", message)
                    self.assertEqual([call.args[0][:2] for call in mocked.call_args_list],
                                     [["docker", "ps"], ["docker", "inspect"]])

    def test_failed_reload_restores_original_and_keeps_private_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Caddyfile"
            original = self.blog + self.site
            path.write_text(original)
            inode = path.stat().st_ino
            with patch.object(self.module, "caddy", side_effect=[None, RuntimeError("reload failed"), None]):
                with self.assertRaises(RuntimeError):
                    self.module.remove_site(path)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(path.stat().st_ino, inode)
            backup = list(path.parent.glob("Caddyfile.before-memsys-removal-*"))
            self.assertEqual(len(backup), 1)
            self.assertEqual(backup[0].read_text(), original)
            self.assertEqual(backup[0].stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
