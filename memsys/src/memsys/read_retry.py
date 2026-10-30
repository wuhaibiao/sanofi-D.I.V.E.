"""Bounded delayed retries for a failed read call to an unavailable endpoint."""
from .read_budget import (ReadBudget, current_read_budget, is_endpoint_outage,
                          is_read_service_error, read_scope)


def is_unavailable_endpoint(error):
    """An exhausted upstream outage has not established a memory answer."""
    return is_endpoint_outage(error)


class ReadTransportChat:
    """Keep successful model output and all non-transport failures untouched.

    Each response owns its wrapper and event log. The shared model and writer
    are not mutated. An explicitly unavailable upstream has produced no answer,
    so it can retry after bounded delays with the same request and model.
    """
    def __init__(self, model, budget=None):
        self.model = model
        self.budget = budget or current_read_budget() or ReadBudget()

    @property
    def retries(self):
        return [e for e in self.budget.events if e['kind'] == 'retry_wait']

    def __getattr__(self, name):
        return getattr(self.model, name)

    def chat(self, messages, *, purpose, temperature=0.0, max_tokens=None,
             json_mode=False, tags=None):
        kwargs = dict(purpose=purpose, temperature=temperature, max_tokens=max_tokens,
                      json_mode=json_mode, tags=tags)
        with read_scope(self.budget):
            self.budget.check()
            if getattr(type(self.model), 'read_budget_managed', False) is True:
                result = self.model.chat(messages, **kwargs)
            else:
                # Injected chat adapters must make one attempt per invocation.
                result = self.budget.call(lambda timeout: self.model.chat(messages, **kwargs), purpose=purpose)
            if isinstance(getattr(result, 'text', None), str):
                self.budget.outputs.append(dict(purpose=purpose, model=result.model,
                    finish_reason=result.finish_reason, text=result.text))
            return result
