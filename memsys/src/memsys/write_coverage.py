"""Report source accounting separately from semantic completeness; no model calls."""
from __future__ import annotations


def source_coverage(data, rounds, facts, is_trivial=None):
    def quoted_number(value):
        try:return int(str(value).lstrip('Rr'))
        except (ValueError,TypeError):return None
    declared = data.get('coverage') if isinstance(data, dict) else None
    entries = declared if isinstance(declared, list) else []
    sources = {(r.number,role):getattr(r,role+'_text') for r in rounds for role in ('user','assistant')
               if getattr(r,role+'_text').strip()}
    records, warnings = [], []
    for key,text in sources.items():
        number,role=key
        matches=[row for row in entries if isinstance(row,dict) and row.get('round')==number and row.get('role')==role]
        role_quote=lambda f:any(quoted_number(q.get('round'))==number and q.get('role')==role for q in f.source_quotes)
        related=[f for f in facts if number in f.rounds and (role_quote(f) or
            not f.source_quotes and (role=='assistant')==(f.kind=='assistant'))]
        quoted=sum(any(quoted_number(q.get('round'))==number and q.get('role')==role
            and isinstance(q.get('quote'),str) and q['quote'].strip() and text.count(q['quote'])==1
            for q in f.source_quotes) for f in related)
        record=dict(round=number,role=role,facts=len(related),source_quoted_facts=quoted,
                    accounting='unreported',semantic_coverage='not_verified',
                    accounting_basis='fact_source_links_only',content_completeness_verified=False)
        if is_trivial is not None and is_trivial(text.strip()):
            record['accounting']='trivial'
            records.append(record)
            continue
        if len(matches)==1:
            row=matches[0]
            decision=row.get('decision')
            omitted=row.get('omitted',[])
            complete=row.get('omissions_complete',True)
            valid=(decision in ('retained','no_new_memory') and isinstance(complete,bool)
                and isinstance(omitted,list) and len(omitted)<=4 and
                all(isinstance(o,dict) and isinstance(o.get('quote'),str) and o['quote'].strip()
                    and len(o['quote'])<=160 and text.count(o['quote'])==1 and isinstance(o.get('reason'),str) and o['reason'].strip()
                for o in omitted) and (decision!='retained' or bool(related) and quoted>0))
            if decision=='no_new_memory':valid=valid and isinstance(row.get('reason'),str) and bool(row['reason'].strip())
            record.update(accounting='declared' if valid else 'invalid',decision=decision,
                          omissions=omitted if valid else [],omissions_complete=complete if valid else False)
        elif matches:
            record['accounting']='invalid'
        records.append(record)
        if record['accounting']!='declared':
            warnings.append(dict(code='source_accounting_'+record['accounting'],round=number,role=role,
                message='Source accounting is missing or invalid; facts were retained without a retry. Semantic coverage is unverified.'))
        elif record.get('omissions') or not record.get('omissions_complete',True):
            warnings.append(dict(code='declared_source_omissions' if record['omissions_complete'] else 'declared_source_omissions_incomplete',round=number,role=role,
                message='The model declared source omissions; see source_coverage for exact excerpts and reasons.'))
    unknown=[row for row in entries if not isinstance(row,dict) or not isinstance(row.get('round'),int)
        or not isinstance(row.get('role'),str) or (row['round'],row['role']) not in sources]
    if unknown:warnings.append(dict(code='unknown_source_accounting',message='Coverage contains unknown source roles or rounds.'))
    return records,warnings
