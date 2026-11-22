"""Writing shards and reading the whole ledger.

The ledger is the `ledger/` folder of an index repository. Each run appends to its own
shard, `ledger/<benchmark>/<runner>-<run id>.jsonl`, so two runners never write the same file.
Everywhere here, `ledger_dir` is the index repository's folder: the one that holds `ledger/`,
the contributor list and index.toml.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..core.identity import Identity
from .events import EVENT_TYPES, TIER_RANK, canonical, duplicate_key, event_id, slug

SHARDS = "ledger"            # the folder of an index repository that holds the shards
REPORTS = "reports"          # and the one that holds the reports that reviewers sign
KEEP = {"value", "outcome", "tasks", "expires", "settings", "version"}      # written even when empty or zero
MANUAL = "manual"            # the shard for records a person makes by hand: briefs, verdicts, locks


class Shard:
    """One runner's file for one benchmark in one run."""

    def __init__(self, ledger_dir: Path, benchmark: str, ident: Identity, run_id: str, protocol: str):
        self.benchmark, self.ident, self.protocol = benchmark, ident, protocol
        self.path = ledger_dir / SHARDS / slug(benchmark) / f"{ident.github}-{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.prev = ""
        if self.path.exists():
            lines = self.path.read_text().splitlines()
            if lines:
                self.prev = json.loads(lines[-1])["id"]

    def append(self, type: str, task_id: str = "", digest: str = "", **data: Any) -> dict:
        assert type in EVENT_TYPES, type
        if type != "trial":         # a trial keeps every field, so that all trials have one shape
            data = {k: v for k, v in data.items() if k in KEEP or v not in ("", None, [], {}, 0, 0.0, False)}
        event = {"v": 1, "type": type, "time": round(time.time(), 3), "runner": self.ident.github,
                 "protocol": self.protocol, "benchmark": self.benchmark, "prev": self.prev, **data}
        if task_id:
            event["task_id"] = task_id
        if digest:
            event["digest"] = digest
        event["id"] = event_id(event)
        event["sig"] = self.ident.sign(event["id"])
        with self.path.open("a") as f:
            f.write(canonical(event) + "\n")
        self.prev = event["id"]
        return event


def read_shard(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def shard_paths(ledger_dir: Path) -> list[Path]:
    return sorted((ledger_dir / SHARDS).glob("*/*.jsonl"))


def digest(ledger_dir: Path) -> str:
    """One checksum over every shard, by path and content. Two copies of a ledger with the same checksum hold the same records."""
    import hashlib
    h = hashlib.sha256()
    for path in shard_paths(ledger_dir):
        h.update(str(path.relative_to(ledger_dir)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


class Ledger:
    """All events, indexed so that a run can ask "is this done already?" before spending.

    `accept_assumed` says whether assumed records count as done. A real run leaves it off,
    so that nothing made up is ever reused as if it had been run.

    `simulated` says whether records of simulated runs are read at all. They are kept in a
    folder of their own, and only a reader that asks for them sees them: a simulated record
    that turns up in an index's ledger is left out here, and `verify` reports it.
    """

    def __init__(self, ledger_dir: Path, accept_assumed: bool = False, simulated: bool = False):
        self.dir, self.accept_assumed, self.simulated = ledger_dir, accept_assumed, simulated
        self.reload()

    def reload(self) -> None:
        """Read every shard again, after a pull brought in other people's records."""
        self.events: list[dict] = []
        self.by_task: dict[str, list[dict]] = defaultdict(list)
        self.taken: dict[tuple, dict] = {}
        self._decisions: dict[tuple, list[dict]] = defaultdict(list)
        self._seen: set[str] = set()
        for path in shard_paths(self.dir):
            for e in read_shard(path):
                self.add(e)

    def add(self, e: dict) -> None:
        if e["id"] in self._seen or (e.get("simulated") and not self.simulated):
            return
        self._seen.add(e["id"])
        self.events.append(e)
        if e.get("task_id"):
            self.by_task[e["task_id"]].append(e)
        key = duplicate_key(e)
        if key is not None:
            self.taken.setdefault(key, e)
        if e["type"] == "decision":
            self._decisions[(e.get("digest") or e.get("benchmark", ""), e.get("stage", ""), e.get("gate", ""), e["protocol"])].append(e)

    def _kinds(self, kind: str) -> tuple[str, ...]:
        return (kind, kind + ":assumed") if self.accept_assumed else (kind,)

    def trial(self, digest: str, agent: str, agent_version: str, model: str, index: int, effort: str = "") -> dict | None:
        for kind in self._kinds("trial"):
            found = self.taken.get((kind, digest, agent, agent_version, model, effort, index))
            if found is not None:
                return found
        return None

    def decision(self, digest: str, stage: str, gate: str, protocol: str) -> dict | None:
        """The standing decision for a gate: the highest tier wins, then one made again over the first, then the latest."""
        found = [e for e in self._decisions.get((digest, stage, gate, protocol), ())
                 if self.accept_assumed or not e.get("assumed")]
        if not found:
            return None
        return max(found, key=lambda e: (TIER_RANK.get(e.get("tier", ""), 0), int(e.get("redo") or 0), e["time"]))

    def brief(self, benchmark: str) -> dict | None:
        briefs = [e for e in self.events if e["type"] == "brief" and e["benchmark"] == benchmark]
        return max(briefs, key=lambda e: e["time"]) if briefs else None

    def claims(self, benchmark: str, now: float, me: str = "", machine: str = "") -> dict[str, tuple[str, float]]:
        """Tasks that are being worked on elsewhere now: task id -> (who, when the claim expires).

        A claim belongs to a person on a machine. A task is held while a claim on it is neither released nor
        expired, unless it is this person's on this machine. So two people cannot run the same task at once,
        and neither can one person from two machines: the second is told the first holds it.
        """
        held: dict[str, dict[tuple[str, str], float]] = defaultdict(dict)       # task -> (runner, machine) -> expiry
        for e in sorted((e for e in self.events if e["type"] == "claim" and e["benchmark"] == benchmark),
                        key=lambda e: e["time"]):
            who = (e["runner"], e.get("machine", ""))
            for tid in e.get("tasks", []):
                if e.get("released"):
                    held[tid].pop(who, None)
                else:
                    held[tid][who] = float(e.get("expires", 0))
        out: dict[str, tuple[str, float]] = {}
        for tid, holders in held.items():
            live = {w: x for w, x in holders.items() if w != (me, machine) and x > now}
            if live:
                runner, _ = who = min(live)
                out[tid] = (f"{runner} (you, on another machine)" if runner == me else runner, live[who])
        return out

    def benchmarks(self) -> list[str]:
        return sorted({e["benchmark"] for e in self.events if e["benchmark"]})


def write_index(ledger_dir: Path, simulated: bool = False) -> Path:
    """Write index.toml, the list of accepted tasks, from the verdicts in the ledger."""
    from .fold import INDEX_FILE, SIMULATED_HEADER, index_text
    path = ledger_dir / INDEX_FILE
    text = (SIMULATED_HEADER if simulated else "") + index_text(Ledger(ledger_dir, simulated=simulated).events)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
    return path
