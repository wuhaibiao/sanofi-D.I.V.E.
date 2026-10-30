"""Conservative structure coverage and presentation sharing; no semantic guesses."""
import json


def structured_value(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    return value if isinstance(value, (dict, list)) else None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def unanchored_event(fact):
    return fact.kind == 'event' and fact.event is None and not fact.semantics.get('event_key')


def explicit_event_status(entry, semantics):
    """Copy explicit model-supplied status fields, never infer from prose."""
    if entry.get('kind') != 'event':
        return semantics
    from .schema import EVENT_STATUSES
    value = structured_value(entry.get('value'))
    candidates = [semantics.get('event_status'), entry.get('event_status')]
    if isinstance(value, dict):
        candidates.extend(value.get(k) for k in ('event_status', 'status'))
    statuses = {s.strip().casefold() for s in candidates
                if isinstance(s, str) and s.strip().casefold() in EVENT_STATUSES}
    known = statuses - {'unknown'}
    if len(known) > 1:
        raise ValueError('conflicting explicit event status fields')
    if statuses:
        semantics = dict(semantics, event_status=next(iter(known or statuses)))
    return semantics


def covers_literal(value, literal):
    """Prove exact cells or named/short items and positions already exist."""
    model = structured_value(value)
    if model is None or not isinstance(literal, dict) or literal.get('omitted_positions'):
        return False
    if literal.get('headers') and (not isinstance(model,dict) or canonical(model.get('headers'))!=canonical(literal['headers'])):
        return False
    if literal.get('type') == 'fields':
        return (isinstance(model, dict) and model.get('type') == 'fields' and
            all(canonical(model.get(k)) == canonical(literal.get(k)) for k in ('headers', 'items')))
    if literal.get('type') == 'table':
        return (isinstance(model, dict) and model.get('type') == 'table' and
            all(canonical(model.get(k)) == canonical(literal.get(k)) for k in ('columns', 'rows')))
    if literal.get('type') != 'list':
        return False
    source = literal.get('items')
    items = model if isinstance(model, list) else model.get('items') if model.get('type') == 'list' else None
    if isinstance(model, dict) and model.get('type') == 'sequence':
        columns, rows = model.get('columns'), model.get('rows')
        if not isinstance(columns, list) or not isinstance(rows, list) or not isinstance(source, list):
            return False
        names = [c.strip().casefold() if isinstance(c, str) else '' for c in columns]
        ordinal = [i for i, name in enumerate(names) if name in ('position', 'step', 'order')]
        if len(ordinal) != 1 or len(rows) != len(source):
            return False
        items = []
        for row, expected in zip(rows, source):
            field = 'name' if 'name' in expected else 'statement'
            aliases = ('name', 'title') if field == 'name' else ('statement', 'action')
            content = [i for i, name in enumerate(names) if name in aliases]
            if len(content) != 1 or not isinstance(row, list) or len(row) != len(columns):
                return False
            items.append(dict(position=row[ordinal[0]], **{field: row[content[0]]}))
    if not isinstance(items, list) or not isinstance(source, list) or len(items) != len(source):
        return False
    for index, (item, expected) in enumerate(zip(items, source), 1):
        field = 'name' if 'name' in expected else 'statement'
        if set(expected) != {'position', field}:
            # Rich/nested items must match every retained value, association and
            # order. A title-only model list cannot cover its child data.
            if not isinstance(item, dict) or any(k not in item or canonical(item[k]) != canonical(v)
                    for k,v in expected.items()):
                return False
            continue
        if isinstance(item, str):
            if expected['position'] != index or item != expected[field]:
                return False
        elif isinstance(item, dict):
            position = item.get('position', index)
            if canonical(position) != canonical(expected['position']) or item.get(field) != expected[field]:
                return False
        else:
            return False
    return True


def omit_covered_literals(model_facts, literal_facts):
    """Keep one richer same-round assistant fact, retaining the exact source quote."""
    retained, covered = [], 0
    for literal in literal_facts:
        value = structured_value(literal.value)
        owner = next((fact for fact in model_facts if fact.kind == 'assistant'
            and set(fact.rounds) == set(literal.rounds) and covers_literal(fact.value, value)), None)
        if owner is None:
            retained.append(literal)
        else:
            for quote in literal.source_quotes:
                if quote not in owner.source_quotes:
                    owner.source_quotes.append(quote)
            covered += 1
    return retained, covered


def shared_value_key(fact):
    """Only share identical structured values with identical interpretation metadata.

    Every distinct sentence and fact ID is still rendered. No stored facts merge.
    """
    value = structured_value(fact.value)
    if value is None:
        return None
    event = fact.event
    return canonical(dict(value=value, kind=fact.kind, subject=fact.subject, attribute=fact.attribute,
        semantics=fact.semantics, event=[event.start.isoformat(), event.end.isoformat(), event.precision] if event else None,
        recorded_at=fact.recorded_at.isoformat(), status=fact.status, confidence=fact.confidence,
        uncertain=fact.uncertain, superseded_by=fact.superseded_by))
