"""Conditional instructions for quantities inside nested user object phrases.

The cue detects a reading risk, not a proved syntactic attachment. It never
extracts a result or decides which object owns a number.
"""
import re

_RELATIVE_QUANTITY = re.compile(
    r"\b(?:of|from|with|containing|including)\b[^.!?\n]{1,220},\s*(?:which|that)\b"
    r"[^.!?\n]{0,180}\b(?:\d+(?:[,.]\d+)*|one|two|three|four|five|six|seven|eight|nine|"
    r"ten|eleven|twelve|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand)\b",
    re.I)

QUANTITY_RELATION_RULES = (
    "A user quantity occurs after a relative clause attached to a nested object phrase. "
    "This is an attachment-risk cue, not proof that the wording is ambiguous. Read the complete "
    "user phrase and linked turns before choosing its antecedent. An assistant's confident "
    "paraphrase cannot resolve an attachment that the user did not resolve. If more than one "
    "attachment remains plausible, quote the full original antecedent phrase together with its "
    "quantity clause and state the uncertainty. Preserve the known stated count instead of saying "
    "no count was mentioned, but do not claim it exclusively measures either object. A qualified "
    "quote is not a proved requested-object total and must not be used as a contribution in arithmetic. "
    "Do not use an unrelated nearby count or assume that every nested phrase is ambiguous.")


def relative_quantity_rules(evidence, operation):
    if operation not in ('count_entities', 'sum_events'):
        return ''
    for text in evidence.values():
        if not isinstance(text, str) or '\n' not in text:
            continue
        header, body = text.split('\n', 1)
        if not re.match(r'^\[E\d+\]', header) or not body.startswith('User: '):
            continue
        user = body[len('User: '):].split('\nAssistant: ', 1)[0]
        if _RELATIVE_QUANTITY.search(user):
            return QUANTITY_RELATION_RULES
    return ''
