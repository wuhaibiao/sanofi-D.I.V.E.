#!/usr/bin/env python3
"""Reload the local API and check fact fields without calling an answer LLM."""
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
from memsys.fact_content import index_signature
from memsys.store import Store


def main():
    runtime_path = Path.home() / 'Library/Application Support/memsys/runtime.json'
    runtime = json.loads(runtime_path.read_text())
    credentials(runtime_path)
    cfg = AppConfig.load(runtime['config_path'])
    assert cfg.memory.recall_mode == 'facts'
    assert not cfg.memory.query_analysis
    base = 'http://127.0.0.1:8848'

    def status():
        req = urllib.request.Request(base + '/memsys/status',
            headers={'X-Memsys-Token': runtime['tokens']['user']})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

    def snapshot():
        store = Store(settings=cfg.storage, user_id='local')
        try:
            with store.read_snapshot():
                records = {kind: sorted(store.records(kind), key=lambda row: str(row.get('id')))
                           for kind in ('fact', 'episode', 'turn')}
                digest = hashlib.sha256(json.dumps(records, sort_keys=True,
                    ensure_ascii=False, default=str).encode()).hexdigest()
                manifest = json.loads(store.get_meta('index_manifest'))
                return dict(records_sha256=digest, counts=store.counts(),
                    index_full_fields=all(manifest.get(k) == v for k, v in index_signature().items()))
        finally:
            store.close()

    before_status = status()
    if before_status['queued'] or before_status['retrying']:
        raise RuntimeError('Local write work is active; API reload deferred.')
    before = snapshot()
    subprocess.run(['launchctl', 'kickstart', '-k', f'gui/{os.getuid()}/com.memsys.memory'], check=True)
    for attempt in range(30):
        try:
            with urllib.request.urlopen(base + '/healthz', timeout=2) as response:
                assert json.load(response)['status'] == 'ready'
            break
        except (OSError, AssertionError):
            if attempt == 29:
                raise RuntimeError('API did not become ready after reload')
            time.sleep(1)
    after = snapshot()
    assert before == after, 'Stored facts/originals changed during service reload'
    client = MemoryClient(base, runtime['tokens']['user'], 'local', timeout_s=60)
    recall = client.recall('我之前提到过哪些个人信息？', budget=32000)
    assert recall['items'], 'Local check recalled no facts'
    required = {'value', 'semantics', 'memory_kind', 'sentence', 'subject', 'attribute'}
    assert all(item['kind'] == 'fact' and required <= item.keys() for item in recall['items'])
    assert '[E' not in recall['memory'], 'Source excerpt entered facts-only memory'
    after_status = status()
    assert after_status['recall_mode'] == 'facts'
    audit = dict(source=source_manifest(), before=before, after=after,
        records_unchanged=True, recalled_facts=len(recall['items']), original_excerpts=0,
        full_fields=True, answer_llm_calls=0, private_history_reextracted=False,
        queued=after_status['queued'], retrying=after_status['retrying'], failed=after_status['failed'])
    path = ROOT / 'runs/fact-retention-20261013/local-deployment-audit.json'
    path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: audit[k] for k in ('records_unchanged', 'recalled_facts',
        'original_excerpts', 'full_fields', 'answer_llm_calls', 'queued', 'retrying', 'failed')}))


if __name__ == '__main__':
    main()
