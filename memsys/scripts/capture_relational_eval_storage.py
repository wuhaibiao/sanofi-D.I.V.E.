#!/usr/bin/env python3
"""Read-only counts of evaluation indexes, captured after write and before cleanup."""
import argparse
from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from check_webchat_reliability import credentials
from memsys.bench.provenance import source_manifest
from memsys.config import AppConfig
from memsys.qdrant import Qdrant
from memsys.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, default=Path.home() / 'Library/Application Support/memsys/runtime.json')
    args = parser.parse_args()
    target = args.out / 'storage-audit.json'
    if target.exists():
        raise ValueError('Preserve the original storage snapshot')
    freeze = json.loads((args.out / 'freeze.json').read_text())
    execution = json.loads((args.out / 'execution.json').read_text())
    if execution.get('current') not in ('read', 'judge'):
        raise ValueError('Capture only after all writing, before evaluation cleanup')
    if source_manifest()['source_sha256'] != freeze['source']['source_sha256']:
        raise ValueError('Production source changed during evaluation')
    credentials(args.runtime)
    config = AppConfig.load(args.out / 'config.frozen.json')
    settings = replace(config.storage, collection_prefix=freeze['prefix'], namespace=freeze['namespace'])
    backend = Qdrant(settings)
    selection = json.loads((args.out / 'selection.json').read_text())
    rows = []
    types, origins = Counter(), Counter()
    for corpus in sorted({row['corpus_id'] for row in selection}):
        store = Store(user_id=corpus, settings=settings, client=backend)
        try:
            with store.read_snapshot():
                facts = store.records('fact')
                entities = store.records('entity')
                relations = store.records('relation')
                ids = {fact['id'] for fact in facts}
                rows.append(dict(corpus_id=corpus, facts=len(facts), entities=len(entities), relations=len(relations),
                    extracted_relations=sum(row.get('origin') == 'extracted' for row in relations),
                    legacy_associations=sum(row.get('origin') == 'legacy_key' for row in relations),
                    orphan_relation_fact_ids=sum(row['fact_id'] not in ids for row in relations)))
                types.update(row['type'] for row in entities)
                origins.update(row.get('origin', 'unknown') for row in relations)
        finally:
            store.close()
    result = dict(phase=execution['current'], source_sha256=freeze['source']['source_sha256'], corpora=len(rows),
        facts=sum(row['facts'] for row in rows), entities=sum(row['entities'] for row in rows),
        relations=sum(row['relations'] for row in rows), entity_types=dict(types), relation_origins=dict(origins),
        orphan_relation_fact_ids=sum(row['orphan_relation_fact_ids'] for row in rows), by_corpus=rows,
        method='Read-only metadata aggregation over latest tenant-visible evaluation records; no model calls, questions or gold labels.')
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: result[key] for key in ('corpora', 'facts', 'entities', 'relations', 'orphan_relation_fact_ids')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
