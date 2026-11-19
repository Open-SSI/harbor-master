"""Benchmarking an agent on tasks (DESIGN.md Section 9).

A benchmark trial is the same thing as a difficulty trial with a different purpose. It runs
through the same function, `stages.trial`, so it leaves the same ledger record with the same
fields, and the same Harbor folder: trajectory, verifier output, result.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from ..core.config import SuiteEntry
from ..tasks import Task
from . import stages

if TYPE_CHECKING:
    from .session import Session


def slot(task: Task, entry: SuiteEntry) -> str:
    """What one person holds while they benchmark one agent on one task."""
    return f"{task.task_id}#{entry.agent}:{entry.version}:{entry.model}:{entry.reasoning_effort}"


async def evaluate(s: Session, entry: SuiteEntry, tasks: list[Task], trials: int) -> list[dict]:
    """Run `trials` trials of one agent configuration on each task. Returns one row per task.

    A trial already in the ledger is not run again, and a task someone else is benchmarking
    with the same configuration right now is left to them.
    """
    await s.pull()
    rows: list[dict] = []
    by_bench: dict[str, list[Task]] = {}
    for t in tasks:
        by_bench.setdefault(t.benchmark, []).append(t)
    mine: list[Task] = []
    for bench, group in by_bench.items():
        held = s.ledger.claims(bench, time.time(), me=s.ident.github)
        free = [t for t in group if slot(t, entry) not in held]
        for t in group:
            if slot(t, entry) in held:
                rows.append({"task": t.task_id, "passes": 0, "trials": 0, "new": 0,
                             "note": f"being run by @{held[slot(t, entry)][0]}, left to them"})
        if free:
            ids = [slot(t, entry) for t in free]
            s.record(bench, "claim", tasks=ids, expires=time.time() + s.cfg.run.claim_hours * 3600)
            s.claimed.setdefault(bench, []).extend(ids)
            mine += free
    await s.publish(f"claim {len(mine)} benchmark tasks")
    gate = asyncio.Semaphore(s.cfg.run.cells)

    async def one(task: Task) -> dict:
        async with gate:
            before = sum(1 for i in range(trials) if s.ledger.trial(
                task.digest, entry.agent, entry.version, entry.model, s.protocol, i, entry.reasoning_effort))
            started = time.time()
            try:
                events = [await stages.trial(s, task, "benchmark", entry.agent, entry.model, i,
                                             lambda i=i: s.backend.run_trial(task, entry, i),
                                             version=entry.version, effort=entry.reasoning_effort)
                          for i in range(trials)]
                passes = sum(bool(e["passed"]) for e in events)
                s.board(task)["stages"].append({
                    "stage": "benchmark", "seconds": round(time.time() - started, 2), "agent": entry.agent,
                    "model": entry.model, "passes": passes, "trials": len(events)})
                s.flush_board(task)
                note = "already in the ledger, not run again" if before == trials else ""
                return {"task": task.task_id, "passes": passes, "trials": len(events), "new": trials - before, "note": note}
            except Exception as err:
                return {"task": task.task_id, "passes": 0, "trials": 0, "new": 0,
                        "note": f"failed: {type(err).__name__}: {str(err)[:120]}"}
            finally:
                s.release(task.benchmark, [slot(task, entry)])      # done or failed: let go either way
                await s.publish(f"benchmark records for {task.name}")

    rows += await asyncio.gather(*(one(t) for t in mine))
    return rows
