"""What a run keeps in memory: benchmarks, working cells, and requests to the user."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from ..tasks import Task

STAGES = ["intake", "triage", "adapt", "verify", "probe", "difficulty", "audit", "fix", "report", "verdict"]


@dataclass
class BenchState:
    id: str
    field: str
    path: Path
    released: str = ""           # the release date from benchmarks.toml, for the recency gate
    license: str = ""            # the benchmark's own license, shown to the license gate
    tasks: list[Task] = field(default_factory=list)
    phase: str = "queued"        # queued | triage | brief | ranking | signoff | adapt | ready | skipped
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
    kind: str                    # brief | selection | budget block work; replace | verdict wait without blocking
    text: str
    target: str = ""
    choices: list[str] = field(default_factory=list)
    answer: asyncio.Future | None = None

    @property
    def blocking(self) -> bool:
        return self.kind in ("brief", "selection", "budget")

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "text": self.text, "target": self.target, "choices": self.choices}
