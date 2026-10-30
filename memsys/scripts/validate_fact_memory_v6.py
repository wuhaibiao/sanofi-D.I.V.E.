#!/usr/bin/env python3
"""One isolated real-provider pass over prewritten functional scenarios."""
from collections import Counter
import argparse
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from check_webchat_reliability import credentials
from memsys.bench.provenance import model_manifest, source_manifest
from memsys.config import AppConfig
from memsys.core import MemorySystem
from memsys.llm import UsageLedger
from memsys.qdrant import Qdrant
from memsys.webchat import SYSTEM, WebChat


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


class Captured:
    def __init__(self, client, path):
        self.client, self.config, self.path = client, getattr(client, 'config', None), path

    def chat(self, messages, **kwargs):
        with self.path.open('a') as stream:
            stream.write(json.dumps(dict(messages=messages, purpose=kwargs['purpose'],
                tags=kwargs.get('tags', {})), ensure_ascii=False) + '\n')
        return self.client.chat(messages, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'runs/fact-memory-v6-20261014')
    parser.add_argument('--cleanup-only', action='store_true')
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    parent = ROOT / 'runs/fact-memory-v6-20261014'
    plan = json.loads((parent / 'plan-freeze.json').read_text())
    fixture_path = ROOT / 'tests/fixtures/fact_memory_v6_synthetic.json'
    assert hashlib.sha256(fixture_path.read_bytes()).hexdigest() == plan['fixture_sha256']
    if not args.cleanup_only:
        assert not (out / 'real-results.json').exists(), 'Keep this one-pass record; use a new directory after a general repair'
    fixture = json.loads(fixture_path.read_text())
    runtime_path = Path.home() / 'Library/Application Support/memsys/runtime.json'
    runtime = json.loads(runtime_path.read_text())
    credentials(runtime_path)
    cfg = AppConfig.load(runtime['config_path'])
    assert cfg.memory.recall_mode == 'facts' and not cfg.memory.query_analysis
    prefix = (json.loads((out / 'real-freeze.json').read_text())['prefix'] if args.cleanup_only
              else 'memsys_v6fixture_' + uuid.uuid4().hex[:12])
    assert re.fullmatch(r'memsys_v6fixture_[0-9a-f]{12}', prefix)
    storage = replace(cfg.storage, collection_prefix=prefix, namespace='self-authored-v6')
    if args.cleanup_only:
        client = Qdrant(storage)
        removed = []
        for collection in client.request('GET', '/collections')['collections']:
            name = collection['name']
            if name.startswith(prefix + '_'):
                client.request('DELETE', '/collections/' + name)
                removed.append(name)
        results = json.loads((out / 'real-results.json').read_text())
        ledger = UsageLedger.from_jsonl(str(out / 'real-usage.jsonl'))
        counts = Counter(r.purpose for r in ledger.records())
        save(out / 'real-summary.json', dict(total=len(fixture['cases']), processed=len(results),
            completed=sum(r.get('completed', False) for r in results), purpose_calls=dict(counts),
            original_excerpts=sum(r.get('supplied_source_excerpts', 0) for r in results), removed_collections=removed,
            cleanup_recovery=True, recovery_model_calls=0,
            limitation='Functional observations from self-authored scenarios; first-pass truncation is preserved.'))
        print('Cleanup recovered; no model calls.', flush=True)
        return
    ledger = UsageLedger(str(out / 'real-usage.jsonl'))
    models = cfg.build_models(ledger, env={}, probe=False)
    captured = {}
    def wrap(client):
        if client is None:
            return None
        if id(client) not in captured:
            captured[id(client)] = Captured(client, out / 'real-prompts.jsonl')
        return captured[id(client)]
    models.chat, models.memory = wrap(models.chat), wrap(models.memory)
    models.by_purpose = {k: wrap(v) for k, v in models.by_purpose.items()}
    frozen = source_manifest()
    save(out / 'real-freeze.json', dict(source=frozen, fixture_sha256=plan['fixture_sha256'], prefix=prefix,
        selected_at=datetime.now(timezone.utc).isoformat(), model_configuration=model_manifest(models),
        method='Self-authored functional pass, one extract and one answer per case. No public dataset, gold labels, judge or answer retries.'))
    system = MemorySystem(models, cfg.memory, trace_dir=str(out / 'real-traces'), storage=storage)
    results = []
    try:
        for case in fixture['cases']:
            assert source_manifest()['source_sha256'] == frozen['source_sha256']
            at = datetime.fromisoformat(case['at'])
            record = dict(case_id=case['id'], source_messages=2)
            try:
                handle = system.open_session(case['id'], at, external_id='self-authored-' + case['id'])
                system.add_turn(handle, 'user', case['user'], at, idempotency_key='source-user')
                system.add_turn(handle, 'assistant', case['assistant'], at, idempotency_key='source-assistant')
                report, _ = system.close_session(handle, at, tags={'session_id': prefix + ':write:' + case['id']})
                assert report.extract_calls == 1
                record['write_report'] = asdict(report)
                record['facts'] = [asdict(f) for f in system.store(case['id']).facts()]
                rec = system.recall(case['id'], case['question'], at + timedelta(days=1), budget=cfg.memory.context_budget_tokens)
                assert all(item['kind'] == 'fact' for item in rec.items) and not rec.trace['selected']['episodes']
                record.update(memory=rec.memory, memory_items=rec.items, trace=rec.trace,
                    memory_utf8_bytes=len(rec.memory.encode()), supplied_source_excerpts=0)
                host = WebChat(SimpleNamespace(req=system, models=models))
                response = host.chat.chat([dict(role='system', content=SYSTEM + '\n\n<retrieved_memory>\n' + rec.context + '\n</retrieved_memory>'),
                    dict(role='user', content=case['question'])], purpose='answer', max_tokens=cfg.memory.answer_max_tokens,
                    tags={'session_id': prefix + ':read:' + case['id'], 'case_id': case['id']})
                record.update(answer=response.text, finish_reason=response.finish_reason, model=response.model,
                    completed=bool(response.text.strip()) and response.finish_reason == 'stop')
                # Separate recorded observations from claims of semantic accuracy.
                record['event_statuses'] = sorted({f['semantics'].get('event_status', 'unknown') for f in record['facts']})
                print('SELF_CASE ' + case['id'] + ' completed=' + str(record['completed']) +
                    ' covered_literals=' + str(report.covered_literal_structures), flush=True)
            except Exception as exc:
                record.update(completed=False, error_type=type(exc).__name__, error=str(exc))
                print('SELF_CASE ' + case['id'] + ' error=' + type(exc).__name__, flush=True)
            results.append(record)
            save(out / 'real-results.json', results)
    finally:
        system.close()
        client = Qdrant(storage)
        removed = []
        for collection in client.request('GET', '/collections')['collections']:
            name = collection['name']
            if name.startswith(prefix + '_'):
                client.request('DELETE', '/collections/' + name)
                removed.append(name)
        usage = list(ledger.records())
        counts = Counter(r.purpose for r in usage)
        save(out / 'real-summary.json', dict(total=len(fixture['cases']), processed=len(results),
            completed=sum(r.get('completed', False) for r in results), purpose_calls=dict(counts),
            extract_calls=counts['extract'], answer_calls=counts['answer'], judge_calls=counts['judge'],
            original_excerpts=sum(r.get('supplied_source_excerpts', 0) for r in results), removed_collections=removed,
            pricing_known=all(r.cost_known for r in usage), source=frozen,
            limitation='Functional observations from self-authored scenarios; no accuracy or generalization estimate.'))
    if not all(r.get('completed') for r in results):
        raise RuntimeError('Functional run has incomplete cases; failures preserved, no question resampling')


if __name__ == '__main__':
    main()
