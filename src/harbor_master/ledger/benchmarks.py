"""The benchmarks an index may curate: signed records, added by anyone on the contributor list (DESIGN.md Section 9).

Adding a benchmark is a `benchmark` record in the adder's own shard, like every other record, so
it needs no lock and no permission from the owner, and two people adding at once never write the
same record file. The list is what the records say. `benchmarks.toml` is that list as one readable
file, written from the records: editing it by hand changes nothing.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable

import tomli_w

from .events import CONTRIBUTORS  # noqa: F401
from .store import MANUAL, Ledger, Shard

BENCH = "_benchmarks"       # the shard folder that holds these records
FILE = "benchmarks.toml"    # the list as one readable file
REQUIRED = ("id", "name", "description", "github", "ref", "commit", "tasks", "version", "released", "paper")
ORDER = ("id", "status", "by", "until", "name", "description", "paper", "github", "homepage", "license", "version", "released",
         "ref", "commit", "tasks", "task_count", "format", "added_by", "added")
HEADER = "# The benchmarks this index may curate, and where each stands: listed, curating (by whom, until when),\n" \
         "# curated or dropped. Written by Harbor-Master from the signed records in ledger/: editing it changes nothing.\n\n"


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


CLAIM = "__benchmark__"     # what a curation claims: the whole benchmark (engine/claims.py)


def statuses(events: Iterable[dict], names: Iterable[str], now: float | None = None) -> dict[str, dict]:
    """Where each benchmark stands, from the records: dropped (no adapter could be made), curating (someone holds it
    now: who, and until when), curated (a run finished every task it selected), or listed (nobody holds it, and it is
    not finished: never started, or let go, by hand or after 12 hours with no progress)."""
    from .fold import abandoned
    events = list(events)
    now = time.time() if now is None else now
    gone = abandoned(events)
    out = {}
    for name in names:
        if name in gone:
            out[name] = {"status": "dropped"}
            continue
        claims = sorted((e for e in events if e["type"] == "claim" and e["benchmark"] == name and CLAIM in e.get("tasks", [])),
                        key=lambda e: e["time"])
        live = {}
        curated = False
        for e in claims:
            who = (e["runner"], e.get("machine", ""))
            if e.get("released"):
                live.pop(who, None)
                curated = curated or bool(e.get("curated"))
            else:
                live[who], curated = float(e.get("expires", 0)), False
        holders = sorted((x, w[0]) for w, x in live.items() if x > now)
        if holders:
            out[name] = {"status": "curating", "by": holders[0][1], "until": time.strftime("%Y-%m-%d %H:%M", time.localtime(holders[0][0]))}
        else:
            out[name] = {"status": "curated" if curated else "listed"}
    return out


def entries(index_dir: Path) -> list[dict]:
    from .verify import load_contributors
    owners = [c["github"] for c in load_contributors(index_dir) if c.get("role") == "owner" and c.get("github")]
    return list(listed(Ledger(index_dir).events, owners).values())


def add(index_dir: Path, ident, protocol: str, entry: dict) -> dict:
    return Shard(index_dir, BENCH, ident, MANUAL, protocol).append("benchmark", value="add", entry=entry)


def remove(index_dir: Path, ident, protocol: str, name: str, reason: str = "") -> dict:
    return Shard(index_dir, BENCH, ident, MANUAL, protocol).append("benchmark", value="remove", target=name, reason=reason)


def with_status(index_dir: Path, found: list[dict]) -> list[dict]:
    """The entries, each with where it stands: status, and who curates it until when."""
    told = statuses(Ledger(index_dir).events, [e["id"] for e in found])
    return [{**e, **{k: v for k, v in told.get(e["id"], {}).items()}} for e in found]


def text(found: list[dict]) -> str:
    rows = [{**{k: e[k] for k in ORDER if e.get(k) not in (None, "", [])},
             **{k: v for k, v in e.items() if k not in ORDER and v not in (None, "", [])}} for e in sorted(found, key=lambda e: e["id"])]
    return HEADER + (tomli_w.dumps({"benchmark": rows}) if rows else "")


def write_list(index_dir: Path) -> None:
    """Make benchmarks.toml say what the records say: every listed benchmark, in one file."""
    path, found = index_dir / FILE, entries(index_dir)
    if not found and not path.exists():
        return
    found = with_status(index_dir, found)
    body = text(found)
    if not path.exists() or path.read_text() != body:
        path.write_text(body)
