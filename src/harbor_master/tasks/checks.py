"""The benchmark-level checks: cheap questions about a whole benchmark, asked before anything costly is spent on it
(DESIGN.md Section 9). No trial is run and no model is asked: each check reads the entry, which holds what the worker
agent found on the web when the benchmark was added, or the files of its pinned release, or asks a registry whether an
image is public.

They are asked twice. When a benchmark is added, the curate screen shows them, one by one, in a box: a failed one says
why it is rejected, and the person confirms that it is listed as dropped. When a run takes a benchmark from the list, the same
checks are asked before its adapter is built, so that an unfit benchmark never costs an adapter.

A check passes, warns, fails, or cannot tell. Only a failure rejects. A benchmark is rejected when its tasks are not for
agents (multiple choice, question answering, short answer) or not public (held out, or gated behind a request), or on
hard evidence: the files of the release (no tests), a registry that refuses every image, or a
release date before the cutoff, which the person sees in the draft and can correct. The rest only warns: how its tasks
are graded as the agent read it, how far it is solved, and its license.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..core.config import Config

PASS, WARN, FAIL, UNKNOWN = "pass", "warn", "fail", "unknown"
SAMPLE = 40                 # tasks read for solutions and tests: enough to judge a benchmark, and quick
SOLVED = 0.9                # a benchmark whose best reported score is above this looks solved: warned, not rejected
OPEN = {"MIT", "APACHE-2.0", "BSD-3-CLAUSE", "BSD-2-CLAUSE", "ISC", "CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "MPL-2.0",
        "ODC-BY", "UNLICENSE", "GPL", "GPL-2.0", "GPL-3.0", "LGPL-3.0", "AGPL-3.0"}


@dataclass
class Check:
    key: str
    label: str              # what is checked, as a person reads it: "released after 2025-06-01"
    status: str = UNKNOWN
    detail: str = ""        # what was found, or why it fails

    @property
    def failed(self) -> bool:
        return self.status == FAIL


def tasks_root(entry: dict, source: Path | None) -> Path | None:
    """The folder of the benchmark's Harbor tasks in its downloaded release, or None when it has none (yet)."""
    if source is None or entry.get("format", "harbor") != "harbor":
        return None
    root = source / entry.get("tasks", ".")
    return root if root.is_dir() else None


# ----- each check: (entry, the release's files or None, settings) -> (status, detail) -----

def recency(entry: dict, source, cfg: Config) -> tuple[str, str]:
    released, cutoff = entry.get("released", ""), cfg.triage.released_after
    if not released:
        return UNKNOWN, "no release date was found"
    if released < cutoff:
        return FAIL, f"released {released}, before {cutoff}: models trained since may have seen its tasks and answers"
    return PASS, f"released {released}"


def license_(entry: dict, source, cfg: Config) -> tuple[str, str]:
    name = str(entry.get("license", "")).strip()
    if not name:
        return WARN, "no license was found: by default, all rights are reserved by its authors"
    key = name.upper()
    if "-NC" in key or "-ND" in key:
        return WARN, f"{name}: non-commercial or no-derivatives, so check that the index's use fits it"
    if key in OPEN or key.startswith(("GPL", "BSD", "CC-BY")):
        return PASS, name
    return WARN, f"{name}: not a license this check knows, so read it"


def kind(entry: dict, source, cfg: Config) -> tuple[str, str]:
    value = entry.get("task_kind", "")
    said = f" ({entry['assessed_from']})" if entry.get("assessed_from") else ""
    if value == "agentic":
        return PASS, "its tasks are executed in an environment: code is written and run, files are produced"
    if value in cfg.triage.exclude_formats:
        return FAIL, f"its tasks are {value.replace('_', ' ')}, graded on one typed reply, not work an agent does{said}"
    if value == "mixed":
        return WARN, f"some of its tasks are graded on one typed reply: the screen drops those{said}"
    if tasks_root(entry, source) is not None:
        return PASS, "its tasks are Harbor tasks, which an agent executes"
    return UNKNOWN, "no source said what its tasks ask"


def grading(entry: dict, source, cfg: Config) -> tuple[str, str]:
    root, value = tasks_root(entry, source), entry.get("grading", "")
    said = f" ({entry['assessed_from']})" if entry.get("assessed_from") else ""
    if root is not None:
        tasks = _sample(root)
        tested = sum(t.has_tests for t in tasks)
        if tasks and not tested:
            return FAIL, f"none of the {len(tasks)} tasks read has tests: nothing grades a submission"
        if tasks and tested < len(tasks):
            return WARN, f"{tested} of the {len(tasks)} tasks read have tests"
    if root is not None and value in ("programmatic", "", "unknown") or value == "programmatic":
        return PASS, "a script or test suite decides" + (": every one of the tasks read has tests" if root is not None else said)
    if value == "llm_judge" and cfg.triage.allow_llm_judge_verifier:
        return PASS, f"a language model grades its submissions, which this index allows{said}"
    if value in ("llm_judge", "human", "none"):
        who = {"llm_judge": "a language model grades", "human": "a person grades", "none": "nothing grades"}[value]
        return WARN, f"the agent read that {who} its submissions: the screen checks each task's own grading{said}"
    return UNKNOWN, "no source said how a submission is graded"


def solutions(entry: dict, source, cfg: Config) -> tuple[str, str]:
    root, value = tasks_root(entry, source), entry.get("solutions", "")
    if root is not None:
        tasks = _sample(root)
        have = sum(t.has_solution for t in tasks)
        if tasks and not have:
            return WARN, f"none of the {len(tasks)} tasks read has a reference solution: for each selected task, a frontier trial must pass it, and that trial's work becomes its reference solution"
        if tasks and have < len(tasks):
            return WARN, f"{have} of the {len(tasks)} tasks read have a reference solution: for each selected task that has none, a frontier trial must pass it, and that trial's work becomes its reference solution"
        if tasks:
            return PASS, f"every one of the {len(tasks)} tasks read has one"
    if value == "all":
        return PASS, "its tasks ship reference solutions"
    if value == "some":
        return WARN, "only some of its tasks ship reference solutions"
    if value == "none":
        return WARN, "the agent read that its tasks ship no reference solution: for each selected task, a frontier trial must pass it, and that trial's work becomes its reference solution"
    return UNKNOWN, "no source said whether its tasks have reference solutions"


def access(entry: dict, source, cfg: Config) -> tuple[str, str]:
    value = entry.get("access", "")
    if value == "held_out":
        return FAIL, "its test tasks are kept private: what can be downloaded is not the benchmark"
    if value == "gated":
        return FAIL, "its tasks are downloaded only after asking for access: they are not public"
    if value == "public" or source is not None:
        return PASS, "anyone can download its tasks" + (": its release was downloaded here" if source is not None else "")
    return UNKNOWN, "no source said whether its tasks can be downloaded"


def solved(entry: dict, source, cfg: Config) -> tuple[str, str]:
    best = entry.get("best_reported")
    if not isinstance(best, (int, float)):
        return UNKNOWN, "no score is reported for it"
    if best > SOLVED:
        return WARN, f"the best reported score is {best:.0%}: it looks solved, and the screen drops the tasks a model solves"
    if best > cfg.triage.max_reported_pass_rate:
        return WARN, f"the best reported score is {best:.0%}: many of its tasks may be too easy, and the screen drops them"
    return PASS, f"the best reported score is {best:.0%}"


def images(entry: dict, source, cfg: Config) -> tuple[str, str]:
    if source is None:
        return UNKNOWN, "its files are not here yet"
    from . import environments
    found = environments.check(tasks_root(entry, source) or source)     # its tasks' images: not those of its archive or tools
    why = environments.verdict(found)
    if why:
        return FAIL, why
    if environments.partly(found):
        return WARN, f"some images are refused by their registry ({', '.join(found['private'][:3])}): those tasks cannot run here"
    if not found["named"]:
        return PASS, "its tasks name no container image of their own"
    looked = len(found["public"]) + len(found["unknown"])
    if found.get("built"):
        return WARN, (f"{found['built']} of its images are built by its own harness, not pulled: whether they build is known "
                      f"only when its adapter runs them. {len(found['public'])} of the {looked} looked up are public")
    return PASS, (f"{len(found['public'])} of the {looked} looked up are public"
                  + (f", and {len(found['unknown'])} could not be told" if found["unknown"] else "") + ": none is refused")


def _sample(root: Path) -> list:
    from .intake import find_tasks, load_task
    return [load_task(d, "check") for d in find_tasks(root)[:SAMPLE]]


# key, what is checked, the check. In this order: the cheapest, and the most telling, first.
CHECKS: list[tuple[str, Callable[[Config], str], Callable]] = [
    ("recency", lambda cfg: f"released after {cfg.triage.released_after}", recency),
    ("kind", lambda cfg: "its tasks are for agents", kind),
    ("grading", lambda cfg: "a program grades it", grading),
    ("solutions", lambda cfg: "it has reference solutions", solutions),
    ("access", lambda cfg: "its tasks are public", access),
    ("solved", lambda cfg: "it is not already solved", solved),
    ("license", lambda cfg: "its license allows evaluation", license_),
    ("images", lambda cfg: "its container images are public", images),
]


def planned(cfg: Config, simulate: bool = False) -> list[Check]:
    """The checks, not asked yet: what the box lists before the first answer."""
    return [Check(key, label(cfg)) for key, label, _ in CHECKS if not (simulate and key == "images")]


def ask(check: Check, entry: dict, source: Path | None, cfg: Config) -> Check:
    """Answer one check. One that breaks cannot tell, and says why: a check never stops the others."""
    fn = next(f for key, _, f in CHECKS if key == check.key)
    try:
        check.status, check.detail = fn(entry, source, cfg)
    except Exception as err:            # a check that breaks cannot tell: it never rejects a benchmark by itself
        check.status, check.detail = UNKNOWN, f"could not be checked: {type(err).__name__}: {str(err)[:120]}"
    return check


def run(entry: dict, source: Path | None, cfg: Config, simulate: bool = False) -> list[Check]:
    return [ask(c, entry, source, cfg) for c in planned(cfg, simulate)]


def why(checks: list[Check]) -> str:
    """Why a benchmark is rejected, in one paragraph: every failed check, with what it found."""
    return " ".join(f"{c.detail[:1].upper() + c.detail[1:]}." for c in checks if c.failed)


def key(entry: dict) -> str:
    """What a record of a benchmark's checks is filed under: the benchmark at its pinned commit."""
    return f"{entry['id']}@{entry.get('commit', '')}"


def summary(checks: list[Check]) -> str:
    n = {s: sum(c.status == s for c in checks) for s in (PASS, WARN, FAIL, UNKNOWN)}
    return (f"{n[PASS]} passed" + (f", {n[WARN]} warned" if n[WARN] else "") + (f", {n[FAIL]} failed" if n[FAIL] else "")
            + (f", {n[UNKNOWN]} could not tell" if n[UNKNOWN] else ""))
