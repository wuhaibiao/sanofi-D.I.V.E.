#!/usr/bin/env python3
"""Fresh, isolated functional calls from frozen source-only inputs; no public dataset or judge."""
import argparse
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from check_webchat_reliability import credentials
from memsys.bench.provenance import model_manifest, prompt_fingerprint, source_manifest
from memsys.budget import budget_counter
from memsys.config import AppConfig
from memsys.core import MemorySystem
from memsys.llm import UsageLedger
from memsys.qdrant import Qdrant
from memsys.webchat import WebChat


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


def append(path, value):
    with path.open('a') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, default=str) + '\n')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Captured:
    def __init__(self, client, path):
        self.client, self.path = client, path
        self.config = getattr(client, 'config', None)

    def chat(self, messages, **kwargs):
        record = dict(purpose=kwargs['purpose'], tags=kwargs.get('tags', {}), messages=messages)
        append(self.path, dict(event='request', **record))
        result = self.client.chat(messages, **kwargs)
        append(self.path, dict(event='response', purpose=record['purpose'], tags=record['tags'], result=asdict(result)))
        return result


def snapshot(client, prefix):
    result = {}
    for item in client.request('GET', '/collections')['collections']:
        name = item['name']
        if name.startswith(prefix + '_'):
            continue
        hasher, offset, count = hashlib.sha256(), None, 0
        while True:
            page = client.scroll(name, {}, offset=offset)
            for point in page['points']:
                hasher.update(json.dumps(point, sort_keys=True, ensure_ascii=False).encode())
                count += 1
            offset = page.get('next_page_offset')
            if offset is None:
                break
        result[name] = dict(points=count, payload_sha256=hasher.hexdigest())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    plan = json.loads((out / 'plan-freeze.json').read_text())
    assert digest(out / 'fixture.frozen.json') == plan['fixture_sha256']
    write_inputs, read_inputs = out / 'write-inputs.json', out / 'read-inputs.json'
    assert write_inputs.exists() and read_inputs.exists(), 'Prepare phase-separated inputs before running'
    assert not (out / 'live-freeze.json').exists(), 'Preserve the original run; do not resume or resample a case'
    writes, reads = json.loads(write_inputs.read_text()), json.loads(read_inputs.read_text())
    assert len(writes) == len(reads) and [c['id'] for c in writes] == [c['id'] for c in reads]
    for case in writes:
        assert set(case) == {'id', 'sessions'}
        for session in case['sessions']:
            assert set(session) == {'at', 'turns'}
            assert all(set(t) == {'role', 'content'} and t['role'] in ('user', 'assistant') for t in session['turns'])
    assert all(set(c) == {'id', 'question', 'today'} for c in reads)
    runtime_path = Path.home() / 'Library/Application Support/memsys/runtime.json'
    runtime = json.loads(runtime_path.read_text())
    credentials(runtime_path)
    cfg = AppConfig.load(runtime['config_path'])
    assert cfg.memory.recall_mode == 'facts' and not cfg.memory.query_analysis
    prefix = 'memsys_fidelity_' + uuid.uuid4().hex[:12]
    assert re.fullmatch(r'memsys_fidelity_[0-9a-f]{12}', prefix)
    storage = replace(cfg.storage, collection_prefix=prefix, namespace='independent-fidelity-functions')
    client = Qdrant(storage)
    before = snapshot(client, prefix)
    ledger = UsageLedger(str(out / 'live-usage.jsonl'))
    models = cfg.build_models(ledger, env={}, probe=False)
    wrappers = {}
    def wrap(model):
        if model is not None and id(model) not in wrappers:
            wrappers[id(model)] = Captured(model, out / 'live-calls.jsonl')
        return wrappers.get(id(model))
    models.chat, models.memory = wrap(models.chat), wrap(models.memory)
    models.by_purpose = {k: wrap(v) for k, v in models.by_purpose.items()}
    source = source_manifest()
    frozen = dict(source=source, prompt_sha256=prompt_fingerprint(), prefix=prefix,
        inputs={p.name:digest(p) for p in (write_inputs, read_inputs)}, configuration_sha256=digest(Path(runtime['config_path'])),
        models=model_manifest(models), passes=2, cases_per_pass=len(writes), before=before,
        started_at=datetime.now(timezone.utc).isoformat(),
        method='Two predetermined fresh passes. One ordinary extraction per batch and one host answer per case. No expected-output fields are read by this process. No semantic judge or score-based retries.')
    save(out / 'live-freeze.json', frozen)
    system = MemorySystem(models, cfg.memory, trace_dir=str(out / 'live-traces'), storage=storage)
    host, counter = WebChat(SimpleNamespace(req=system, models=models)), budget_counter(cfg.memory)
    started, records = time.monotonic(), []
    try:
        for pass_number in (1, 2):
            for write, read in zip(writes, reads):
                assert source_manifest()['source_sha256'] == source['source_sha256'], 'Product changed during functional verification'
                record = dict(pass_number=pass_number, case_id=write['id'], completed=False)
                user = 'pass-%d:%s' % (pass_number, write['id'])
                tags = dict(case_id=write['id'], pass_number=str(pass_number))
                try:
                    reports = []
                    for n, session in enumerate(write['sessions']):
                        at = datetime.fromisoformat(session['at'])
                        handle = system.open_session(user, at, external_id='independent-source-%d' % n)
                        for i, turn in enumerate(session['turns']):
                            system.add_turn(handle, turn['role'], turn['content'], at, idempotency_key='source-%d' % i)
                        report, _ = system.close_session(handle, at, tags=tags)
                        reports.append(asdict(report))
                    store = system.store(user)
                    record.update(write_reports=reports, facts=[asdict(f) for f in store.facts()])
                    assert all(store.session(sid)['turns_done'] == store.session(sid)['next_idx'] for sid in
                               [s['id'] for s in store.records('session')]), 'Incomplete source ingestion'
                    today = datetime.fromisoformat(read['today'])
                    question = read['question']
                    available = system.answer_memory_budget(host.system_prompt, [dict(role='user', content=question)], today)
                    recall = system.recall(user, question, today, budget=min(cfg.memory.context_budget_tokens, available),
                        coverage_budget=available, tags={'question_id': user})
                    assert not recall.trace['selected']['episodes'] and all(i['kind']=='fact' for i in recall.items)
                    messages = [dict(role='system', content=host.system_prompt+'\n\n<retrieved_memory>\n'+recall.context+'\n</retrieved_memory>'),
                                dict(role='user', content=question)]
                    assert counter.messages(messages) <= cfg.memory.answer_input_budget_tokens
                    response = host.answer(messages, tags=tags)
                    record.update(question=question, memory=recall.memory, memory_items=recall.items, trace=recall.trace,
                        answer=response.text, finish_reason=response.finish_reason, generation_attempts=response.generation_attempts,
                        completed=bool(response.text.strip()) and response.finish_reason == 'stop')
                    assert len(response.generation_attempts) == 1
                    assert all(r['extract_calls'] == 1 for r in reports)
                except Exception as exc:
                    record.update(error_type=type(exc).__name__, error=str(exc))
                records.append(record)
                append(out / 'live-results.jsonl', record)
                print('FUNCTION pass=%d case=%s completed=%s' % (pass_number, write['id'], record['completed']), flush=True)
    finally:
        system.close()
        removed = []
        for item in client.request('GET', '/collections')['collections']:
            name = item['name']
            if name.startswith(prefix + '_'):
                client.request('DELETE', '/collections/'+name)
                removed.append(name)
        after = snapshot(client, prefix)
        usage = list(ledger.records())
        summary = dict(completed=sum(r['completed'] for r in records), total=len(writes)*2, processed=len(records),
            purpose_calls=dict(Counter(r.purpose for r in usage)), transport_retries=sum(r.retries for r in usage),
            original_source_excerpts=0,
            removed_collections=removed, unrelated_payloads_unchanged=before==after, before=before, after=after,
            final_source=source_manifest(), source_unchanged=source_manifest()['source_sha256']==source['source_sha256'],
            elapsed_s=round(time.monotonic()-started,3),
            limitation='Self-authored functional observations. Not a public benchmark, accuracy estimate or generalization proof.')
        save(out / 'live-summary.json', summary)
    if summary['completed'] != summary['total'] or not summary['unrelated_payloads_unchanged']:
        raise RuntimeError('Functional verification incomplete or storage integrity changed; all original outcomes preserved')


if __name__ == '__main__':
    main()
