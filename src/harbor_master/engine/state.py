"""What a run keeps in memory: benchmarks, working cells, and requests to the user."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from ..tasks import Task

STAGES = ["intake", "triage", "adapt", "verify", "probe", "difficulty", "audit", "fix", "report", "verdict"]

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
    detail: str = ""
    selected: int = 0
    done: int = 0
    kept: int = 0
    dropped: int = 0             # dropped in a working cell
    triaged_out: int = 0
    not_selected: int = 0
    cost: float = 0.0
    paused: bool = False
    ranking: list[tuple[str, float, bool]] = field(default_factory=list)   # (task id, rating, selected)

    @property
    def queued(self) -> int:
        return max(0, self.selected - self.done)


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


@dataclass
class Request:
    id: int
    kind: str                    # brief | selection | budget | adapter | lock block work; replace | verdict wait without blocking
    text: str
    target: str = ""
    choices: list[str] = field(default_factory=list)
    answer: asyncio.Future | None = None

    @property
    def blocking(self) -> bool:
        return self.kind in ("brief", "selection", "budget", "adapter", "lock")

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "text": self.text, "target": self.target, "choices": self.choices}
