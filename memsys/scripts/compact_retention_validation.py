#!/usr/bin/env python3
"""Clone model-derived facts, then restore only compact literal structures."""
from dataclasses import asdict, replace
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from check_webchat_reliability import credentials
from evaluate_public_types import read,save,rows,digest
from memsys.config import AppConfig
from memsys.core import MemorySystem
from memsys.bench.provenance import source_manifest
from memsys.llm import UsageLedger
from memsys.write import Writer


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',choices=('retained-24','retained-unseen-24'))
    parser.add_argument('--name')
    args=parser.parse_args()
    if bool(args.source)!=bool(args.name):parser.error('--source and --name must be supplied together')
    cohorts=[(args.source,args.name)] if args.source else [('retained-24','compact-24'),('retained-unseen-24','compact-unseen-24')]
    parent=ROOT/'runs/fact-retention-20261013';credentials(Path.home()/'Library/Application Support/memsys/runtime.json')
    for source_name,name in cohorts:
        base,out=parent/source_name,parent/name
        assert not out.exists();out.mkdir()
        old=read(base/'freeze.json');audit=read(base/'restoration-audit.json')
        excluded={a['corpus_id']:{fid for r in a['reports'] for fid in r['added']} for a in audit['rows']}
        retry_added={}
        for retry in audit.get('operational_retries',[]):
            retry_added.setdefault(retry['corpus_id'],set()).update(retry['report']['added'])
        for f in ('sample.json','selection.json','config.frozen.json'):shutil.copy2(base/f,out/f)
        (out/'config.frozen.json').chmod(0o600)
        cfg=AppConfig.load(out/'config.frozen.json')
        src_cfg=replace(cfg.storage,collection_prefix=old['prefix'],namespace=old['namespace'])
        dst_cfg=replace(cfg.storage,collection_prefix='memsys_compact_'+uuid.uuid4().hex[:12],namespace='compact-'+name)
        ledger=UsageLedger(str(out/'LongMemEval-restore-usage.jsonl'));models=cfg.build_models(ledger,env={},probe=False)
        source=MemorySystem(models,cfg.memory,storage=src_cfg);target=MemorySystem(models,cfg.memory,storage=dst_cfg)
        writes=rows(base/'LongMemEval-write.jsonl');restorations=[];retry_literals_removed=[]
        try:
            for w in writes:
                a=source.store(w['corpus_id']);b=target.store(w['corpus_id'])
                with a.read_snapshot(),b.transaction():
                    # Whole-batch retry reports also contain literal derivations.
                    # Those IDs are not in restore_structures() reports, which
                    # only record subsequent additions. Remove both proven sets.
                    for fact in a.records('fact'):
                        if (fact['id'] in retry_added.get(w['corpus_id'],set()) and
                                fact.get('attribute') in ('provided_list','provided_table') and
                                fact.get('semantics',{}).get('event_key','').startswith('structure@')):
                            excluded[w['corpus_id']].add(fact['id'])
                            retry_literals_removed.append(dict(corpus_id=w['corpus_id'],fact_id=fact['id']))
                    for kind in ('session','turn','episode','episode_chunk','fact','benchmark_source','web_conversation'):
                        for record in a.records(kind):
                            if kind=='fact' and record['id'] in excluded[w['corpus_id']]:continue
                            if kind=='turn':key=record['session_id']+':'+str(record['idx'])
                            elif kind=='benchmark_source':key=record['report']['source_session_id']
                            else:key=record['id']
                            b.put_record(kind,key,record)
                    for key in ('embedding_model','index_fingerprint','index_manifest','embedding_dimension'):
                        value=a.get_meta(key)
                        if value is not None:b.set_meta(key,value)
                restored=Writer(b,models,cfg.memory,tags={'corpus_id':w['corpus_id'],'restoration':'compact_literal'}).restore_structures()
                restorations.append(dict(corpus_id=w['corpus_id'],added=sum(len(r.added) for r in restored),reports=[asdict(r) for r in restored]))
                w.update(facts=[asdict(f) for f in b.facts()],counts=b.counts())
                print('COMPACT '+name+' '+w['corpus_id']+' added='+str(restorations[-1]['added']),flush=True)
            assert all(r.purpose=='embed' for r in ledger.records())
        finally:source.close();target.close()
        (out/'LongMemEval-write.jsonl').write_text('\n'.join(json.dumps(w,ensure_ascii=False,default=str) for w in writes)+'\n')
        for f in ('LongMemEval-write-usage.jsonl','LongMemEval-write-prompts.jsonl','LongMemEval-write-summary.json'):
            if (base/f).exists():shutil.copy2(base/f,out/f)
        save(out/'restoration-audit.json',dict(restoration_llm_calls=0,removed_prior_broad_derivations=True,
            operational_retries=audit.get('operational_retries',[]),retry_literals_removed=retry_literals_removed,rows=restorations))
        shutil.copytree(ROOT/'src/memsys',out/'frozen-src/memsys',ignore=shutil.ignore_patterns('__pycache__'))
        save(out/'freeze.json',dict(source=source_manifest(),runner_sha256=digest(ROOT/'scripts/evaluate_public_types.py'),
            sample_sha256=digest(out/'sample.json'),selection_sha256=digest(out/'selection.json'),config_sha256=digest(out/'config.frozen.json'),
            prefix=dst_cfg.collection_prefix,namespace=dst_cfg.namespace,selected_at=datetime.now(timezone.utc).isoformat(),
            method='Model-derived facts and exact histories cloned to a fresh tenant; broad V4 literal derivations excluded using restoration and whole-batch retry audits. V5 restores compact named items, literal positions, short actions and complete table cells before answering, with no new extraction LLM call. One unseen source previously required a whole-batch V4 extraction retry at output limit 12000; that model extraction is retained. This is repair/restoration validation, not a fresh V5 end-to-end score.',
            original_write_freeze=old,recall_mode='facts'))
        for phase in ('read','judge'):
            command=[sys.executable,str(ROOT/'scripts/evaluate_public_types.py'),'--out',str(out),'--dataset','LongMemEval','--phase',phase]
            print('START '+name+' '+phase,flush=True)
            with (out/('LongMemEval-'+phase+'.log')).open('a') as log:
                proc=subprocess.Popen(command,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
                for line in proc.stdout:log.write(line);log.flush();print(line,end='',flush=True)
                if proc.wait():raise RuntimeError(name+' '+phase+' failed')


if __name__=='__main__':main()
