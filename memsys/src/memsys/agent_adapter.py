"""Optional example host Agent. Final generation is outside the memory API."""
from . import prompts
from .budget import budget_counter
from .llm import ModelError
from .read import parse_answer
from .timeutil import format_day


class AgentAdapter:
    def __init__(self, memory, chat=None):
        self.memory = memory
        self.chat = chat or memory.models.chat or memory.models.memory

    def respond(self, user_id, message, at, session=None, tags=None, cancel_event=None, read_timeout_s=None):
        from .core import Reply
        if self.chat is None:
            raise ModelError("host Agent requires a chat model")
        if cancel_event is not None and cancel_event.is_set():
            raise ModelError("host request cancelled")
        conversation = self.memory._conversation(self.memory.store(user_id), session.session_id) if session else ""
        counter = budget_counter(self.memory.settings)
        base = [dict(role='system', content=prompts.ANSWER_SYSTEM),
                dict(role='user', content=prompts.ANSWER_CHAT.format(today=format_day(at), memory='', rules=prompts.ANSWER_RULES,
                    conversation=conversation) + '\n\nLatest user message: ' + message)]
        available = self.memory.settings.answer_input_budget_tokens - counter.messages(base) - 128
        if available < 200:
            raise ModelError('host input leaves no safe fact-context budget')
        recall = self.memory.recall(user_id, message, at, tags=tags,
            budget=min(self.memory.settings.context_budget_tokens, available), coverage_budget=available)
        messages = [dict(role="system", content=prompts.ANSWER_SYSTEM),
                    dict(role="user", content=prompts.ANSWER_CHAT.format(today=format_day(at), memory=recall.memory, rules=prompts.ANSWER_RULES,
                                                                       conversation=conversation) + "\n\nLatest user message: " + message)]
        if counter.messages(messages) > self.memory.settings.answer_input_budget_tokens:
            raise ModelError("host prompt exceeds answer_input_budget_tokens")
        # A clipped generation can grow its output budget; no reviewer call.
        from .generation import complete_chat
        response = complete_chat(self.chat,messages,purpose='answer',max_tokens=self.memory.settings.answer_max_tokens,
            max_tokens_limit=self.memory.settings.answer_max_tokens_limit,tags=tags)
        if not response.text.strip() or response.finish_reason in ('length', 'incomplete', 'content_filter', 'refusal'):
            raise ModelError('host answer did not finish: ' + str(response.finish_reason))
        notes, text = parse_answer(response.text)
        trace = dict(recall.trace, mode="host_agent", answer=text, notes=notes)
        if session:
            self.memory.add_turn(session, "user", message, at)
            self.memory.add_turn(session, "assistant", text, at)
            self.memory._maybe_flush(self.memory.store(user_id), session, dict(tags or {}))
        return Reply(text, notes, trace)
