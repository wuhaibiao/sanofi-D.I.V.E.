"""Select fixed cases by type and history length; never use answer correctness."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--per-type', type=int, default=5)
    parser.add_argument('--seed', default='memory-representative-20261010')
    args = parser.parse_args()
    if args.per_type < 2:
        raise SystemExit('per-type must be at least two to cover different history lengths')
    path = Path(args.data)
    rows = json.loads(path.read_text())
    def history_size(item):
        return sum(len(turn.get('content', '').encode('utf-8'))
                   for session in item['haystack_sessions'] for turn in session)
    def tie(item):
        return hashlib.sha256((args.seed+'|'+str(item['question_id'])).encode()).hexdigest()
    selected, details = [], []
    for kind in sorted({r['question_type'] for r in rows}):
        pool = sorted((r for r in rows if r['question_type']==kind and not str(r['question_id']).endswith('_abs')),
                      key=lambda r: (history_size(r), tie(r)))
        count = min(args.per_type, len(pool))
        indices = [0] if count == 1 else sorted({round((len(pool)-1)*n/(count-1)) for n in range(count)})
        chosen = [pool[index] for index in indices]
        abstentions = sorted((r for r in rows if r['question_type']==kind and str(r['question_id']).endswith('_abs')),
                             key=lambda r: (history_size(r), tie(r)))
        if abstentions:
            chosen.append(abstentions[len(abstentions)//2])
        selected.extend(chosen)
        details.append(dict(question_type=kind,answerable=count,abstention=int(bool(abstentions)),
                            question_ids=[r['question_id'] for r in chosen],
                            history_bytes=[history_size(r) for r in chosen]))
    assert len({r['question_id'] for r in selected}) == len(selected)
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    sample=out/'sample.json';sample.write_text(json.dumps(selected,ensure_ascii=False,indent=2)+'\n')
    manifest=dict(method='Equal answerable cases per type at evenly spaced UTF-8 history-length ranks; one median-length abstention per available type. Hash-seeded tie breaks. No correctness, prior outputs or gold-answer contents used.',
                  selector_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  seed=args.seed,source=str(path.resolve()),source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                  sample_sha256=hashlib.sha256(sample.read_bytes()).hexdigest(),total=len(selected),types=details,
                  limitations='Small stratified diagnostic sample; not an unbiased estimate of full-dataset accuracy. Includes extreme history lengths.')
    (out/'selection.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(total=len(selected),types=[{k:r[k] for k in ('question_type','answerable','abstention')} for r in details])))


if __name__ == '__main__':
    main()
