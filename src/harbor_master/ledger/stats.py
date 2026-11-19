"""Statistics of a curation, computed from the ledger: the funnel, what the gates did, what the model
tiers were called for and what it cost, the agent trials, what people decided, and what was skipped.

Everything here is a pure function of the events, so the tables can be made again at any time and
by anyone who has the ledger. `tables` returns them as rows, `markdown` and `write` lay them out.
"""
from __future__ import annotations

import csv
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .fold import abandoned, fold, index_entries

Table = tuple[list[str], list[list]]
STAGE_ORDER = ["session", "intake", "adapt", "triage", "rank", "verify", "probe", "difficulty", "audit", "fix", "report"]
TIER_ORDER = ["decision", "worker", "frontier", "chat"]
DROPS = ["verify", "probe", "difficulty", "audit", "fix"]


def _day(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def _money(x: float) -> float:
    return round(x, 4)


def _order(value: str, order: list[str]) -> tuple[int, str]:
    return (order.index(value) if value in order else len(order), value)


def funnel(events: list[dict]) -> Table:
    """Per benchmark: how many tasks were submitted, and where each of them stands or stopped."""
    states = fold(events).values()
    submitted: dict[str, int] = defaultdict(int)
    for e in events:
        if e["type"] == "submission":
            submitted[e["benchmark"]] += len(e.get("tasks", {}))
    skipped = {e["benchmark"]: e.get("value", "") for e in sorted(events, key=lambda e: e["time"]) if e["type"] == "skip"}
    gone = abandoned(events)
    picked: dict[str, int] = {}             # the latest selection of each benchmark
    for e in sorted(events, key=lambda e: e["time"]):
        if e["type"] == "decision" and e.get("gate") == "ranking":
            picked[e["benchmark"]] = sum(1 for _, _, chosen in e.get("ranking", []) if chosen)
    names = sorted({*submitted, *(s.benchmark for s in states), *skipped})
    head = ["benchmark", "submitted", "failed a gate", "not selected", "selected", *(f"dropped at {d}" for d in DROPS),
            "in progress", "report ready", "accepted", "rejected", "replaced", "contested", "last skip"]
    rows = []
    for b in names:
        mine = [s for s in states if s.benchmark == b]
        n = lambda status, stage=None: sum(1 for s in mine if s.status == status and (stage is None or s.stage == stage))  # noqa: E731
        failed, out = n("dropped", "triage"), n("not_selected")
        total = submitted.get(b) or len(mine)
        rows.append([b, total, failed, out, picked.get(b, 0), *(n("dropped", d) for d in DROPS),
                     n("in_progress"), n("report_ready"), n("accepted"), n("rejected"), n("replaced"), n("contested"),
                     "abandoned" if b in gone else skipped.get(b, "")])
    return head, rows


def gates(events: list[dict]) -> Table:
    """Per benchmark and admission gate: how many tasks were asked, dropped there, or sent up a tier."""
    count: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for e in events:
        if e["type"] != "decision" or e.get("stage") != "triage":
            continue
        if e.get("gate") == "gates":
            for gate in (e.get("value") or {}):
                count[(e["benchmark"], gate)][0] += 1
            if e.get("outcome") == "drop" and e.get("reason"):
                count[(e["benchmark"], e["reason"].split(":")[0])][1] += 1
        elif e.get("outcome") == "escalate":
            count[(e["benchmark"], e.get("gate", ""))][2] += 1
    return (["benchmark", "gate", "tasks asked", "dropped here", "sent up a tier"],
            [[b, g, *v] for (b, g), v in sorted(count.items())])


def tasks(events: list[dict]) -> Table:
    """One row per task: where it stands, why, and what it has cost."""
    rows = [[s.benchmark, s.task_id.split("/", 1)[-1], s.status, s.stage, s.reason, s.trials, s.passes, _money(s.cost), s.digest[:12]]
            for s in sorted(fold(events).values(), key=lambda s: s.task_id)]
    return ["benchmark", "task", "status", "stage reached", "reason", "agent trials", "passed", "cost", "digest"], rows


def usage(events: list[dict]) -> Table:
    """What the model tiers were called for: per benchmark, stage and tier."""
    total: dict[tuple[str, str, str, str], list] = defaultdict(lambda: [0, 0, 0.0])
    for e in events:
        if e["type"] == "usage":
            for r in e.get("rows", []):
                slot = total[(e["benchmark"] or "(session)", r["stage"], r["tier"], r.get("model", ""))]
                slot[0], slot[1], slot[2] = slot[0] + r["calls"], slot[1] + r.get("tokens", 0), slot[2] + r.get("spent", 0.0)
    rows = [[b, stage, tier, model, calls, tokens, _money(cost)] for (b, stage, tier, model), (calls, tokens, cost)
            in sorted(total.items(), key=lambda kv: (kv[0][0], _order(kv[0][1], STAGE_ORDER), _order(kv[0][2], TIER_ORDER)))]
    return ["benchmark", "stage", "tier", "model", "calls", "tokens", "cost"], rows


def usage_by_stage(events: list[dict]) -> Table:
    """The same, over all benchmarks: one row per stage and tier."""
    total: dict[tuple[str, str], list] = defaultdict(lambda: [0, 0, 0.0])
    for _, stage, tier, _, calls, tokens, cost in usage(events)[1]:
        slot = total[(stage, tier)]
        slot[0], slot[1], slot[2] = slot[0] + calls, slot[1] + tokens, slot[2] + cost
    rows = [[stage, tier, calls, tokens, _money(cost)] for (stage, tier), (calls, tokens, cost)
            in sorted(total.items(), key=lambda kv: (_order(kv[0][0], STAGE_ORDER), _order(kv[0][1], TIER_ORDER)))]
    return ["stage", "tier", "calls", "tokens", "cost"], rows


def trials(events: list[dict]) -> Table:
    """Runs through Harbor, by what they were for and which agent ran: how many, how they went, what they cost."""
    total: dict[tuple[str, str, str], list] = defaultdict(lambda: [0, 0, 0, 0, 0, 0.0, 0.0])
    for e in events:
        if e["type"] != "trial":
            continue
        slot = total[(e.get("purpose", ""), e.get("agent", ""), e.get("model", ""))]
        slot[0] += 1
        slot[1] += int(bool(e.get("passed")))
        slot[2] += int(bool(e.get("error")))
        slot[3] += int(e.get("tokens_in", 0) or 0)
        slot[4] += int(e.get("tokens_out", 0) or 0)
        slot[5] += float(e.get("cost", 0) or 0)
        slot[6] += float(e.get("container_seconds", 0) or 0)
    rows = [[purpose, agent, model, n, passed, errors, tin, tout, _money(cost), round(seconds / 3600, 2)]
            for (purpose, agent, model), (n, passed, errors, tin, tout, cost, seconds) in sorted(total.items())]
    return ["purpose", "agent", "model", "trials", "passed", "infrastructure errors", "tokens in", "tokens out", "cost", "container hours"], rows


def human(events: list[dict]) -> Table:
    """Every decision a person made, as the ledger has it: briefs, approvals, verdicts, adapters, locks."""
    rows = []
    for e in sorted(events, key=lambda e: e["time"]):
        kind, detail = "", ""
        if e["type"] == "brief":
            kind, detail = "brief", e.get("text", "")
        elif e["type"] == "verdict":
            kind, detail = f"verdict: {e.get('value', '')}", e.get("reason", "")
        elif e["type"] == "protocol":
            kind, detail = "protocol lock", e.get("note", "")
        elif e["type"] == "decision" and e.get("tier") == "human":
            if e.get("gate") == "signoff":
                kind, detail = "selection approved", f"{e.get('selected', '')} tasks"
            elif e.get("stage") == "adapt":
                kind = "adapter abandoned" if e.get("outcome") == "drop" else "adapter brought or fixed by hand"
                detail = e.get("reason", "") or e.get("value", "")
            else:
                kind, detail = f"decision: {e.get('stage', '')}", str(e.get("value", ""))
        if kind:
            rows.append([_day(e["time"]), e["runner"], kind, e["benchmark"] if e["benchmark"] != "_protocol" else "",
                         e.get("task_id", "").split("/", 1)[-1], " ".join(str(detail).split())[:200]])
    return ["when", "who", "what", "benchmark", "task", "detail"], rows


def human_counts(events: list[dict]) -> Table:
    count: dict[str, int] = defaultdict(int)
    for row in human(events)[1]:
        count[row[2]] += 1
    return ["decision by a person", "times"], [[k, v] for k, v in sorted(count.items())]


def skips(events: list[dict]) -> Table:
    """Every time a run left a benchmark out, and why."""
    rows = [[_day(e["time"]), e["benchmark"], e["runner"], e.get("value", ""), " ".join(e.get("reason", "").split())[:240]]
            for e in sorted(events, key=lambda e: e["time"]) if e["type"] == "skip"]
    return ["when", "benchmark", "who", "why, in a word", "why"], rows


def adapters(events: list[dict]) -> Table:
    """Every adapter that was built or given up: who wrote the last version, in how many rounds, at what cost."""
    rows = [[_day(e["time"]), e["benchmark"], e.get("outcome", ""), e.get("tier", ""), e.get("model", ""), e.get("rounds", 0),
             e.get("value", ""), _money(float(e.get("cost", 0) or 0))]
            for e in sorted(events, key=lambda e: e["time"]) if e["type"] == "decision" and e.get("stage") == "adapt"]
    return ["when", "benchmark", "outcome", "written by", "model", "model rounds", "result", "cost"], rows


def summary(events: list[dict]) -> Table:
    states = fold(events).values()
    by_tier: dict[str, list] = defaultdict(lambda: [0, 0, 0.0])
    for _, tier, calls, tokens, cost in usage_by_stage(events)[1]:
        slot = by_tier[tier]
        slot[0], slot[1], slot[2] = slot[0] + calls, slot[1] + tokens, slot[2] + cost
    trial_rows = trials(events)[1]
    model_cost, trial_cost = sum(v[2] for v in by_tier.values()), sum(r[8] for r in trial_rows)
    accepted = len(index_entries(events))
    people = human(events)[1]
    rows: list[list] = [
        ["benchmarks with records", len({e["benchmark"] for e in events if e["benchmark"] and not e["benchmark"].startswith("_")})],
        ["tasks submitted", len(states)],
        ["tasks that failed a gate", sum(1 for s in states if s.status == "dropped" and s.stage == "triage")],
        ["tasks not selected", sum(1 for s in states if s.status == "not_selected")],
        ["tasks dropped after selection", sum(1 for s in states if s.status == "dropped" and s.stage != "triage")],
        ["reports waiting for a verdict", sum(1 for s in states if s.status == "report_ready")],
        ["tasks accepted", accepted],
        ["tasks rejected or replaced", sum(1 for s in states if s.status in ("rejected", "replaced"))],
        *([f"{tier} tier calls", by_tier[tier][0]] for tier in sorted(by_tier, key=lambda t: _order(t, TIER_ORDER))),
        *([f"{tier} tier tokens", by_tier[tier][1]] for tier in sorted(by_tier, key=lambda t: _order(t, TIER_ORDER))),
        *([f"{tier} tier cost", _money(by_tier[tier][2])] for tier in sorted(by_tier, key=lambda t: _order(t, TIER_ORDER))),
        ["model tiers, total cost", _money(model_cost)],
        ["runs through Harbor", sum(r[3] for r in trial_rows)],
        ["runs through Harbor, cost", _money(trial_cost)],
        ["total cost", _money(model_cost + trial_cost)],
        ["cost per accepted task", _money((model_cost + trial_cost) / accepted) if accepted else ""],
        ["decisions by a person", len(people)],
        ["people who made them", len({r[1] for r in people})],
        ["times a benchmark was skipped", len(skips(events)[1])],
        ["benchmarks abandoned", len(abandoned(events))],
        ["records", len(events)],
    ]
    return ["figure", "value"], rows


TABLES = {     # file name -> (title, what it says, the function)
    "summary": ("Summary", "The whole curation in one column.", summary),
    "funnel": ("Where the tasks went", "Per benchmark: tasks submitted, and where each stands or stopped.", funnel),
    "gates": ("Admission gates", "Per benchmark and gate: tasks asked, dropped there, and sent up a tier.", gates),
    "usage_by_stage": ("Model calls and cost, by stage and tier", "Over all benchmarks. A call is one request to a model.", usage_by_stage),
    "usage": ("Model calls and cost, by benchmark", "The same, per benchmark, with the model that was called.", usage),
    "trials": ("Runs through Harbor", "The reference solution, the no-op and the agents: how many runs, how they went, what they cost. Runs of the probe's mutants are not recorded one by one: their outcome is in the probe's record.", trials),
    "adapters": ("Adapters", "Each adapter that was built or given up.", adapters),
    "human_counts": ("Decisions by a person, counted", "How many times a person had to decide something, by kind.", human_counts),
    "human": ("Decisions by a person", "Each one, in order.", human),
    "skips": ("Skipped benchmarks", "Each time a run left a benchmark out, and why.", skips),
    "tasks": ("Every task", "One row per task. In the CSV only: the table is too long to read here.", tasks),
}
IN_CSV_ONLY = {"tasks"}


def tables(events: Iterable[dict]) -> dict[str, Table]:
    events = list(events)
    return {name: fn(events) for name, (_, _, fn) in TABLES.items()}


def _cell(v: object) -> str:
    text = f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else f"{v:,.4f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)
    return text.replace("|", "\\|").replace("\n", " ")


def markdown(found: dict[str, Table], title: str, notes: list[str], source: list[list] | None = None) -> str:
    out = [f"# {title}", "", *notes, ""]
    if source:
        out += ["## Source", "", "What these tables were made from, so that they can be made again. Data: `source.csv`.", "",
                "| | |", "|---|---|", *(f"| {_cell(k)} | {_cell(v)} |" for k, v in source), ""]
    for name, (heading, what, _) in TABLES.items():
        head, rows = found[name]
        out += [f"## {heading}", "", what + f" Data: `{name}.csv`.", ""]
        if name in IN_CSV_ONLY:
            out += [f"{len(rows)} rows.", ""]
        elif not rows:
            out += ["Nothing recorded.", ""]
        else:
            out += ["| " + " | ".join(head) + " |", "|" + "|".join("---:" if all(isinstance(r[i], (int, float)) for r in rows) else "---"
                                                                 for i in range(len(head))) + "|"]
            out += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
            out.append("")
    return "\n".join(out)


def write(found: dict[str, Table], folder: Path, title: str, notes: list[str], source: list[list] | None = None) -> Path:
    """Write stats.md and one CSV per table into `folder`. `source` says what they were made from. Returns the Markdown file."""
    folder.mkdir(parents=True, exist_ok=True)
    if source:
        found = {"source": (["what", "value"], source), **found}
    for name, (head, rows) in found.items():
        with (folder / f"{name}.csv").open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([h.replace(" ", "_").replace(",", "") for h in head])
            writer.writerows(rows)
    path = folder / "stats.md"
    path.write_text(markdown(found, title, notes, source))
    return path
