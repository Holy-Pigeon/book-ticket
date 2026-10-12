"""Timing helpers. Call Event.wait(delay) after each serial response."""
import math
import time


class DeadlineStop:
    """Event-compatible stop view, including checks immediately before a POST."""
    def __init__(self, event, deadline, abort=None):
        self.event, self.deadline = event, deadline
        self.abort = abort

    def is_set(self):
        return (self.event.is_set() or time.monotonic() >= self.deadline
                or (self.abort is not None and bool(self.abort())))

    def wait(self, seconds):
        if self.is_set():
            return True
        self.event.wait(min(seconds, max(0, self.deadline-time.monotonic())))
        return self.is_set()


def backoff(attempt, base=5, ceiling=60):
    if attempt < 1 or not all(math.isfinite(x) and x > 0 for x in (base, ceiling)):
        raise ValueError('attempt, base and ceiling must be positive')
    if base >= ceiling:
        return ceiling
    steps_to_cap = math.ceil(math.log2(ceiling) - math.log2(base))
    if attempt - 1 >= steps_to_cap:
        return ceiling
    return min(ceiling, math.ldexp(base, attempt - 1))
