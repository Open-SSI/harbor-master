"""Time estimates, computed by code from how long stages have taken so far."""
from __future__ import annotations

import statistics
from typing import TYPE_CHECKING

from .state import CellState

if TYPE_CHECKING:
    from .session import Session


def estimate(s: Session, cell: CellState) -> tuple[float, float] | None:
    """A range, in seconds, for the task in a cell to finish. None when there is no history yet."""
    if cell.task is None:
        return None
    total, known = 0.0, False
    for stage in (3, 4, 5, 6, 8):
        if stage < cell.stage:
            continue
        seen = s.durations.get((cell.task.benchmark, stage)) or [
            x for (b, s), xs in s.durations.items() if s == stage for x in xs]
        if seen:
            known = True
            total += statistics.median(seen) * (0.5 if stage == cell.stage else 1.0)
    return (0.7 * total, 1.5 * total) if known else None
