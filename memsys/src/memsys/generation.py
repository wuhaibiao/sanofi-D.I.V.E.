"""Complete generations with bounded output growth; never publish clipped drafts."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, fields

from .llm import ChatResult, IncompleteJSONError, ModelError


@dataclass(frozen=True)
class AnswerResult(ChatResult):
    generation_attempts: list = field(default_factory=list)


def generate_answer(model, messages, *, max_tokens, max_tokens_limit=None, tags=None):
    """Generate the user-facing answer directly, without an audit envelope."""
    attempts = []
    response = complete_chat(model, messages, purpose='answer', max_tokens=max_tokens,
                             max_tokens_limit=max_tokens_limit, tags=tags, attempts=attempts)
    if response.finish_reason in ('length', 'incomplete', 'content_filter', 'refusal'):
        raise ModelError('answer did not finish: ' + response.finish_reason)
    if not response.text.strip():
        raise ModelError('empty answer')
    values = {f.name: getattr(response, f.name) for f in fields(ChatResult)}
    values['text'] = response.text.strip()
    return AnswerResult(**values, generation_attempts=attempts)


def complete_chat(model, messages, *, purpose, max_tokens, max_tokens_limit=None,
                  json_mode=False, tags=None, attempts=None, responses=None):
    """Retry the same full input at most twice, only after an output limit stop.

    Network retries belong to the transport. Model refusals and content errors
    do not trigger regeneration. The caller can persist the small attempt records;
    provider usage remains recorded once per actual call in UsageLedger.
    """
    records = attempts if attempts is not None else []
    limit = max(max_tokens, max_tokens_limit or max_tokens)
    requested = max_tokens
    for attempt in range(3):
        headroom=getattr(getattr(model,'config',None),'output_token_headroom',0)
        record=dict(max_tokens=requested,request_output_tokens=requested+headroom)
        records.append(record)
        try:
            result = model.chat(messages, purpose=purpose, max_tokens=requested,
                                json_mode=json_mode, tags=tags)
        except ModelError as exc:
            record.update(error=str(exc)[:500],status=exc.status)
            raise
        if responses is not None:
            responses.append(result)
        record.update(finish_reason=result.finish_reason,
            prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
            response_bytes=len(result.text.encode('utf-8')),
            response_sha256=hashlib.sha256(result.text.encode('utf-8')).hexdigest())
        if result.finish_reason not in ('length', 'incomplete'):
            return result
        if requested >= limit or attempt == 2:
            if json_mode:
                error = IncompleteJSONError(result.finish_reason, output_limit=requested,
                                            completion_tokens=result.completion_tokens)
            else:
                error = ModelError('answer did not finish: ' + result.finish_reason)
            error.generation_attempts = list(records)
            raise error
        requested = min(requested * 2, limit)
    raise AssertionError('unreachable generation attempt')
