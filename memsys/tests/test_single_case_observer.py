"""The observer records data without replacing successful model output."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request


def create_observer(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("trace_one_case_test", Path(__file__).resolve().parents[1] / "scripts" / "trace_one_case.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from memsys.llm import OpenAICompatClient
    from memsys.protocols import ResponsesClient, AnthropicMessagesClient
    from memsys.core import MemorySystem
    # Register original methods with pytest's cleanup before direct observation.
    for cls in (OpenAICompatClient, ResponsesClient, AnthropicMessagesClient):
        for name in ("chat", "embed"):
            if name in cls.__dict__:
                monkeypatch.setattr(cls, name, cls.__dict__[name])
    for name in ("close_session", "respond"):
        monkeypatch.setattr(MemorySystem, name, getattr(MemorySystem, name))
    return module, tmp_path, monkeypatch


def test_normal_and_budgeted_request_response_unchanged(observer):
    module, out, monkeypatch = observer
    from memsys.llm import ModelConfig, UsageLedger
    from memsys.protocols import ResponsesClient
    from memsys.read_budget import ReadBudget, read_scope
    payload = {"status": "completed", "output": [{"type": "message", "phase": "final_answer",
                "content": [{"type": "output_text", "text": "unchanged-output"}]}],
               "usage": {"input_tokens": 7, "output_tokens": 2}}
    raw = json.dumps(payload).encode()
    sent = []

    class Response(io.BytesIO):
        status = 200
        headers = {"content-type": "application/json"}
        def read1(self, *args):
            return self.read(*args)

    def fake_urlopen(request, **kwargs):
        sent.append(request.data)
        return Response(raw)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    recorder = module.Recorder(out)
    recorder.install()
    client = ResponsesClient(ModelConfig(model="unit", base_url="http://unit.invalid/v1"), UsageLedger(), api_key="unit-secret-123456")
    messages = [{"role": "user", "content": "literal input"}]
    first = client.chat(messages, purpose="answer", max_tokens=16)
    with read_scope(ReadBudget(10, 0, 0, 0)):
        second = client.chat(messages, purpose="answer", max_tokens=16)
    assert first.text == second.text == "unchanged-output"
    assert sent[0] == sent[1]
    rows = [json.loads(p.read_text()) for p in sorted((out / "http").glob("*.json"))]
    assert len(rows) == 2
    assert all(r["response"]["raw_utf8"] == raw.decode() for r in rows)
    assert all(r["request"]["body"]["input"] == messages for r in rows)
    assert all("unit-secret-123456" not in p.read_text() for p in out.rglob("*.json"))
    assert messages == [{"role": "user", "content": "literal input"}]


def test_close_session_preserves_originals_and_report(observer):
    module, out, monkeypatch = observer
    from datetime import datetime
    from memsys.core import MemorySystem
    from memsys.config import Models, MemorySettings
    from memsys.fake import HeuristicChat, HashEmbedder
    monkeypatch.setattr(urllib.request, "urlopen", urllib.request.urlopen)
    recorder = module.Recorder(out)
    recorder.install()
    chat = HeuristicChat()
    models = Models(chat=chat, memory=chat, embedder=HashEmbedder(), embedding_name="unit")
    system = MemorySystem(models, MemorySettings(rerank_backend="fusion"), data_dir=str(out / "stores"))
    try:
        at = datetime(2025, 1, 2, 12)
        handle = system.open_session("unit-user", at, "external-1")
        system.add_turn(handle, "user", "My favorite color is blue.", at)
        report, lifecycle = system.close_session(handle, at)
        snapshot = json.loads((out / "states" / "session-01-after-close.json").read_text())
        assert snapshot["tables"]["turns"]["rows"][0]["content"] == "My favorite color is blue."
        recorded = json.loads((out / "states" / "session-01-report.json").read_text())
        assert recorded["write_report"]["session_id"] == report.session_id
        assert recorded["external_id"] == "external-1"
        assert snapshot["pending_extract_rounds"] == 0
    finally:
        system.close()


class ObserverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memsys-observer-test-")
        self.addCleanup(temporary.cleanup)
        def patchattr(target, name, value):
            operation = patch.object(target, name, value)
            operation.start()
            self.addCleanup(operation.stop)
        self.observer = create_observer(Path(temporary.name), SimpleNamespace(setattr=patchattr))

    def test_model_inputs_outputs_and_budget_unchanged(self):
        test_normal_and_budgeted_request_response_unchanged(self.observer)

    def test_source_and_write_report_unchanged(self):
        test_close_session_preserves_originals_and_report(self.observer)


if __name__ == "__main__":
    unittest.main()
