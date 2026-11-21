"""Drafting a benchmark's entry when someone adds it to an index (DESIGN.md Section 9).

The person gives whatever they have: a repository in any form, a Hugging Face page, a paper, a project page, or a
name. The worker agent searches the web for the benchmark's repository and for what is known about it: name,
description, paper, and the date it was released, which the recency gate compares. The code then does what needs no
judgment: it pins the release, downloads it, and finds the tasks. The person checks the draft once, or says what to
change, and only then is it signed.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import skills
from ..core.config import Config
from . import sessions

SKILL = "session/describe-benchmark"
EDITABLE = ("name", "description", "paper", "released", "license", "homepage")
# What the agent reads, for the benchmark-level checks (tasks/checks.py): not edited by hand, but kept in the entry.
ASSESSED = ("task_kind", "grading", "solutions", "access", "best_reported", "assessed_from")
KINDS = ["agentic", "multiple_choice", "question_answering", "short_answer", "mixed", "unknown"]
GRADING = ["programmatic", "llm_judge", "human", "none", "unknown"]
SOLUTIONS = ["all", "some", "none", "unknown"]
ACCESS = ["public", "gated", "held_out", "unknown"]
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["repository", "release", *EDITABLE, *ASSESSED, "sources"],
    "properties": {"repository": {"type": "string", "description": "the plain address to download it from, or empty"},
                   "release": {"type": "string", "description": "the newest release tag, or empty"},
                   "name": {"type": "string"}, "description": {"type": "string"}, "paper": {"type": "string"},
                   "released": {"type": "string", "description": "YYYY-MM-DD, or empty"},
                   "license": {"type": "string"},
                   "homepage": {"type": "string"},
                   "task_kind": {"type": "string", "enum": KINDS},
                   "grading": {"type": "string", "enum": GRADING},
                   "solutions": {"type": "string", "enum": SOLUTIONS},
                   "access": {"type": "string", "enum": ACCESS},
                   "best_reported": {"type": "number", "description": "the best share of tasks any model or agent is reported to solve, 0 to 1, or -1"},
                   "assessed_from": {"type": "string", "description": "one sentence: where those five came from"},
                   "sources": {"type": "array", "items": {"type": "string"}, "maxItems": 5}},
}
PAPER = re.compile(r"https?://(?:arxiv\.org/(?:abs|pdf)/[\w.\-/]+|doi\.org/[\w.\-/()]+|openreview\.net/forum\?id=[\w\-]+)")


def guess(repo: Path) -> dict:
    """What a README gives away with no model: its first paragraph, and the first link to a paper."""
    readme = next((p for p in (repo / n for n in ("README.md", "README.rst", "README.txt", "README")) if p.is_file()), None)
    if readme is None:
        return {}
    text = readme.read_text(errors="replace")
    prose = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip() and not re.match(r"[#<!\[|`=\-*]", b.strip())]
    link = PAPER.search(text)
    out = {"description": " ".join(prose[0].split())[:300] if prose else "", "paper": link.group(0).rstrip(".)") if link else ""}
    return {k: v for k, v in out.items() if v}


def merge(entry: dict, data: dict) -> dict:
    """Take what was filled in. An empty answer leaves what the draft had. What the agent assessed for the checks is
    taken as it is, and an unknown one leaves what was there."""
    for key in ASSESSED:
        value = data.get(key)
        if key == "best_reported":
            if isinstance(value, (int, float)) and 0 <= value <= 1:
                entry[key] = round(float(value), 3)
        elif isinstance(value, str) and value.strip() and value != "unknown":
            entry[key] = " ".join(value.split())[:300]
    for key in EDITABLE:
        value = data.get(key)
        if isinstance(value, str):
            value = " ".join(value.split())
        if key == "released" and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])", value or ""):
            continue                # not a date: the one the draft has stays
        if value:
            entry[key] = value
    return entry


def direct(entry: dict, text: str) -> bool:
    """A reply written as `paper: URL`, one to a line, is taken as it stands. Anything else needs the agent."""
    found = {}
    for line in (ln for ln in text.splitlines() if ln.strip()):
        m = re.match(r"\s*(name|description|paper|released|license|homepage)\s*[:=]\s*(.+)$", line, re.I)
        if m is None:
            return False
        found[m.group(1).lower()] = m.group(2).strip()
    merge(entry, found)
    return bool(found)


def _lines(entry: dict, keys) -> str:
    return "\n".join(f"{k}: {entry[k]}" for k in keys if entry.get(k) not in (None, "", [])) or "(nothing)"


async def find(cfg: Config, given: str, entry: dict | None = None, repo: Path | None = None, note: str = "") -> tuple[dict, float]:
    """One session of the worker tier that searches the web for the benchmark: from what the person gave, its
    repository, its newest release and its entry. With `entry` and `repo`, a draft is being corrected: the agent sees
    it, and can read the repository as it was downloaded. Returns what it found, and what it cost."""
    skill = skills.load(SKILL)
    entry = entry or {}
    known = _lines(entry, ("id", "github", "ref", "version", "tasks", "task_count", "format"))
    reply = await sessions.ask(cfg.tier("worker"), skill.system,
                               skill.fill(given=given, known=known, draft=_lines(entry, EDITABLE), note=note or "(nothing yet)"),
                               SCHEMA, cwd=repo if repo is not None and repo.is_dir() else None, max_turns=24, web=True)
    return reply.data, reply.cost
