"""Isolate malformed fact entries without inventing values or source links."""
from __future__ import annotations

import math
import re


def _round_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    match = re.fullmatch(r'(?:r(?:ound)?\s*)?(\d+)', str(value).strip(), re.I)
    return int(match[1]) if match else None


def _quoted_rounds(item, rounds):
    quotes = item.get('source_quotes')
    if not isinstance(quotes, list) or not quotes:
        return [], []
    resolved, bound_quotes = [], []
    for quote in quotes:
        if not isinstance(quote, dict) or quote.get('role') not in ('user', 'assistant'):
            return [], []
        text = quote.get('quote')
        if not isinstance(text, str) or not text.strip():
            return [], []
        matches = [(r.number, getattr(r, quote['role'] + '_text').count(text)) for r in rounds
                   if text in getattr(r, quote['role'] + '_text')]
        if len(matches) != 1 or matches[0][1] != 1:
            return [], []
        number = matches[0][0]
        if number not in resolved:
            resolved.append(number)
        bound_quotes.append(dict(quote, round=number))
    return resolved, bound_quotes


def isolate_entries(entries, rounds, normalize, *, entities=None):
    """Return normalized facts, rejected indices and lossless protocol repairs.

    A literal quote may establish a unique source round. Topic similarity,
    historical reference context and a guessed source number never do.
    """
    accepted, rejected, repaired = [], [], []
    numbers = [r.number for r in rounds]
    for index, entry in enumerate(entries):
        def reject(code, detail=''):
            rejected.append(dict(index=index, code=code, detail=detail))
        if not isinstance(entry, dict):
            reject('fact_not_object')
            continue
        if not isinstance(entry.get('text'), str):
            reject('text_not_string')
            continue
        if not entry['text'].strip():
            # Empty placeholders are not assertions, matching prior behavior.
            continue
        item = dict(entry)
        raw = item.get('rounds')
        values = raw if isinstance(raw, list) else [] if raw is None else [raw]
        parsed = [_round_number(value) for value in values]
        valid = bool(parsed) and all(number in numbers for number in parsed)
        source = list(dict.fromkeys(parsed)) if valid else []
        if not valid:
            source, bound_quotes = _quoted_rounds(item, rounds)
            if source:
                item['source_quotes'] = bound_quotes
        method = 'literal_unique_quotes' if source and not valid else 'round_representation'
        if not source and not values and len(numbers) == 1:
            source, method = list(numbers), 'only_source_round'
        if not source:
            reject('unresolved_source_rounds')
            continue
        item['rounds'] = source
        if raw != source:
            repaired.append(dict(index=index, field='rounds', method=method, rounds=source))
        try:
            facts = normalize({'facts': [item], 'entities': entities}, numbers)
            if len(facts) != 1 or not facts[0].rounds:
                reject('normalization_did_not_retain_fact')
                continue
        except (ValueError, TypeError, OverflowError) as exc:
            reject('invalid_fact_value', type(exc).__name__ + ': ' + str(exc))
            continue
        accepted.extend(facts)
    return accepted, rejected, repaired
