"""Compact, lossless presentation of the fields in one extracted fact."""
from __future__ import annotations

import json
import re
import hashlib
from pathlib import Path


def index_signature():
    return dict(fact_content_format=4,
                fact_content_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())


def value_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def fact_text(fact, *, value_reference=None) -> str:
    """Include values missing from the sentence; never clip a field to make it fit."""
    text = (fact['text'] if isinstance(fact, dict) else fact.text).strip()
    value = value_text(fact.get('value') if isinstance(fact, dict) else fact.value)
    # A literal match is conservative: 10 does not match 100, and a dictionary
    # cannot be inferred from a sentence saying that a table was supplied.
    literal = re.escape(value)
    if value and not re.search(r"(?<!\w)" + literal + r"(?!\w)", text, re.IGNORECASE):
        text += ("\nValue: same structured value as [%s]" % value_reference if value_reference
                 else "\nValue: " + value)
    semantics = (fact.get('semantics') if isinstance(fact, dict) else getattr(fact, 'semantics', None)) or {}
    state = semantics.get('event_status')
    if state in ('planned', 'ongoing', 'completed', 'cancelled'):
        text += '\nEvent status: ' + state
    relations = fact.get('relations', []) if isinstance(fact, dict) else getattr(fact, 'relations', [])
    if relations:
        from .relational_memory import contains_name
        view = []
        for relation in relations:
            if not relation.get('grounded'):
                continue
            row = dict(subject=relation['subject']['name'], predicate=relation['predicate'], object=relation['object']['name'])
            for field in ('state', 'valid_from', 'valid_to', 'qualifiers'):
                if relation[field] and relation[field] != 'unknown':
                    row[field] = relation[field]
            for field in ('subject', 'object'):
                entity = relation[field]
                aliases = [a for a in entity['aliases'] if not contains_name(text, a)]
                if aliases:
                    row[field + '_aliases'] = aliases
                if entity['identity'] and not contains_name(text, entity['identity']):
                    row[field + '_identity'] = entity['identity']
            view.append(row)
        if view:
            text += '\nRelations: ' + json.dumps(view, ensure_ascii=False, separators=(',', ':'))
    return text


def fact_identity(fact) -> str:
    """Exact content identity, including a value that a summary sentence may omit."""
    value = value_text(fact.value)
    try:
        parsed = json.loads(value)
        if isinstance(parsed, (dict, list)):
            value = parsed
    except (ValueError, TypeError):
        pass
    event = fact.event
    content = dict(text=fact.text.strip(), value=value, kind=fact.kind,
        subject=fact.subject, attribute=fact.attribute, semantics=fact.semantics,
        event=[event.start.isoformat(), event.end.isoformat(), event.precision] if event else None)
    relations = getattr(fact, 'relations', [])
    if relations:
        from .relational_memory import relation_identity
        content['relations'] = sorted(relation_identity(r) for r in relations)
    return json.dumps(content,
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_value(value) -> str:
    """Serialize structured values without repr or silent truncation."""
    if isinstance(value, dict) and value.get('type') in ('table', 'sequence', 'list'):
        kind = value['type']
        if kind == 'list':
            if not isinstance(value.get('items'), list):
                raise ValueError('list memory value requires an ordered items array')
        else:
            columns, rows = value.get('columns'), value.get('rows')
            if (not isinstance(columns, list) or not columns or
                    not all(isinstance(c, str) and c.strip() for c in columns) or
                    len(set(columns)) != len(columns) or not isinstance(rows, list) or
                    not all(isinstance(row, list) and len(row) == len(columns) for row in rows)):
                raise ValueError('table/sequence memory requires unique columns and matching rows')
    return value_text(value)
