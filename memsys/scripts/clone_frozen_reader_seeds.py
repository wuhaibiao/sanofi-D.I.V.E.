"""Clone audited pre-answer stores for a paired reader comparison."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def identity(root):
    code = ('import json; from memsys.config import AppConfig; '
            'from memsys.bench.provenance import source_manifest,prompt_fingerprint; '
            'import sys; c=AppConfig.load(sys.argv[1]); '
            'print(json.dumps([source_manifest(),prompt_fingerprint(),c.memory.write_fingerprint(),'
            'c.memory.index_fingerprint("paired_same_embedding")]))')
    return json.loads(subprocess.check_output(
        [sys.executable, '-c', code, str(root/'config.frozen.json')],
        env=dict(os.environ, PYTHONPATH=str(root/'frozen-src')), text=True))


def validated_seed(seed, target, allow_superset=False):
    """Verify immutable writer identity before exposing any reusable stores."""
    seed, target = seed.resolve(), target.resolve()
    old = json.loads((seed/'freeze.json').read_text())
    new = json.loads((target/'freeze.json').read_text())
    for root, state in ((seed, old), (target, new)):
        actual, prompts, _, _ = identity(root)
        if actual['source_sha256'] != state['source']['source_sha256'] or prompts != state['prompts']:
            raise ValueError('Frozen code or prompts changed')
        if sha(root/'config.frozen.json') != state['config_sha256']:
            raise ValueError('Frozen configuration changed')
        if sha(root/state['sample_file']) != state['sample_sha256']:
            raise ValueError('Frozen sample changed')
    if old['source']['write_source_sha256'] != new['source']['write_source_sha256']:
        raise ValueError('Writer changed; fresh ingestion required')
    if old['source']['index_source_sha256'] != new['source']['index_source_sha256']:
        raise ValueError('Index code changed; this comparison requires identical stores')
    old_config = json.loads((seed/'config.frozen.json').read_text())
    new_config = json.loads((target/'config.frozen.json').read_text())
    def writer_roles(config):
        roles=config['roles']
        return {role:dict(profile_name=roles.get(role,roles.get('memory')),
                          profile=config['models'][roles.get(role,roles.get('memory'))])
                for role in ('extract','resolve','embedding')}
    if writer_roles(old_config)!=writer_roles(new_config) or old_config.get('fallbacks')!=new_config.get('fallbacks'):
        raise ValueError('Write/embedding model configuration changed')
    if identity(seed)[2:] != identity(target)[2:]:
        raise ValueError('Write or index fingerprint changed')
    def samples(root, state):
        rows = json.loads((root/state['sample_file']).read_text())
        result = {r['question_id']:r for r in rows}
        if len(result) != len(rows):
            raise ValueError('Duplicate sample IDs')
        return result
    items = samples(seed, old)
    targets = samples(target, new)
    if (not allow_superset and items != targets) or (allow_superset and
            (not set(items) <= set(targets) or any(items[qid] != targets[qid] for qid in items))):
        raise ValueError('Paired source inputs differ')
    binding = json.loads((seed/'ingest-freeze.json').read_text())
    if not binding['immutable_seed']:
        raise ValueError('Pre-answer stores are not frozen')
    for filename, key in (('source-audit.json','source_audit_sha256'), ('ingestion.json','ingestion_sha256')):
        if sha(seed/filename) != binding[key]:
            raise ValueError('Original ingestion proof changed')
    audit = json.loads((seed/'source-audit.json').read_text())
    ingestion = json.loads((seed/'ingestion.json').read_text())
    if audit['status'] != 'passed' or ingestion['status'] != 'completed' or ingestion['failures']:
        raise ValueError('Seed ingestion is incomplete or failed')
    stores = {r['question_id']:r['store_sha256'] for r in binding['stores']}
    if len(stores)!=len(binding['stores']) or set(stores) != set(items):
        raise ValueError('Missing immutable stores')
    for qid, digest in stores.items():
        if sha(seed/'store-seed'/(qid+'.sqlite')) != digest:
            raise ValueError('Pre-answer store changed: '+qid)
    return old, new, old_config, new_config, stores, ingestion, binding


def clone(seed, target):
    seed, target = seed.resolve(), target.resolve()
    old, new, old_config, new_config, stores, ingestion, binding = validated_seed(seed,target)
    if (target/'current').exists() or (target/'store-seed').exists():
        raise ValueError('Target already has execution data')
    (target/'current/stores').mkdir(parents=True)
    (target/'store-seed').mkdir()
    for qid in stores:
        filename = qid+'.sqlite'
        shutil.copy2(seed/'store-seed'/filename, target/'current/stores'/filename)
        shutil.copy2(seed/'store-seed'/filename, target/'store-seed'/filename)
    ingestion['scope'] = 'Reused audited GPT-6 Luna ingestion; no new write execution in this reader comparison.'
    ingestion['reused_from'] = str(seed)
    save(target/'ingestion.json', ingestion)
    lineage = dict(seed=str(seed), seed_freeze_sha256=sha(seed/'freeze.json'),
                   seed_ingest_freeze_sha256=sha(seed/'ingest-freeze.json'),
                   writer_unchanged=True, write_model_config_unchanged=True,
                   read_model_config_changes={role:dict(before=old_config['roles'].get(role),after=new_config['roles'].get(role))
                                              for role in set(old_config['roles'])|set(new_config['roles'])
                                              if old_config['roles'].get(role)!=new_config['roles'].get(role)},
                   write_and_index_fingerprints_unchanged=True, write_fingerprint=identity(target)[2],
                   read_config_changes={k:dict(before=old_config['memory'].get(k),after=new_config['memory'].get(k))
                                        for k in set(old_config['memory'])|set(new_config['memory'])
                                        if old_config['memory'].get(k)!=new_config['memory'].get(k)},
                   originals_unchanged=True, pre_answer_stores=True, stores=binding['stores'])
    save(target/'reader-seed-lineage.json', lineage)
    new.update(evaluation_mode='paired_reader_on_identical_stores', reingest=False,
               reader_seed_lineage_sha256=sha(target/'reader-seed-lineage.json'))
    save(target/'freeze.json', new)
    print(json.dumps(dict(cloned=len(stores), writer_unchanged=True, pre_answer_stores=True)))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seed', type=Path, required=True)
    p.add_argument('--target', type=Path, required=True)
    args = p.parse_args()
    clone(args.seed, args.target)
