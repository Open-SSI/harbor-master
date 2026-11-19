"""The difficulty filter's stop rule (DESIGN.md Section 4, stage 5)."""
from __future__ import annotations

import math


def max_passes(n_total: int, max_pass_rate: float) -> int:
    return math.floor(max_pass_rate * n_total + 1e-9)


def outcome(passes: int, fails: int, n_total: int, max_pass_rate: float) -> str | None:
    """"drop" once too many trials passed, "keep" once enough failed, None while undecided."""
    allowed = max_passes(n_total, max_pass_rate)
    if passes > allowed:
        return "drop"
    if fails >= n_total - allowed:
        return "keep"
    return None
