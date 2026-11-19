"""Who works on which task (DESIGN.md Section 7, Two people at once).

Before any work, a run pulls the shared ledger, leaves out what is already done or held by
someone else, writes a claim for the rest, and pushes it. The push is the lock: if someone
else pushed first, this run pulls, sees their claim, and gives those tasks up.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ..ledger import fold
from ..tasks import Task
from .state import BenchState

if TYPE_CHECKING:
    from .session import Session

DONE = {"dropped", "report_ready", "accepted", "rejected", "replaced", "contested"}


def completed(s: Session, benchmark: str) -> dict[str, str]:
    """Tasks that already have an outcome under this protocol: task id -> status. Assumed results
    count only in a run that itself assumes."""
    events = (e for e in s.ledger.events if e.get("benchmark") == benchmark and e["protocol"] == s.protocol
              and (s.assumed or not e.get("assumed")))
    return {tid: st.status for tid, st in fold(events).items() if st.status in DONE}


def _names(tasks: list[Task]) -> str:
    return ", ".join(t.name for t in tasks[:4]) + (f" and {len(tasks) - 4} more" if len(tasks) > 4 else "")


async def take(s: Session, bench: BenchState, chosen: list[Task]) -> list[Task]:
    """Claim what is free among `chosen`, and return it. The rest is rejected, with the reason noted."""
    await s.pull()
    done = completed(s, bench.id)
    held = s.ledger.claims(bench.id, time.time(), me=s.ident.github)
    finished = [t for t in chosen if t.task_id in done]
    running = [t for t in chosen if t.task_id in held and t.task_id not in done]
    free = [t for t in chosen if t.task_id not in done and t.task_id not in held]
    if finished:
        s.note_recent("rejected", bench.id, f"{len(finished)} already completed, not run again: {_names(finished)}")
    if running:
        who = sorted({held[t.task_id][0] for t in running})
        s.note_recent("rejected", bench.id, f"{len(running)} being run by @{', @'.join(who)}, left to them: {_names(running)}")
    if not free:
        return []
    ids = [t.task_id for t in free]
    s.record(bench.id, "claim", tasks=ids, expires=time.time() + s.cfg.run.claim_hours * 3600)
    s.claimed.setdefault(bench.id, []).extend(ids)
    for _ in range(8):
        if await s.push(f"claim {len(free)} tasks of {bench.id}"):
            break
        # Someone pushed first. Their work is pulled in now: give up whatever they claimed.
        held = s.ledger.claims(bench.id, time.time(), me=s.ident.github)
        lost = [t for t in free if t.task_id in held]
        if lost:
            who = sorted({held[t.task_id][0] for t in lost})
            s.release(bench.id, [t.task_id for t in lost])
            s.note_recent("rejected", bench.id, f"@{', @'.join(who)} claimed {len(lost)} first, left to them: {_names(lost)}")
            free = [t for t in free if t.task_id not in held]
    return free


SCREEN = "__screening__"      # held while one runner screens, ranks and selects a benchmark


async def screen(s: Session, bench: BenchState) -> str:
    """Take the right to screen a benchmark. Returns "" when taken, or who holds it now.

    Screening is cheap, but two people doing it at once would write the same decisions twice.
    """
    await s.pull()                                  # others' decisions are reused from here on
    for attempt in range(8):
        holder = s.ledger.claims(bench.id, time.time(), me=s.ident.github).get(SCREEN)
        if holder:
            s.release(bench.id, [SCREEN])
            return holder[0]
        if attempt == 0:
            s.record(bench.id, "claim", tasks=[SCREEN], expires=time.time() + 3600)
            s.claimed.setdefault(bench.id, []).append(SCREEN)
        if await s.push(f"screening {bench.id}"):
            return ""
    return ""
