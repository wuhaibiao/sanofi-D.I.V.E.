"""Whole-round recent context and extraction-before-eviction safeguards."""
from dataclasses import dataclass
from .budget import budget_counter
from .llm import ModelError
from .store import WriteConflict


@dataclass
class WorkingWindow:
    messages: list
    dropped: list
    trace: dict


def select_window(history, settings):
    counter = budget_counter(settings)
    groups = []
    for turn in history:
        if (not groups or turn.get('session_id') != groups[-1][0].get('session_id') or
                turn['role'] == 'user' and any(t['role'] != 'user' for t in groups[-1])):
            groups.append([])
        groups[-1].append(turn)
    # The latest uncompleted user input is mandatory and has its own full input
    # budget. The independent working-window budget applies to completed history.
    current = groups.pop() if groups and all(t['role'] == 'user' for t in groups[-1]) else []
    kept = groups[-settings.chat_window_turns:] if settings.chat_window_turns else []
    dropped = groups[:len(groups) - len(kept)]
    def messages(rounds):
        return [{k: turn[k] for k in ('role', 'content')} for group in rounds for turn in group]
    while kept and counter.messages(messages(kept)) > settings.chat_window_tokens:
        dropped.append(kept.pop(0))
    flat = [turn for group in kept for turn in group] + current
    return WorkingWindow(flat, [turn for group in dropped for turn in group], dict(
        completed_round_limit=settings.chat_window_turns, completed_rounds=len(kept),
        dropped_rounds=len(dropped), history_units=counter.messages(messages(kept)),
        history_budget=settings.chat_window_tokens, current_input_units=counter.messages(messages([current])),
        counter=counter.manifest(), eviction_checked=False))


def ensure_eviction_ready(memory, store, window, tags=None):
    """Only complete, successfully extracted rounds may leave working context."""
    from .write import Writer
    targets = {}
    for turn in window.dropped:
        sid = turn.get('session_id')
        if sid is None or 'idx' not in turn:
            raise ModelError('working memory eviction requires saved source positions')
        targets[sid] = max(targets.get(sid, 0), turn['idx'] + 1)
    reports = []
    for sid, upto in targets.items():
        session = store.session(sid)
        if session['turns_done'] < upto:
            if memory.models.for_purpose('extract') is None:
                raise ModelError('recent history cannot leave context before a memory extraction model is available')
            try:
                report = Writer(store, memory.models, memory.settings, tags).consolidate(sid, upto_turn=upto)
                if report.deferred or report.retry_rounds:
                    raise ModelError('recent history extraction is incomplete; saved messages remain pending')
                reports.append(report.summary())
            except WriteConflict:
                if store.session(sid)['turns_done'] < upto:
                    raise
        if store.session(sid)['turns_done'] < upto or any(e.session_id == sid for e in store.backlog()):
            raise ModelError('recent history has unprocessed memory; refusing silent context eviction')
    window.trace.update(eviction_checked=True, extraction_reports=reports,
                        checked_sessions=len(targets))
    return window
