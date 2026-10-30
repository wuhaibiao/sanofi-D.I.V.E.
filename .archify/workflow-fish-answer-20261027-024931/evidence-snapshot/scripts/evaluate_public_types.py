#!/usr/bin/env python3
"""Fresh Qdrant replay: four questions per public dataset category.

Gold labels never enter storage, retrieval, or answer prompts. The answer adapter
uses the current WebChat prompt and pending-evidence policy, without persisting
benchmark answers into a shared source corpus. Write/read/judge use separate
processes. Existing production collections and product settings are untouched.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from check_webchat_reliability import credentials
from independent_data import convert_convomem
from blind_eval_inputs import materialize, load_phase
from memsys.bench.judge import anscheck_prompt, _grade
from memsys.bench.provenance import model_manifest, source_manifest
from memsys.budget import budget_counter
from memsys.config import AppConfig
from memsys.core import MemorySystem
from memsys.llm import UsageLedger
from memsys.qdrant import Qdrant
from memsys.webchat import WebChat, SYSTEM, commit

SEED = 'public-each-category-four-20261013-v1'
RESEARCH = ROOT / 'runs/accuracy-v78-count-field-feedback/new-evaluation-research'
CONVO = {
    'user-facts': ('user_evidence', [1, 2, 3, 4, 5, 6], 'single-session-user'),
    'assistant-facts': ('assistant_facts_evidence', [1, 2, 3, 4, 5, 6], 'single-session-assistant'),
    'changing-facts': ('changing_evidence', [2, 3, 4, 5, 6], 'knowledge-update'),
    'abstention': ('abstention_evidence', [1, 2, 3], 'single-session-user'),
    'preferences': ('preference_evidence', [1, 2], 'single-session-preference'),
    'implicit-connections': ('implicit_connection_evidence', [1, 2, 3], 'multi-session'),
}
LOCO = {1: 'multi-hop', 2: 'temporal', 3: 'open-domain', 4: 'single-hop', 5: 'adversarial'}


def read(path):
    return json.loads(Path(path).read_text())


def save(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + '.partial')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + '\n')
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def append(path, data):
    with Path(path).open('a') as stream:
        stream.write(json.dumps(data, ensure_ascii=False, default=str) + '\n')


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()] if Path(path).exists() else []


def fetch(url, path):
    if not path.exists():
        with urllib.request.urlopen(url, timeout=60) as response:
            path.write_bytes(response.read())
    return read(path)


def clean_long(row):
    return {**{key: row[key] for key in ('question_id', 'question_type', 'question', 'question_date', 'answer',
              'haystack_dates', 'haystack_session_ids')},
            'haystack_sessions': [[{key: turn[key] for key in ('role', 'content')} for turn in session]
                                  for session in row['haystack_sessions']]}


def convert_loco(conversation, qa, qid):
    data = conversation['conversation']
    keys = sorted((k for k in data if k.startswith('session_') and k[8:].isdigit()), key=lambda k: int(k[8:]))
    dates, sessions = [], []
    for key in keys:
        dates.append(datetime.strptime(data[key + '_date_time'], '%I:%M %p on %d %B, %Y').isoformat())
        text = ['Recorded conversation between human participants ' + data['speaker_a'] + ' and ' + data['speaker_b'] + '.']
        for turn in data[key]:
            if turn['speaker'] not in (data['speaker_a'], data['speaker_b']):
                raise ValueError('Unknown human speaker')
            text.append(turn['speaker'] + ': ' + turn['text'])
            if turn.get('blip_caption'):
                text.append('Released image caption: ' + turn['blip_caption'])
        sessions.append([dict(role='user', content='\n'.join(text))])
    answer = 'No information available in the conversation; the requested fact was not mentioned.' if qa['category'] == 5 else str(qa['answer'])
    return dict(question_id=qid, question_type=LOCO[qa['category']], question=qa['question'], answer=answer,
                question_date=max(dates), haystack_dates=dates, haystack_sessions=sessions,
                haystack_session_ids=[str(conversation['sample_id']) + '/' + key for key in keys])


def prepare(args):
    if (args.out / 'freeze.json').exists() or (args.out / 'sample.json').exists():
        raise ValueError('Sample already frozen; use a fresh output directory')
    args.out.mkdir(parents=True, exist_ok=True)
    raw = args.out / 'raw'
    raw.mkdir(exist_ok=True)
    rng = random.Random(SEED)
    selected, metadata, downloads = [], [], []
    original = ROOT / 'data/longmemeval_oracle.json'
    pool = read(original)
    categories = sorted({r['question_type'] for r in pool}) + ['abstention']
    for category in categories:
        candidates = sorted((r for r in pool if (str(r['question_id']).endswith('_abs') if category == 'abstention'
                            else r['question_type'] == category and not str(r['question_id']).endswith('_abs'))),
                            key=lambda r: str(r['question_id']))
        for row in rng.sample(candidates, 4):
            clean = clean_long(row)
            clean['question_id'] = 'lme_' + clean['question_id']
            selected.append(clean)
            metadata.append(dict(question_id=clean['question_id'], dataset='LongMemEval', category=category,
                judge_type=row['question_type'], abstention=category == 'abstention', source_question_id=row['question_id']))
    downloads.append(dict(dataset='LongMemEval', path=str(original), sha256=digest(original),
        url='https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json',
        scope='Existing local Oracle file; previously used development pool'))
    revision = read(RESEARCH / 'ConvoMem-revision.json')['sha']
    excluded = read(RESEARCH / 'schema-calibration-exclusion.json')['exclude_persona_from_final_sample']
    used = {excluded}
    choices = []
    for category, (folder, levels, judge_type) in CONVO.items():
        levels = list(levels)
        rng.shuffle(levels)
        for i in range(4):
            level = levels[i % len(levels)]
            sub = 'core_benchmark/evidence_questions/' + folder + '/' + str(level) + '_evidence'
            tree = fetch('https://huggingface.co/api/datasets/Salesforce/ConvoMem/tree/' + revision + '/' + sub + '?limit=1000',
                         raw / ('tree-' + category + '-' + str(level) + '.json'))
            files = sorted((r for r in tree if r['type'] == 'file' and r['path'].endswith('.json')
                            and Path(r['path']).stem not in used), key=lambda r: r['path'])
            entry = rng.choice(files)
            used.add(Path(entry['path']).stem)
            choices.append((category, judge_type, level, entry, rng.getrandbits(64)))
    def download(choice):
        category, judge_type, level, entry, row_seed = choice
        url = 'https://huggingface.co/datasets/Salesforce/ConvoMem/resolve/' + revision + '/' + urllib.parse.quote(entry['path'])
        path = raw / (category + '-' + Path(entry['path']).name)
        payload = fetch(url, path)
        data = payload['evidence_items']
        expected = entry.get('lfs', {}).get('oid')
        if expected and digest(path) != expected:
            raise ValueError('ConvoMem source checksum mismatch')
        index = random.Random(row_seed).randrange(len(data))
        qid = 'convo_' + hashlib.sha256((entry['path'] + ':' + str(index)).encode()).hexdigest()[:16]
        item = convert_convomem(data[index], judge_type, qid, datetime(2026, 10, 13))
        meta = dict(question_id=qid, dataset='ConvoMem', category=category, judge_type=judge_type,
                    abstention=category == 'abstention', source_path=entry['path'], row_index=index, evidence_level=level)
        proof = dict(dataset='ConvoMem', path=str(path), url=url, revision=revision, sha256=digest(path))
        return item, meta, proof
    with ThreadPoolExecutor(max_workers=6) as executor:
        for item, meta, proof in executor.map(download, choices):
            selected.append(item)
            metadata.append(meta)
            downloads.append(proof)
    loco_path = RESEARCH / 'locomo10.json'
    revision_loco = read(RESEARCH / 'LoCoMo-revision.json')
    if digest(loco_path) != revision_loco['sha256']:
        raise ValueError('LoCoMo pinned source changed')
    conversations = read(loco_path)
    # Four distinct complete conversation corpora, with one random QA per category per corpus.
    eligible = [i for i in range(1, len(conversations))
                if set(LOCO) <= {q['category'] for q in conversations[i]['qa']}]
    conversation_indexes = rng.sample(eligible, 4)
    for ci in conversation_indexes:
        conversation = conversations[ci]
        for category, label in LOCO.items():
            candidates = [(i, q) for i, q in enumerate(conversation['qa']) if q['category'] == category]
            index, qa = rng.choice(candidates)
            qid = 'loco_' + str(ci) + '_' + str(index)
            selected.append(convert_loco(conversation, qa, qid))
            metadata.append(dict(question_id=qid, dataset='LoCoMo', category=label,
                judge_type='temporal-reasoning' if category == 2 else 'multi-session', abstention=category == 5,
                scoring_reference=(str(qa.get('answer', '')).split(';')[0].strip() if category == 3 else None),
                conversation_index=ci, row_index=index, original_category=category))
    downloads.append(dict(dataset='LoCoMo', path=str(loco_path), **revision_loco))
    counts = Counter((m['dataset'], m['category']) for m in metadata)
    if len(selected) != 72 or len({r['question_id'] for r in selected}) != 72 or set(counts.values()) != {4}:
        raise ValueError('Require four unique questions per category and 72 total')
    # Exact source corpora may be shared, while questions/answers remain outside them.
    for item, meta in zip(selected, metadata):
        corpus = [item[key] for key in ('haystack_dates', 'haystack_sessions', 'haystack_session_ids')]
        meta['corpus_id'] = meta['dataset'].lower() + '-' + hashlib.sha256(json.dumps(corpus, ensure_ascii=False).encode()).hexdigest()[:16]
    save(args.out / 'sample.json', selected)
    save(args.out / 'selection.json', metadata)
    blind_hashes = materialize(args.out, selected, metadata)
    shutil.copy2(args.config, args.out / 'config.frozen.json')
    os.chmod(args.out / 'config.frozen.json', 0o600)
    shutil.copytree(ROOT / 'src/memsys', args.out / 'frozen-src/memsys', ignore=shutil.ignore_patterns('__pycache__'))
    freeze = dict(seed=SEED, source=source_manifest(), runner_sha256=digest(__file__), blind_input_sha256=blind_hashes,
        blind_input_builder_sha256=digest(Path(__file__).with_name('blind_eval_inputs.py')),
        config_sha256=digest(args.out / 'config.frozen.json'), sample_sha256=digest(args.out / 'sample.json'),
        selection_sha256=digest(args.out / 'selection.json'), prefix='memsys_public_' + uuid.uuid4().hex[:12],
        namespace='public-four-per-category', counts={d: dict(Counter(m['category'] for m in metadata if m['dataset'] == d))
            for d in ('LongMemEval', 'ConvoMem', 'LoCoMo')}, downloads=downloads,
        method='Seeded metadata-only sampling, four per category; LoCoMo four distinct noncalibration full corpora containing all five category labels, shared across categories; no answers, performance or input length used for selection.',
        answer_mode='Current WebChat fresh-conversation prompt, recall budget and pending-evidence policy; benchmark answers are not persisted into source memories.',
        score_mode='Uniform LLM Boolean semantic grading; configured answer model also acts as judge; not official leaderboard scores.',
        selected_at=datetime.now(timezone.utc).isoformat())
    save(args.out / 'freeze.json', freeze)
    print(json.dumps(dict(selected=72, counts=freeze['counts'], unique_corpora=len({m['corpus_id'] for m in metadata})), ensure_ascii=False), flush=True)


class CapturedChat:
    def __init__(self, client, path, session_scope=None):
        self.client, self.path, self.config = client, path, getattr(client, 'config', None)
        self.lock = threading.Lock()
        self.session_scope = session_scope

    def chat(self, messages, **kwargs):
        if self.session_scope:
            kwargs['tags'] = dict(kwargs.get('tags') or {})
            key = kwargs['tags'].get('session_id') or kwargs['tags'].get('question_id') or 'phase'
            kwargs['tags']['session_id'] = self.session_scope + ':' + key
        with self.lock:
            append(self.path, dict(purpose=kwargs['purpose'], messages=messages, tags=kwargs.get('tags', {})))
        return self.client.chat(messages, **kwargs)


def setup(args):
    freeze = read(args.out / 'freeze.json')
    for filename, field in [('config.frozen.json', 'config_sha256')]:
        if digest(args.out / filename) != freeze[field]:
            raise ValueError('Frozen input changed: ' + filename)
    metas, items = load_phase(args.out, freeze, args.phase, args.dataset)
    if source_manifest()['source_sha256'] != freeze['source']['source_sha256'] or digest(__file__) != freeze['runner_sha256']:
        raise ValueError('Frozen source or runner changed')
    if digest(Path(__file__).with_name('blind_eval_inputs.py')) != freeze.get('blind_input_builder_sha256'):
        raise ValueError('Frozen blind-input builder changed')
    credentials(args.runtime)
    config = AppConfig.load(args.out / 'config.frozen.json')
    if any(any(k in model.extra_body for k in ('previous_response_id', 'conversation', 'thread_id'))
           for model in config.models.values()):
        raise ValueError('Stateful provider links are forbidden in isolated evaluation')
    config.storage = replace(config.storage, collection_prefix=freeze['prefix'], namespace=freeze['namespace'])
    ledger = UsageLedger(str(args.out / (args.dataset + '-' + args.phase + '-usage.jsonl')))
    models = config.build_models(ledger, env={}, probe=False)
    wrappers = {}
    def wrap(client):
        if client is None:
            return None
        if id(client) not in wrappers:
            wrappers[id(client)] = CapturedChat(client, args.out / (args.dataset + '-' + args.phase + '-prompts.jsonl'),
                session_scope=freeze['prefix'] + ':' + args.phase)
        return wrappers[id(client)]
    models.chat, models.memory = wrap(models.chat), wrap(models.memory)
    models.by_purpose = {key: wrap(value) for key, value in models.by_purpose.items()}
    return config, models, ledger, metas, items


def write(args):
    config, models, ledger, metas, items = setup(args)
    system = MemorySystem(models, config.memory, str(args.out / 'write'), storage=config.storage)
    done = {r['corpus_id'] for r in rows(args.out / (args.dataset + '-write.jsonl')) if r.get('complete')}
    web = WebChat(SimpleNamespace(req=system, models=models, clock=lambda: datetime.now()))
    seen = set()
    started = time.monotonic()
    try:
        for item, _ in items:
            corpus = metas[item.question_id]['corpus_id']
            if corpus in seen or corpus in done:
                continue
            seen.add(corpus)
            store = system.store(corpus)
            report = dict(corpus_id=corpus, sessions=[], complete=False)
            for i, session in enumerate(item.sessions):
                print('WRITE ' + corpus + ' session ' + str(i + 1) + '/' + str(len(item.sessions)), flush=True)
                begin = time.monotonic()
                existing = store.record('benchmark_source', session.session_id)
                if existing and existing.get('finished'):
                    report['sessions'].append(existing['report'])
                    continue
                handle = system.open_session(corpus, session.date, session.session_id)
                sid = handle.session_id
                def originals():
                    for j, turn in enumerate(session.turns):
                        system.add_turn(handle, turn.role, turn.content, session.date, idempotency_key='source-' + str(j))
                    key = 'source-' + sid
                    store.put_record('web_conversation', key, dict(id=key, title='Benchmark source', session_ids=[sid],
                        created_at=session.date.isoformat(), updated_at=session.date.isoformat(), archived=False))
                commit(store, originals)
                issue = None
                extraction = None
                try:
                    extraction, lifecycle = system.close_session(handle, session.date, tags={'corpus_id': corpus})
                except Exception as exc:
                    issue = dict(type=type(exc).__name__, error=str(exc)[:1000])
                actual = [{key: t[key] for key in ('role', 'content')} for t in store.turns(sid)]
                expected = [dict(role=t.role, content=t.content) for t in session.turns]
                state = store.session(sid)
                proof = dict(session_id=sid, source_session_id=session.session_id, source_exact=actual == expected,
                    raw_messages=len(actual), processed_messages=state['turns_done'], status=state['status'], error=issue,
                    report=asdict(extraction) if extraction else None, elapsed_s=round(time.monotonic() - begin, 3))
                if not proof['source_exact']:
                    raise ValueError('Original source mismatch')
                finished = issue is None and state['status'] == 'closed' and state['turns_done'] == len(actual)
                store.put_record('benchmark_source', session.session_id, dict(finished=finished, report=proof))
                report['sessions'].append(proof)
            report.update(complete=all(s['error'] is None and s['status']=='closed' and
                s['processed_messages']==s['raw_messages'] for s in report['sessions']),
                counts=store.counts(), facts=[asdict(f) for f in store.facts()],
                pending_messages=sum(s['raw_messages'] - s['processed_messages'] for s in report['sessions']))
            append(args.out / (args.dataset + '-write.jsonl'), report)
    finally:
        system.close()
        save(args.out / (args.dataset + '-write-summary.json'), dict(pid=os.getpid(), elapsed_s=round(time.monotonic() - started, 3), usage=ledger.summary()))


def answer(args):
    config, models, ledger, metas, items = setup(args)
    system = MemorySystem(models, config.memory, str(args.out / 'read'), str(args.out / 'traces'), storage=config.storage)
    host = WebChat(SimpleNamespace(req=system, models=models))
    path = args.out / (args.dataset + '-answers.jsonl')
    done = {r['question_id'] for r in rows(path)}
    counter = budget_counter(config.memory)
    started = time.monotonic()
    try:
        for i, (item, _) in enumerate(items):
            if item.question_id in done:
                continue
            begin = time.monotonic()
            meta = metas[item.question_id]
            record = dict(question_id=item.question_id, dataset=args.dataset, category=meta['category'], question=item.question)
            try:
                store = system.store(meta['corpus_id'])
                for session in item.sessions:
                    source = store.record('benchmark_source', session.session_id)
                    if not source or not source.get('finished'):
                        raise ValueError('Source ingestion incomplete: ' + session.session_id)
                pending = host.pending_rounds(store, '__new_benchmark_question__')
                available = system.answer_memory_budget(host.system_prompt, [dict(role='user', content=item.question)], item.question_date)
                recall = system.recall(meta['corpus_id'], item.question, item.question_date,
                    budget=min(config.memory.context_budget_tokens, available), coverage_budget=available,
                    tags={'question_id': item.question_id})
                messages = [dict(role='system', content=host.system_prompt + '\n\n<retrieved_memory>\n' + recall.context + '\n</retrieved_memory>'),
                            dict(role='user', content=item.question)]
                if counter.messages(messages) > config.memory.answer_input_budget_tokens:
                    raise ValueError('Answer input budget exceeded')
                messages, recent = host.include_pending(messages, pending, counter)
                if config.memory.recall_mode == 'facts':
                    if any(row.get('kind') != 'fact' for row in recall.items) or recent or pending:
                        raise ValueError('Facts-only evaluation received original conversation evidence')
                    if recall.trace['selected']['episodes'] or 'Past conversation excerpts' in recall.memory:
                        raise ValueError('Facts-only evaluation included source excerpts')
                record.update(memory_items=recall.items, pending_evidence_items=len(recent),
                    input_units=counter.messages(messages), recall_mode=config.memory.recall_mode)
                response = host.answer(messages, recall.items,
                    tags={'question_id': item.question_id, 'dataset': args.dataset},memory=recall.memory)
                record.update(hypothesis=response.text.strip(),finish_reason=response.finish_reason,actual_model=response.model)
                record['answer_evidence'] = getattr(response,'evidence_audit',{})
                if not response.text.strip() or response.finish_reason in ('length', 'incomplete', 'content_filter', 'refusal'):
                    raise ValueError('Empty, truncated or blocked answer: ' + str(response.finish_reason))
                record.update(status='answered')
            except Exception as exc:
                record.update(status='error', error_type=type(exc).__name__, error=str(exc)[:1000])
            record['elapsed_s'] = round(time.monotonic() - begin, 3)
            append(path, record)
            print('READ ' + str(i + 1) + '/' + str(len(items)) + ' ' + item.question_id + ' ' + record['status'] + ' ' + str(record['elapsed_s']) + 's', flush=True)
    finally:
        system.close()
        save(args.out / (args.dataset + '-read-summary.json'), dict(pid=os.getpid(), elapsed_s=round(time.monotonic() - started, 3), models=model_manifest(models), usage=ledger.summary()))


def judge(args):
    config, models, ledger, metas, items = setup(args)
    golds = {item.question_id: gold for item, gold in items}
    path = args.out / (args.dataset + '-grades.jsonl')
    done = {r['question_id'] for r in rows(path) if isinstance(r.get('correct'), bool)}
    for answer in rows(args.out / (args.dataset + '-answers.jsonl')):
        qid = answer['question_id']
        if qid in done:
            continue
        meta, gold = metas[qid], golds[qid]
        record = dict(question_id=qid, dataset=args.dataset, category=meta['category'], reference=gold.answer)
        if answer['status'] != 'answered':
            record.update(status='answer_error', correct=False)
        else:
            prompt = anscheck_prompt(meta['judge_type'], gold.question,
                meta.get('scoring_reference') or gold.answer, answer['hypothesis'], abstention=meta['abstention'])
            try:
                label, actual_model, attempts = _grade(models.chat or models.memory, prompt, qid)
                record.update(status='graded', correct=label, actual_model=actual_model, attempts=attempts)
            except Exception as exc:
                record.update(status='judge_error', correct=None, error=str(exc)[:1000])
        append(path, record)
        print('JUDGE ' + qid + ' ' + str(record['correct']), flush=True)


def report(args):
    selection = read(args.out / 'selection.json')
    summary = dict(total=len(selection), datasets={}, elapsed_by_phase={}, limitation='Small stratified public diagnostic; Oracle/evidence-only inputs for LongMemEval/ConvoMem, full corpora for LoCoMo; same answer model used for semantic judging; no official leaderboard claim.')
    detail = []
    for dataset in ('LongMemEval', 'ConvoMem', 'LoCoMo'):
        answers = {r['question_id']: r for r in rows(args.out / (dataset + '-answers.jsonl'))}
        grades = {r['question_id']: r for r in rows(args.out / (dataset + '-grades.jsonl'))}
        subset = [m for m in selection if m['dataset'] == dataset]
        categories = {}
        for category in sorted({m['category'] for m in subset}):
            chosen = [m for m in subset if m['category'] == category]
            records = [grades.get(m['question_id'], {}) for m in chosen]
            categories[category] = dict(correct=sum(r.get('correct') is True for r in records), total=4,
                answered=sum(answers.get(m['question_id'], {}).get('status') == 'answered' for m in chosen),
                graded=sum(isinstance(r.get('correct'), bool) for r in records))
        writes = rows(args.out / (dataset + '-write.jsonl'))
        summary['datasets'][dataset] = dict(correct=sum(r.get('correct') is True for r in grades.values()), total=len(subset),
            answered=sum(a.get('status') == 'answered' for a in answers.values()), by_category=categories,
            failed_extract_sessions=sum(s.get('error') is not None or bool((s.get('report') or {}).get('extract_failures'))
                for w in writes for s in w['sessions']), pending_messages=sum(w['pending_messages'] for w in writes),
            degraded_answers=sum(a.get('pending_evidence_items', 0) > 0 for a in answers.values()))
        for meta in subset:
            detail.append(dict(**meta, answer=answers.get(meta['question_id']), grade=grades.get(meta['question_id'])))
        for phase in ('write', 'read'):
            p = args.out / (dataset + '-' + phase + '-summary.json')
            if p.exists():
                summary['elapsed_by_phase'][dataset + '-' + phase] = read(p)['elapsed_s']
    summary['correct'] = sum(d['correct'] for d in summary['datasets'].values())
    summary['answered'] = sum(d['answered'] for d in summary['datasets'].values())
    summary['complete'] = summary['answered'] == summary['total'] and all(c['graded'] == 4 for d in summary['datasets'].values() for c in d['by_category'].values())
    usage = UsageLedger()
    for path in args.out.glob('*-usage.jsonl'):
        for record in UsageLedger.from_jsonl(str(path)).records():
            usage.record(record)
    summary['usage'] = usage.summary()
    summary['pricing_known'] = all(r.cost_known for r in usage.records())
    save(args.out / 'summary.json', summary)
    save(args.out / 'answers-and-grades.json', detail)
    lines = ['# 每个公开评测集每题型四题评测', '', '固定随机样本；当前本机配置与源码；写入、回答和评分独立进程。', '',
             '| 评测集 | 正确 | 回答完成 |', '|---|---:|---:|']
    for dataset, data in summary['datasets'].items():
        lines.append('| ' + dataset + ' | ' + str(data['correct']) + '/' + str(data['total']) + ' | ' + str(data['answered']) + '/' + str(data['total']) + ' |')
    lines += ['', '| 评测集 | 题型 | 正确 | 已评分 |', '|---|---|---:|---:|']
    for dataset, data in summary['datasets'].items():
        for category, score in data['by_category'].items():
            lines.append('| ' + dataset + ' | ' + category + ' | ' + str(score['correct']) + '/4 | ' + str(score['graded']) + '/4 |')
    lines += ['', '## 范围与限制', '', summary['limitation'], '', '抽取失败、待整理消息与临时原文使用数量详见 summary.json。货币费用在价格未配置时保持未知。', '', '## 未通过题目', '']
    for row in detail:
        if not (row.get('grade') or {}).get('correct'):
            lines += ['### ' + row['question_id'] + ' · ' + row['dataset'] + ' / ' + row['category'], '',
                      '**问题：** ' + (row.get('answer') or {}).get('question', ''), '',
                      '**标准答案：** ' + (row.get('grade') or {}).get('reference', ''), '',
                      '**实际回答：** ' + (row.get('answer') or {}).get('hypothesis', (row.get('answer') or {}).get('error', '未完成')), '']
    (args.out / 'report.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({k: summary[k] for k in ('correct', 'total', 'answered', 'complete')}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path.home() / 'Library/Application Support/memsys/config.json')
    parser.add_argument('--runtime', type=Path, default=Path.home() / 'Library/Application Support/memsys/runtime.json')
    parser.add_argument('--phase', choices=('prepare', 'write', 'read', 'judge', 'report', 'all'), default='all')
    parser.add_argument('--dataset', choices=('LongMemEval', 'ConvoMem', 'LoCoMo'))
    args = parser.parse_args()
    args.out = args.out.resolve()
    if args.phase != 'all':
        return {'prepare': prepare, 'write': write, 'read': answer, 'judge': judge, 'report': report}[args.phase](args)
    prepare(args)
    started = time.monotonic()
    outcomes = []
    for dataset in ('LongMemEval', 'ConvoMem', 'LoCoMo'):
        for phase in ('write', 'read', 'judge'):
            command = [sys.executable, str(Path(__file__).resolve()), '--out', str(args.out), '--phase', phase,
                       '--dataset', dataset, '--runtime', str(args.runtime)]
            print('START ' + dataset + ' ' + phase, flush=True)
            with (args.out / (dataset + '-' + phase + '.log')).open('a') as stream:
                result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, cwd=ROOT)
            outcomes.append(dict(dataset=dataset, phase=phase, exit_code=result.returncode))
            save(args.out / 'execution.json', dict(started_at=read(args.out / 'freeze.json')['selected_at'],
                elapsed_s=round(time.monotonic() - started, 3), outcomes=outcomes))
            print('DONE ' + dataset + ' ' + phase + ' exit=' + str(result.returncode), flush=True)
            if result.returncode:
                report(args)
                return result.returncode
        report(args)
    report(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
