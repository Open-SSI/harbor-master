"""The difficulty filter's stop rule (DESIGN.md Section 4, stage 5)."""
from __future__ import annotations

import math


def max_passes(n_total: int, max_pass_rate: float) -> int:
    return math.floor(max_pass_rate * n_total + 1e-9)


def outcome(passes: int, fails: int, n_total: int, max_pass_rate: float, need_pass: bool = False) -> str | None:
    """"drop" once too many trials passed, "keep" once enough failed, None while undecided. With `need_pass` (a task
    that has no reference solution) it is kept only once a trial passed too, and "unsolved" when none of them did."""
    allowed = max_passes(n_total, max_pass_rate)
    if passes > allowed:
        return "drop"
    if need_pass and passes == 0:
        return "unsolved" if fails >= n_total else None
    if fails >= n_total - allowed:
        return "keep"
    return None
