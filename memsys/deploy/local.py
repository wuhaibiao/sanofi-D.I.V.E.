#!/usr/bin/env python3
"""Native macOS deployment: Qdrant, BGE-M3 HTTP, and the memory API.

No Docker, model inference, provider probes, cloud changes, or shell-sourced secrets.
"""
import argparse
import json
import os
from pathlib import Path
import plistlib
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = Path.home() / "Library/Application Support/memsys"
REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
PORTS = {"qdrant": 6333, "embedding": 11434, "memory": 8848}
LABELS = {role: "com.memsys." + role for role in PORTS}


def dotenv(path):
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        pieces = shlex.split(value, comments=True)
        if len(pieces) > 1:
            raise ValueError("dotenv values with spaces must be quoted")
        values[key.strip()] = pieces[0] if pieces else ""
    return values


def private_json(path, value):
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    try:
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def profile(state):
    value = json.loads((state / "runtime.json").read_text())
    if Path(value["project_root"]) != ROOT:
        raise ValueError("local runtime belongs to a different project; left unchanged")
    return value


def prepare(state, qdrant_binary):
    source = json.loads((ROOT / "config.json").read_text())
    roles = {role: source["roles"][role] for role in ("memory", "extract") if source.get("roles", {}).get(role)}
    if not roles:
        raise ValueError("configure an extraction provider in config.json first")
    models = {name: source["models"][name] for name in set(roles.values())}
    available = dict(dotenv(ROOT / ".env"), **os.environ)
    for item in models.values():
        if item.get("api_key_env") and not available.get(item["api_key_env"]):
            raise ValueError("configured provider credential is absent; add it to the private project .env")
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    existing = profile(state) if (state / "runtime.json").exists() else None
    for name in ("bin", "logs", "qdrant/storage", "qdrant/snapshots", "memory"):
        (state / name).mkdir(parents=True, mode=0o700, exist_ok=True)
    binary = state / "bin/qdrant"
    if not binary.exists():
        if not qdrant_binary.is_file():
            raise ValueError("provide a native Qdrant 1.19.2 binary with --qdrant-binary")
        result = subprocess.run([str(qdrant_binary), "--version"], capture_output=True, text=True, check=True)
        if "qdrant 1.19.2" not in result.stdout:
            raise ValueError("this deployment pins Qdrant 1.19.2")
        shutil.copy2(qdrant_binary, binary)
        binary.chmod(0o700)
    tokens = existing["tokens"] if existing else {name: secrets.token_urlsafe(48) for name in ("qdrant", "embedding", "admin", "user")}
    snapshot = Path.home() / ".cache/huggingface/hub/models--BAAI--bge-m3/snapshots" / REVISION
    model_key = "bge-m3-native-fp32"
    models[model_key] = dict(model="bge-m3", base_url="http://127.0.0.1:11434/v1", api="openai",
        api_key_env="EMBEDDING_API_KEY", embed_batch_size=16)
    roles["embedding"] = model_key
    from memsys.config import MemorySettings
    memory = MemorySettings.from_dict({k: v for k, v in source.get("memory", {}).items() if not k.startswith("_")}).to_dict()
    memory.update(rerank_backend="bge", rerank_url="", rerank_device="mps", rerank_dtype="float32",
        worker_count=1, embedding_model_revision=REVISION, query_analysis=False, query_policy="off",
        answer_review_policy="off", max_storage_bytes=None)
    config = dict(models=models, roles=roles, memory=memory,
        storage=dict(url="http://127.0.0.1:6333", collection_prefix="memsys", namespace="local", api_key_env="QDRANT_API_KEY"))
    config_path = state / "config.json"
    if not config_path.exists():
        private_json(config_path, config)
    runtime = dict(project_root=str(ROOT), python=str(ROOT / ".venv/bin/python"), tokens=tokens,
        embedding_device=existing.get("embedding_device", "mps") if existing else "mps",
        embedding_snapshot=str(snapshot), config_path=str(config_path), qdrant_binary=str(binary))
    private_json(state / "runtime.json", runtime)
    yaml = ("storage:\n  storage_path: " + json.dumps(str(state / "qdrant/storage")) +
        "\n  snapshots_path: " + json.dumps(str(state / "qdrant/snapshots")) +
        "\nservice:\n  host: 127.0.0.1\n  http_port: 6333\n  grpc_port: 6334\ntelemetry_disabled: true\n")
    (state / "qdrant.yaml").write_text(yaml)
    (state / "qdrant.yaml").chmod(0o600)
    directory = Path.home() / "Library/LaunchAgents"
    directory.mkdir(parents=True, exist_ok=True)
    for role, label in LABELS.items():
        path = directory / (label + ".plist")
        if path.exists() and plistlib.loads(path.read_bytes()).get("WorkingDirectory") != str(ROOT):
            raise ValueError("a launch agent with this label belongs to another project")
        definition = dict(Label=label, ProgramArguments=[runtime["python"], str(Path(__file__).resolve()),
            "_service", role, "--state-dir", str(state)], WorkingDirectory=str(ROOT), RunAtLoad=True,
            KeepAlive=True, ThrottleInterval=15, ProcessType="Background",
            StandardOutPath=str(state / "logs" / (role + ".log")),
            StandardErrorPath=str(state / "logs" / (role + ".error.log")))
        path.write_bytes(plistlib.dumps(definition))
        path.chmod(0o600)
    print("Native deployment prepared. Private credentials and data:", state)


def request(url, headers=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, headers=headers or {}), timeout=3) as response:
        return json.load(response)


def service(role, state):
    settings = profile(state)
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
    if role == "qdrant":
        os.environ["QDRANT__SERVICE__API_KEY"] = settings["tokens"]["qdrant"]
        os.execv(settings["qdrant_binary"], [settings["qdrant_binary"], "--config-path", str(state / "qdrant.yaml")])
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    if role == "embedding":
        os.environ["EMBEDDING_API_KEY"] = settings["tokens"]["embedding"]
        from memsys.embedding_server import main
        sys.argv = ["embedding_server", "--model-dir", settings["embedding_snapshot"], "--device", settings.get("embedding_device", "mps")]
        main()
        return
    from memsys.config import AppConfig
    from memsys.llm import UsageLedger
    from memsys.serve import MemoryProxy, MemoryService
    config = AppConfig.load(settings["config_path"])
    original = dotenv(ROOT / ".env")
    for item in config.models.values():
        key = item.api_key_env
        if key and key in original and not os.environ.get(key):
            os.environ[key] = original[key]
    os.environ["QDRANT_API_KEY"] = settings["tokens"]["qdrant"]
    os.environ["EMBEDDING_API_KEY"] = settings["tokens"]["embedding"]
    os.environ["MEMSYS_API_TOKEN"] = settings["tokens"]["admin"]
    os.environ["MEMSYS_USER_TOKENS"] = json.dumps({"local": settings["tokens"]["user"]})
    os.environ["MEMSYS_HOME"] = str(state / "memory")
    models = config.build_models(UsageLedger(str(state / "logs/usage.jsonl")), env={}, probe=False)
    memory = MemoryService(models, config.memory, str(state / "memory"), storage=config.storage,
                           log=lambda message: print(message, flush=True))
    server = MemoryProxy(("127.0.0.1", PORTS["memory"]), memory, None, default_user="local", web_local_user="local")
    print("Memory API ready on http://127.0.0.1:8848.", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        memory.shutdown()


def start(state):
    settings = profile(state)
    domain = "gui/" + str(os.getuid())
    for role, label in LABELS.items():
        target = domain + "/" + label
        if subprocess.run(["launchctl", "print", target], capture_output=True).returncode:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", PORTS[role])) == 0:
                    raise ValueError("port %d is already occupied by a different service" % PORTS[role])
            subprocess.run(["launchctl", "bootstrap", domain,
                str(Path.home() / "Library/LaunchAgents" / (label + ".plist"))], check=True)
        if role == "qdrant":
            for _ in range(40):
                try:
                    request("http://127.0.0.1:6333/collections", {"api-key": settings["tokens"]["qdrant"]})
                    break
                except OSError:
                    time.sleep(.25)
            else:
                raise ValueError("Qdrant did not become ready; inspect local logs")
    print("Local services started as login agents.")


def status(state):
    settings = profile(state)
    for role, port in PORTS.items():
        url = "http://127.0.0.1:%d%s" % (port, "/" if role == "qdrant" else "/healthz")
        try:
            data = request(url, {"api-key": settings["tokens"]["qdrant"]} if role == "qdrant" else None)
            print(role, json.dumps(data, ensure_ascii=False))
        except OSError as exc:
            print(role, "unavailable:", type(exc).__name__)
    print("Private credentials:", state / "runtime.json")


def stop(state):
    profile(state)
    for role, label in reversed(list(LABELS.items())):
        target = "gui/%d/%s" % (os.getuid(), label)
        subprocess.run(["launchctl", "bootout", target], capture_output=True)
        for _ in range(120):
            registered = subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0
            with socket.socket() as sock:
                listening = sock.connect_ex(("127.0.0.1", PORTS[role])) == 0
            if not registered and not listening:
                break
            time.sleep(.25)
        else:
            raise ValueError("service did not stop cleanly: " + role)
    print("Local services stopped; data and login-agent files retained.")


def restart(state):
    stop(state)
    start(state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "start", "status", "stop", "restart", "_service"))
    parser.add_argument("role", nargs="?", choices=list(PORTS))
    parser.add_argument("--state-dir", type=Path, default=STATE)
    parser.add_argument("--qdrant-binary", type=Path, default=Path("/tmp/memsys-qdrant-functional/qdrant"))
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("native deployment currently targets macOS")
    if args.command == "prepare":
        prepare(args.state_dir, args.qdrant_binary)
    elif args.command == "_service":
        if args.role is None:
            parser.error("service role is required")
        service(args.role, args.state_dir)
    else:
        globals()[args.command](args.state_dir)


if __name__ == "__main__":
    main()
