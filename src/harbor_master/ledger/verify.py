"""The contributor list, and the checks every shard must pass before it merges."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

from ..core import identity as identity_mod
from .events import CONTRIBUTORS, EVENT_TYPES, TRIAL_PURPOSES, duplicate_key, event_id
from . import protocol
from .fold import INDEX_FILE, fold, index_entries, submitted
from .store import read_shard, shard_paths

def load_contributors(ledger_dir: Path) -> list[dict]:
    path = ledger_dir / CONTRIBUTORS
    if not path.exists():
        return []
    return tomllib.loads(path.read_text()).get("contributor", [])


def active(contributors: list[dict]) -> list[dict]:
    """The people who can contribute now: listed, with a GitHub handle and a signing key."""
    return [c for c in contributors if c.get("github") and c.get("key")]


def slug(field: str) -> str:
    return field.strip().lower().replace(" ", "-")


def may_curate(c: dict | None, field: str) -> bool:
    """Whether a contributor may curate or judge tasks of a field. The owner may, in every field.
    Anyone else needs the field, or "*", in their `fields`."""
    if c is None or not (c.get("github") and c.get("key")):
        return False
    if c.get("role") == "owner":
        return True
    mine = {slug(f) for f in c.get("fields", [])}
    return "*" in mine or (bool(field) and slug(field) in mine)


def fields_of(contributors: list[dict], github: str) -> set[str] | None:
    """The fields a person may curate. None means every field. An empty set means none."""
    c = next((c for c in active(contributors) if c["github"] == github), None)
    if c is None:
        return set()
    if c.get("role") == "owner" or "*" in c.get("fields", []):
        return None
    return {slug(f) for f in c.get("fields", [])}


def status(c: dict) -> str:
    if c.get("github") and c.get("key"):
        if c.get("role") == "owner" or "*" in c.get("fields", []):
            return "can curate every field"
        return "can curate: " + ", ".join(c["fields"]) if c.get("fields") else "listed, but no fields set: cannot curate"
    missing = [w for w in ("github", "key") if not c.get(w)]
    return "listed, waiting for " + " and ".join("GitHub handle" if w == "github" else "signing key" for w in missing)


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


def verify(ledger_dir: Path, fields: dict[str, str] | None = None, max_per_benchmark: int | None = None,
           simulated: bool = False, index_dir: Path | None = None) -> list[str]:
    """Check every shard. Returns a list of problems, empty when the ledger is sound.

    `fields` maps a benchmark to its field, for the per-field permission on verdicts.
    A record of a simulated run is a problem wherever it is found, and supports nothing,
    unless `simulated` says this is the folder where simulated runs keep theirs. That folder has
    no contributor list of its own: `index_dir` names the index it belongs to.
    """
    contributors = load_contributors(index_dir or ledger_dir)
    problems = check_contributors(contributors)
    by_handle = {c["github"]: c for c in active(contributors)}
    taken: dict[tuple, str] = {}
    events: list[dict] = []
    domains = {task: info.get("field", "") for task, info in
               submitted(e for p in shard_paths(ledger_dir) for e in _safe(p)).items()}
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
            elif e["type"] == "benchmark" and e.get("value") == "add" and not any(
                    may_curate(who, f) for f in (e.get("entry") or {}).get("fields", [])):
                problems.append(f"{at}: @{e['runner']} may not add a benchmark in {', '.join((e.get('entry') or {}).get('fields', [])) or 'no field'}")
            elif e["type"] == "verdict":
                domain = domains.get(e.get("task_id", ""), "")
                if domain and not may_curate(who, domain):
                    problems.append(f"{at}: @{e['runner']} may not judge {domain} tasks")
            key = duplicate_key(e)
            if key is not None:
                if key in taken:
                    problems.append(f"{at}: already recorded at {taken[key]} ({e['type']})")
                else:
                    taken[key] = at
            if bool(e.get("simulated")) == simulated:       # a record in the wrong place supports nothing
                events.append(e)
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
