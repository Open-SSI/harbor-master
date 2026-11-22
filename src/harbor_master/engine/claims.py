"""Who curates which benchmark (DESIGN.md Section 7, Two people at once).

A benchmark is curated by one person at a time. Before any work, a run pulls the shared ledger and claims the
whole benchmark with one signed record, which it pushes. The push is the lock: if someone else pushed a claim
first, this run pulls, sees it, and leaves the benchmark to them. Nothing about single tasks is claimed or
shared while the run works: its records go out when a report is ready, when a verdict is signed, and when the
run ends. A run that goes on longer than the claim renews it, and a machine that stops lets it run out.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ..ledger import fold
from ..tasks import Task
from .state import BenchState

if TYPE_CHECKING:
    from .session import Session

DONE = {"dropped", "report_ready", "accepted", "rejected", "contested"}
BENCH = "__benchmark__"     # the one thing a curation claims: the whole benchmark


def completed(s: Session, benchmark: str) -> dict[str, str]:
    """Tasks that already have an outcome: task id -> status. Outcomes reached under an earlier protocol of the index
    count too, so that a change of settings reruns only what has none. Assumed results count only in a run that itself
    assumes."""
    events = (e for e in s.ledger.events if e.get("benchmark") == benchmark and (s.assumed or not e.get("assumed")))
    return {tid: st.status for tid, st in fold(events).items() if st.status in DONE}


def _names(tasks: list[Task]) -> str:
    return ", ".join(t.name for t in tasks[:4]) + (f" and {len(tasks) - 4} more" if len(tasks) > 4 else "")


async def take(s: Session, bench: BenchState, chosen: list[Task]) -> list[Task]:
    """What is left to run among `chosen`: the tasks that have no outcome yet. The benchmark is this run's already,
    so nothing is claimed or pushed here."""
    done = completed(s, bench.id)
    finished = [t for t in chosen if t.task_id in done]
    if finished:
        s.note_recent("rejected", bench.id, f"{len(finished)} already completed, not run again: {_names(finished)}")
        states = fold(e for e in s.ledger.events if e.get("benchmark") == bench.id)
        for t in finished:              # a report written in an earlier run still waits for this reviewer's verdict
            if done[t.task_id] == "report_ready":
                t.digest = states[t.task_id].digest or t.digest
                s.report_ready(t)
    return [t for t in chosen if t.task_id not in done]


async def claim(s: Session, bench: BenchState) -> str:
    """Claim the whole benchmark for this run. Returns "" when it is this run's, or who holds it now."""
    await s.pull()                                  # others' records are reused from here on
    for attempt in range(8):
        holder = s.ledger.claims(bench.id, time.time(), me=s.ident.github, machine=s.machine).get(BENCH)
        if holder:
            s.release(bench.id, [BENCH])
            return holder[0]
        if attempt == 0:
            s.record(bench.id, "claim", tasks=[BENCH], expires=time.time() + s.cfg.run.claim_hours * 3600)
            s.claimed.setdefault(bench.id, []).append(BENCH)
            s.claim_until[bench.id] = time.time() + s.cfg.run.claim_hours * 3600
        if await s.push(f"curating {bench.id}"):
            return ""
    # The claim never reached the index: without it, someone else could curate the same benchmark. So it is not taken.
    s.release(bench.id, [BENCH])
    s.claim_until.pop(bench.id, None)
    raise ClaimError(f"the claim on {bench.id} could not be pushed to the index's repository, so it is not taken. "
                     "Check that your clone can push to the index's repository, then try again.")


class ClaimError(RuntimeError):
    """A claim that could not be shared: the benchmark is left to anyone."""


async def renew(s: Session) -> None:
    """Called at each sign of progress: a trial or a task that finished. A claim with less than half its time left is
    renewed. One that ran out, because nothing finished for `claim_hours`, is someone else's to take: this run takes it
    again only if nobody has, and otherwise stops working on that benchmark."""
    now, hours = time.time(), s.cfg.run.claim_hours * 3600
    held = [b for b in s.claim_until if BENCH in s.claimed.get(b, [])]
    due = [b for b in held if s.claim_until[b] - now < hours / 2]
    if not due:
        return
    if any(s.claim_until[b] <= now for b in due):        # ran out: see whether someone else took it meanwhile
        await s.pull()
    kept = []
    for b in due:
        holder = s.ledger.claims(b, now, me=s.ident.github, machine=s.machine).get(BENCH) if s.claim_until[b] <= now else None
        if holder:
            s.claimed[b] = [t for t in s.claimed[b] if t != BENCH]
            s.claim_until.pop(b, None)
            dropped = [t for t in s.queue if t.benchmark == b]
            s.queue[:] = [t for t in s.queue if t.benchmark != b]
            s.note_recent("rejected", b, f"the claim ran out with no progress, and @{holder[0]} has taken the benchmark: "
                          f"{len(dropped)} queued task{'s are' if len(dropped) != 1 else ' is'} left to them")
            continue
        s.record(b, "claim", tasks=[BENCH], expires=now + hours)
        s.claim_until[b] = now + hours
        kept.append(b)
    if kept:
        await s.publish(f"still curating {', '.join(kept)}")
