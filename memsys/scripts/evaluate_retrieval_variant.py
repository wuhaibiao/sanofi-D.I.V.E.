"""Frozen retrieval-only comparison: copy one fact store, optionally reindex, answer, then judge.

Reference answers reach only the judge. Existing answers, labels and traces are never copied.
Use separate output directories for each variant and sample; resume only identical identities.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import hashlib
import json
import sqlite3
import sys
import threading
import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class ResponseReplay:
    """Reuse a completed response only for an identical model request.

    This is an experimental control, not a production cache. Actual requests
    remain in the underlying ledger; replay events are reported separately.
    Never cache failed or truncated responses.
    """

    def __init__(self, model, directory, events):
        self.model = model
        self.config = model.config
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events = events
        self.lock = threading.Lock()

    def chat(self, messages, **kwargs):
        from memsys.llm import ChatResult
        from memsys.bench.provenance import model_details
        # Per-call deadlines/retry limits are transport controls, not sampling
        # inputs. Keep all generation parameters and the fixed model identity.
        # Version the key so historical caches with weaker completion checks
        # cannot silently enter a new controlled experiment.
        request = {"schema": 2, "model": model_details(self.model), "messages": messages,
                   "arguments": {k: v for k, v in kwargs.items()
                                 if k not in ("tags", "timeout_s", "max_retries")}}
        key = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = self.directory / (key + '.json')
        with (self.directory / (key + '.lock')).open('a') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            if path.exists():
                reply = ChatResult(**json.loads(path.read_text()))
                replayed = True
            else:
                reply = self.model.chat(messages, **kwargs)
                replayed = False
                if (reply.finish_reason in ('stop', 'completed', 'end_turn')
                        and isinstance(reply.text,str) and reply.text.strip()):
                    temporary = path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(dataclasses.asdict(reply), ensure_ascii=False))
                    temporary.replace(path)
        event = {'request_sha256': key, 'purpose': kwargs.get('purpose'),
                 'question_id': (kwargs.get('tags') or {}).get('question_id'), 'replayed': replayed}
        with self.lock, Path(self.events).open('a') as output:
            output.write(json.dumps(event) + '\n')
        return reply


def install_frozen_plans(path, items):
    """Replace only query planning; check that each plan belongs to this input."""
    from datetime import date
    import memsys.core as core
    from memsys.read import QueryPlan
    from memsys.timeutil import DateSpan
    records = json.loads(Path(path).read_text())
    for item in items:
        row = records.get(item.question_id)
        if not row or row['question'] != item.question or row['question_date'] != item.question_date.isoformat():
            raise ValueError('Frozen plan input mismatch: ' + item.question_id)

    def frozen(models, question, today, settings, tags=None, **kwargs):
        row = records[(tags or {})['question_id']]
        if row['question'] != question or row['question_date'] != today.isoformat():
            raise ValueError('Frozen plan call mismatch')
        values = deepcopy(row['plan'])
        if values['time_range']:
            values['time_range'] = DateSpan(*(date.fromisoformat(v) for v in values['time_range']), precision='day')
        return QueryPlan(**values)

    core.plan_query = frozen


def install_rerank_replay(directory):
    """Replay measured local scores only after reproducing every input window.

    Tokenization is repeated with the pinned tokenizer. Changed text, windows,
    precision or model identity always executes the real reranker instead.
    This makes quality comparisons economical; replayed timing is not a live
    performance measurement.
    """
    import memsys.reranking as module
    from memsys.model_tokens import ModelTokenizer, bounded_spans
    original = module.local_reranker
    records = {row['message']: row['reranking'] for path in Path(directory).glob('*.json')
               for row in [json.loads(path.read_text())] if row.get('reranking', {}).get('backend') == 'bge'}
    tokenizers, tokenizer_lock = {}, threading.Lock()

    class Replay:
        def __init__(self, settings): self.settings = settings
        def rank(self, question, documents):
            s = self.settings
            saved = records.get(question)
            if saved and all(saved.get(k) == getattr(s, 'rerank_' + k) for k in ('model','revision','dtype')):
                identity = (s.rerank_model, s.rerank_tokenizer_revision)
                with tokenizer_lock:
                    if identity not in tokenizers:
                        tokenizers[identity] = ModelTokenizer(*identity)
                    tokenizer = tokenizers[identity]
                windows = []
                for doc in documents:
                    header = doc.header
                    if doc.question_context:
                        end = next(bounded_spans(doc.question_context, tokenizer.count,
                                   min(64, s.rerank_max_tokens // 4)))[1]
                        header += 'User question: ' + doc.question_context[:end] + '\n'
                    count = lambda text: tokenizer.pair_count(question, header + text)
                    for start, end in bounded_spans(doc.text, count, s.rerank_max_tokens, s.rerank_overlap_tokens):
                        windows.append(dict(key=doc.key, text=header+doc.text[start:end],
                            source_start=doc.source_start+start, source_end=doc.source_start+end,
                            source_role=doc.source_role, pair_tokens=count(doc.text[start:end])))
                keys = ('key','text','source_start','source_end','source_role','pair_tokens')
                measured = [{k: w[k] for k in keys} for w in saved['windows']]
                if windows == measured and tokenizer.manifest() == saved['tokenizer']:
                    trace = deepcopy(saved)
                    trace.update(score_replay=True, queue_s=0, inference_and_load_s=0,
                                 timing_scope='replayed score; excluded from live inference comparison')
                    return dict(saved['scores']), trace
            return original(s).rank(question, documents)

    module.local_reranker = Replay


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_identity(path):
    connection=sqlite3.connect('file:'+str(Path(path).resolve())+'?mode=ro',uri=True)
    connection.row_factory=sqlite3.Row
    try:
        names={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        values={}
        for table in ('sessions','turns','episodes','facts','fact_sources','fact_semantics','fact_spans','episode_views','turn_views','conversation_exclusions'):
            if table in names:
                values[table]=[{k:v for k,v in dict(row).items() if k not in ('access_count','last_access')}
                               for row in connection.execute('SELECT * FROM '+table+' ORDER BY rowid')]
        return hashlib.sha256(json.dumps(values,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    finally:
        connection.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--sample',type=Path,required=True)
    parser.add_argument('--stores',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--reindex',action='store_true')
    parser.add_argument('--plans',type=Path,help='Frozen query plans bound to question text and date')
    parser.add_argument('--recall-only',action='store_true',help='Inspect retrieval without generation or judging')
    parser.add_argument('--response-cache',type=Path,help='Shared completed-response cache for controlled ablations')
    parser.add_argument('--rerank-traces',type=Path,help='Replay local scores only when all input windows match')
    parser.add_argument('--sample-status',default='previously used regression sample',
                        help='Cohort status at selection, e.g. previously unanswered diagnostic sample')
    args=parser.parse_args()
    sys.path.insert(0,str(args.source.resolve()))
    from memsys.cli import load_dotenv
    from memsys.config import AppConfig
    from memsys.core import MemorySystem, _safe_name
    from memsys.llm import UsageLedger, ModelError
    from memsys.bench.longmemeval import load
    from memsys.bench.jsonl import read_jsonl, append_jsonl
    from memsys.bench.judge import judge
    from memsys.bench.report import build_report
    from memsys.bench.provenance import source_manifest, prompt_fingerprint, model_manifest
    load_dotenv()
    config=AppConfig.load(str(args.config))
    if config.fallbacks:
        raise SystemExit('Quality comparison requires fixed models without automatic fallbacks.')
    if getattr(config.memory,'rerank_backend','llm')=='bge' and not config.memory.rerank_strict:
        raise SystemExit('BGE quality comparison requires rerank_strict=true.')
    pairs=load(str(args.sample)); items=[item for item,_ in pairs]
    if args.plans:
        install_frozen_plans(args.plans, items)
    if args.rerank_traces:
        install_rerank_replay(args.rerank_traces)
    origins={item.question_id:source_identity(args.stores/(_safe_name(item.question_id)+'.sqlite')) for item in items}
    identity={'source':source_manifest(),'prompts':prompt_fingerprint(),'config_sha256':digest(args.config),
              'sample_sha256':digest(args.sample),'frozen_fact_stores':origins,'reindex':args.reindex,
              'workers':args.workers,'sample_status':args.sample_status,
              'scope':'retrieval isolation; no extraction or resolve calls; not end-to-end writing quality'}
    if args.plans or args.recall_only or args.response_cache or args.rerank_traces:
        identity.update(plans_sha256=digest(args.plans) if args.plans else None,
                        recall_only=args.recall_only, experiment_runner_sha256=digest(__file__),
                        response_control='first completed response per identical model request' if args.response_cache else 'live')
        if args.rerank_traces:
            identity['rerank_trace_sha256'] = {p.name:digest(p) for p in sorted(args.rerank_traces.glob('*.json'))}
    args.out.mkdir(parents=True,exist_ok=True)
    frozen=args.out/'freeze.json'
    if frozen.exists() and json.loads(frozen.read_text()) != identity:
        raise SystemExit('Frozen identity changed; use a new output directory.')
    frozen.write_text(json.dumps(identity,ensure_ascii=False,indent=2))
    stores=args.out/'stores'; stores.mkdir(exist_ok=True)
    for item in items:
        destination=stores/(_safe_name(item.question_id)+'.sqlite')
        if destination.exists():
            if source_identity(destination)!=origins[item.question_id]:
                raise SystemExit('Copied source changed for '+item.question_id)
            continue
        original=sqlite3.connect('file:'+str((args.stores/destination.name).resolve())+'?mode=ro',uri=True)
        copied=sqlite3.connect(str(destination))
        try:
            original.backup(copied)
        finally:
            original.close();copied.close()
    ledger=UsageLedger(str(args.out/'usage.jsonl')); models=config.build_models(ledger,probe=False)
    if args.response_cache:
        models.chat=ResponseReplay(models.chat,args.response_cache,args.out/'response-events.jsonl')
    run_config={'mode':'memory','questions':len(items),'models':models.names,'embedding':models.embedding_name,
                'memory':config.memory.to_dict(),'model_settings':model_manifest(models),'provenance':source_manifest(),
                'write_fingerprint':config.memory.write_fingerprint(),'prompt_fingerprint':prompt_fingerprint(),
                'data':{'file':str(args.sample),'sha256':identity['sample_sha256']},'comparison':identity['scope']}
    (args.out/'run_config.json').write_text(json.dumps(run_config,ensure_ascii=False,indent=2))
    answers=args.out/('recalls.jsonl' if args.recall_only else 'hypotheses.jsonl')
    done={r['question_id'] for r in read_jsonl(str(answers))}
    lock=threading.Lock(); errors=[]; aborted=threading.Event()
    def one(item):
        if item.question_id in done or aborted.is_set():
            return
        system=MemorySystem(models,config.memory,data_dir=str(stores),trace_dir=str(args.out/'traces'))
        began=time.monotonic()
        try:
            if args.reindex:
                system.reindex(item.question_id)
            indexed=time.monotonic()
            operation=system.recall if args.recall_only else system.respond
            reply=operation(item.question_id,item.question,item.question_date,tags={'question_id':item.question_id})
            elapsed=time.monotonic()-indexed
            if source_identity(stores/(_safe_name(item.question_id)+'.sqlite'))!=origins[item.question_id]:
                raise ValueError('retrieval unexpectedly changed original facts or lifecycle')
            t=reply.trace
            timing={'question_id':item.question_id,'mode':'memory','ingest_s':indexed-began,'answer_s':elapsed,
                    'cached_ingest':True,'reindexed':args.reindex,'plan_s':t['timings']['plan_s'],
                    'search_s':t['timings']['search_s'],'rerank_s':t['timings']['rerank_s'],
                    'generate_s':t['timings'].get('answer_s',0),'context_tokens':t['context_tokens'],
                    'history_tokens':t['history_tokens'],'memory_tokens':t['memory_tokens'],
                    'effective_budget':t['effective_budget'],'budget_counter':t.get('budget_counter'),
                    'aggregation':t.get('aggregation'),'index_coverage':t.get('index_coverage'),
                    'reranking':{k:v for k,v in t.get('reranking',{}).items() if k not in ('windows','scores')},
                    'frozen_fact_store_sha256':origins[item.question_id]}
            with lock:
                append_jsonl(str(answers),{'question_id':item.question_id,
                    **({'memory':reply.memory} if args.recall_only else {'hypothesis':reply.text})})
                append_jsonl(str(args.out/'timings.jsonl'),timing)
                print('answered '+item.question_id,flush=True)
        except Exception as exc:
            if isinstance(exc,ModelError) and exc.account_problem:
                aborted.set()
            with lock:
                errors.append(item.question_id)
                append_jsonl(str(args.out/'errors.jsonl'),{'question_id':item.question_id,'error':type(exc).__name__+': '+str(exc)})
                print('failed '+item.question_id+': '+type(exc).__name__+': '+str(exc)[:180],flush=True)
        finally:
            system.close()
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        list(executor.map(one,items))
    if source_manifest()!=identity['source'] or prompt_fingerprint()!=identity['prompts'] or digest(args.config)!=identity['config_sha256']:
        raise SystemExit('Source, prompts or configuration changed during the experiment.')
    answered={r['question_id'] for r in read_jsonl(str(answers))}
    if len(answered)!=len(items):
        (args.out/'status.json').write_text(json.dumps({'status':'incomplete','answered':len(answered),'total':len(items),'errors':errors}))
        return 1
    if args.recall_only:
        if any(r.purpose in ('extract','resolve','query','answer','judge') for r in ledger.records()):
            raise SystemExit('Retrieval-only inspection unexpectedly called a cloud model.')
        (args.out/'status.json').write_text(json.dumps({'status':'complete','recalled':len(answered),'total':len(items)}))
        return 0
    # Answers are complete before any reference is sent to the model.
    golds={gold.question_id:gold for _,gold in pairs}
    name=config.roles['judge']
    judge_model=config.client(name,ledger)
    if args.response_cache:
        judge_model=ResponseReplay(judge_model,args.response_cache,args.out/'response-events.jsonl')
    evaluation=judge(str(answers),golds,judge_model,name,workers=args.workers,
                     progress=lambda text:print(text,flush=True))
    (args.out/'report.md').write_text(build_report(str(args.out),golds,evaluation))
    labels=read_jsonl(evaluation)
    if any(r.purpose in ('extract','resolve') for r in ledger.records()):
        raise SystemExit('Retrieval comparison unexpectedly called extraction/resolve.')
    (args.out/'status.json').write_text(json.dumps({'status':'complete','answered':len(answered),
        'judged':len(labels),'correct':sum(bool(r['autoeval_label']['label']) for r in labels),'total':len(items)}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
