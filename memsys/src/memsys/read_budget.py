"""Request-local read deadlines and a single owner for network retries."""
from __future__ import annotations

import contextvars
import math
import threading
import time
from contextlib import contextmanager

from .llm import ModelError, RETRYABLE_STATUS

_CURRENT = contextvars.ContextVar("memsys_read_budget", default=None)


class ReadExecutionError(ModelError):
    def __init__(self, message, state):
        super().__init__(message)
        self.read_state = state


def is_endpoint_outage(error):
    import re
    return (isinstance(error, ModelError) and error.status in (429, 451)
            and bool(re.search(r"endpoint is unavailable", str(error), re.I)))


def is_read_service_error(error):
    return (isinstance(error, ReadExecutionError) or is_endpoint_outage(error)
            or bool(getattr(error, "read_network_failure", False)))


def current_read_budget():
    return _CURRENT.get()


@contextmanager
def read_scope(budget):
    token = _CURRENT.set(budget)
    try:
        yield budget
    finally:
        _CURRENT.reset(token)


def check_read_budget():
    budget = current_read_budget()
    if budget is not None:
        budget.check()


class ReadBudget:
    def __init__(self, timeout_s=180.0, retries_per_call=1, retries_total=2,
                 retry_wait_s=30.0, cancel_event=None, *, clock=None, sleeper=None):
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("read timeout must be finite and positive")
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0
               for v in (retries_per_call, retries_total)):
            raise ValueError("read retry limits must be non-negative integers")
        if not math.isfinite(retry_wait_s) or retry_wait_s < 0:
            raise ValueError("read retry wait must be finite and non-negative")
        self.timeout_s = timeout_s
        self.retries_per_call = retries_per_call
        self.retries_total = retries_total
        self.retry_wait_s = retry_wait_s
        self.cancel_event = cancel_event or threading.Event()
        self.clock = clock or time.monotonic
        self.sleeper = sleeper
        self.started = self.clock()
        self.deadline = self.started + timeout_s
        self.additional_retries = 0
        self.events = []
        self.outputs = []

    def remaining(self):
        return max(0.0, self.deadline - self.clock())

    def check(self):
        if self.cancel_event.is_set():
            raise ReadExecutionError("read execution cancelled", "cancelled")
        if self.remaining() <= 0:
            raise ReadExecutionError("read execution exceeded its wall-clock budget", "timeout")

    def timeout(self, configured):
        self.check()
        return min(configured, self.remaining())

    def snapshot(self):
        return dict(additional_retries=self.additional_retries,
                    limit_per_failed_call=self.retries_per_call,
                    limit_per_response=self.retries_total, timeout_s=self.timeout_s,
                    elapsed_s=round(self.clock() - self.started, 3),
                    remaining_s=round(self.remaining(), 3), events=list(self.events))

    def degraded(self, purpose, error):
        self.check()  # deadlines and cancellation never become optional fallback
        self.events.append(dict(kind="optional_degradation", purpose=purpose, error=str(error)[:500]))

    def call(self, send, *, purpose, timeout_s=120.0):
        """send(timeout) performs exactly one attempt, with all inner retries off."""
        for attempt in range(self.retries_per_call + 1):
            timeout = self.timeout(timeout_s)
            started = self.clock()
            event = dict(kind="network_attempt", purpose=purpose, attempt=attempt + 1, timeout_s=timeout)
            self.events.append(event)
            try:
                result = send(timeout)
                self.check()
            except ModelError as exc:
                event.update(status=exc.status, error=str(exc)[:500], outcome="error",
                             elapsed_s=round(self.clock() - started, 3))
                # Quota/auth/legal/content failures are never network retries.
                network = (is_endpoint_outage(exc) or getattr(exc, "transport_error", False)
                           or exc.status in RETRYABLE_STATUS - {429})
                if network:
                    exc.read_network_failure = True
                    exc.read_state = "service_error"
                self.check()
                if (not network or attempt == self.retries_per_call
                        or self.additional_retries >= self.retries_total):
                    raise
                wait = getattr(exc, "retry_after_s", None)
                wait = self.retry_wait_s if wait is None else wait
                if wait >= self.remaining():
                    raise ReadExecutionError("network retry cannot fit within remaining read budget", "timeout") from exc
                wait_started = self.clock()
                wait_event = dict(kind="retry_wait", purpose=purpose, wait_s=wait)
                self.events.append(wait_event)
                try:
                    if self.sleeper is not None:
                        self.sleeper(wait)
                    else:
                        self.cancel_event.wait(wait)
                    self.check()
                finally:
                    wait_event["elapsed_s"] = round(self.clock() - wait_started, 3)
                self.additional_retries += 1
            else:
                event["outcome"] = "ok"
                return result
            finally:
                event.setdefault("elapsed_s", round(self.clock() - started, 3))
