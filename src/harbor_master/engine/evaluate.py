"""Evaluating a released index (DESIGN.md Section 9): take cells from the to-do list, and run their trials.

A cell is one accepted task and one roster configuration. A trial here is the same thing as a difficulty
trial of stage 1: the same backend call, the same Harbor folder, the same fields in its record. Only the
place differs: the record goes to `eval/`, and the folder to `trials/` in the task store.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from ..ledger import evals
from ..tasks import Task

if TYPE_CHECKING:
    from .session import Session


def todo(s: Session, index: list[dict], roster: list) -> list[evals.Cell]:
    """The cells of the index as this run sees them now: what is done, what is held, and what is free."""
    return evals.cells(index, roster, s.cfg.eval, evals.agent_trials(s.ledger.events, evals.read(s.ledger_dir, s.sandbox)),
                       evals.read(s.ledger_dir, s.sandbox), time.time(), s.ident.github, s.machine)


async def take(s: Session, index: list[dict], roster: list, k: int) -> list[evals.Cell]:
    """Claim the next `k` free cells, and return the ones this run now holds. The push is the lock: when two
    people claim the same cell, the earlier claim holds it, and the other gets it back as someone else's."""
    await s.pull()
    cells = todo(s, index, roster)
    mine = [c for c in cells if c.open and c.mine]              # still held from an earlier take: run those first
    room = max(0, min(k, s.cfg.eval.max_claim) - len(mine))
    new = [c for c in cells if c.open and not c.mine and not c.holder][:room]
    if new:
        evals.append(s.ledger_dir, s.ident, "claim", s.sandbox, cells=[c.key for c in new], machine=s.machine,
                     expires=round(time.time() + s.cfg.eval.claim_hours * 3600))
        await s.publish(f"claim {len(new)} cells to evaluate")
        await s.pull()
    wanted = {c.key for c in (*mine, *new)}
    return [c for c in todo(s, index, roster) if c.key in wanted and c.mine and c.open]


def keep(s: Session, result) -> str:
    """Put a trial's Harbor folder, as one archive, in `trials/` of the task store. Returns its digest, which the
    record carries, or "" when the run left no archive."""
    from ..tasks import store
    path = s.run_dir.parents[1] / result.artifact if result.artifact else None
    if path is None or not path.is_file():
        return ""
    digest = evals.file_digest(path)
    there = store.remote(s.cfg)
    if there is not None:
        key = f"trials/{digest.removeprefix('sha256:')}.tar.gz"
        if not there.has(key):
            there.put(key, path)
    return digest


@dataclass
class Slot:
    """One place where a cell is run: what the eval screen shows as a working cell."""
    n: int
    cell: evals.Cell | None = None
    detail: str = ""
    started: float = 0.0


class Evaluation:
    """An evaluation that is going: it takes cells, runs them side by side, and takes more, until nothing is free.
    The eval screen draws it, and `hm eval` without a screen runs one round of it."""

    def __init__(self, s: Session, index: list[dict], roster: list, fetch: Callable[[dict], Task], k: int = 0):
        self.s, self.index, self.roster, self.fetch = s, index, roster, fetch
        self.k = k or s.cfg.run.cells
        self.slots = [Slot(i + 1) for i in range(s.cfg.run.cells)]
        self.rows: list[dict] = []
        self.recent: deque[tuple[float, str, str]] = deque(maxlen=200)      # (when, kind, text)
        self.started, self.paused, self.quitting, self.finished = time.time(), False, False, False
        self.state = "starting"

    def note(self, kind: str, text: str) -> None:
        self.recent.appendleft((time.time(), kind, text))

    def cells(self) -> list[evals.Cell]:
        return todo(self.s, self.index, self.roster)

    async def run(self, rounds: int = 0) -> list[dict]:
        """Take and run, round after round. `rounds` stops after that many takes: one, for a run without a screen."""
        s, n = self.s, 0
        while not self.quitting:
            while self.paused and not self.quitting:
                self.state = "paused"
                await asyncio.sleep(0.1)
            self.state = "taking cells"
            cells = await take(s, self.index, self.roster, self.k)
            if not cells:
                break
            self.note("claimed", f"{len(cells)} cell{'s' if len(cells) != 1 else ''}, held for {s.cfg.eval.claim_hours:g} hours")
            self.state = "running"
            queue = list(cells)

            async def work(slot: Slot) -> None:
                while queue and not self.quitting:
                    self.rows.append(await self.one(slot, queue.pop(0)))

            await asyncio.gather(*(work(slot) for slot in self.slots))
            n += 1
            if rounds and n >= rounds:
                break
        left = [c for c in self.cells() if c.open]
        self.state = "ended" if self.quitting else "finished"
        self.note("done", "everything is evaluated" if not left else
                  f"{len(left)} cell{'s are' if len(left) != 1 else ' is'} left" + ("" if self.quitting else ", held by others for now"))
        self.finished = True
        return self.rows

    async def one(self, slot: Slot, cell: evals.Cell) -> dict:
        s, entry = self.s, cell.config
        row = {"task": cell.task["task"], "config": entry.name, "new": 0, "note": ""}
        slot.cell, slot.started = cell, time.time()
        try:
            task = self.fetch(cell.task)
            for i in cell.missing:
                slot.detail = f"trial {i + 1} of {cell.trials}"
                result = await s.container(s.backend.run_trial(task, entry, i))
                s.trial_cost += result.cost
                while result.error and await s.failed(task.name, f"trial {i + 1} of {cell.trials}, with {entry.name}", result.error):
                    result = await s.container(s.backend.run_trial(task, entry, i))
                    s.trial_cost += result.cost
                artifact = "" if s.sandbox else await asyncio.to_thread(keep, s, result)
                ran = {"agent_version_ran": result.agent_version} if result.agent_version not in ("", entry.version) else {}
                done = evals.append(
                    s.ledger_dir, s.ident, "trial", s.sandbox, task_id=task.task_id, digest=cell.task["digest"],
                    agent=entry.agent, agent_version=entry.version, model=entry.model, reasoning_effort=entry.reasoning_effort,
                    trial_index=i, purpose="benchmark", passed=result.passed, reward=result.reward,
                    tokens_in=result.tokens_in, tokens_out=result.tokens_out, cost=result.cost,
                    container_seconds=round(result.seconds, 1), error=result.error, artifact=artifact, **ran)
                if done["error"]:
                    row["note"] = f"trial {i + 1} failed to run: {done['error'][:100]}"
                    self.note("failed", f"{task.name} with {entry.name}: {done['error'][:80]}")
                    break               # an infrastructure failure fills no slot: the cell goes back on the list
                cell.done[i] = done
                row["new"] += 1
                self.note("passed" if done["passed"] else "trial", f"{task.name} with {entry.name}: trial {i + 1} "
                          + ("passed" if done["passed"] else "did not pass"))
                if self.quitting:
                    break
        except Exception as err:
            row["note"] = f"failed: {type(err).__name__}: {str(err)[:120]}"
            self.note("failed", f"{cell.task['task']} with {entry.name}: {row['note']}")
        slot.detail = "sharing its records"
        await s.publish(f"evaluation of {cell.task['task']} with {entry.name}")
        slot.cell, slot.detail = None, ""
        row["passes"], row["trials"] = sum(bool(e["passed"]) for e in cell.done.values()), len(cell.done)
        return row


async def evaluate(s: Session, index: list[dict], roster: list, k: int, fetch: Callable[[dict], Task]) -> list[dict]:
    """One round with no screen: take up to `k` cells and run what each still lacks. Returns one row per cell.
    `fetch` gives the accepted version of a task, from this machine or from the store."""
    return await Evaluation(s, index, roster, fetch, k).run(rounds=1)
