#!/usr/bin/env python3
"""Upgrade local graph/fact indexes and reload the host without re-extracting history."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
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
from memsys.relational_memory import RelationIndex
from memsys.store import Store

UPDATES = dict(relations_enabled=True, relation_hops=2, relation_seed_facts=6, relation_max_nodes=32,
    relation_max_edges=96, relation_max_candidates=32, answer_fact_limit=128,
    chat_window_turns=12, chat_window_tokens=12000, min_context_tokens=1500)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, default=Path.home() / 'Library/Application Support/memsys/runtime.json')
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError('Preserve earlier deployment records; use a fresh output directory')
    args.out.mkdir(parents=True)
    runtime = json.loads(args.runtime.read_text())
    credentials(args.runtime)
    config_path = Path(runtime['config_path'])
    original_cfg = json.loads(config_path.read_text())
    base = 'http://127.0.0.1:8848'
    def status():
        request = urllib.request.Request(base + '/memsys/status', headers={'X-Memsys-Token': runtime['tokens'].get('admin', runtime['tokens']['user'])})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    current = status()
    if current['queued'] or current['retrying']:
        raise RuntimeError('Active local write jobs must finish before index upgrade')
    for path, label in ((config_path, 'runtime'), (ROOT / 'config.json', 'project')):
        if path.exists():
            backup = args.out / (label + '-config.before.json')
            shutil.copy2(path, backup)
            os.chmod(backup, 0o600)
            data = json.loads(path.read_text())
            data.setdefault('memory', {}).update(UPDATES)
            AppConfig.from_dict(data)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            os.chmod(path, 0o600)
    cfg = AppConfig.load(config_path)
    if cfg.memory.recall_mode != 'facts':
        raise ValueError('Default facts-only mode must be preserved')
    users = Store.users(cfg.storage)
    def snapshot(user):
        store = Store(user_id=user, settings=cfg.storage)
        try:
            data = {kind: sorted([{k:v for k,v in row.items() if k not in ('vector',)} for row in store.records(kind)],
                                 key=lambda row: str(row.get('id', row.get('idx', '')))) for kind in ('fact', 'episode', 'turn')}
            return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        finally:
            store.close()
    before = {user:snapshot(user) for user in users}
    ledger = UsageLedger(str(args.out / 'index-usage.jsonl'))
    system = MemorySystem(cfg.build_models(ledger, env={}, probe=False), cfg.memory, storage=cfg.storage)
    records = []
    try:
        for user in users:
            indexed = system.reindex(user)
            graph = RelationIndex(system.store(user)).rebuild()
            manifest = json.loads(system.store(user).get_meta('index_manifest'))
            if not all(manifest.get(key) == value for key,value in index_signature().items()):
                raise AssertionError('Fact index manifest did not upgrade')
            records.append(dict(user_hash=hashlib.sha256(user.encode()).hexdigest()[:12], indexed=indexed, graph=graph,
                                originals_unchanged=snapshot(user) == before[user]))
    finally:
        system.close()
    if not all(row['originals_unchanged'] for row in records):
        raise AssertionError('Existing fact or original content changed during index upgrade')
    if any(row.purpose != 'embed' for row in ledger.records()):
        raise AssertionError('Index upgrade unexpectedly called a generative model')
    (args.out / 'index-check.json').write_text(json.dumps(dict(records=records, updates=UPDATES,
        generative_calls=0, source=source_manifest()), ensure_ascii=False, indent=2) + '\n')
    subprocess.run(['launchctl', 'kickstart', '-k', f'gui/{os.getuid()}/com.memsys.memory'], check=True)
    ready = False
    for _ in range(45):
        try:
            with urllib.request.urlopen(base + '/healthz', timeout=2) as response:
                ready = json.load(response)['status'] == 'ready'
            if ready:
                break
        except OSError:
            pass
        time.sleep(1)
    if not ready or status()['recall_mode'] != 'facts':
        raise RuntimeError('Updated local API did not become ready in facts-only mode')
    recall = MemoryClient(base, runtime['tokens']['user'], 'local', timeout_s=120).recall('What facts do you remember?', budget=cfg.memory.context_budget_tokens)
    if not recall.get('relations', {}).get('enabled') or any(item.get('kind') != 'fact' or 'relations' not in item for item in recall['items']):
        raise AssertionError('Updated HTTP API did not expose fact relation metadata')
    data = dict(source=source_manifest(), updates=UPDATES, upgraded_users=len(users), records=records,
        ready=ready, recall_mode=status()['recall_mode'], source_excerpts=sum(item['kind'] != 'fact' for item in recall['items']),
        relations=recall.get('relations'), restored_from_facts_only=True,
        limitation='Old facts receive only explicit-key association indexes; missing past relations are not reconstructed without re-extraction.')
    (args.out / 'report.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key:data[key] for key in ('upgraded_users','ready','recall_mode','source_excerpts')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
