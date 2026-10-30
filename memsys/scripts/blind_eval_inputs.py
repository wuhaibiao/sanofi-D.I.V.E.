"""Physical separation of source, question and label files for future evals."""
import hashlib
import json
from pathlib import Path

from memsys.bench.longmemeval import GoldLabel, HistorySession, ReplayItem, _split
from memsys.schema import Turn
from memsys.timeutil import parse_datetime

FILES = {'write': 'write-inputs.json', 'read': 'read-inputs.json', 'judge': 'judge-inputs.json'}
FIELDS = {
    'write': {'question_id', 'dataset', 'category', 'corpus_id', 'sessions'},
    'read': {'question_id', 'dataset', 'category', 'corpus_id', 'question', 'question_date', 'session_ids'},
    'judge': {'question_id', 'dataset', 'category', 'corpus_id', 'question', 'question_date',
              'answer', 'judge_type', 'abstention'},
}


def payloads(sample, selection):
    """Trusted preparation only, after metadata-only selection is fixed."""
    metadata = {row['question_id']: row for row in selection}
    result = {phase: [] for phase in FILES}
    for source in sample:
        item, gold = _split(source)
        meta = metadata[item.question_id]
        common = {key: meta[key] for key in ('question_id', 'dataset', 'category', 'corpus_id')}
        sessions = [dict(session_id=s.session_id, date=s.date.isoformat(),
            turns=[dict(role=t.role, content=t.content) for t in s.turns]) for s in item.sessions]
        result['write'].append(dict(common, sessions=sessions))
        result['read'].append(dict(common, question=item.question, question_date=item.question_date.isoformat(),
            session_ids=[s.session_id for s in item.sessions]))
        result['judge'].append(dict(common, question=item.question, question_date=item.question_date.isoformat(),
            answer=meta.get('scoring_reference') or gold.answer, judge_type=meta['judge_type'], abstention=meta['abstention']))
    return result


def materialize(out, sample, selection):
    hashes = {}
    for phase, data in payloads(sample, selection).items():
        path = Path(out) / FILES[phase]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        hashes[phase] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def load_phase(out, freeze, phase, dataset):
    """Read ONLY the phase's frozen input file; never fall back to sample.json."""
    expected = freeze.get('blind_input_sha256', {}).get(phase)
    if not expected:
        raise ValueError('Blind inputs are missing; prepare a new run. Legacy scores are unchanged.')
    data = (Path(out) / FILES[phase]).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('Frozen blind input changed: ' + phase)
    metadata, items = {}, []
    for row in json.loads(data):
        if set(row) != FIELDS[phase]:
            raise ValueError('Unexpected fields in blind ' + phase + ' input')
        if row['dataset'] != dataset:
            continue
        meta = {key: row[key] for key in ('question_id', 'dataset', 'category', 'corpus_id')}
        if phase == 'write':
            sessions = []
            for session in row['sessions']:
                if set(session) != {'session_id', 'date', 'turns'}:
                    raise ValueError('Unexpected source-session fields')
                for turn in session['turns']:
                    if set(turn) != {'role', 'content'} or turn['role'] not in ('user', 'assistant'):
                        raise ValueError('Unexpected source-turn fields')
                sessions.append(HistorySession(session['session_id'], parse_datetime(session['date']),
                    tuple(Turn(t['role'], t['content']) for t in session['turns'])))
            if not sessions:
                raise ValueError('Missing source sessions')
            item = ReplayItem(row['question_id'], '', sessions[0].date, tuple(sessions))
            gold = None
        else:
            at = parse_datetime(row['question_date'])
            sessions = tuple(HistorySession(sid, at, ()) for sid in row.get('session_ids', []))
            item = ReplayItem(row['question_id'], row['question'], at, sessions)
            gold = None
            if phase == 'judge':
                gold = GoldLabel(row['question_id'], row['judge_type'], row['question'], row['answer'])
                meta.update(judge_type=row['judge_type'], abstention=row['abstention'])
        metadata[item.question_id] = meta
        items.append((item, gold))
    return metadata, items
