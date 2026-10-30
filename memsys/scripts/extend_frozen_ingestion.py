"""Reuse immutable audited stores when adding sampled questions, before reading.

The existing ingestion tool then writes only the new questions. This prepares
source lineage, not a synthetic successful write or a scored answer.
"""
import argparse
import json
from pathlib import Path
import shutil

from clone_frozen_reader_seeds import save, sha, validated_seed


def extend(seed,target):
    seed,target=seed.resolve(),target.resolve()
    old,new,_,_,stores,ingestion,binding=validated_seed(seed,target,allow_superset=True)
    rows=json.loads((target/new['sample_file']).read_text())
    pending=[r for r in rows if r['question_id'] not in stores]
    if not pending:
        raise ValueError('No added questions; use the exact reader clone helper.')
    if (target/'current').exists() or (target/'store-seed').exists():
        raise ValueError('Target already has execution data.')
    original_rows={r['question_id']:r for r in ingestion['results']}
    if len(original_rows)!=len(ingestion['results']) or set(original_rows)!=set(stores):
        raise ValueError('Original ingestion manifest is incomplete.')
    if any(original_rows[qid]['store_sha256']!=digest for qid,digest in stores.items()):
        raise ValueError('Ingestion store identity differs from immutable binding.')
    destination=target/'current/stores'
    destination.mkdir(parents=True)
    reused=[]
    for qid,digest in stores.items():
        shutil.copy2(seed/'store-seed'/(qid+'.sqlite'),destination/(qid+'.sqlite'))
        reused.append(dict(original_rows[qid],cached_ingest=True,ingest_s=0,
                           reused_from=str(seed),reuse_scope='audited immutable pre-answer store; no new extraction'))
    (target/'current/ingestion.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in reused))
    save(target/'added-sample.json',pending)
    proof=dict(seed=str(seed),seed_freeze_sha256=sha(seed/'freeze.json'),
        seed_ingest_freeze_sha256=sha(seed/'ingest-freeze.json'),writer_unchanged=True,
        originals_unchanged=True,reused=len(reused),pending_new_writes=len(pending),
        stores=binding['stores'],added_sample_sha256=sha(target/'added-sample.json'),
        reused_manifest_sha256=sha(target/'current/ingestion.jsonl'),mem0_execution=False)
    save(target/'ingestion-reuse.json',proof)
    new.update(evaluation_mode='extended_sample_with_audited_ingestion_reuse',
               ingestion_reuse_sha256=sha(target/'ingestion-reuse.json'),
               reingest=False,pending_new_writes=len(pending))
    save(target/'freeze.json',new)
    print(json.dumps(dict(reused=len(reused),new_writes_required=len(pending),mem0_execution=False)))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seed',type=Path,required=True)
    p.add_argument('--target',type=Path,required=True)
    args=p.parse_args()
    extend(args.seed,args.target)
