#!/usr/bin/env python3
"""Upgrade local fact indexes and reload the API; never re-extract private history."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from check_webchat_reliability import credentials
from memsys.bench.provenance import source_manifest
from memsys.client import MemoryClient
from memsys.config import AppConfig
from memsys.core import MemorySystem
from memsys.fact_content import index_signature
from memsys.llm import UsageLedger
from memsys.store import Store


def main():
    out = ROOT / 'runs/fact-memory-v6-20261014'
    runtime_path = Path.home() / 'Library/Application Support/memsys/runtime.json'
    runtime = json.loads(runtime_path.read_text())
    credentials(runtime_path)
    cfg = AppConfig.load(runtime['config_path'])
    assert cfg.memory.recall_mode == 'facts'
    base = 'http://127.0.0.1:8848'
    def status():
        req = urllib.request.Request(base + '/memsys/status', headers={'X-Memsys-Token': runtime['tokens']['user']})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    def snapshot():
        store = Store(settings=cfg.storage, user_id='local')
        try:
            with store.read_snapshot():
                data = {kind: sorted(store.records(kind), key=lambda r: str(r.get('id')))
                        for kind in ('fact', 'episode', 'turn')}
                return dict(records_sha256=hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                            counts=store.counts())
        finally:
            store.close()
    current = status()
    if current['queued'] or current['retrying']:
        raise RuntimeError('Local write jobs are active; retry deployment after they finish')
    before = snapshot()
    ledger = UsageLedger(str(out / 'local-index-usage.jsonl'))
    models = cfg.build_models(ledger, env={}, probe=False)
    system = MemorySystem(models, cfg.memory, storage=cfg.storage)
    try:
        indexed = system.reindex('local')
        manifest = json.loads(system.store('local').get_meta('index_manifest'))
        assert all(manifest.get(k) == v for k, v in index_signature().items())
    finally:
        system.close()
    assert snapshot() == before, 'Raw facts or originals changed during index upgrade'
    assert all(r.purpose == 'embed' for r in ledger.records())
    subprocess.run(['launchctl', 'kickstart', '-k', f'gui/{os.getuid()}/com.memsys.memory'], check=True)
    for attempt in range(30):
        try:
            with urllib.request.urlopen(base + '/healthz', timeout=2) as response:
                assert json.load(response)['status'] == 'ready'
            break
        except (OSError, AssertionError):
            if attempt == 29:
                raise RuntimeError('API did not become ready')
            time.sleep(1)
    after = snapshot()
    assert after == before, 'Raw facts or originals changed during API reload'
    recall = MemoryClient(base, runtime['tokens']['user'], 'local', timeout_s=60).recall('我之前提到过哪些个人信息？', budget=32000)
    required = {'value', 'semantics', 'memory_kind', 'recorded_at', 'event_date', 'event_status'}
    assert recall['items'] and all(i['kind'] == 'fact' and required <= i.keys() for i in recall['items'])
    assert '[E' not in recall['memory']
    latest = status()
    audit = dict(source=source_manifest(), before=before, after=after, indexed_records=indexed,
        index_signature=index_signature(), records_unchanged=True, recalled_facts=len(recall['items']),
        original_excerpts=0, new_temporal_fields=True, private_history_reextracted=False,
        deployment_llm_calls=0, queued=latest['queued'], retrying=latest['retrying'], failed=latest['failed'])
    (out / 'local-deployment-audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: audit[k] for k in ('indexed_records', 'records_unchanged', 'recalled_facts',
        'original_excerpts', 'new_temporal_fields', 'queued', 'retrying', 'failed')}))


if __name__ == '__main__':
    main()
