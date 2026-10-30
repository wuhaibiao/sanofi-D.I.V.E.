#!/usr/bin/env python3
"""Offline forensic checks of existing frozen runs; no model or database calls."""
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from memsys.bench.leakcheck import check, project_files
from memsys.fact_content import fact_text
from memsys.timeutil import parse_datetime, format_day, format_stamp
from memsys.write import Round, build_rounds

PARENT = ROOT / 'runs/fact-retention-20261013'
OUT = ROOT / 'runs/evaluation-integrity-20261014'


def read(path):
    return json.loads(path.read_text())


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def constants(path):
    values = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(value, str):
                    values[target.id] = value
    return values


def main():
    OUT.mkdir(exist_ok=True)
    source_files = [p for p in (ROOT / 'src/memsys').rglob('*.py') if 'bench' not in p.parts]
    leaks = check(str(ROOT / 'data/longmemeval_oracle.json'), [str(p) for p in source_files])
    repository_files = project_files(str(ROOT))
    repository_leaks = check(str(ROOT / 'data/longmemeval_oracle.json'), repository_files)
    checks, errors, file_copies, capture_limits = Counter(), [], [], []
    seen = set()
    for out in sorted(PARENT.iterdir()):
        if not (out / 'freeze.json').exists():
            continue
        freeze, sample, selection = read(out / 'freeze.json'), read(out / 'sample.json'), read(out / 'selection.json')
        for name in ('sample', 'selection'):
            checks['frozen_input_hashes'] += 1
            if digest(out / (name + '.json')) != freeze[name + '_sha256']:
                errors.append(dict(where=out.name, check='frozen_' + name))
        metadata = {m['question_id']: m for m in selection}
        items = {q['question_id']: q for q in sample}
        by_corpus = {metadata[qid]['corpus_id']: q for qid, q in items.items()}
        writes = {w['corpus_id']: w for w in lines(out / 'LongMemEval-write.jsonl')}
        facts = {cid: {str(f['id']): f for f in w['facts']} for cid, w in writes.items()}
        sources = {(cid, s['session_id']): s['source_session_id'] for cid, w in writes.items() for s in w['sessions']}
        prompt_constants = constants(out / 'frozen-src/memsys/prompts.py')
        host_system = constants(out / 'frozen-src/memsys/webchat.py')['SYSTEM']
        for path in sorted(out.glob('LongMemEval-*-prompts.jsonl')):
            signature = digest(path)
            if signature in seen:
                file_copies.append(str(path.relative_to(PARENT)))
                continue
            seen.add(signature)
            for record in lines(path):
                try:
                    if record['purpose'] == 'extract':
                        cid, sid = record['tags']['corpus_id'], record['tags']['session_id']
                        q = by_corpus[cid]
                        source_id = sources[cid, sid]
                        index = q['haystack_session_ids'].index(source_id)
                        at = parse_datetime(q['haystack_dates'][index])
                        turns = [dict(role=t['role'], content=t['content'], at=at.isoformat())
                                 for t in q['haystack_sessions'][index]]
                        rounds = [Round(i + 1, i, *r) for i, r in enumerate(build_rounds(turns))]
                        user = record['messages'][1]['content']
                        existing = user.split('block):\n', 1)[1].split('\n\nRecent processed messages', 1)[0]
                        recent = user.split('Recent processed messages (reference only; never extract facts or source quotes from this block):\n', 1)[1].split('\n\nNew conversation rounds:', 1)[0]
                        for f in json.loads(existing):
                            stored = facts[cid][f['id'].lstrip('F')]
                            assert f['text'] == fact_text(stored)
                            checks['existing_facts_bound_to_write_records'] += 1
                        # These runs ingest each complete source session once.
                        assert json.loads(recent) == []
                        expected = prompt_constants['EXTRACT_USER'].format(session_date=format_stamp(at),
                            rounds='\n\n'.join(r.render() for r in rounds), existing_memories=existing, recent_messages=recent)
                        assert record['messages'] == [dict(role='system', content=prompt_constants['EXTRACT_SYSTEM']),
                                                      dict(role='user', content=expected)]
                        checks['exact_extract_prompts_from_originals_and_stored_facts'] += 1
                    elif record['purpose'] == 'answer':
                        qid = record['tags']['question_id']
                        q, meta = items[qid], metadata[qid]
                        trace = read(out / 'traces' / (qid + '.json'))
                        context = prompt_constants['MEMORY_CONTEXT'].format(today=format_day(parse_datetime(q['question_date'])),
                            memory=trace['memory'], rules=prompt_constants['MEMORY_RULES'])
                        expected = [dict(role='system', content=host_system + '\n\n<retrieved_memory>\n' + context + '\n</retrieved_memory>'),
                                    dict(role='user', content=q['question'])]
                        assert record['messages'] == expected
                        checks['answer_questions_exactly_match_frozen_questions'] += 1
                        checks['answer_prompts_exactly_match_frozen_rules_and_trace'] += 1
                        assert trace['selected']['episodes'] == []
                        answers = {a['question_id']: a for a in lines(out / 'LongMemEval-answers.jsonl')}
                        answer = answers[qid]
                        if 'memory_items' in answer:
                            assert answer['pending_evidence_items'] == 0
                            for f in answer['memory_items']:
                                stored = facts[meta['corpus_id']][f['key'].lstrip('F')]
                                assert f['kind'] == 'fact' and f['text'] == fact_text(stored)
                        else:
                            assert answer['status'] == 'error'
                            for key in trace['selected']['facts']:
                                stored = facts[meta['corpus_id']][key.lstrip('F')]
                                assert fact_text(stored) in trace['memory']
                            capture_limits.append(dict(cohort=out.name, question_id=qid,
                                note='Failed answer lacks item metadata; exact model prompt reconstructed from frozen question/rules and fact-bound trace instead.'))
                        checks['answer_memory_bound_to_stored_facts'] += 1
                    elif record['purpose'] == 'judge':
                        checks['label_bearing_judge_calls'] += 1
                except (AssertionError, KeyError, ValueError, IndexError) as exc:
                    errors.append(dict(where=str(path.relative_to(PARENT)), purpose=record['purpose'],
                        tags=record.get('tags'), type=type(exc).__name__, error=str(exc)))
    plan = read(PARENT / 'unseen-24/selection-plan.json')
    used = set(read(PARENT / 'prior-question-exclusion.json')['used_ids'])
    pool = read(ROOT / 'data/longmemeval_oracle.json')
    remaining = [q for q in pool if str(q['question_id']) not in used]
    rng, selected = random.Random(plan['seed']), []
    for category, count in plan['counts'].items():
        eligible = sorted([q for q in remaining if (str(q['question_id']).endswith('_abs') if category == 'abstention'
            else q['question_type'] == category and not str(q['question_id']).endswith('_abs'))], key=lambda q: str(q['question_id']))
        selected.extend(str(q['question_id']) for q in rng.sample(eligible, count))
    actual = [m['source_question_id'] for m in read(PARENT / 'unseen-24/selection.json')]
    selection_exact = actual == selected and not (set(actual) & used)
    report = dict(production_files_scanned=len(source_files), production_literal_hits=[vars(x) for x in leaks],
        repository_files_scanned=len(repository_files), repository_literal_hits=[vars(x) for x in repository_leaks],
        production_source_sha256={str(p.relative_to(ROOT)): digest(p) for p in source_files},
        observed_prompt_checks=dict(checks), prompt_reconstruction_errors=errors,
        record_capture_limits=capture_limits,
        duplicate_prompt_files_excluded=file_copies, unused_selection_metadata_replay_exact=selection_exact,
        unused_local_pool=len(remaining), used_local_ids=len(used),
        development_answers_were_inspected=True, old_24_is_development_regression=True,
        unused_24_received_repeated_answer_runs=True,
        before_runner_sha256=digest(OUT / 'evaluate_public_types.before.py'),
        actual_remote_model_calls_in_this_audit=0,
        conclusion='No observed runtime label injection or literal hardcoding in inspected modules/logs. Old question failures and golds informed development; neither the old regression nor repeated extra-24 runs is an untouched blind test. Static scan cannot certify absence of semantic overfitting or unrecorded history.')
    (OUT / 'audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ('production_files_scanned', 'production_literal_hits',
        'observed_prompt_checks', 'prompt_reconstruction_errors', 'unused_selection_metadata_replay_exact')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
