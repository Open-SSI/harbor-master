"""What a backend must provide: the three model tiers and Harbor (DESIGN.md Sections 2 and 3).

To add a real backend, subclass `Backend` and return it from `make_backend`. Every method
either returns one typed record or one trial result, and reports its cost through `usage`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from ..core.config import Config, SuiteEntry
from ..tasks import Task


@dataclass
class Decision:
    value: str
    prob: float
    tier: str
    model: str
    cost: float = 0.0
    tokens: int = 0
    rationale: str = ""
    skill: str = ""            # the skill and version that produced it, such as triage/gates@3f2a91c0d4


@dataclass
class TrialResult:
    passed: bool
    reward: float
    tokens_in: int
    tokens_out: int
    cost: float
    seconds: float
    agent_version: str = ""    # the version that actually ran, read from the agent, not from the config
    verifier_seconds: float = 0.0
    checksum: str = ""         # Harbor's own task checksum, when Harbor ran it
    artifact: str = ""         # where the trial's files are
    error: str = ""            # an infrastructure failure, as opposed to a wrong answer
    job: str = ""              # Harbor's working folder for this run. Not recorded: it is packed or removed


Usage = Callable[[str, float, int], None]  # (tier, cost, tokens)


class NotImplementedBackend(RuntimeError):
    pass


class Backend(ABC):
    def __init__(self, cfg: Config, usage: Usage):
        self.cfg, self.usage = cfg, usage

    # tier D: typed decisions with a probability
    @abstractmethod
    async def decide(self, gate: str, choices: list[str], task: Task) -> Decision: ...
    async def decide_gates(self, task: Task, gates: dict[str, list[str]]) -> dict[str, Decision]:
        """Several gates on one task. A backend that can answer them in one request overrides this."""
        return {gate: await self.decide(gate, choices, task) for gate, choices in gates.items()}

    async def importance(self, brief: str, task: Task) -> float | None:
        """How much one task matters under the brief, as a number where higher is more. None when this
        backend cannot score a task on its own, in which case ranking compares everything in pairs."""
        return None

    @abstractmethod
    async def compare(self, brief: str, a: Task, b: Task) -> float: ...

    # tier W and F: agent sessions that end in one typed record
    @abstractmethod
    async def confirm(self, gate: str, choices: list[str], task: Task, first: Decision) -> Decision: ...
    @abstractmethod
    async def adapt(self, benchmark: str, n_tasks: int) -> dict: ...
    @abstractmethod
    async def write_mutants(self, task: Task, n: int = 8) -> dict: ...
    @abstractmethod
    async def audit(self, task: Task, round_no: int) -> Decision: ...
    @abstractmethod
    async def second_opinion(self, task: Task, round_no: int) -> Decision: ...
    @abstractmethod
    async def repair(self, task: Task, defect: str, round_no: int, tier: str) -> dict: ...
    @abstractmethod
    async def write_report(self, task: Task, blackboard: dict) -> dict: ...
    @abstractmethod
    async def answer(self, question: str, context: str) -> str: ...

    # Harbor: runs of the verifier and of agents
    @abstractmethod
    async def run_oracle(self, task: Task) -> TrialResult: ...
    @abstractmethod
    async def run_noop(self, task: Task) -> TrialResult: ...
    @abstractmethod
    async def run_mutant(self, task: Task, mutant: dict) -> bool: ...
    @abstractmethod
    async def run_trial(self, task: Task, entry: SuiteEntry, index: int) -> TrialResult:
        """Run one agent trial. Must report the agent version that actually ran."""
        ...
