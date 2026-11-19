"""The contributor list, and the checks every shard must pass before it merges."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

from ..core import identity as identity_mod
from .events import CONTRIBUTORS, EVENT_TYPES, TRIAL_PURPOSES, duplicate_key, event_id
from . import protocol
from .fold import INDEX_FILE, fold, index_entries
from .store import read_shard, shard_paths

def load_contributors(ledger_dir: Path) -> list[dict]:
    path = ledger_dir / CONTRIBUTORS
    if not path.exists():
        return []
    return tomllib.loads(path.read_text()).get("contributor", [])


def active(contributors: list[dict]) -> list[dict]:
    """The people who can contribute now: listed, with a GitHub handle and a signing key."""
    return [c for c in contributors if c.get("github") and c.get("key")]


def can_contribute(c: dict | None) -> bool:
    """Whether a listed person can contribute now: anyone with a GitHub handle and a signing key, to any benchmark."""
    return bool(c and c.get("github") and c.get("key"))


def status(c: dict) -> str:
    if can_contribute(c):
        return "owner" if c.get("role") == "owner" else "can contribute"
    missing = [w for w in ("github", "key") if not c.get(w)]
    return "listed, waiting for " + " and ".join("GitHub handle" if w == "github" else "signing key" for w in missing)


PEOPLE = "_contributors"      # the shard folder where the owner signs the contributor list


def list_digest(index_dir: Path) -> str:
    import hashlib
    path = index_dir / CONTRIBUTORS
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()[:16]


def sign_list(index_dir: Path, ident, protocol: str, note: str = "") -> dict:
    """The owner's signature on the contributor list as it is now. It needs no lock: the list changes no result."""
    from .store import MANUAL, Shard
    return Shard(index_dir, PEOPLE, ident, MANUAL, protocol).append("contributors", value=list_digest(index_dir), note=note)


def list_problem(index_dir: Path, events) -> str:
    """Why this copy of the contributor list cannot be trusted, or "". Once an owner has signed the list,
    a copy that differs from the last one they signed is refused."""
    owners = {c["github"] for c in load_contributors(index_dir) if c.get("role") == "owner"}
    signed = sorted((e for e in events if e["type"] == "contributors"), key=lambda e: e["time"])
    if not signed:
        return ""
    if signed[-1]["runner"] not in owners:
        return f"{CONTRIBUTORS} was last signed by @{signed[-1]['runner']}, who is not the owner on it"
    if signed[-1].get("value") != list_digest(index_dir):
        return f"{CONTRIBUTORS} is not the list its owner signed. The owner changes it with /contributors add, and nobody else does"
    return ""


def check_contributors(contributors: list[dict]) -> list[str]:
    """Two entries may share a name, never a handle or a key. An unfilled entry is not a problem."""
    problems = []
    for what in ("github", "key"):
        seen: dict[str, str] = {}
        for c in contributors:
            value = c.get(what)
            if not value:
                continue
            if value in seen:
                problems.append(f"contributors: {c.get('name')} and {seen[value]} share a {what}")
            else:
                seen[value] = c.get("name", "?")
    return problems


def owner_keys(contributors: list[dict]) -> list[str]:
    """The owner's handle and signing key, as lines. A workspace remembers them from the day it joined."""
    return sorted(f"{c['github']} {c['key']}" for c in contributors if c.get("role") == "owner" and c.get("github") and c.get("key"))


def _safe(path: Path) -> list[dict]:
    try:
        return read_shard(path)
    except json.JSONDecodeError:
        return []


def verify(ledger_dir: Path, max_per_benchmark: int | None = None,
           simulated: bool = False, index_dir: Path | None = None) -> list[str]:
    """Check every shard. Returns a list of problems, empty when the ledger is sound.

    A record of a simulated run is a problem wherever it is found, and supports nothing,
    unless `simulated` says this is the folder where simulated runs keep theirs. That folder has
    no contributor list of its own: `index_dir` names the index it belongs to.
    """
    contributors = load_contributors(index_dir or ledger_dir)
    problems = check_contributors(contributors)
    by_handle = {c["github"]: c for c in active(contributors)}
    taken: dict[tuple, str] = {}
    events: list[dict] = []
    for path in shard_paths(ledger_dir):
        where = f"{path.parent.name}/{path.name}"
        prev = ""
        try:
            shard = read_shard(path)
        except json.JSONDecodeError as err:
            problems.append(f"{where}: not valid JSON lines ({err})")
            continue
        for n, e in enumerate(shard, 1):
            at = f"{where}:{n}"
            missing = [k for k in ("id", "sig", "type", "time", "runner", "protocol", "prev") if k not in e]
            if missing:
                problems.append(f"{at}: missing {', '.join(missing)}")
                continue
            if e["type"] not in EVENT_TYPES:
                problems.append(f"{at}: unknown event type {e['type']}")
            if e["type"] == "trial" and e.get("agent_version") and e.get("agent_version_ran") \
                    and e["agent_version"] != e["agent_version_ran"]:
                problems.append(f"{at}: pinned to {e['agent']} {e['agent_version']} but {e['agent_version_ran']} ran")
            if e["type"] == "trial" and e.get("purpose") not in TRIAL_PURPOSES:
                problems.append(f"{at}: unknown trial purpose {e.get('purpose')}")
            if event_id(e) != e["id"]:
                problems.append(f"{at}: content does not match its id (edited after signing)")
            if bool(e.get("simulated")) != simulated:
                problems.append(f"{at}: a record of a simulated run, which never counts in an index" if e.get("simulated")
                                else f"{at}: a real record among the simulated ones")
            if e["prev"] != prev:
                problems.append(f"{at}: hash chain broken")
            prev = e["id"]
            who = by_handle.get(e["runner"])
            if who is None:
                problems.append(f"{at}: runner @{e['runner']} is not on the contributor list")
            elif not identity_mod.verify(who.get("key", ""), e["id"], e["sig"]):
                problems.append(f"{at}: signature does not match @{e['runner']}'s key")
            key = duplicate_key(e)
            if key is not None:
                if key in taken:
                    problems.append(f"{at}: already recorded at {taken[key]} ({e['type']})")
                else:
                    taken[key] = at
            if bool(e.get("simulated")) == simulated:       # a record in the wrong place supports nothing
                events.append(e)
    wrong = list_problem(index_dir or ledger_dir, events) if not simulated else ""
    if wrong:
        problems.append(wrong)
    found = protocol.locks(events)
    owners = {c["github"] for c in contributors if c.get("role") == "owner"}
    for lock in found:
        if lock["runner"] not in owners:
            problems.append(f"protocol {lock['protocol']} was locked by @{lock['runner']}, who is not the owner")
    if found:
        allowed, since = {lock["protocol"] for lock in found}, found[0]["time"]
        seen: set[str] = set()
        for e in events:
            if e["type"] in ("decision", "trial", "repair", "report", "verdict") and e["time"] >= since \
                    and e["protocol"] not in allowed and e["protocol"] not in seen:
                seen.add(e["protocol"])
                problems.append(f"records were made under protocol {e['protocol']}, which was never locked "
                                f"(first by @{e['runner']})")
    import hashlib
    for e in events:        # a report is what a reviewer signs on: the file must be the one that was recorded
        if e["type"] == "report" and e.get("report_digest") and e.get("location"):
            path = (index_dir or ledger_dir) / e["location"]
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()[:16] != e["report_digest"]:
                problems.append(f"{e.get('task_id', '?')}: its report was changed after it was recorded ({e['location']})")
    index_file = ledger_dir / INDEX_FILE
    if index_file.exists():
        try:
            listed = {t["task"]: t.get("digest") for t in tomllib.loads(index_file.read_text()).get("task", [])}
        except tomllib.TOMLDecodeError:
            listed = None
            problems.append(f"{INDEX_FILE} cannot be read")
        real = {t["task"]: t["digest"] for t in index_entries(events)}
        for task, digest in (listed or {}).items():
            if real.get(task) != digest:
                problems.append(f"{INDEX_FILE} lists {task}, which no accept verdict supports: it was edited by hand")
    states = fold(events)
    if max_per_benchmark is not None:
        counts: dict[str, int] = {}
        for s in states.values():
            if s.status == "accepted":
                counts[s.benchmark] = counts.get(s.benchmark, 0) + 1
        for bench, n in counts.items():
            if n > max_per_benchmark:
                problems.append(f"{bench}: {n} accepted tasks, more than the {max_per_benchmark} one benchmark may have")
    for tid, s in states.items():
        if s.status != "accepted":
            continue
        mine = [e for e in events if e.get("task_id") == tid and e.get("digest") == s.digest]
        has = lambda purpose, passed: any(  # noqa: E731
            e["type"] == "trial" and e.get("purpose") == purpose and bool(e.get("passed")) == passed for e in mine)
        if not has("oracle", True):
            problems.append(f"{tid}: accepted without a passing oracle run")
        if not has("noop", False):
            problems.append(f"{tid}: accepted without a failing no-op run")
        if any(e.get("assumed") for e in mine):
            problems.append(f"{tid}: accepted on assumed results that were never run")
        if not any(e["type"] == "decision" and e.get("stage") == "difficulty" and e.get("outcome") == "pass"
                   for e in mine):
            problems.append(f"{tid}: accepted without a difficulty result under the threshold")
    return problems
