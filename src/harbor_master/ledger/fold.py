"""Folding events into state: where each task stands, who contributed, how agents scored."""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

@dataclass
class TaskState:
    task_id: str
    benchmark: str
    digest: str = ""
    status: str = "in_progress"  # in_progress | dropped | not_selected | report_ready | accepted | rejected | replaced | contested
    stage: str = ""
    reason: str = ""
    passes: int = 0
    trials: int = 0
    cost: float = 0.0
    verdicts: list[dict] = field(default_factory=list)


def fold(events: Iterable[dict]) -> dict[str, TaskState]:
    states: dict[str, TaskState] = {}
    for e in sorted(events, key=lambda e: e["time"]):
        tid = e.get("task_id")
        if not tid:
            # Two records describe a whole benchmark at once: what was submitted, and what was selected.
            if e["type"] == "submission":
                for task_id, info in e.get("tasks", {}).items():
                    states.setdefault(task_id, TaskState(task_id, e["benchmark"])).digest = info.get("digest", "")
            elif e["type"] == "decision" and e.get("gate") == "ranking":
                for name, _, picked in e.get("ranking", []):
                    st = states.setdefault(f"{e['benchmark']}/{name}", TaskState(f"{e['benchmark']}/{name}", e["benchmark"]))
                    if not picked and st.status == "in_progress":
                        st.status, st.stage = "not_selected", "select"
                    elif picked and st.status == "not_selected":
                        st.status = "in_progress"
            continue
        s = states.setdefault(tid, TaskState(tid, e["benchmark"]))
        s.cost += float(e.get("cost", 0) or 0)
        if e.get("digest"):
            s.digest = e["digest"]
        t = e["type"]
        if t == "decision":
            s.stage = e.get("stage", s.stage)
            outcome = e.get("outcome")
            if outcome in ("drop", "not_selected"):
                s.status = "dropped" if outcome == "drop" else "not_selected"
                s.reason = e.get("reason", "")
            elif outcome in ("pass", "fix", "selected") and s.status in ("dropped", "not_selected"):
                s.status, s.reason = "in_progress", ""
        elif t == "trial" and e.get("purpose") in ("difficulty", "benchmark"):
            s.trials += 1
            s.passes += int(bool(e.get("passed")))
        elif t == "repair":
            s.digest = e.get("new_digest", s.digest)
            s.passes = s.trials = 0  # results under the old digest no longer describe this task
        elif t == "report":
            s.status, s.stage = "report_ready", "report"
        elif t == "verdict":
            s.verdicts.append(e)
            latest: dict[str, dict] = {}                # a reviewer's later verdict replaces their earlier one
            for v in s.verdicts:
                if v.get("digest") == e.get("digest"):
                    latest[v["runner"]] = v
            values = {v["value"] for v in latest.values()}
            if "accept" in values and values & {"drop", "replaced"}:
                s.status = "contested"                  # two reviewers disagree: the lead decides
            elif values == {"accept"}:
                s.status = "accepted"
            elif "replaced" in values:
                s.status, s.reason = "replaced", e.get("reason", "")
            elif "drop" in values:
                s.status, s.reason = "rejected", e.get("reason_code", "")
            else:
                s.status = "in_progress"                # sent back
    return states


def submitted(events: Iterable[dict]) -> dict[str, dict]:
    """Every submitted task: task id -> its digest, from the per-benchmark submission records."""
    out: dict[str, dict] = {}
    for e in events:
        if e["type"] == "submission":
            out.update(e.get("tasks", {}))
    return out


def abandoned(events: Iterable[dict]) -> dict[str, dict]:
    """The benchmarks given up because no adapter could be made to pass: benchmark -> the record that says so.
    The latest record of the adapt stage decides, so an adapter that passes later reopens the benchmark."""
    last: dict[str, dict] = {}
    for e in sorted((e for e in events if e["type"] == "decision" and e.get("stage") == "adapt"), key=lambda e: e["time"]):
        last[e["benchmark"]] = e
    return {b: e for b, e in last.items() if e.get("outcome") == "drop"}


def credits(events: Iterable[dict]) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for e in events:
        c = out[e["runner"]]
        c["cost"] += float(e.get("cost", 0) or 0)
        c["tokens"] += int(e.get("tokens_in", 0) or 0) + int(e.get("tokens_out", 0) or 0)
        c["container_seconds"] += float(e.get("container_seconds", 0) or 0)
        if e["type"] == "submission":
            c["submissions"] += len(e.get("tasks", {})) - 1        # one record carries many tasks
        c[{"trial": "trials", "submission": "submissions", "repair": "repairs", "verdict": "verdicts",
           "brief": "briefs", "decision": "decisions"}.get(e["type"], "other")] += 1
    return out


def leaderboard(events: Iterable[dict], roster: list[dict] | None = None,
                suite: Iterable[tuple[str, str]] = ()) -> tuple[list[dict], int]:
    """How each agent configuration does on the accepted tasks. Returns the rows and the number of accepted tasks.

    Only accepted tasks count, at the version that was accepted. A trial that failed for an
    infrastructure reason, or whose results were assumed, is left out. A configuration's score
    is its pass rate on each task, averaged over the tasks it has run, so a task with more
    trials does not weigh more. `roster` adds a row for every configuration that should be run,
    even with nothing yet. `suite` marks the configurations the tasks were selected against.
    """
    events = list(events)
    index = {s.task_id: s.digest for s in fold(events).values() if s.status == "accepted"}
    rows: dict[tuple, dict] = {}

    def row(agent: str, version: str, model: str, effort: str, name: str = "") -> dict:
        return rows.setdefault((agent, version, model, effort), {
            "name": name, "agent": agent, "version": version, "model": model, "effort": effort, "trials": 0,
            "per_task": {}, "runners": set(), "selected_against": (agent, model) in set(suite)})

    for c in roster or []:
        row(c["agent"], c.get("version", ""), c["model"], c.get("reasoning_effort", ""), c.get("name", ""))
    for e in events:
        if e["type"] != "trial" or e.get("purpose") not in ("difficulty", "benchmark") or e.get("assumed") or e.get("error"):
            continue
        if index.get(e["task_id"]) != e["digest"]:
            continue
        r = row(e.get("agent", ""), e.get("agent_version", ""), e.get("model", ""), e.get("reasoning_effort", ""))
        r["trials"] += 1
        r["runners"].add(e["runner"])
        p = r["per_task"].setdefault(e["task_id"], [0, 0])
        p[0] += int(bool(e.get("passed")))
        p[1] += 1
    out = []
    for r in rows.values():
        done = r.pop("per_task")
        r["tasks"] = len(done)
        r["score"] = sum(p / n for p, n in done.values()) / len(done) if done else None
        r["complete"] = bool(index) and len(done) == len(index)
        out.append(r)
    out.sort(key=lambda r: (not r["complete"], -(r["score"] if r["score"] is not None else -1), r["name"] or r["model"]))
    return out, len(index)


def index_entries(events: Iterable[dict]) -> list[dict]:
    """The accepted tasks, with who accepted each and when. This is the index."""
    events = list(events)
    out = []
    for s in sorted(fold(events).values(), key=lambda s: s.task_id):
        if s.status != "accepted":
            continue
        verdicts = [v for v in s.verdicts if v["value"] == "accept" and v.get("digest") == s.digest]
        out.append({"task": s.task_id, "benchmark": s.benchmark,
                    "digest": s.digest, "accepted_by": sorted({v["runner"] for v in verdicts}),
                    "accepted_on": time.strftime("%Y-%m-%d", time.gmtime(max(v["time"] for v in verdicts)))})
    return out


INDEX_FILE = "index.toml"
SIMULATED_HEADER = "# SIMULATED. These tasks were accepted in simulated runs on this machine. None of this is in the index.\n"
INDEX_HEADER = """# The index: the accepted tasks.
#
# Nobody edits this file. It is written from the signed verdicts in the ledger, and
# `hm ledger verify` rejects a copy that lists anything those verdicts do not support.
# A task gets in by a contributor's accept verdict, and out by a replace or reject verdict.

"""


def index_text(events: Iterable[dict]) -> str:
    import tomli_w
    entries = index_entries(events)
    return INDEX_HEADER + (tomli_w.dumps({"task": entries}) if entries else "# No task has been accepted yet.\n")


def accepted(events: Iterable[dict], benchmark: str) -> list[TaskState]:
    """The tasks of one benchmark that are in the index now."""
    return [s for s in fold(e for e in events if e.get("benchmark") == benchmark).values() if s.status == "accepted"]
