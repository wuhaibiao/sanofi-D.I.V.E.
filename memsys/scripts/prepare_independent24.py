"""Prepare an independent public24 after both fixed development gates pass.

Selection reads file paths, persona IDs, row indexes and temporal categories.
Question wording, references, scores and runtime never guide selection.
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import random
import time
import urllib.parse
import urllib.request

from independent_data import convert_convomem, convert_locomo
from clone_frozen_reader_seeds import identity

SEED = 'independent-modules-four-20261012'
MODULES = [('multi-session','implicit_connection_evidence',3),
           ('knowledge-update','changing_evidence',3),
           ('single-session-user','user_evidence',3),
           ('single-session-assistant','assistant_facts_evidence',3),
           ('single-session-preference','preference_evidence',2)]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def load(path):
    return json.loads(path.read_text())


def passed_gate(root):
    freeze, summary = load(root/'freeze.json'), load(root/'summary.json')
    actual,prompts,_,_=identity(root)
    if (actual['source_sha256']!=freeze['source']['source_sha256'] or prompts!=freeze['prompts']
            or sha(root/'config.frozen.json')!=freeze['config_sha256']
            or sha(root/freeze['sample_file'])!=freeze['sample_sha256']):
        raise ValueError('Gate frozen identity changed')
    rows = load(root/freeze['sample_file'])
    expected = {r['question_id'] for r in rows}
    if len(rows)!=24 or Counter(r['question_type'] for r in rows)!={k:4 for k in [m[0] for m in MODULES]+['temporal-reasoning']}:
        raise ValueError('Gate must have four per module')
    hyp = root/'current/hypotheses.jsonl'
    answers = [json.loads(line) for line in hyp.read_text().splitlines() if line.strip()]
    if len(answers)!=24 or {r['question_id'] for r in answers}!=expected:
        raise ValueError('Gate answers incomplete or duplicated')
    grades = list((root/'current').glob('hypotheses.jsonl.eval-results-*'))
    if len(grades)!=1:
        raise ValueError('Gate requires one fixed original judge')
    labels = [json.loads(line) for line in grades[0].read_text().splitlines() if line.strip()]
    if len(labels)!=24 or {r['question_id'] for r in labels}!=expected:
        raise ValueError('Gate grades incomplete or duplicated')
    if any(type(r['autoeval_label']['label']) is not bool for r in labels):
        raise ValueError('Gate labels must be original Booleans')
    if sum(r['autoeval_label']['label'] for r in labels)<23:
        raise ValueError('Fixed gate below23/24')
    audit = load(root/'source-audit.json')
    if (audit['status']!='passed' or audit['checked']!=24 or not audit['exact_originals']
            or not audit['exact_temporal_anchors']
            or audit['source_sha256']!=freeze['source']['source_sha256']
            or audit['sample_sha256']!=freeze['sample_sha256']):
        raise ValueError('Gate source coverage incomplete')
    execution=load(root/'current/execution.json')
    if execution['status']!='completed' or execution['completed']!=24 or execution['counts']['errors']:
        raise ValueError('Gate execution incomplete')
    return dict(root=str(root.resolve()),source=freeze['source']['source_sha256'],
                config=freeze['config_sha256'],sample_sha256=freeze['sample_sha256'],
                answers_sha256=sha(hyp),grades_sha256=sha(grades[0]),
                source_audit_sha256=sha(root/'source-audit.json'),summary_sha256=sha(root/'summary.json'))


def fetch(url, path, deadline, expected_sha=None):
    if not path.exists():
        remaining=deadline-time.monotonic()
        if remaining<=0:
            raise TimeoutError('Public preparation wall deadline')
        with urllib.request.urlopen(url,timeout=min(30,remaining)) as response:
            data=response.read()
        temporary=path.with_suffix(path.suffix+'.partial')
        temporary.write_bytes(data)
        if expected_sha and sha(temporary)!=expected_sha:
            raise ValueError('Published download checksum mismatch')
        temporary.replace(path)
    if expected_sha and sha(path)!=expected_sha:
        raise ValueError('Downloaded source changed')
    return dict(url=url,sha256=sha(path),bytes=path.stat().st_size)


def prepare(args):
    gates=[passed_gate(p.resolve()) for p in args.gates]
    if gates[0]['source']!=gates[1]['source'] or gates[0]['config']!=gates[1]['config']:
        raise ValueError('Both development gates must use the same frozen candidate')
    if (args.out/'data24.json').exists():
        raise ValueError('Independent sample already frozen; use its existing file')
    args.out.mkdir(parents=True,exist_ok=True)
    raw=args.out/'raw';raw.mkdir(exist_ok=True)
    deadline=time.monotonic()+args.max_wall_s
    revision=load(args.research/'ConvoMem-revision.json')['sha']
    locomo=load(args.research/'LoCoMo-revision.json')
    excluded=load(args.research/'schema-calibration-exclusion.json')['exclude_persona_from_final_sample']
    method=dict(seed=SEED,counts={k:4 for k in [m[0] for m in MODULES]+['temporal-reasoning']},
                modules=MODULES,convomem_revision=revision,locomo_revision=locomo['revision'],
                excluded_persona=excluded,excluded_locomo_conversation='first released conversation',
                method='Seeded random paths from distinct personas; one uniformly random row per file. Four distinct noncalibration LoCoMo conversations, one random category2 row per conversation. No semantic or performance filtering.',
                gates=gates,selector_sha256=sha(Path(__file__)),adapter_sha256=sha(Path(__file__).with_name('independent_data.py')))
    method_path=args.out/'method.json'
    if method_path.exists() and load(method_path)!=method:
        raise ValueError('Preparation identity changed; earlier selections must be retained')
    save(method_path,method)
    rng=random.Random(SEED);used={excluded};choices=[];downloads=[]
    for typ,folder,count in MODULES:
        sub='core_benchmark/evidence_questions/'+folder+'/'+str(count)+'_evidence'
        url='https://huggingface.co/api/datasets/Salesforce/ConvoMem/tree/'+revision+'/'+sub+'?limit=1000'
        listing=raw/(folder+'-files.json')
        downloads.append(fetch(url,listing,deadline))
        entries=load(listing)
        pool=sorted((r for r in entries if r['type']=='file' and Path(r['path']).suffix=='.json'
                     and Path(r['path']).stem not in used),key=lambda r:r['path'])
        for entry in rng.sample(pool,4):
            persona=Path(entry['path']).stem;used.add(persona)
            choices.append(dict(question_type=typ,persona=persona,path=entry['path'],
                                expected_sha256=entry.get('lfs',{}).get('oid')))
    # Save file choices before fetching any candidate question/reference texts.
    choices_path=args.out/'file-choices.json'
    if choices_path.exists() and load(choices_path)!=choices:
        raise ValueError('Frozen file selection changed')
    save(choices_path,choices)
    rows=[];selected=[]
    for choice in choices:
        url='https://huggingface.co/datasets/Salesforce/ConvoMem/resolve/'+revision+'/'+urllib.parse.quote(choice['path'])
        file=raw/(choice['question_type']+'-'+choice['persona']+'.json')
        downloads.append(fetch(url,file,deadline,choice['expected_sha256']))
        items=load(file)['evidence_items'];index=rng.randrange(len(items))
        qid='ind_convomem_'+hashlib.sha256((choice['path']+':'+str(index)).encode()).hexdigest()[:16]
        rows.append(convert_convomem(items[index],choice['question_type'],qid,datetime(2026,10,12)))
        selected.append(dict(choice,question_id=qid,row_index=index,source_sha256=sha(file)))
    source=args.research/'locomo10.json'
    if sha(source)!=locomo['sha256']:
        raise ValueError('Pinned LoCoMo source changed')
    conversations=load(source)
    pool=sorted(enumerate(conversations[1:],1),key=lambda pair:str(pair[1]['sample_id']))
    for ci,conversation in rng.sample(pool,4):
        indexes=[i for i,qa in enumerate(conversation['qa']) if qa['category']==2]
        index=rng.choice(indexes)
        qid='ind_locomo_'+hashlib.sha256((str(conversation['sample_id'])+':'+str(index)).encode()).hexdigest()[:16]
        rows.append(convert_locomo(conversation,conversation['qa'][index],qid))
        selected.append(dict(question_id=qid,question_type='temporal-reasoning',
                             conversation_index=ci,sample_id=conversation['sample_id'],row_index=index,
                             source_sha256=locomo['sha256']))
    if len({r['question_id'] for r in rows})!=24:
        raise ValueError('Independent IDs are not unique')
    save(args.out/'data24.json',rows)
    save(args.out/'selection.json',dict(method_sha256=sha(method_path),selected=selected,downloads=downloads,
                                     locomo=locomo,data_sha256=sha(args.out/'data24.json'),
                                     scope='Independent24 public oracle diagnostic, not either dataset official score'))
    print(json.dumps(dict(prepared=24,counts=Counter(r['question_type'] for r in rows),
                         contents_displayed=False,data_sha256=sha(args.out/'data24.json'))))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--gates',nargs=2,type=Path,required=True)
    p.add_argument('--research',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--max-wall-s',type=float,default=300)
    prepare(p.parse_args())
