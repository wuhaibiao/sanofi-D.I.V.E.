"""Read-only check of frozen ingestion sources, including temporal anchors."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_store(path, item):
    from memsys.timeutil import to_iso
    from memsys.write import build_rounds
    with sqlite3.connect('file:'+str(path.resolve())+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        actual_sessions=db.execute('SELECT * FROM sessions').fetchall()
        by_id={s['external_id']:s for s in actual_sessions}
        if len(by_id)!=len(actual_sessions) or set(by_id)!={s.session_id for s in item.sessions}:
            raise ValueError('Original session identities differ')
        totals=dict(sessions=0,turns=0,episodes=0)
        for session in item.sessions:
            stored=by_id[session.session_id]
            stamp=to_iso(session.date)
            if stored['opened_at']!=stamp or stored['closed_at']!=stamp or stored['status']!='closed':
                raise ValueError('Original session date or completion state differs')
            actual=db.execute('SELECT role,content,at FROM turns WHERE session_id=? ORDER BY idx',
                              (stored['id'],)).fetchall()
            expected=[('user' if t.role=='user' else 'assistant',t.content,stamp) for t in session.turns]
            if [tuple(t) for t in actual]!=expected or stored['turns_done']!=len(expected):
                raise ValueError('Original turn content, role, date or write coverage differs')
            expected_rounds=[(to_iso(at),u,a) for at,u,a in build_rounds(actual)]
            episodes=db.execute('SELECT at,user_text,assistant_text FROM episodes WHERE session_id=? ORDER BY round_idx',
                                (stored['id'],)).fetchall()
            if [tuple(e) for e in episodes]!=expected_rounds:
                raise ValueError('Original episode text or temporal anchor differs')
            totals['sessions']+=1
            totals['turns']+=len(actual)
            totals['episodes']+=len(episodes)
        if db.execute('SELECT count(*) FROM extract_backlog').fetchone()[0]:
            raise ValueError('Extraction backlog is not empty')
        if db.execute('SELECT count(*) FROM fact_sources s LEFT JOIN episodes e ON e.id=s.episode_id WHERE e.id IS NULL').fetchone()[0]:
            raise ValueError('Fact references a missing original episode')
    return dict(totals,exact_originals=True,exact_temporal_anchors=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--iteration',type=Path,required=True)
    p.add_argument('--partial',action='store_true')
    args=p.parse_args()
    root=args.iteration.resolve()
    sys.path.insert(0,str(root/'frozen-src'))
    from memsys.bench.longmemeval import load
    from memsys.bench.provenance import source_manifest,prompt_fingerprint
    freeze=json.loads((root/'freeze.json').read_text())
    source=source_manifest()
    sample=root/freeze['sample_file']
    if (source['source_sha256']!=freeze['source']['source_sha256'] or prompt_fingerprint()!=freeze['prompts']
            or sha(sample)!=freeze['sample_sha256'] or sha(root/'config.frozen.json')!=freeze['config_sha256']):
        raise SystemExit('Frozen evaluation identity changed')
    ingestion=json.loads((root/'ingestion.json').read_text())
    items={item.question_id:item for item,_ in load(str(sample))}
    rows={row['question_id']:row for row in ingestion['results']}
    if not args.partial and (ingestion['status']!='completed' or set(rows)!=set(items)):
        raise SystemExit('Complete frozen ingestion before formal source audit')
    results=[]
    for qid,row in rows.items():
        path=root/'current/stores'/(qid+'.sqlite')
        if sha(path)!=row['store_sha256']:
            raise SystemExit('Recorded memory store changed before reading')
        results.append(dict(question_id=qid,**verify_store(path,items[qid]),store_sha256=row['store_sha256']))
    report=dict(status='passed' if len(results)==len(items) else 'partial',expected=len(items),checked=len(results),
                exact_originals=True,exact_temporal_anchors=True,source_sha256=source['source_sha256'],
                sample_sha256=sha(sample),auditor_sha256=sha(__file__),results=results)
    (root/('source-audit-partial.json' if args.partial else 'source-audit.json')).write_text(
        json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='results'}))


if __name__=='__main__':
    main()
