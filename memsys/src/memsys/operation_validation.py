"""Conservative shape checks for cardinality, not benchmark-specific routing.

A time qualifier is not the unit being counted. Strong object-count syntax can
correct a state/lookup planner error; ambiguous arithmetic remains model-led.
"""
from __future__ import annotations

import re

_CARDINALITY = re.compile(
    r"^\s*how many\s+(?P<head>.+?)\s+"
    r"(?:have|has|had|did|do|does|are|were|was|is|will|can|should|must|would|could)\b",
    re.I)
_HEAD_BOUNDARY = re.compile(r"\b(?:in|from|within|during|over|after|before|since|of|per|a)\b", re.I)
_MEASURED_UNIT = re.compile(
    r"\b(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?|pages?|"
    r"dollars?|cents?|liters?|litres?|gallons?|kilograms?|pounds?|percent)\b", re.I)


def entity_cardinality(question):
    """Recognize an explicit noun count, leaving durations and amounts alone."""
    match = _CARDINALITY.search(question)
    if match is None:
        return False
    head = _HEAD_BOUNDARY.split(match.group('head'), maxsplit=1)[0].strip()
    return bool(head and re.search(r'[a-z]',head,re.I) and not _MEASURED_UNIT.search(head))


def validate_operation(question, declared, fallback):
    """Correct only high-confidence count-vs-state errors and expose the reason."""
    if (fallback=='count_entities' and entity_cardinality(question)
            and declared in ('lookup','latest_state','historical_state','list','advice','date_difference')):
        return 'count_entities', dict(declared=declared, selected='count_entities',
            reason='explicit object cardinality; a time qualifier is not the measured quantity')
    return declared, {}
