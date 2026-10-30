"""Organize selected facts for advice without new retrieval or inferred goals."""
from collections import OrderedDict
import json


def advice_sections(facts):
    """Return fact groups, never duplicated statements or guessed current tasks.

    New intent labels organize facts; legacy unlabeled facts stay visible. A
    grouping key is a source-described activity, not proof of an active plan.
    """
    tasks, unassigned, other, techniques = OrderedDict(), [], [], []
    for fact in sorted(facts, key=lambda f: (f.kind == 'assistant', f.when().start, f.id)):
        if fact.kind == 'assistant':
            techniques.append(fact)
            continue
        role = fact.semantics.get('context_role')
        if role in ('goal', 'constraint', 'resource'):
            key = fact.semantics.get('task_key')
            (tasks.setdefault(key, []) if key else unassigned).append(fact)
        else:
            other.append(fact)
    sections = []
    for key, members in tasks.items():
        sections.append(('### Task candidate: ' + json.dumps(key, ensure_ascii=False), members))
    if unassigned:
        sections.append(('### Goals, constraints and resources with no confirmed task grouping', unassigned))
    if other:
        sections.append(('### Other user context (including legacy facts with unclassified task roles)', other))
    if techniques:
        sections.append(('### Prior assistant techniques and options (not user commitments)', techniques))
    return sections


ADVICE_CONTEXT = '''## Advice task context
Choose the task relevant to the current request from the source-described candidates below.
A task heading does not establish that a plan is current: read the facts' dates, status,
negation and uncertainty. Keep goals, constraints and resources linked only where the facts
support that association. Legacy unclassified facts may also contain an applicable goal.
Use specific applicable goals to choose the substance of suggestions. A familiar anecdote
in the opening does not substitute for advancing the task. If tasks are ambiguous, give
conditional branches or ask a focused question. Unrelated goals need not be used.
The assistant material supplies possible methods, not adopted preferences or equipment.
'''
