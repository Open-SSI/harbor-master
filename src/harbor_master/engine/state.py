"""What a run keeps in memory: benchmarks, working cells, and requests to the user."""
from __future__ import annotations

import asyncio
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..tasks import Task

MAX_CELLS = 8                # work cells a run can have; how many take tasks is the person's choice
STAGES = ["intake", "triage", "adapt", "verify", "probe", "worker", "audit", "fix", "difficulty", "report", "verdict"]
STAGE_NO = {name: n for n, name in enumerate(STAGES)}     # a stage's number, for a cell to say which one it is in

# What the work in hand is for: every model call is counted under the benchmark and the stage it was made in.
# A context variable, because many tasks are in flight at once and each must see its own.
BENCH: ContextVar[str] = ContextVar("hm_benchmark", default="")
STAGE: ContextVar[str] = ContextVar("hm_stage", default="session")


@dataclass
class BenchState:
    id: str
    path: Path
    released: str = ""           # the release date from the benchmark's entry, for the recency gate
    license: str = ""            # the benchmark's own license, shown to the license gate
    tasks: list[Task] = field(default_factory=list)
    phase: str = "queued"        # queued | fetch | adapt | review | triage | brief | ranking | signoff | ready | skipped
    entry: dict | None = None    # its entry on the index's benchmark list, when it is on it
    reported: dict[str, dict] = field(default_factory=dict)    # per task, what the benchmark's own results say
    skip: str = ""               # why it was skipped, as one word for the record: fetch_failed, no_brief, ...
    confirmed: str = ""          # what a person said when asked whether to reject it: keep, go or drop
    checked: str = ""            # what its benchmark-level checks came to, under their box: "8 checked", "rejected"
    detail: str = ""
    selected: int = 0
    done: int = 0
    kept: int = 0
    dropped: int = 0             # dropped in a working cell
    triaged_out: int = 0
    not_selected: int = 0
    cost: float = 0.0
    paused: bool = False
    ranking: list[tuple[str, float, bool]] = field(default_factory=list)   # (task id, rating, selected), in the order tasks are taken
    most: int = 0                # the most tasks that may be selected: the cap, and never more than there are
    chosen: list[str] = field(default_factory=list)     # the task ids selected to curate, finished before or not
    # The tasks this run let go with no outcome (an error, a model that could not be reached, a stop), and why: nothing
    # is recorded for them, and the next run takes them again. They count in `done`, which is what the cells are through
    left: list[tuple[str, str]] = field(default_factory=list)
    on_step: Callable[[BenchState, str], None] | None = field(default=None, repr=False, compare=False)

    def __setattr__(self, name: str, value) -> None:
        """A new step of the benchmark's own stages (fetch, checks, adapt, triage, brief, ranking) goes to the log, not
        only to its status line, which the next step overwrites. A counter moving on ("triage 12 of 296") is not a step."""
        before = self.__dict__.get(name)
        object.__setattr__(self, name, value)
        if name == "detail" and value and value != before and self.on_step is not None:
            shape = re.sub(r"[\d.,$]+", "#", str(value))
            if shape != self.__dict__.get("_last_step"):
                object.__setattr__(self, "_last_step", shape)
                self.on_step(self, str(value))

    @property
    def queued(self) -> int:
        return max(0, self.selected - self.done)

    @property
    def settled(self) -> int:
        """The chosen tasks that have an outcome: from earlier runs, or reached in this one. Not those left over."""
        return len(self.chosen) - self.selected + self.done - len(self.left)


@dataclass
class CellState:
    n: int
    task: Task | None = None
    stage: int = 0
    detail: str = ""
    started: float = 0.0
    cost: float = 0.0
    attention: bool = False
    runner: asyncio.Task | None = None
    stopping: bool = False
    on_stage: object = None      # called with a stage's name as the cell enters it


@dataclass
class Request:
    id: int
    kind: str                    # brief | selection | rejected | hung | drop | lock | cells block work; verdict waits without blocking
    text: str
    target: str = ""
    choices: list[str] = field(default_factory=list)
    answer: asyncio.Future | None = None

    @property
    def blocking(self) -> bool:
        return self.kind in ("brief", "selection", "rejected", "hung", "drop", "lock", "cells")

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "text": self.text, "target": self.target, "choices": self.choices}
