"""Configuration, in two files (DESIGN.md Section 10): protocol.toml, in the index repository,
is the locked protocol. .hm/hm.toml is this machine's own settings."""
from __future__ import annotations

import hashlib
import json
import tomllib
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

LOCAL = ".hm"                      # this machine's own files inside the clone of an index. Git ignores the folder
CONFIG_NAME = "hm.toml"            # in .hm: parallelism and budget. The user's to change
PROTOCOL_NAME = "protocol.toml"    # the protocol: everything that can change a result. Locked
ABOUT_NAME = "about.toml"          # what the index is: its name and description. Not locked
PROTOCOL_SECTIONS = ("tiers", "escalation", "triage", "adapt", "select", "verify", "difficulty", "fix", "index")


class Tier(BaseModel):
    provider: str = "mock"
    model: str


class Tiers(BaseModel):
    decision: Tier = Tier(provider="openrouter", model="typesafe/jev-1.14")
    worker: Tier = Tier(provider="claude-agent-sdk", model="claude-sonnet-5-5")
    frontier: Tier = Tier(provider="claude-agent-sdk", model="claude-opus-5-5")
    chat: str = "worker"  # which tier answers questions typed in the session


class Escalation(BaseModel):
    min_confidence: float = 0.85
    # A gate that a later stage checks for free can accept a less sure answer.
    # The verify stage measures the verifier's real time, so a wrong "cheap" costs nothing.
    gate_min_confidence: dict[str, float] = {"verifier_cost": 0.6}
    frontier_sample: float = 0.05


class Triage(BaseModel):
    released_after: str = "2025-06-01"
    require_reference_solution: bool = True
    exclude_formats: list[str] = ["multiple_choice", "question_answering", "short_answer"]
    allow_llm_judge_verifier: bool = False
    max_verifier_seconds: int = 300
    max_reported_pass_rate: float = 0.5   # a task that a model is already reported to solve more often than this is dropped


class Adapt(BaseModel):
    """Building the adapter for a benchmark that is not in Harbor format."""
    max_rounds: int = 3               # how many times a model may write it before a person is asked
    frontier_after_round: int = 2     # the worker tier writes rounds up to this one, the frontier tier the rest
    oracle_tasks: int = 2             # generated tasks whose reference solution is run through Harbor as a check
    require_review: bool = False      # if true, a person confirms an adapter that passes before it is used


class Select(BaseModel):
    top_k: int = 24
    max_fraction: float = 0.25
    comparisons_per_task: int = 12
    pool_factor: int = 3              # compare in pairs only the best pool_factor x cap tasks by score
    stratify: bool = True
    backfill: bool = False
    require_brief: bool = True
    require_signoff: bool = True      # the lead approves the selection before any trial is spent


class Verify(BaseModel):
    parity: str = "sampled"  # "off" | "sampled" | "full"
    parity_tasks: int = 5      # parity runs with the worker tier's model


@lru_cache(maxsize=None)
def presets() -> dict[str, dict]:
    """The agent configurations that have a short name: presets.toml, beside this file."""
    return tomllib.loads((Path(__file__).parent / "presets.toml").read_text()).get("preset", {})


class SuiteEntry(BaseModel):
    """One agent configuration: a Harbor agent and a model. `use = "NAME"` takes a preset's fields,
    and any field written beside it replaces the preset's."""
    agent: str
    model: str
    reasoning_effort: str = ""
    version: str = ""          # the agent's own version. Pin it: a different build is a different request
    key: str = ""              # the variable in secrets.env it needs
    env: dict[str, str] = {}   # settings passed to the agent. Never a key

    @model_validator(mode="before")
    @classmethod
    def _preset(cls, data):
        if not isinstance(data, dict) or "use" not in data:
            return data
        known = presets()
        if data["use"] not in known:
            raise ValueError(f"no preset is named {data['use']!r}. The presets: {', '.join(known)}. See: hm roster presets")
        out = {**known[data["use"]], **{k: v for k, v in data.items() if k != "use"}}
        if "name" in cls.model_fields:
            out.setdefault("name", data["use"])     # on a roster, the preset's name is the configuration's name
        return out


class RosterEntry(SuiteEntry):
    """One agent configuration to benchmark on the index: roster.toml in the index repository."""
    name: str


def roster(workspace: Path) -> list[RosterEntry]:
    path = workspace / "roster.toml"
    listed = tomllib.loads(path.read_text()).get("config", []) if path.exists() else []
    return [RosterEntry.model_validate(c) for c in listed]


class Difficulty(BaseModel):
    suite: list[SuiteEntry] = [
        SuiteEntry(agent="claude-code", model="claude-fable-5-1", reasoning_effort="max", version="2.1.292"),
        SuiteEntry(agent="codex", model="gpt-6-astra", reasoning_effort="max", version="0.162.1"),
    ]
    trials_per_config: int = 3
    max_pass_rate: float = 0.40
    max_agent_hours: float = 4.0       # the most one agent trial may run. A task's own limit applies when it is shorter
    early_stop: bool = True
    cheap_solve: bool = True           # the worker tier's model tries first. A task it solves every time never reaches the suite
    cheap_trials: int = 2              # its tries, run side by side. The task is dropped only if all of them pass
    cheap_agent: str = "claude-code"   # the agent that model runs in
    cheap_agent_hours: float = 1.0     # the most that one of them may run


class Fix(BaseModel):
    max_rounds: int = 3
    frontier_after_round: int = 2


class Index(BaseModel):
    max_per_benchmark: int = 8     # accepted tasks one benchmark may have in the index


class Run(BaseModel):
    cells: int = 4
    max_containers: int = 16
    sandbox: str = "docker"
    budget_usd: float = 200.0
    budget_usd_per_task: float = 50.0
    max_tokens_per_agent_session: int = 400_000
    on_incomplete: str = "resume"  # "resume" | "restart" | "skip"
    claim_hours: float = 12.0      # how long a claim on tasks holds if the run never releases it


class Requires(BaseModel):
    harbor_master: str = "0.1.0"   # the oldest Harbor-Master that may run this index


class About(BaseModel):
    """What the index is, from about.toml. Shown wherever indexes are listed."""
    name: str = ""
    title: str = ""
    description: str = ""
    homepage: str = ""
    license: str = ""
    tasks: str = ""                # where accepted tasks are kept: a Hugging Face dataset repository, or a folder


class Config(BaseModel):
    tiers: Tiers = Field(default_factory=Tiers)
    escalation: Escalation = Field(default_factory=Escalation)
    triage: Triage = Field(default_factory=Triage)
    adapt: Adapt = Field(default_factory=Adapt)
    select: Select = Field(default_factory=Select)
    verify: Verify = Field(default_factory=Verify)
    difficulty: Difficulty = Field(default_factory=Difficulty)
    fix: Fix = Field(default_factory=Fix)
    index: Index = Field(default_factory=Index)
    run: Run = Field(default_factory=Run)
    requires: Requires = Field(default_factory=Requires)
    about: About = Field(default_factory=About)

    @property
    def protocol(self) -> str:
        """Hash of the settings that make results comparable.

        Changing anything hashed here starts a new protocol, under which filled slots are
        empty again. A shared ledger locks one protocol at a time: see ledger/protocol.py.
        """
        return hashlib.sha256(json.dumps(self.protocol_settings(), sort_keys=True).encode()).hexdigest()[:12]

    def protocol_settings(self) -> dict:
        """Everything that can change a result: the tiers and their models, every threshold and rule,
        and the wording each agent is given. Left out: [run], which says how this machine works,
        [requires], and about.toml."""
        from .. import skills
        body = {k: getattr(self, k).model_dump() for k in PROTOCOL_SECTIONS}
        body["skills"] = skills.digest()
        return body

    def tier(self, name: str) -> Tier:
        if name == "chat":
            name = self.tiers.chat
        return getattr(self.tiers, name)


def local(workspace: Path) -> Path:
    """This machine's own folder in a workspace: settings, registered benchmarks, sources, runs."""
    return workspace / LOCAL


def simulated(workspace: Path) -> Path:
    """Where simulated runs keep their records: on this machine only, never in the index's ledger."""
    return workspace / LOCAL / "simulated"


def load(workspace: Path) -> Config:
    """The protocol and the index's description from the index repository, and this machine's
    settings from .hm/hm.toml. A protocol setting written in hm.toml is ignored."""
    from .. import skills
    read = lambda p: tomllib.loads(p.read_text()) if p.exists() else {}  # noqa: E731
    mine, shared = read(local(workspace) / CONFIG_NAME), read(workspace / PROTOCOL_NAME)
    skills.use(workspace / "skills")        # an index may word a skill its own way
    return Config.model_validate({"run": mine.get("run", {}), "about": read(workspace / ABOUT_NAME),
                                  **{k: v for k, v in shared.items() if k in (*PROTOCOL_SECTIONS, "requires")}})


def find_workspace(start: Path | None = None) -> Path | None:
    """The nearest directory at or above `start` that is a clone of an index: it holds a protocol.toml."""
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / PROTOCOL_NAME).exists():
            return d
    return None
