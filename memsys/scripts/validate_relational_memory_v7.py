#!/usr/bin/env python3
"""Real-provider, authored-fixture protocol smoke in disposable Qdrant collections."""
import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from check_webchat_reliability import credentials
from memsys.bench.provenance import source_manifest
from memsys.config import AppConfig
from memsys.core import MemorySystem
from memsys.llm import UsageLedger
from memsys.qdrant import Qdrant
from memsys.webchat import SYSTEM


class Captured:
    def __init__(self, client, path, scope):
        self.client, self.path, self.scope = client, path, scope
        self.config = getattr(client, 'config', None)

    def chat(self, messages, **kwargs):
        tags = kwargs.get('tags') or {}
        key = tags.get('session_id') or str(uuid.uuid4())
        kwargs['tags'] = dict(tags, session_id=self.scope + ':' + kwargs['purpose'] + ':' + key)
        with self.path.open('a') as stream:
            stream.write(json.dumps(dict(purpose=kwargs['purpose'], messages=messages), ensure_ascii=False) + '\n')
        response = self.client.chat(messages, **kwargs)
        with self.path.with_name('responses.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(purpose=kwargs['purpose'], text=response.text,
                finish_reason=response.finish_reason), ensure_ascii=False) + '\n')
        return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--window-only', action='store_true')
    parser.add_argument('--runtime', type=Path, default=Path.home() / 'Library/Application Support/memsys/runtime.json')
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError('Keep prior smoke output; choose a new directory')
    args.out.mkdir(parents=True)
    fixture_path = ROOT / 'tests/fixtures/relational_memory_v7_synthetic.json'
    fixture = json.loads(fixture_path.read_text())
    source = source_manifest()
    report = dict(fixture_sha256=hashlib.sha256(fixture_path.read_bytes()).hexdigest(), source=source, cases=[], complete=False,
                  selected_case='window' if args.window_only else 'all')
    def save():
        (args.out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    save()
    runtime = json.loads(args.runtime.read_text())
    credentials(args.runtime)
    config = AppConfig.load(runtime['config_path'])
    config.memory = replace(config.memory, extract_max_tokens=10000, chat_window_turns=1, chat_window_tokens=1200)
    prefix = 'memsys_v7smoke_' + uuid.uuid4().hex[:12]
    config.storage = replace(config.storage, collection_prefix=prefix, namespace='authored-v7-protocol-smoke')
    ledger = UsageLedger(str(args.out / 'usage.jsonl'))
    models = config.build_models(ledger, env={}, probe=False)
    wrapped = {}
    def wrap(client):
        if client is not None and id(client) not in wrapped:
            wrapped[id(client)] = Captured(client, args.out / 'prompts.jsonl', prefix)
        return wrapped.get(id(client)) if client is not None else None
    models.chat, models.memory = wrap(models.chat), wrap(models.memory)
    models.by_purpose = {key: wrap(value) for key, value in models.by_purpose.items()}
    system = MemorySystem(models, config.memory, trace_dir=str(args.out / 'traces'), storage=config.storage)
    at = datetime.fromisoformat(fixture['at'])
    backend = Qdrant(config.storage)
    before = {row['name'] for row in backend.request('GET', '/collections')['collections']}
    try:
        if not args.window_only:
            case = fixture['cross_session']
            reports = []
            for source_session in case['sessions']:
                date = datetime.fromisoformat(source_session['at'])
                session = system.open_session('chain', date)
                system.add_turn(session, 'user', source_session['user'], date)
                write, _ = system.close_session(session, date)
                reports.append(dict(added=len(write.added), relations=write.relation_edges, warnings=write.relation_warnings))
            available = system.answer_memory_budget(SYSTEM, [dict(role='user', content=case['question'])], at)
            recall = system.recall('chain', case['question'], at,
                budget=min(config.memory.context_budget_tokens, available), coverage_budget=available)
            messages = [dict(role='system', content=SYSTEM + '\n\n<retrieved_memory>\n' + recall.context + '\n</retrieved_memory>'),
                        dict(role='user', content=case['question'])]
            response = (models.chat or models.memory).chat(messages, purpose='answer', max_tokens=config.memory.answer_max_tokens)
            row = dict(name='authored-cross-session', writes=reports, answer=response.text,
                source_excerpts=len(recall.trace['selected']['episodes']), relations=recall.trace.get('relations'),
                indexed_relations=len(system.store('chain').records('relation')),
                literal_present=case['required_answer_literal'].casefold() in response.text.casefold(),
                finish_reason=response.finish_reason)
            report['cases'].append(row)
            save()
            if not row['literal_present'] or row['source_excerpts'] or row['indexed_relations'] < 1 or response.finish_reason in ('length', 'incomplete'):
                raise AssertionError('Authored cross-session protocol smoke failed; original result retained')
        case = fixture['window']
        session = system.open_session('window', at)
        for message in case['history']:
            system.add_turn(session, message['role'], message['content'], at)
        reply = system.respond('window', case['question'], at, session=session)
        prompts = [json.loads(line) for line in (args.out / 'prompts.jsonl').read_text().splitlines()]
        answer_input = '\n'.join(message['content'] for message in [p for p in prompts if p['purpose'] == 'answer'][-1]['messages'])
        row = dict(name='authored-working-window', answer=reply.text,
            literal_present=case['required_answer_literal'].casefold() in reply.text.casefold(),
            old_user_message_absent=case['forbidden_old_user_message'] not in answer_input,
            processed_messages=system.store('window').session(session.session_id)['turns_done'],
            source_excerpts=len(reply.trace['selected']['episodes']))
        report['cases'].append(row)
        save()
        if not row['literal_present'] or not row['old_user_message_absent'] or row['processed_messages'] < 2 or row['source_excerpts']:
            raise AssertionError('Authored working-window protocol smoke failed; original result retained')
        report['complete'] = True
    finally:
        system.close()
        collections = {row['name'] for row in backend.request('GET', '/collections')['collections']}
        owned = {name for name in collections if name.startswith(prefix + '_')}
        for name in sorted(owned):
            backend.request('DELETE', '/collections/' + name)
        after = {row['name'] for row in backend.request('GET', '/collections')['collections']}
        report['cleanup'] = dict(deleted=sorted(owned), unrelated_collections_preserved=before <= after)
        report['source_unchanged'] = source_manifest()['source_sha256'] == source['source_sha256']
        save()
    print(json.dumps(dict(complete=report['complete'], cases=len(report['cases']), cleanup=report['cleanup']), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
