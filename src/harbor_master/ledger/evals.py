"""The evaluation of a released index (DESIGN.md Section 9): its records, and the to-do list they add up to.

The records are in `eval/`, one file per person, so two people never write the same file. There are three:
the owner's `release`, which opens the evaluation; a `claim` on some cells for a few hours; and a `trial`,
which has the fields of a difficulty trial of stage 1. A cell is one accepted task, at its accepted version,
and one roster configuration. The to-do list is not stored: everyone computes the same one from the index,
the roster and the trials there are, among them the difficulty trials of stage 1.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..core import identity as identity_mod
from .events import canonical, event_id

EVAL = "eval"
TYPES = ("release", "claim", "trial")
AGENT_TRIALS = ("difficulty", "benchmark")      # the purposes that fill a cell's slots


def read(root: Path, simulated: bool = False) -> list[dict]:
    """Every evaluation record under `root`, oldest first. A simulated one is read only where simulated runs keep theirs."""
    events = [json.loads(line) for path in sorted((root / EVAL).glob("*.jsonl")) for line in path.read_text().splitlines() if line.strip()]
    return sorted((e for e in events if bool(e.get("simulated")) == simulated), key=lambda e: e["time"])


def append(root: Path, ident: identity_mod.Identity, type: str, simulated: bool = False, **data) -> dict:
    assert type in TYPES, type
    path = root / EVAL / f"{ident.github}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    event = {"v": 1, "type": type, "time": round(time.time(), 3), "runner": ident.github,
             "prev": json.loads(lines[-1])["id"] if lines else "", **data}
    if simulated:
        event["simulated"] = True
    event["id"] = event_id(event)
    event["sig"] = ident.sign(event["id"])
    with path.open("a") as f:
        f.write(canonical(event) + "\n")
    return event


def release(events: list[dict]) -> dict | None:
    """The owner's release, which opens the evaluation, or None while the index is still being built."""
    found = [e for e in events if e["type"] == "release"]
    return found[-1] if found else None


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def agent_trials(*streams: list[dict]) -> list[dict]:
    """The trials that fill slots: an agent's attempts at a task, from stage 1 and from stage 2, never an assumed one."""
    return [e for events in streams for e in events
            if e["type"] == "trial" and e.get("purpose") in AGENT_TRIALS and not e.get("assumed")]


@dataclass
class Cell:
    """One accepted task and one roster configuration: what a person claims, and runs `trials` times."""
    task: dict                          # its entry in the index: task, benchmark, digest
    config: object                      # the roster's entry
    trials: int
    retries: int
    done: dict[int, dict] = field(default_factory=dict)     # trial index -> the trial that filled it
    errors: int = 0                     # trials that failed for an infrastructure reason, which fill nothing
    holder: str = ""                    # who holds it now, when that is someone else, or this person on another machine
    mine: bool = False                  # held by this person on this machine

    @property
    def key(self) -> str:
        return f"{self.task['digest']}#{self.config.name}"

    @property
    def missing(self) -> list[int]:
        return [i for i in range(self.trials) if i not in self.done]

    @property
    def failed(self) -> bool:
        """Given up: it kept failing for an infrastructure reason. It leaves the to-do list, and shows in hm todo."""
        return bool(self.missing) and self.errors > self.retries

    @property
    def open(self) -> bool:
        return bool(self.missing) and not self.failed


def cells(index: list[dict], roster: list, settings, trials: list[dict], events: list[dict], now: float,
          me: str = "", machine: str = "") -> list[Cell]:
    """Every cell of the index, the least covered first and then in an order that is the same for everyone."""
    by_slot: dict[tuple, list[dict]] = {}
    for e in sorted(trials, key=lambda e: e["time"]):
        by_slot.setdefault((e["digest"], e.get("agent", ""), e.get("agent_version", ""), e.get("model", ""),
                            e.get("reasoning_effort", "")), []).append(e)
    held: dict[str, tuple[str, str]] = {}           # cell -> (who, machine) of the earliest claim that still holds
    for e in events:
        if e["type"] == "claim" and float(e.get("expires", 0)) > now:
            for key in e.get("cells", []):
                held.setdefault(key, (e["runner"], e.get("machine", "")))
    out = []
    for task in index:
        for config in roster:
            cell = Cell(task, config, settings.trials, settings.retries)
            for e in by_slot.get((task["digest"], config.agent, config.version, config.model, config.reasoning_effort), []):
                if e.get("error"):
                    cell.errors += 1
                elif e.get("trial_index", 0) < cell.trials:
                    cell.done.setdefault(e.get("trial_index", 0), e)
            who = held.get(cell.key)
            if who is not None and cell.open:
                cell.mine = who == (me, machine)
                cell.holder = "" if cell.mine else who[0] + (" (you, on another machine)" if who[0] == me else "")
            out.append(cell)
    return sorted(out, key=lambda c: (len(c.done), hashlib.sha256(c.key.encode()).hexdigest()))


def verify(root: Path, contributors: list[dict], simulated: bool = False) -> list[str]:
    """Check every evaluation record: its file is its signer's, the chain holds, the signature is theirs,
    only the owner released, and no slot was filled twice."""
    problems, people = [], {c["github"]: c for c in contributors if c.get("github") and c.get("key")}
    owners = {c["github"] for c in contributors if c.get("role") == "owner"}
    taken: dict[tuple, str] = {}
    for path in sorted((root / EVAL).glob("*.jsonl")):
        prev = ""
        for n, line in enumerate(path.read_text().splitlines(), 1):
            at = f"{EVAL}/{path.name}:{n}"
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"{at}: not valid JSON")
                break
            missing = [k for k in ("id", "sig", "type", "time", "runner", "prev") if k not in e]
            if missing or e["type"] not in TYPES:
                problems.append(f"{at}: " + (f"missing {', '.join(missing)}" if missing else f"unknown record type {e['type']}"))
                continue
            if e["runner"] != path.stem:
                problems.append(f"{at}: signed by @{e['runner']} in @{path.stem}'s file")
            if event_id(e) != e["id"]:
                problems.append(f"{at}: content does not match its id (edited after signing)")
            if e["prev"] != prev:
                problems.append(f"{at}: hash chain broken")
            prev = e["id"]
            if bool(e.get("simulated")) != simulated:
                problems.append(f"{at}: a record of a simulated run, which never counts in an index" if e.get("simulated")
                                else f"{at}: a real record among the simulated ones")
            who = people.get(e["runner"])
            if who is None:
                problems.append(f"{at}: runner @{e['runner']} is not on the contributor list")
            elif not identity_mod.verify(who.get("key", ""), e["id"], e["sig"]):
                problems.append(f"{at}: signature does not match @{e['runner']}'s key")
            if e["type"] == "release" and e["runner"] not in owners:
                problems.append(f"{at}: the index was released by @{e['runner']}, who is not the owner")
            if e["type"] == "trial" and not e.get("error"):
                slot = (e.get("digest"), e.get("agent"), e.get("agent_version"), e.get("model"), e.get("reasoning_effort"), e.get("trial_index"))
                if slot in taken:
                    problems.append(f"{at}: already recorded at {taken[slot]} (trial)")
                taken.setdefault(slot, at)
    return problems
