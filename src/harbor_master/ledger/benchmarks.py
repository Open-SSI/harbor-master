"""The benchmarks an index may curate: signed records, added by anyone on the contributor list (DESIGN.md Section 10).

Adding a benchmark is a `benchmark` record in the adder's own shard, like every other record, so
it needs no lock and no permission from the owner, and two people adding at once never write the
same file. The list is what the records say. `benchmarks/<id>.toml` is a readable copy of each
entry, written from the records: editing one by hand changes nothing.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable

import tomli_w

from .events import CONTRIBUTORS  # noqa: F401
from .store import MANUAL, Ledger, Shard

BENCH = "_benchmarks"       # the shard folder that holds these records
FOLDER = "benchmarks"       # the readable copies, one file per benchmark
REQUIRED = ("id", "name", "description", "github", "ref", "commit", "tasks", "version", "released", "fields", "paper")
ORDER = ("id", "name", "description", "paper", "github", "homepage", "license", "fields", "version", "released", "ref", "commit",
         "tasks", "task_count", "format", "added_by", "added")
HEADER = "# Written by Harbor-Master from the signed record in ledger/. Editing this file changes nothing:\n" \
         "# a benchmark is added or changed on the hm screen, with /benchmarks add URL\n\n"


def missing(entry: dict) -> list[str]:
    """What an entry still lacks. A benchmark with no paper says so: paper = "none"."""
    return [k for k in REQUIRED if not entry.get(k)]


def listed(events: Iterable[dict], owners: Iterable[str] = ()) -> dict[str, dict]:
    """The list, by ID. The first record for an ID stands: only the person who added it, or an owner,
    may replace it with a new release or take it off."""
    owners, out, by = set(owners), {}, {}
    for e in sorted((e for e in events if e["type"] == "benchmark"), key=lambda e: e["time"]):
        entry = e.get("entry") or {}
        name = entry.get("id") or e.get("target", "")
        allowed = name not in by or e["runner"] == by[name] or e["runner"] in owners
        if e.get("value") == "add" and name and allowed and not missing(entry):
            by.setdefault(name, e["runner"])
            out[name] = {**entry, "added_by": by[name], "added": time.strftime("%Y-%m-%d", time.gmtime(e["time"]))}
        elif e.get("value") == "remove" and name in out and allowed:
            del out[name], by[name]
    return out


def entries(index_dir: Path) -> list[dict]:
    from .verify import load_contributors
    owners = [c["github"] for c in load_contributors(index_dir) if c.get("role") == "owner" and c.get("github")]
    return list(listed(Ledger(index_dir).events, owners).values())


def add(index_dir: Path, ident, protocol: str, entry: dict) -> dict:
    return Shard(index_dir, BENCH, ident, MANUAL, protocol).append("benchmark", value="add", entry=entry)


def remove(index_dir: Path, ident, protocol: str, name: str, reason: str = "") -> dict:
    return Shard(index_dir, BENCH, ident, MANUAL, protocol).append("benchmark", value="remove", target=name, reason=reason)


def text(entry: dict) -> str:
    ordered = {k: entry[k] for k in ORDER if entry.get(k) not in (None, "", [])}
    return HEADER + tomli_w.dumps({**ordered, **{k: v for k, v in entry.items() if k not in ordered and v not in (None, "", [])}})


def write_views(index_dir: Path) -> None:
    """Make benchmarks/ say what the records say: one file per listed benchmark, and no other."""
    folder, now = index_dir / FOLDER, {e["id"]: e for e in entries(index_dir)}
    if not now and not folder.exists():
        return
    folder.mkdir(exist_ok=True)
    for name, entry in now.items():
        path, body = folder / f"{name}.toml", text(entry)
        if not path.exists() or path.read_text() != body:
            path.write_text(body)
    for path in folder.glob("*.toml"):
        if path.stem not in now:
            path.unlink()
