"""Run native Mem0 OSS with the project's frozen models, budgets and evaluator.

This is a whole-system comparison: Mem0 uses native additive extraction/search
and a single answer call; memsys retains its provenance/calculation review.
Scores are labelled as such, rather than attributing all differences to retrieval.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if not os.environ.get('PYTHONPATH'):
    sys.path.insert(0,str(ROOT/'src'))

from memsys import prompts
from memsys.bench.longmemeval import load
from memsys.bench.provenance import fingerprint
from memsys.bench.runner import Runner,_done_ids
from memsys.budget import budget_counter
from memsys.config import AppConfig
from memsys.llm import UsageLedger
from memsys.read import effective_budget,parse_answer
from memsys.read_budget import ReadBudget,read_scope
from memsys.read_retry import ReadTransportChat
from memsys.timeutil import format_day

from mem0_bridge import close_memory,memory_for,prohibit_mem0_execution


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def pack_memories(rows,counter,limit):
    """Keep native ranking but never truncate an atomic extracted memory."""
    kept=[]
    omitted=[]
    lines=['## Retrieved memories']
    for index,row in enumerate(rows,1):
        metadata=row.get('metadata') or {}
        observed=metadata.get('recorded_at','unknown')
        line='[M%d] recorded %s · %s'%(index,observed,row.get('memory',''))
        trial='\n'.join(lines+[line])
        if counter.count(trial)>limit:
            omitted.append(row.get('id'))
        else:
            lines.append(line)
            kept.append(row.get('id'))
    text='\n'.join(lines) if kept else '## Memory\n(no retrieved memory fits the budget)'
    if counter.count(text)>limit:
        text=counter.truncate(text,limit)
    return text,dict(selected=kept,omitted=omitted,budget_count=counter.count(text),budget=limit)


class Mem0Runner(Runner):
    def __init__(self,*args,top_k=200,**kwargs):
        prohibit_mem0_execution()
        super().__init__(*args,**kwargs)
        self.top_k=top_k
        self.dimension=None

    def _memory(self,item,*,ingest_only=False):
        root=Path(self.stores_dir)/item.question_id
        root.mkdir(parents=True,exist_ok=True)
        checkpoint=root/'ingestion.json'
        input_identity=fingerprint(dict(models=self.model_settings,
            settings=self.settings.write_fingerprint(),
            input=[asdict(s) for s in item.sessions],mem0=importlib.metadata.version('mem0ai'),
            adapter=hashlib.sha256(Path(__file__).with_name('mem0_bridge.py').read_bytes()).hexdigest()))
        state=json.loads(checkpoint.read_text()) if checkpoint.exists() else dict(
            fingerprint=input_identity,completed_pairs=[],complete=False)
        if state['fingerprint']!=input_identity:
            raise ValueError('Mem0 input, models or adapter changed; use a new comparison directory')
        if self.dimension is None:
            self.dimension=len(self.models.embedder.embed(['memory comparison dimension probe'],purpose='embed_probe')[0])
        memory=memory_for(self.models,self.settings,root,item.question_id,self.dimension,
                          state_root=Path(self.out_dir)/'sdk-state')
        counter=budget_counter(self.settings)
        started=time.monotonic()
        cached=bool(state.get('complete'))
        try:
            if not cached:
                completed=set(state['completed_pairs'])
                for session_index,session in enumerate(item.sessions):
                    pairs=[]
                    for turn in session.turns:
                        if turn.role=='user' or not pairs:
                            pairs.append([])
                        pairs[-1].append(dict(role=turn.role,content=turn.content))
                    for pair_index,pair in enumerate(pairs):
                        key='%d:%d'%(session_index,pair_index)
                        if key in completed:
                            continue
                        if self.cancel_event.is_set() or time.monotonic()>=self.deadline:
                            raise TimeoutError('Mem0 ingestion batch budget exhausted')
                        # Supply the same session clock available to memsys. This
                        # instruction contains no question, answer or evidence labels.
                        clock='This conversation was recorded on %s. Preserve speaker roles and distinguish event dates from this record date.'%session.date.isoformat()
                        memory.add(pair,user_id=item.question_id,
                            metadata=dict(recorded_at=session.date.isoformat(),session_id=session.session_id),prompt=clock)
                        completed.add(key)
                        state['completed_pairs']=sorted(completed)
                        save(checkpoint,state)
                state['complete']=True
                save(checkpoint,state)
            ingested=time.monotonic()
            if ingest_only:
                return None,dict(ingest_s=round(ingested-started,3),cached_ingest=cached)
            history='\n'.join(turn.content for session in item.sessions for turn in session.turns)
            budget=effective_budget(self.settings,counter.count(history),counter.count(item.question))
            read_budget=ReadBudget(min(self.settings.read_timeout_s,max(.001,self.deadline-ingested)),
                self.settings.read_retries_per_call,self.settings.read_retries_total,
                self.settings.read_retry_wait_s,self.cancel_event)
            with read_scope(read_budget):
                found=memory.search(item.question,filters={'user_id':item.question_id},limit=self.top_k,rerank=False)
                searched_at=time.monotonic()
                rows=found.get('results',[]) if isinstance(found,dict) else found
                context,selection=pack_memories(rows,counter,budget)
                def messages_for(text):
                    return [dict(role='system',content=prompts.ANSWER_SYSTEM),dict(role='user',content=
                        prompts.ANSWER_QA.format(today=format_day(item.question_date),memory=text,
                                                question=item.question,rules=prompts.ANSWER_RULES))]
                messages=messages_for(context)
                if counter.messages(messages)>self.settings.answer_input_budget_tokens:
                    allowance=max(0,self.settings.answer_input_budget_tokens-counter.messages(messages_for('')))
                    context,selection=pack_memories(rows,counter,min(budget,allowance))
                    messages=messages_for(context)
                if counter.messages(messages)>self.settings.answer_input_budget_tokens:
                    raise ValueError('Shared answer instructions exceed input budget')
                selected_at=time.monotonic()
                response=ReadTransportChat(self.models.chat,read_budget).chat(messages,purpose='answer',
                    max_tokens=self.settings.answer_max_tokens,tags=dict(question_id=item.question_id,framework='mem0_oss'))
            notes,answer=parse_answer(response.text)
            trace=dict(question_id=item.question_id,framework='mem0_oss',memory=context,notes=notes,
                selection=selection,retrieved=len(rows),top_k=self.top_k,memories=rows,
                budget_counter=counter.manifest(),prompt_budget_count=counter.messages(messages),
                read_transport=read_budget.snapshot(),comparison_mode='whole_system_same_models_and_budgets',
                limitations='Native Mem0 single-call answer; no memsys post-generation source/calculation review.')
            save(Path(self.traces_dir)/(item.question_id+'.json'),trace)
            return answer,dict(ingest_s=round(ingested-started,3),cached_ingest=cached,
                answer_s=round(time.monotonic()-ingested,3),search_s=round(searched_at-ingested,3),
                selection_s=round(selected_at-searched_at,3),plan_s=0.0,rerank_s=0.0,
                generate_s=round(time.monotonic()-selected_at,3),
                memory_tokens=counter.count(context),effective_budget=budget,
                context_tokens=counter.count(context)+counter.count(item.question),
                history_tokens=counter.count(history),
                prompt_budget_count=counter.messages(messages),read_transport=read_budget.snapshot(),
                framework='mem0_oss',comparison_mode='whole_system_same_models_and_budgets')
        finally:
            close_memory(memory)


def main():
    prohibit_mem0_execution()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--top-k',type=int,default=200)
    parser.add_argument('--max-wall-s',type=float,default=7200)
    args=parser.parse_args()
    os.environ['MEM0_TELEMETRY']='false'
    args.out.mkdir(parents=True,exist_ok=True)
    from memsys.cli import load_dotenv
    load_dotenv(str(Path.cwd()/'.env'))
    config=AppConfig.load(str(args.config))
    if config.fallbacks:
        parser.error('Comparison requires fixed roles and no model fallbacks')
    ledger=UsageLedger(str(args.out/'usage.jsonl'))
    models=config.build_models(ledger)
    rows=load(str(args.data))
    metadata_path=args.out/'mem0-identity.json'
    identity=dict(mem0_version=importlib.metadata.version('mem0ai'),
        runtime_packages={dist.metadata['Name']:dist.version for dist in importlib.metadata.distributions()},
        adapter_sha256=hashlib.sha256(Path(__file__).with_name('mem0_bridge.py').read_bytes()).hexdigest(),
        driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),top_k=args.top_k,
        data_sha256=hashlib.sha256(args.data.read_bytes()).hexdigest(),
        config_sha256=hashlib.sha256(args.config.read_bytes()).hexdigest(),
        comparison_mode='whole_system_same_models_and_budgets',
        telemetry=False,ingestion='chronological user/assistant pairs with source clock; no question or labels')
    if metadata_path.exists() and json.loads(metadata_path.read_text())!=identity:
        raise ValueError('Frozen Mem0 comparison identity changed')
    save(metadata_path,identity)
    runner=Mem0Runner(models,config.memory,str(args.out),progress=lambda text:print(text,flush=True),
        data_info=dict(sha256=identity['data_sha256']),top_k=args.top_k,max_wall_s=args.max_wall_s)
    summary=runner.run([item for item,_ in rows],workers=1)
    print(json.dumps(summary),flush=True)
    return 0 if len(_done_ids(runner.paths['hypotheses.jsonl']))==len(rows) else 1


if __name__=='__main__':
    raise SystemExit(main())
