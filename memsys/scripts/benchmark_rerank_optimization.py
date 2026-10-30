#!/usr/bin/env python3
"""Local BGE runtime comparison using only self-authored synthetic documents."""
import argparse
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def fixture():
    """No benchmark files, questions, reference labels or model calls are used."""
    notes = (' Equipment calibration is recorded separately from inventory. '
             'A purchase plan is not a completed purchase. Each independent shipment retains '
             'its date, quantity, owner, status and storage location. The recorded count refers '
             'to the specified container and cannot be added to a cumulative total twice.')
    english = [dict(key='en-' + str(i), text=(
        f'On 2025-06-{i+1:02d}, Workshop {i} assigned container C{i} to inspector Noa. '
        f'The container holds {i+11} sealed blue instruments; another {i+2} are planned but not delivered. '
        f'The container belongs to department D{i%5}, rather than the neighboring department. '
        f'It is inspected on weekday {i%5+1}; weekend inspections require explicit approval.' + notes)) for i in range(24)]
    english[3]['text'] = ('On 2025-06-12, Aster Studio transferred the Cedar project from Noa to Mira. '
        'Mira is the current project owner; Noa remains the previous owner. The transfer is completed. '
        'The budget is 120 units, of which 12 have been spent. The 120 is a total, not an increment.' + notes)
    english[10]['text'] = ('On 2025-05-03, Aster Studio considered transferring Cedar to Rowan. '
        'This plan was cancelled. Rowan never became Cedar project owner. The considered budget was 210 units.' + notes)
    chinese = [dict(key='zh-' + str(i), text=(
        f'2025年7月{i+1}日，站点{i}的负责人是顾{i}，保管编号为箱{i}的物品。'
        f'箱内现有{i+10}件完整仪器，另有{i+2}件仍在采购计划中，尚未交付。'
        '数量只适用于这个箱子，不能把累计库存当作新增数量。周末与工作日有不同安排，临时安排不覆盖永久安排。'
        '所有记录都应保留对象、时间、状态和生效范围；建议本身并不意味着已经采纳或执行。')) for i in range(24)]
    chinese[5]['text'] = ('林澈通常周一至周四乘地铁上班，周五骑自行车。2025年7月18日因为下雨改乘公交，'
        '这只是当天的临时例外，不改变通常安排。地铁单程35分钟，骑车单程25分钟。'
        '他拥有一辆蓝色自行车，没有购买此前推荐的红色车型。原先计划花120元维修，实际支付了110元。')
    chinese[17]['text'] = ('周宁每天骑自行车，单程35分钟。她推荐林澈购买一辆红色自行车，'
        '但没有说林澈已经购买。推荐、拥有、临时例外和通常安排是不同事实。')
    long_docs = []
    for i in range(16):
        body = f'Archive item {i}. All values apply to this item only.\n'
        for n in range(30 if i < 4 else 3):
            body += f'{n+1}. Inspection {n+1}: drawer {i}, count {n+10}, status planned, owner team {i%4}.\n'
        if i == 1:
            body += 'FINAL STATUS: The Oriole shipment was delivered on 2025-08-19. Actual quantity: 17. Planned quantity: 70.\n'
        else:
            body += f'FINAL STATUS: Shipment sample-{i} is still planned; no delivery date has been established.\n'
        long_docs.append(dict(key='long-' + str(i), text=body,
                              header='Structured archive record.\n', source_start=37, source_role='User'))
    return [dict(case_id='english-state', queries=['Who currently owns the Cedar project at Aster Studio?',
                'Did Rowan become the Cedar project owner, or was that transfer cancelled?'], documents=english),
            dict(case_id='chinese-scope', queries=['林澈通常周五怎么上班，单程多久？',
                '林澈买了推荐的红色自行车吗？'], documents=chinese),
            dict(case_id='long-tail', queries=['When was the Oriole shipment delivered, and what quantity actually arrived?',
                'Oriole货物实际交付了多少件，计划数量是多少？'], documents=long_docs)]


class TokenizerObserver:
    def __init__(self, inner):
        self.inner = inner
        self.characters = self.encodes = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def encode(self, text, *args, **kwargs):
        self.characters += len(text) + (len(args[0]) if args and isinstance(args[0], str) else 0)
        self.encodes += 1 + bool(args and isinstance(args[0], str))
        return self.inner.encode(text, *args, **kwargs)

    def __call__(self, text, *args, **kwargs):
        def strings(value):
            return [value] if isinstance(value, str) else [s for row in value for s in strings(row)]
        values = strings(text)
        self.characters += sum(map(len, values))
        self.encodes += len(values)
        return self.inner(text, *args, **kwargs)


def run(args):
    os.environ['HF_HUB_OFFLINE'] = '1'
    sys.path.insert(0, str(args.source_root.resolve()))
    from memsys.bench.provenance import source_manifest
    from memsys.config import MemorySettings
    from memsys.reranking import BGEReranker, RerankDocument
    settings = MemorySettings.from_dict(json.loads((args.out / 'memory-settings.json').read_text()))
    settings = replace(settings, rerank_dtype=args.dtype, rerank_batch_size=args.batch)
    target = args.out / args.name
    target.mkdir()
    cases = json.loads((args.out / 'inputs.json').read_text())
    backend = BGEReranker(settings)
    started = time.monotonic()
    backend._load()
    load_s = time.monotonic() - started
    fast = backend.tokenizer.tokenizer
    observer = TokenizerObserver(fast)
    backend.tokenizer.tokenizer = observer
    backend.rank('Which item is green?', [RerankDocument('warm', 'A green calibration item is ready.')])
    info = dict(name=args.name, dtype=args.dtype, batch=args.batch, load_s=load_s,
                source=source_manifest(), device=backend.device, repeats=args.repeats,
                fixture_sha256=hashlib.sha256((args.out / 'inputs.json').read_bytes()).hexdigest(),
                method='Self-authored inputs only. One process/model per variant, sequential calls, '
                       'warm-up excluded. Cold-passage, shared-passage/new-query, and repeated-query conditions. '
                       'No LLM or Qdrant calls. Full candidate/window sets retained.')
    save(target / 'manifest.json', info)
    output = target / 'measurements.jsonl'
    for repeat in range(args.repeats):
        for case in cases:
            documents = [RerankDocument(**row) for row in case['documents']]
            for condition, query_idx in [('cold-passages', 0), ('shared-passages-new-query', 1), ('repeat-request', 0)]:
                if condition == 'cold-passages' and hasattr(backend, 'encodings'):
                    backend.encodings.clear()
                query = case['queries'][query_idx]
                before_chars, before_encodes = observer.characters, observer.encodes
                extra_before = backend.encodings.info() if hasattr(backend, 'encodings') else {}
                begin = time.monotonic()
                scores, trace = backend.rank(query, documents)
                elapsed = time.monotonic() - begin
                assert set(scores) == {d.key for d in documents} and all(math.isfinite(v) for v in scores.values())
                after = backend.encodings.info() if hasattr(backend, 'encodings') else {}
                chars = observer.characters-before_chars + after.get('encoded_characters', 0)-extra_before.get('encoded_characters', 0)
                encodes = observer.encodes-before_encodes + after.get('encoded_texts', 0)-extra_before.get('encoded_texts', 0)
                by_key = {d.key:d for d in documents}
                ends = {}
                for w in trace['windows']:
                    key = w['key']
                    assert w['start'] <= ends.get(key, 0) and w['end'] > ends.get(key, 0)
                    ends[key] = w['end']
                    assert w['pair_tokens'] <= settings.rerank_max_tokens
                assert all(ends[d.key] == len(d.text) for d in documents)
                # Compare actual optimized packing with the unmodified HF pair
                # tokenizer, including masks and segment IDs, outside timing.
                token_equivalence = True
                if repeat == 0 and condition == 'cold-passages' and hasattr(backend, 'encodings'):
                    q = backend.encodings.encode(query)
                    for w in trace['windows']:
                        doc = backend.encodings.document(q, '', w['text'])
                        packed = doc.prepare(w['text'], fast.model_input_names)
                        assert packed == dict(fast(query, w['text'], truncation=False))
                record = dict(case_id=case['case_id'], condition=condition, repeat=repeat,
                    elapsed_s=elapsed, encoded_characters=chars, encoded_sequences=encodes,
                    scores=scores, order=sorted(scores, key=lambda k:(-scores[k], k)),
                    full_source_coverage=True, exact_reference_tokens=token_equivalence, trace=trace)
                with output.open('a') as f:
                    f.write(json.dumps(record, ensure_ascii=False) + '\n')
                print(args.name + ' ' + case['case_id'] + ' ' + condition + ' ' + str(repeat+1) +
                      ' ' + str(round(elapsed, 3)) + 's', flush=True)
    records = [json.loads(line) for line in output.read_text().splitlines()]
    result = dict(info, measurements=len(records), by_condition={})
    for condition in sorted({r['condition'] for r in records}):
        group = [r for r in records if r['condition'] == condition]
        values = sorted(r['elapsed_s'] for r in group)
        result['by_condition'][condition] = dict(mean_s=statistics.mean(values), median_s=statistics.median(values),
            max_s=max(values), p95_s=values[max(0, math.ceil(.95*len(values))-1)],
            encoded_characters=sum(r['encoded_characters'] for r in group))
    save(target / 'summary.json', result)
    print('DONE ' + args.name + ' ' + json.dumps(result['by_condition']), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'run'))
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, default=ROOT / 'src')
    parser.add_argument('--name', default='optimized-fp32-b4')
    parser.add_argument('--dtype', choices=('float32', 'float16'), default='float32')
    parser.add_argument('--batch', type=int, choices=(4, 8), default=4)
    parser.add_argument('--repeats', type=int, default=2)
    args = parser.parse_args()
    args.out = args.out.resolve()
    if args.action == 'prepare':
        if (args.out / 'inputs.json').exists():
            raise ValueError('Preserve frozen synthetic fixture')
        save(args.out / 'inputs.json', fixture())
        save(args.out / 'acceptance.json', dict(
            input_policy='Self-authored and frozen before measurements; never read public benchmark inputs/answers.',
            retain_all_candidates_and_source_characters=True, require_exact_fp32_pair_tokens=True,
            same_batch_fp32_max_score_difference=0.0005, cross_batch_fp32_max_score_difference=0.001,
            default_precision_policy='Keep float32; FP16 results are diagnostic and do not prove end-to-end quality.',
            batch8_policy='Adopt only if full coverage and top1/top5 identities hold, score tolerance passes, '
                          'and paired median timing improves at least 5%; otherwise retain batch4.'))
        print('Frozen 3 self-authored scenarios; 64 documents; 6 questions. No benchmark inputs.')
    else:
        run(args)


if __name__ == '__main__':
    main()
