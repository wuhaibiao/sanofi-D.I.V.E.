"""Prepare fresh memory stores without consuming the separate read budget.

Uses the normal Runner ingestion and cache fingerprint. Gold labels are removed
by the benchmark loader before any item reaches the memory system.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def original_coverage(path, item):
    with sqlite3.connect('file:'+str(path.resolve())+'?mode=ro', uri=True) as connection:
        rows = connection.execute('SELECT external_id,id FROM sessions').fetchall()
        sessions = dict(rows)
        if len(sessions) != len(rows) or set(sessions) != {s.session_id for s in item.sessions}:
            raise ValueError('Original session coverage differs from replay input')
        turns = 0
        for session in item.sessions:
            actual = connection.execute('SELECT role,content FROM turns WHERE session_id=? ORDER BY idx',
                                        (sessions[session.session_id],)).fetchall()
            expected = [('user' if t.role == 'user' else 'assistant', t.content) for t in session.turns]
            if actual != expected:
                raise ValueError('Original turn coverage differs from replay input')
            turns += len(actual)
    return dict(sessions=len(rows), turns=turns, exact_originals=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iteration', type=Path, required=True)
    parser.add_argument('--max-wall-s', type=float, default=7200)
    args = parser.parse_args()
    if args.max_wall_s <= 0:
        parser.error('write budget must be positive')
    iteration = args.iteration.resolve()
    state = json.loads((iteration/'freeze.json').read_text())
    sys.path.insert(0, str(iteration/'frozen-src'))
    from memsys.bench.jsonl import append_jsonl, read_jsonl
    from memsys.bench.longmemeval import load
    from memsys.bench.provenance import source_manifest, prompt_fingerprint
    from memsys.bench.runner import Runner
    from memsys.cli import load_dotenv
    from memsys.config import AppConfig
    from memsys.llm import UsageLedger

    sample = iteration/state['sample_file']
    config = iteration/'config.frozen.json'
    identity = dict(source=source_manifest()['source_sha256'], prompts=prompt_fingerprint(),
                    sample_sha256=sha(sample), config_sha256=sha(config), runner_sha256=sha(__file__))
    if (identity['source'] != state['source']['source_sha256'] or identity['prompts'] != state['prompts']
            or identity['sample_sha256'] != state['sample_sha256']
            or identity['config_sha256'] != state['config_sha256']):
        raise SystemExit('Frozen source, prompt, configuration or sample changed')
    run = iteration/'current'
    run.mkdir(exist_ok=True)
    if (run/'hypotheses.jsonl').exists():
        raise SystemExit('Prepare ingestion before answering; do not rebuild a scored run')
    manifest = iteration/'ingestion.json'
    if manifest.exists() and json.loads(manifest.read_text())['identity'] != identity:
        raise SystemExit('Ingestion identity changed; use a fresh iteration')
    load_dotenv(str(Path(__file__).resolve().parents[1]/'.env'))
    c = AppConfig.load(str(config))
    models = c.build_models(UsageLedger(str(run/'ingest-usage.jsonl')))
    runner = Runner(models, c.memory, str(run), reingest=False, max_wall_s=args.max_wall_s)
    items = [item for item, _ in load(str(sample))]
    recorded = {r['question_id']:r for r in read_jsonl(str(run/'ingestion.jsonl'))}
    completed, failures = {}, []
    started = time.monotonic()
    previous, consecutive = None, 0

    def save(reason=None):
        report = dict(identity=identity, status='completed' if len(completed)==len(items) else 'incomplete',
                      expected=len(items), completed=len(completed), results=list(completed.values()),
                      failures=failures, reason=reason, wall_s=round(time.monotonic()-started,3),
                      max_wall_s=args.max_wall_s, scope='Actual frozen-model ingestion; no answers or grades generated.')
        manifest.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')

    reason = None
    try:
        for item in items:
            if time.monotonic()-started >= args.max_wall_s or consecutive >= 2:
                reason = 'write budget exhausted' if consecutive < 2 else 'two consecutive failures of the same kind'
                break
            path = run/'stores'/(item.question_id+'.sqlite')
            if item.question_id in recorded:
                row = recorded[item.question_id]
                if sha(path) != row['store_sha256']:
                    raise ValueError('Previously recorded memory store changed')
                original_coverage(path,item)
                completed[item.question_id] = row
                continue
            t0 = time.monotonic()
            try:
                answer, timing = runner._memory(item, ingest_only=True)
                if answer is not None:
                    raise AssertionError('Ingestion unexpectedly generated an answer')
                coverage = timing['write_coverage']
                incomplete = {k:coverage.get(k) for k in ('extract_truncated_rounds','pending_extract_rounds',
                              'resolve_fallbacks','resolve_errors') if coverage.get(k)}
                if incomplete:
                    raise ValueError('Incomplete write coverage: '+json.dumps(incomplete,ensure_ascii=False))
                row = dict(question_id=item.question_id, **timing, original_coverage=original_coverage(path,item),
                           store_sha256=sha(path))
                append_jsonl(str(run/'ingestion.jsonl'),row)
                completed[item.question_id] = row
                previous, consecutive = None, 0
                print(json.dumps(dict(question_id=item.question_id,status='ingested',
                                      wall_s=round(time.monotonic()-t0,3))),flush=True)
            except Exception as exc:
                kind = (type(exc).__name__,getattr(exc,'status',None))
                consecutive = consecutive+1 if previous==kind else 1
                previous = kind
                row = dict(question_id=item.question_id,error=str(exc),kind=kind,wall_s=round(time.monotonic()-t0,3))
                failures.append(row)
                append_jsonl(str(run/'ingest-errors.jsonl'),row)
                print(json.dumps(row,ensure_ascii=False),flush=True)
                if getattr(exc,'account_problem',False):
                    reason = 'account access failure'
                    break
            save()
    finally:
        save(reason)
    return 0 if len(completed)==len(items) else 1


if __name__ == '__main__':
    raise SystemExit(main())
