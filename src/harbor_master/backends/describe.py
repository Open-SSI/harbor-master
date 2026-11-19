"""Drafting a benchmark's entry when someone adds it to an index (DESIGN.md Section 10).

The person gives an address. The code reads what can be read without judgment: the release, the
commit, where the tasks are. A small agent reads the repository for the rest: name, description,
paper, fields. The person confirms the draft or says what to change, and only then is it signed.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import skills
from ..core.config import Config
from . import claude

SKILL = "session/describe-benchmark"
EDITABLE = ("name", "description", "paper", "fields", "license", "homepage")
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": list(EDITABLE),
    "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "paper": {"type": "string"},
                   "fields": {"type": "array", "items": {"type": "string"}}, "license": {"type": "string"},
                   "homepage": {"type": "string"}},
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
    """Take what was filled in. An empty answer leaves what the draft had."""
    for key in EDITABLE:
        value = data.get(key)
        if key == "fields":
            value = [f.strip().lower() for f in value or [] if f.strip()]
        elif isinstance(value, str):
            value = " ".join(value.split())
        if value:
            entry[key] = value
    return entry


def direct(entry: dict, text: str) -> bool:
    """A reply written as `paper: URL`, one field to a line, is taken as it stands. Anything else needs the agent."""
    found = {}
    for line in (ln for ln in text.splitlines() if ln.strip()):
        m = re.match(r"\s*(name|description|paper|fields?|license|homepage)\s*[:=]\s*(.+)$", line, re.I)
        if m is None:
            return False
        key = "fields" if m.group(1).lower().startswith("field") else m.group(1).lower()
        found[key] = [f for f in re.split(r"\s*,\s*", m.group(2).strip())] if key == "fields" else m.group(2).strip()
    merge(entry, found)
    return bool(found)


def _lines(entry: dict, keys) -> str:
    return "\n".join(f"{k}: {entry[k]}" for k in keys if entry.get(k) not in (None, "", [])) or "(nothing)"


async def fill(cfg: Config, entry: dict, repo: Path, note: str = "") -> tuple[dict, float]:
    """One short session of the worker tier, reading the repository. Returns what it filled in, and what it cost."""
    skill = skills.load(SKILL)
    known = _lines(entry, ("id", "github", "version", "released", "tasks", "task_count", "format"))
    reply = await claude.ask(cfg.tier("worker").model, skill.system,
                             skill.fill(known=known, draft=_lines(entry, EDITABLE), note=note or "(nothing yet)"),
                             SCHEMA, cwd=repo, max_turns=10)
    return reply.data, reply.cost
