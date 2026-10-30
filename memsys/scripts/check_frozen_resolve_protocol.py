"""Four real memsys protocol-repair integrations with explicitly injected faults.

This is component verification, not benchmark accuracy. Invalid first responses
are synthetic; only the normal memsys repair calls use the configured resolver.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys

from clone_frozen_reader_seeds import identity,sha,save

CASES=('missing_target','missing_decision','duplicate_decision','unavailable_target')


class FaultOnceResolve:
    def __init__(self,client,case):
        self.client,self.case=client,case
        self.injected=[]
        self.actual_repairs=0

    def __getattr__(self,name):
        return getattr(self.client,name)

    def chat(self,messages,**kwargs):
        from memsys.llm import ChatResult
        if not self.injected:
            indexes=sorted({int(n) for n in re.findall(r'\[N(\d+)\]',messages[-1]['content'])})
            if not indexes:raise ValueError('Cannot inject without actual new-fact IDs.')
            rows=[]
            for n in indexes:
                if self.case=='missing_decision':continue
                item=dict(new='N%d'%n,action='update',target=None)
                if self.case=='unavailable_target':item['target']='F999999999'
                rows.append(item)
                if self.case=='duplicate_decision':rows.append(dict(item,action='new'))
            fault=dict(decisions=rows)
            self.injected.append(dict(indexes=indexes,response=fault))
            return ChatResult(json.dumps(fault),'synthetic_protocol_fault',0,0,0,0,0,'stop')
        self.actual_repairs += (kwargs.get('tags') or {}).get('resolve_protocol_repair')=='1'
        return self.client.chat(messages,**kwargs)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--iteration',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    args=p.parse_args()
    root,out=args.iteration.resolve(),args.out.resolve()
    state=json.loads((root/'freeze.json').read_text())
    source,prompts,_,_=identity(root)
    if (source['source_sha256']!=state['source']['source_sha256'] or prompts!=state['prompts']
            or sha(root/'config.frozen.json')!=state['config_sha256']):
        raise ValueError('Frozen writer changed.')
    if out.exists():raise ValueError('Use a fresh protocol output directory.')
    out.mkdir(parents=True)
    sys.path.insert(0,str(root/'frozen-src'))
    from memsys.config import AppConfig
    from memsys.core import MemorySystem
    from memsys.cli import load_dotenv
    from memsys.llm import UsageLedger
    from memsys.bench.provenance import source_manifest,prompt_fingerprint
    load_dotenv()
    config=AppConfig.load(str(root/'config.frozen.json'))
    ledger=UsageLedger(str(out/'usage.jsonl'))
    report=dict(scope='synthetic fault injection with actual memsys extraction/repair; not benchmark accuracy',
        source=source,prompts=prompts,config_sha256=state['config_sha256'],runner_sha256=sha(Path(__file__)),
        mem0_execution=False,total=len(CASES),passed=0,results=[])
    for case in CASES:
        models=config.build_models(ledger,probe=False)
        wrapper=FaultOnceResolve(models.for_purpose('resolve'),case)
        models.by_purpose['resolve']=wrapper
        system=MemorySystem(models,config.memory,data_dir=str(out/case/'stores'))
        try:
            for day,text in ((1,'My test project is located in the cedar workshop.'),
                             (5,'My test project has moved to the silver workshop and is no longer in the cedar workshop.')):
                at=datetime(2025,4,day)
                sid=system.open_session(case,at,'protocol-day-%d'%day)
                system.add_turn(sid,'user',text,at)
                write,_=system.close_session(sid,at)
                if day==1:original_ids={f.id for f in system.store(case).facts()}
            remaining_ids={f.id for f in system.store(case).facts()}
            row=dict(case=case,injected=wrapper.injected,actual_repair_calls=wrapper.actual_repairs,
                resolve_errors=write.resolve_errors,resolve_repairs=write.resolve_repairs,
                original_facts_retained=bool(original_ids) and original_ids<=remaining_ids,
                pending_extract_rounds=system.store(case).backlog_count())
            row['passed']=(len(wrapper.injected)==1 and wrapper.actual_repairs==1
                and not row['resolve_errors'] and row['pending_extract_rounds']==0
                and row['original_facts_retained']
                and len(write.resolve_repairs)==1 and write.resolve_repairs[0]['status']=='complete')
        finally:
            system.close()
        report['results'].append(row);report['passed']+=int(row['passed'])
        save(out/'report.json',report)
        print(json.dumps(dict(case=case,passed=row['passed'],actual_repair_calls=wrapper.actual_repairs)),flush=True)
    if (source_manifest()['source_sha256']!=source['source_sha256'] or prompt_fingerprint()!=prompts):
        raise ValueError('Frozen source changed during component checks.')
    save(out/'report.json',report)
    return 0 if report['passed']==report['total'] else 1


if __name__=='__main__':
    raise SystemExit(main())
