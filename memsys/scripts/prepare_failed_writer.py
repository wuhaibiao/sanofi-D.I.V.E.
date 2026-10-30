"""Freeze all recorded writer failures for a diagnostic repair, never an accuracy cohort."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from memsys.bench.provenance import source_manifest,prompt_fingerprint
from clone_frozen_reader_seeds import identity,save,sha


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--failed-iteration',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    args=p.parse_args()
    origin,out=args.failed_iteration.resolve(),args.out.resolve()
    old=json.loads((origin/'freeze.json').read_text())
    source,prompts,_,_=identity(origin)
    sample=origin/old['sample_file']
    if (source['source_sha256']!=old['source']['source_sha256'] or prompts!=old['prompts']
            or sha(sample)!=old['sample_sha256']
            or sha(origin/'config.frozen.json')!=old['config_sha256']):
        raise ValueError('Original failed iteration identity changed.')
    failure_file=origin/'current/ingest-errors.jsonl'
    failures=[json.loads(s) for s in failure_file.read_text().splitlines() if s.strip()]
    failed={r['question_id'] for r in failures}
    rows=json.loads(sample.read_text())
    selected=[r for r in rows if r['question_id'] in failed]
    if not failed or {r['question_id'] for r in selected}!=failed:
        raise ValueError('Failure IDs must be nonempty and bound to original source inputs.')
    if out.exists():
        raise ValueError('Use a fresh diagnostic directory.')
    out.mkdir(parents=True)
    save(out/'sample.json',selected)
    shutil.copy2(origin/'config.frozen.json',out/'config.frozen.json')
    shutil.copytree(ROOT/'src/memsys',out/'frozen-src/memsys',ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(Path(__file__),out/'runner.py')
    selection=dict(method='all recorded ingestion failures; not chosen by reference answer or correctness',
        scope='writer diagnosis only, never a balanced accuracy or full benchmark score',
        origin=str(origin),origin_freeze_sha256=sha(origin/'freeze.json'),
        origin_sample_sha256=sha(sample),failure_file_sha256=sha(failure_file),
        failures=failures,total=len(selected),mem0_execution=False)
    save(out/'selection.json',selection)
    state=dict(created_at=datetime.now(timezone.utc).isoformat(),source=source_manifest(),
        prompts=prompt_fingerprint(),config_sha256=sha(out/'config.frozen.json'),
        sample_file='sample.json',sample_sha256=sha(out/'sample.json'),sample_size=len(selected),
        selection_sha256=sha(out/'selection.json'),runner_sha256=sha(out/'runner.py'),
        models_fixed=True,reingest=True,workers=1,stages=[],scope=selection['scope'])
    save(out/'freeze.json',state)
    print(json.dumps(dict(failed_sources=len(selected),fresh_write=True,mem0_execution=False,
                         scope=selection['scope'])))


if __name__=='__main__':
    main()
