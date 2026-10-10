"""Timing helpers. Call Event.wait(delay) after each serial response."""
import math


def backoff(attempt, base=5, ceiling=60):
    if attempt < 1 or not all(math.isfinite(x) and x > 0 for x in (base, ceiling)):
        raise ValueError('attempt, base and ceiling must be positive')
    if base >= ceiling:
        return ceiling
    steps_to_cap = math.ceil(math.log2(ceiling) - math.log2(base))
    if attempt - 1 >= steps_to_cap:
        return ceiling
    return min(ceiling, math.ldexp(base, attempt - 1))
