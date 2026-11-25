"""Configuration, in two files (DESIGN.md Section 9): protocol.toml, in the index repository,
is the protocol. .hm/hm.toml is this machine's own settings."""
from __future__ import annotations

import hashlib
import json
import tomllib
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

LOCAL = ".hm"                      # this machine's own files inside the clone of an index. Git ignores the folder
CONFIG_NAME = "hm.toml"            # at the top of the index folder, git-ignored: how this machine runs. The user's to change
CONFIG_EXAMPLE = "hm.example.toml"     # committed beside it, what every clone's hm.toml is made from
SECRETS_EXAMPLE = "secrets.env.example"   # committed: the keys an index needs, what every clone's secrets.env is made from
PROTOCOL_NAME = "protocol.toml"    # the protocol: everything that can change a result. Locked
ABOUT_NAME = "about.toml"          # what the index is: its name and description. Not locked
PROTOCOL_SECTIONS = ("tiers", "escalation", "triage", "adapt", "select", "difficulty", "fix")


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
    # A gate whose wrong answer is caught later can accept a less sure one: a verifier over max_verifier_seconds
    # still fails its trials.
    gate_min_confidence: dict[str, float] = {"verifier_cost": 0.6}


class Triage(BaseModel):
    released_after: str = "2025-06-01"
    require_reference_solution: bool = True   # a task with none must be passed by a frontier trial, whose work becomes its solution; off, it is dropped
    exclude_formats: list[str] = ["multiple_choice", "question_answering", "short_answer"]
    allow_llm_judge_verifier: bool = False
    max_verifier_seconds: int = 300   # a verifier that takes longer fails, and its task is dropped
    max_build_seconds: int = 600      # building a task's environment: one that takes longer fails, and its task is dropped
    max_verify_minutes: float = 30.0  # a reference solution run by the adapter check: longer fails the check
    max_reported_pass_rate: float = 0.4   # a task that a model is already reported to solve more often than this is dropped


class Adapt(BaseModel):
    """Building the adapter for a benchmark that is not in Harbor format."""
    max_rounds: int = 2               # rounds of writing it in all: the worker's, then the frontier model's
    frontier_after_round: int = 1     # the worker tier writes rounds up to this one, the frontier tier the rest
    oracle_tasks: int = 3             # generated tasks whose reference solution is run through Harbor as a check
    require_review: bool = False      # if true, a person confirms an adapter that passes before it is used


class Select(BaseModel):
    top_k: int = 24
    max_fraction: float = 0.25
    comparisons_per_task: int = 12
    pool_factor: int = 3              # compare in pairs only the best pool_factor x cap tasks by score
    stratify: bool = True
    require_brief: bool = True
    require_signoff: bool = True      # the lead approves the selection before any trial is spent


@lru_cache(maxsize=None)
def presets() -> dict[str, dict]:
    """The agent configurations that have a short name: presets.toml, beside this file."""
    return tomllib.loads((Path(__file__).parent / "presets.toml").read_text()).get("preset", {})


AGENTS = ("claude-code", "codex")      # the agents a trial runs: Claude models, and OpenAI models


class SuiteEntry(BaseModel):
    """One agent configuration: Claude Code or Codex, and a model. `use = "NAME"` takes a preset's fields,
    and any field written beside it replaces the preset's."""
    agent: str
    model: str
    reasoning_effort: str = ""
    version: str = ""          # the agent's own version. Pin it: a different build is a different request
    env: dict[str, str] = {}   # settings passed to the agent. Never a key

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str) -> str:
        if value not in AGENTS:
            raise ValueError(f"{value!r} is not an agent Harbor-Master runs: {' or '.join(AGENTS)}")
        return value

    @model_validator(mode="before")
    @classmethod
    def _preset(cls, data):
        if not isinstance(data, dict) or "use" not in data:
            return data
        known = presets()
        if data["use"] not in known:
            raise ValueError(f"no preset is named {data['use']!r}. The presets: {', '.join(known)}.")
        return {**known[data["use"]], **{k: v for k, v in data.items() if k != "use"}}


class Difficulty(BaseModel):
    suite: list[SuiteEntry] = [
        SuiteEntry(agent="claude-code", model="claude-fable-5-1", reasoning_effort="max", version="2.1.292"),
        SuiteEntry(agent="codex", model="gpt-6-astra", reasoning_effort="max", version="0.162.1"),
    ]
    max_pass_rate: float = 0.50       # kept when at most this share of the frontier trials pass: 3 of 6
    trials: int = 3                    # tries per task by each agent of the suite
    max_agent_hours: float = 4.0       # the most one agent trial may run. A task's own limit applies when it is shorter
    early_stop: bool = True
    cheap_solve: bool = True           # the worker tier's model tries first. A task it solves every time never reaches the suite
    cheap_trials: int = 2              # its tries, run side by side. The task is dropped only if all of them pass
    cheap_agent: str = "claude-code"   # the agent that model runs in
    cheap_agent_hours: float = 1.0     # the most that one of them may run


class Fix(BaseModel):
    max_rounds: int = 2               # repair rounds for a task: the worker model's, then the frontier model's
    frontier_after_round: int = 1


class Run(BaseModel):
    cells: int = 4
    max_containers: int = 16
    max_gpu_containers: int = 1    # of them, those whose task asks for a GPU. Docker shares the machine's GPUs, never reserves one
    sandbox: str = "docker"
    claim_hours: float = 12.0      # how long a claim on a benchmark holds. A run that goes on renews it
    watch_minutes: float = 3.0     # how often a running trial's logs are looked at
    quiet_minutes: float = 9.0     # a trial whose logs have not moved for this long is read by the worker agent: hung, or working
    agent_setup_minutes: float = 30.0   # the most that installing an agent in a task's container may take, before it runs


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
    canary: str = ""               # marks the repository as an index: CANARY and a GUID of its own
    version: str = "0.1.0"         # the index's version: its owner sets it


class Config(BaseModel):
    tiers: Tiers = Field(default_factory=Tiers)
    escalation: Escalation = Field(default_factory=Escalation)
    triage: Triage = Field(default_factory=Triage)
    adapt: Adapt = Field(default_factory=Adapt)
    select: Select = Field(default_factory=Select)
    difficulty: Difficulty = Field(default_factory=Difficulty)
    fix: Fix = Field(default_factory=Fix)
    run: Run = Field(default_factory=Run)
    requires: Requires = Field(default_factory=Requires)
    about: About = Field(default_factory=About)

    @property
    def protocol(self) -> str:
        """Hash of the settings that make results comparable.

        Every record names the protocol it was made under. Changing anything hashed here starts a new protocol, but
        what earlier ones decided stands: only what has no outcome yet runs under the new settings.
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
    settings from hm.toml beside them (before, .hm/hm.toml). A protocol setting written in hm.toml is ignored."""
    from .. import skills
    read = lambda p: tomllib.loads(p.read_text()) if p.exists() else {}  # noqa: E731
    mine = read(workspace / CONFIG_NAME) or read(local(workspace) / CONFIG_NAME)
    shared = read(workspace / PROTOCOL_NAME)
    skills.use(workspace / "skills")        # an index may word a skill its own way
    old = shared.get("difficulty", {}).pop("trials_per_config", None) or shared.pop("eval", {}).get("trials")   # where it used to be
    if old is not None:
        shared.setdefault("difficulty", {}).setdefault("trials", old)
    return Config.model_validate({"run": mine.get("run", {}), "about": read(workspace / ABOUT_NAME),
                                  **{k: v for k, v in shared.items() if k in (*PROTOCOL_SECTIONS, "requires")}})


CANARY = "harbor-master-canary GUID "


def is_index(folder: Path) -> str:
    """Why `folder` is not an index, or "" when it is: it has a protocol.toml, and an about.toml with the canary."""
    if not (folder / PROTOCOL_NAME).is_file():
        return f"it has no {PROTOCOL_NAME}"
    try:
        about = tomllib.loads((folder / ABOUT_NAME).read_text())
    except (OSError, ValueError):
        return f"it has no {ABOUT_NAME} that can be read"
    return "" if str(about.get("canary", "")).startswith(CANARY) else f"its {ABOUT_NAME} has no canary line"


def find_workspace(start: Path | None = None) -> Path | None:
    """The nearest directory at or above `start` that is a clone of an index: it holds a protocol.toml."""
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / PROTOCOL_NAME).exists():
            return d
    return None
