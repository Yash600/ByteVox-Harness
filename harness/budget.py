"""Hierarchical budgets: every agent budget has the run budget as its parent."""
import threading
import time


class BudgetExceeded(Exception):
    def __init__(self, scope, reason):
        super().__init__(f"{scope}: {reason}")
        self.scope = scope
        self.reason = reason


class Budget:
    def __init__(self, name, max_calls, max_seconds, max_tokens, parent=None):
        self.name = name
        self.max_calls = max_calls
        self.max_seconds = max_seconds
        self.max_tokens = max_tokens
        self.parent = parent
        self.calls = 0
        self.tokens = 0
        self.start = time.monotonic()
        self._lock = threading.Lock()

    def elapsed(self):
        return time.monotonic() - self.start

    def check(self):
        """Raise BudgetExceeded if this budget or any ancestor is spent."""
        if self.parent:
            self.parent.check()
        if self.calls >= self.max_calls:
            raise BudgetExceeded(self.name, f"step limit reached ({self.max_calls} LLM calls)")
        if self.tokens >= self.max_tokens:
            raise BudgetExceeded(self.name, f"token limit reached ({self.max_tokens} tokens)")
        if self.elapsed() >= self.max_seconds:
            raise BudgetExceeded(self.name, f"time limit reached ({self.max_seconds:.0f}s)")

    def credit(self, seconds):
        """Give back time spent waiting on a provider's rate limit. Only the agent's own clock
        is credited: the run-level clock keeps running, so the whole run still ends on time."""
        self.start += max(0.0, seconds)

    def charge(self, tokens):
        with self._lock:
            self.calls += 1
            self.tokens += tokens
        if self.parent:
            self.parent.charge(tokens)

    def snapshot(self):
        return {"calls": self.calls, "max_calls": self.max_calls, "tokens": self.tokens,
                "max_tokens": self.max_tokens, "seconds": round(self.elapsed(), 1),
                "max_seconds": self.max_seconds}
