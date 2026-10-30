#!/usr/bin/env python3
"""Restore generic literal structures before any new answer; no LLM re-extraction."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from check_webchat_reliability import credentials
from evaluate_public_types import read, save, rows, digest, CapturedChat
from memsys.bench.provenance import source_manifest, fingerprint
from memsys.config import AppConfig
from memsys.core import MemorySystem
from memsys.llm import UsageLedger
from memsys.write import Writer


def main():
    parent=ROOT/'runs/fact-retention-20261013'
    credentials(Path.home()/'Library/Application Support/memsys/runtime.json')
    for original,name in [('rebuilt-24','retained-24'),('unseen-24','retained-unseen-24')]:
        base,out=parent/original,parent/name
        if (out/'freeze.json').exists():continue
        old=read(base/'freeze.json');writes=rows(base/'LongMemEval-write.jsonl')
        assert len(writes)==24
        if original=='unseen-24':assert not rows(base/'LongMemEval-answers.jsonl')
        out.mkdir(exist_ok=True)
        for file in ('sample.json','selection.json','config.frozen.json'):
            shutil.copy2(base/file,out/file)
        (out/'config.frozen.json').chmod(0o600)
        ledger=UsageLedger(str(out/'LongMemEval-restore-usage.jsonl'))
        config=AppConfig.load(out/'config.frozen.json')
        config.storage=replace(config.storage,collection_prefix=old['prefix'],namespace=old['namespace'])
        models=config.build_models(ledger,env={},probe=False)
        retries=[]
        extract=models.for_purpose('extract')
        models.by_purpose['extract']=CapturedChat(extract,out/'LongMemEval-retry-prompts.jsonl')
        system=MemorySystem(models,config.memory,storage=config.storage)
        audits=[]
        try:
            for w in writes:
                store=system.store(w['corpus_id'])
                for proof in w['sessions']:
                    if proof['error'] or proof['processed_messages']!=proof['raw_messages']:
                        # Retry an operationally incomplete batch before any question,
                        # with a larger output allowance, never split or salvage JSON.
                        retry_settings=replace(config.memory,extract_max_tokens=12000)
                        date=datetime.fromisoformat(store.session(proof['session_id'])['opened_at'])
                        report=Writer(store,models,retry_settings,tags={'corpus_id':w['corpus_id'],'retry':'output_limit'}).consolidate(proof['session_id'],closed_at=date)
                        retries.append(dict(corpus_id=w['corpus_id'],session_id=proof['session_id'],
                            initial_error=proof['error'],retry_output_limit=12000,report=asdict(report)))
                        proof.update(error=None,status='closed',processed_messages=proof['raw_messages'],report=asdict(report))
                        store.put_record('benchmark_source',proof['source_session_id'],dict(finished=True,report=proof))
                        w['complete']=all(s['error'] is None and s['processed_messages']==s['raw_messages'] for s in w['sessions'])
                        print('RETRY '+name+' '+w['corpus_id']+' completed',flush=True)
                before={f.id:fingerprint(asdict(f)) for f in store.facts()}
                episodes=fingerprint([asdict(e) for e in store.episodes()])
                reports=Writer(store,models,config.memory,tags={'corpus_id':w['corpus_id'],'restoration':'literal'}).restore_structures()
                assert all(fingerprint(asdict(store.fact(fid)))==value for fid,value in before.items())
                assert episodes==fingerprint([asdict(e) for e in store.episodes()])
                restored=sum(len(r.added) for r in reports)
                audits.append(dict(corpus_id=w['corpus_id'],added=restored,old_facts_unchanged=True,
                                   originals_unchanged=True,reports=[asdict(r) for r in reports]))
                w.update(facts=[asdict(f) for f in store.facts()],counts=store.counts(),
                         restoration_reports=[asdict(r) for r in reports])
                print('RESTORE '+name+' '+w['corpus_id']+' added='+str(restored),flush=True)
            assert not any(r.purpose in ('answer','query','judge') for r in ledger.records())
            assert sum(r.purpose=='extract' for r in ledger.records())==len(retries)
        finally:system.close()
        (out/'LongMemEval-write.jsonl').write_text('\n'.join(json.dumps(w,ensure_ascii=False,default=str) for w in writes)+'\n')
        for file in ('LongMemEval-write-usage.jsonl','LongMemEval-write-prompts.jsonl','LongMemEval-write-summary.json'):
            if (base/file).exists():shutil.copy2(base/file,out/file)
        save(out/'restoration-audit.json',dict(restoration_llm_calls=0,operational_retries=retries,
            added=sum(a['added'] for a in audits),
            old_facts_unchanged=True,originals_unchanged=True,rows=audits))
        shutil.copytree(ROOT/'src/memsys',out/'frozen-src/memsys',ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(ROOT/'scripts/evaluate_public_types.py',out/'runner.frozen.py')
        save(out/'freeze.json',dict(source=source_manifest(),runner_sha256=digest(ROOT/'scripts/evaluate_public_types.py'),
            sample_sha256=digest(out/'sample.json'),selection_sha256=digest(out/'selection.json'),
            config_sha256=digest(out/'config.frozen.json'),prefix=old['prefix'],namespace=old['namespace'],
            selected_at=datetime.now(timezone.utc).isoformat(),recall_mode='facts',
            method='The V3 LLM extraction and source histories are preserved. V4 appends generic literal list/table/numbered-action facts from safe saved episodes before new answers, without gold input or further extraction LLM calls. This is a source-structure restoration check, not a fresh V4 end-to-end ingestion score.',
            original_write_freeze=old,additional_llm_extraction_calls=len(retries),retry_output_limit=12000 if retries else None))


if __name__=='__main__':main()
