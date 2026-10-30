"""Literal user-turn index for reading advice in long mixed-role histories."""
from __future__ import annotations
import re


def temporal_user_index(evidence, question, limit=3000):
    """Copy short dated-event cues; never infer event dates or completion."""
    if not re.search(r'\b(?:ago|since|until|before|after|between|when|date|last|days?|weeks?|months?|years?)\b|多久|昨天|上周|何时',question,re.I):
        return ''
    blocks=[]
    size=0
    cue=re.compile(r'\b(?:today|yesterday|tomorrow|ago|last week|finished|completed|cancelled|canceled|started|joined|sold|moved|returned)\b|今天|昨天|明天|完成|取消',re.I)
    for text in evidence.values():
        if not isinstance(text,str) or '\n' not in text:continue
        header,body=text.split('\n',1)
        if not re.match(r'^\[E\d+\]',header) or not body.startswith('User: '):continue
        user=body[len('User: '):].split('\nAssistant: ',1)[0]
        for sentence in re.split(r'(?<=[.!?])\s+|\n',user):
            if not cue.search(sentence):continue
            block=header+'\nUser: '+sentence
            if size+len(block)>limit:continue
            blocks.append(block);size+=len(block)
    if not blocks:return ''
    return ('Literal user event cues. Headers are recording dates, not necessarily occurrence dates. '
            'Keep each statement\'s plans, uncertainty and relative-time words; full originals remain authoritative.\n\n'
            +'\n\n'.join(blocks))


def literal_user_index(evidence, purpose='advice'):
    """Copy user prefixes without interpreting their truth or completion state.

    Original episode blocks start with a source header and an explicit role
    label. The first assistant label ends the user prefix; other evidence
    shapes are skipped. Full originals remain the authoritative evidence.
    """
    blocks = []
    for text in evidence.values():
        if not isinstance(text, str) or '\n' not in text:
            continue
        header, body = text.split('\n', 1)
        if not header.startswith('[') or not body.startswith('User: '):
            continue
        prefix = body.split('\nAssistant: ', 1)[0]
        if prefix[len('User: '):].strip():
            blocks.append(header + '\n' + prefix)
    if not blocks:
        return ''
    if purpose == 'quantity':
        from .quantity_scope import relative_quantity_rules
        relation_rules = relative_quantity_rules(evidence, 'count_entities')
        return ('Original user statements, copied verbatim for quantity scope. Read complete noun phrases '
                'and linked turns before assigning a count. A relative clause may qualify an embedded noun; '
                'an assistant paraphrase cannot override the original wording. If attachment is uncertain, '
                'give the original full object phrase and count with that qualification; do not assert '
                'one object alone has the count. Full originals remain authoritative.'
                + ('\n' + relation_rules if relation_rules else '') + '\n\n' + '\n\n'.join(blocks))
    return ('Original user statements, copied verbatim for reference. Explicitly connect advice to the '
            'applicable recalled circumstance and explain its relevance; general category tips alone lose that link. '
            'Assess objects separately: use of "my new/current" supports possession unless prospective. '
            'Recommendations do not revoke possession; questions/plans remain uncertain. Build advice on '
            'existing items instead of suggesting the same purchase. '
            'Full original conversations follow.\n\n' + '\n\n'.join(blocks))
