"""Seal fully audited pre-answer memsys stores for repeatable reader experiments."""
import argparse
import json
from pathlib import Path
import shutil

from audit_frozen_ingestion import verify_store
from clone_frozen_reader_seeds import identity,save,sha


def seal(root):
    root=root.resolve()
    state=json.loads((root/'freeze.json').read_text())
    source,prompts,_,_=identity(root)
    sample=root/state['sample_file']
    if (source['source_sha256']!=state['source']['source_sha256'] or prompts!=state['prompts']
            or sha(sample)!=state['sample_sha256']
            or sha(root/'config.frozen.json')!=state['config_sha256']):
        raise ValueError('Frozen ingestion identity changed.')
    ingestion=json.loads((root/'ingestion.json').read_text())
    audit=json.loads((root/'source-audit.json').read_text())
    if (ingestion['status']!='completed' or ingestion['failures'] or audit['status']!='passed'
            or audit['source_sha256']!=source['source_sha256'] or audit['sample_sha256']!=sha(sample)
            or (root/'current/hypotheses.jsonl').exists()):
        raise ValueError('Require complete, failure-free, audited pre-answer ingestion.')
    if (root/'store-seed').exists() or (root/'ingest-freeze.json').exists():
        raise ValueError('Immutable stores already exist; do not replace them.')
    # Use the frozen package for source/session interpretation, as the auditor does.
    import sys
    sys.path.insert(0,str(root/'frozen-src'))
    from memsys.bench.longmemeval import load
    items={item.question_id:item for item,_ in load(str(sample))}
    records={r['question_id']:r for r in ingestion['results']}
    if len(records)!=len(ingestion['results']) or set(records)!=set(items):
        raise ValueError('Original ingestion item coverage differs.')
    stores=[]
    for qid,item in items.items():
        path=root/'current/stores'/(qid+'.sqlite')
        if sha(path)!=records[qid]['store_sha256']:
            raise ValueError('Pre-answer store changed: '+qid)
        verify_store(path,item)
        stores.append(dict(question_id=qid,store_sha256=sha(path)))
    (root/'store-seed').mkdir()
    for row in stores:
        filename=row['question_id']+'.sqlite'
        shutil.copy2(root/'current/stores'/filename,root/'store-seed'/filename)
    proof=dict(immutable_seed=True,source_sha256=source['source_sha256'],
        source_audit_sha256=sha(root/'source-audit.json'),ingestion_sha256=sha(root/'ingestion.json'),
        sample_sha256=sha(sample),stores=stores,mem0_execution=False)
    save(root/'ingest-freeze.json',proof)
    print(json.dumps(dict(sealed=len(stores),pre_answer=True,mem0_execution=False)))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--iteration',type=Path,required=True)
    seal(p.parse_args().iteration)
