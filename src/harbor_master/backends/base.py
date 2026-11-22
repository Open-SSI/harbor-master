"""What a backend must provide: the three model tiers and Harbor (DESIGN.md Sections 2 and 3).

To add a real backend, subclass `Backend` and return it from `make_backend`. Every method
either returns one typed record or one trial result, and reports its cost through `usage`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from ..core.config import Config, SuiteEntry
from ..tasks import Task

# The checks a task gets before any agent trial. Each is an audit by an agent that reads the files, decided by the
# decision tier, and audited again by the frontier tier when that decision is unsure. `reject` is the value that would
# drop the task, which a person confirms.
CHECKS = {
    "solution": {"stage": "verify", "skill": "verify/solution", "choices": ["valid", "invalid"], "reject": "invalid",
                 "doing": "auditing the reference solution", "says": "the reference solution looks invalid"},
    "coverage": {"stage": "probe", "skill": "probe/coverage", "choices": ["covered", "unstated"], "reject": "unstated",
                 "doing": "checking the tests against the instruction", "says": "the tests check something the instruction does not state"},
}


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
    evidence: str = ""         # what the answer rests on, checked: a file of the task and a line quoted from it


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
    exit_code: int | None = None   # how the solution script or agent itself ended, as Harbor wrote it; None when unknown


Usage = Callable[[str, float, int], None]  # (tier, cost, tokens)


class TaskDropped(Exception):
    """A person chose to drop the task while one of its trials was running: the work cell drops it and moves on."""

    def __init__(self, reason: str, why: str):
        super().__init__(why)
        self.reason, self.why = reason, why


class NotImplementedBackend(RuntimeError):
    pass


class Unreachable(RuntimeError):
    """A model's session could not be had: it did not start, the connection broke, or the provider was overloaded, and
    waiting and trying again did not help. Nothing the task did: it is never repaired or dropped for it, only left for
    the next run."""


class Backend(ABC):
    def __init__(self, cfg: Config, usage: Usage):
        self.cfg, self.usage = cfg, usage
        # Set by the session: a person is asked what to do with a hung trial, and answers "retry" or "drop".
        self.on_hung: Callable[[Task, str, Decision, float], Awaitable[str]] | None = None
        # Set by the session: a long trial's latest step, every few minutes, for the log.
        self.on_progress: Callable[[Task, str, str, float], None] | None = None
        self.on_spend: Callable[[str, float | None], None] | None = None    # a running trial's cost so far; None when it ended

    def remember(self, task: Task, trial: dict) -> None:
        """A trial recorded earlier, perhaps by another run on this machine, for the audits to read. Nothing by default."""

    async def stuck(self, task: Task, what: str, log: str, quiet: float) -> str:
        """A trial whose logs stopped moving: the worker agent reads them. "wait" when it judges the trial still working;
        when hung, what the person chose: "retry" or "drop". With nobody to ask, it is run again."""
        d = await self.judge_stuck(what, log, quiet)
        if d.value != "hung":
            return "wait"
        return await self.on_hung(task, what, d, quiet) if self.on_hung else "retry"

    @abstractmethod
    async def judge_stuck(self, what: str, log: str, quiet: float) -> Decision:
        """Whether a trial whose logs have been still for `quiet` seconds is "hung" or "working", read from its log by
        the worker agent, with what it was doing in the rationale."""
        ...

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
    async def reported(self, benchmark: str, repo: Path, names: list[str]) -> dict:
        """What the benchmark's own results say, task by task: {"tasks": {name: {"pass_rate", "model"}},
        "source", "cost"}. Only tasks with a reported result are named. None reported: an empty "tasks"."""
        return {"tasks": {}, "source": "", "cost": 0.0}

    @abstractmethod
    async def check_audit(self, task: Task, check: str, tier: str, earlier: list[Decision] | None = None) -> Decision:
        """An agent of `tier` reads the task's files for one of CHECKS: its own verdict is the Decision's value (with no
        probability), and what it found, verdict and decisive points first, its rationale. `earlier` holds the audits the
        decision tier was unsure on, or decided against."""
        ...
    @abstractmethod
    async def check_decide(self, task: Task, check: str, audits: list[Decision]) -> Decision:
        """The decision tier decides one of CHECKS from the audits, with a probability. Empty when it declined."""
        ...
    @abstractmethod
    async def diagnose(self, task: Task, defect: str, why: str) -> Decision:
        """What would make a selected task valid, from the decision tier, before a repair is paid for: write_solution,
        repair_solution, repair_verifier, repair_environment or drop. An empty value when it declined to say."""
        ...
    @abstractmethod
    async def audit(self, task: Task, round_no: int) -> Decision: ...
    @abstractmethod
    async def second_opinion(self, task: Task, round_no: int) -> Decision: ...
    @abstractmethod
    async def repair(self, task: Task, defect: str, round_no: int, tier: str, why: str = "") -> dict:
        """{"fixed", "what", "cost"}, and "path" when the repair made a new copy of the task: the version that goes on."""
        ...
    @abstractmethod
    async def final_audit(self, task: Task) -> Decision: ...
    @abstractmethod
    async def write_report(self, task: Task, blackboard: dict) -> dict: ...
    @abstractmethod
    async def answer(self, question: str, context: str) -> str: ...

    # Harbor: runs of the verifier and of agents
    @abstractmethod
    async def run_trial(self, task: Task, entry: SuiteEntry, index: int, max_hours: float = 0) -> TrialResult:
        """Run one agent trial. Must report the agent version that actually ran. `max_hours` is the most it
        may run, the difficulty filter's own limit when 0."""
        ...
