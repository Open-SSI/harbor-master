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


def drops(events: list[dict]) -> Table:
    """Per benchmark: at which stage tasks stopped, and why."""
    count: dict[tuple[str, str, str], int] = defaultdict(int)
    for s in fold(events).values():
        if s.status == "not_selected":
            count[(s.benchmark, "select", "ranked under the cap")] += 1
        elif s.status == "dropped":
            count[(s.benchmark, s.stage or "?", s.reason.replace("_", " ").replace(":", ": ") or "no reason recorded")] += 1
        elif s.status in ("rejected", "replaced"):
            count[(s.benchmark, "verdict", f"{s.status} by a reviewer")] += 1
    order = ["triage", "select", *DROPS, "verdict"]
    rows = [[b, stage, reason, n] for (b, stage, reason), n in sorted(count.items(), key=lambda kv: (kv[0][0], _order(kv[0][1], order), -kv[1]))]
    return ["benchmark", "stage", "reason", "tasks"], rows


TABLES = {     # file name -> what it holds, and the function that makes it
    "summary": ("The whole curation as figures and values.", summary),
    "benchmarks": ("Per benchmark: tasks submitted, and where each stands or stopped.", funnel),
    "drops": ("Per benchmark: at which stage tasks stopped, and why.", drops),
    "gates": ("Per benchmark and admission gate: tasks asked, dropped there, and sent up a tier.", gates),
    "models": ("Model calls, tokens and cost per stage and tier, over all benchmarks.", usage_by_stage),
    "models_by_benchmark": ("The same per benchmark, with the model that was called.", usage),
    "harbor_runs": ("Runs through Harbor by purpose, agent and model.", trials),
    "decisions": ("Every decision a person made that left a record, in order.", human),
    "decision_counts": ("Those decisions counted by kind.", human_counts),
    "skips": ("Every time a run left a benchmark out, and why.", skips),
    "adapters": ("Every adapter that was built or given up.", adapters),
    "tasks": ("One row per task: status, stage reached, reason, trials, cost.", tasks),
}
DATA = "tables"         # the folder, beside stats.md, that holds one CSV per table


def tables(events: Iterable[dict]) -> dict[str, Table]:
    events = list(events)
    return {name: fn(events) for name, (_, fn) in TABLES.items()}


# ----- the report -----

def _n(v: object) -> str:
    return f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else str(v)


def _usd(x: float) -> str:
    return "$0" if not x else "<$0.01" if x < 0.01 else f"${x:,.2f}"


def _share(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else ""


def _table(head: list[str], rows: list[list], right: set[int] | None = None) -> list[str]:
    """A Markdown table. Columns in `right` are numbers, set against the right edge."""
    right = right if right is not None else {i for i in range(1, len(head))}
    clean = lambda v: str(v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    return ["| " + " | ".join(head) + " |", "|" + "|".join("---:" if i in right else "---" for i in range(len(head))) + "|",
            *("| " + " | ".join(clean(v) for v in row) + " |" for row in rows), ""]


def markdown(found: dict[str, Table], title: str, notes: list[str], source: list[list] | None = None) -> str:
    """The report: what happened to the tasks, what it cost, what people had to do, and where the figures come from.
    It shows totals. Every table in full, and the ones too long to read, are CSV files in the folder beside it."""
    figure = dict(found["summary"][1])
    data = lambda name: f"`{DATA}/{name}.csv`"  # noqa: E731
    out = [f"# {title}", ""]
    out += [f"> {note}" for note in notes[:1] if note.startswith("**SIMULATED")] + [""]
    made = dict(source or [])
    out += [f"{_n(figure['records'])} records of {made.get('people with records', '?')} "
            f"{'person' if str(made.get('people with records')) == '1' else 'people'}, made on {made.get('made', '')}. "
            f"Every table below is also a CSV file in `{DATA}/`, in full.", ""]

    # 1. the tasks
    total = figure["tasks submitted"]
    selected = sum(r[4] for r in found["benchmarks"][1])
    waiting_on_run = sum(r[10] for r in found["benchmarks"][1])
    out += ["## 1. What happened to the tasks", ""]
    out += _table(["", "tasks", "of all"], [
        ["submitted", _n(total), "100%" if total else ""],
        ["failed an admission gate", _n(figure["tasks that failed a gate"]), _share(figure["tasks that failed a gate"], total)],
        ["passed the gates, ranked under the cap", _n(figure["tasks not selected"]), _share(figure["tasks not selected"], total)],
        ["selected for the costly stages", _n(selected), _share(selected, total)],
        ["— dropped after selection", _n(figure["tasks dropped after selection"]), _share(figure["tasks dropped after selection"], total)],
        ["— report waiting for a verdict", _n(figure["reports waiting for a verdict"]), _share(figure["reports waiting for a verdict"], total)],
        ["— **accepted**", f"**{_n(figure['tasks accepted'])}**", _share(figure["tasks accepted"], total)],
        ["— rejected or replaced by a reviewer", _n(figure["tasks rejected or replaced"]), _share(figure["tasks rejected or replaced"], total)],
        ["passed the gates, not ranked or not finished yet", _n(waiting_on_run), _share(waiting_on_run, total)],
    ])
    out += ["**By benchmark**", ""]
    out += _table(["benchmark", "submitted", "passed the gates", "selected", "dropped later", "waiting for a verdict", "accepted", "rejected", "note"],
                  [[r[0], _n(r[1]), _n(r[1] - r[2]), _n(r[4]), _n(sum(r[5:10])), _n(r[11]), _n(r[12]), _n(r[13] + r[14]),
                    "abandoned: no adapter passes" if r[16] == "abandoned" else f"skipped: {r[16].replace('_', ' ')}" if r[16] else ""]
                   for r in found["benchmarks"][1]], right={1, 2, 3, 4, 5, 6, 7})
    if found["drops"][1]:
        why: dict[tuple[str, str], int] = defaultdict(int)
        for _, stage, reason, n in found["drops"][1]:
            why[(stage, reason)] += n
        order = ["triage", "select", *DROPS, "verdict"]
        out += ["**Why tasks stopped**", ""]
        out += _table(["stage", "reason", "tasks"], [[stage, reason, _n(n)] for (stage, reason), n in
                                                    sorted(why.items(), key=lambda kv: (_order(kv[0][0], order), -kv[1]))], right={2})
    if found["gates"][1]:
        gate: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        for _, name, asked, dropped, up in found["gates"][1]:
            gate[name] = [gate[name][0] + asked, gate[name][1] + dropped, gate[name][2] + up]
        out += ["**Admission gates**", ""]
        out += _table(["gate", "tasks asked", "dropped here", "sent up a tier"], [[g, *map(_n, v)] for g, v in sorted(gate.items())])
    out += [f"Per benchmark: {data('benchmarks')}, {data('drops')}, {data('gates')}. Per task: {data('tasks')}.", ""]

    # 2. the cost
    out += ["## 2. What it cost", ""]
    tiers = [t for t in TIER_ORDER if f"{t} tier calls" in figure]
    runs = found["harbor_runs"][1]
    cost_rows = [[f"{t} tier", _n(figure[f"{t} tier calls"]) + " calls", _n(figure[f"{t} tier tokens"]), _usd(figure[f"{t} tier cost"])] for t in tiers]
    cost_rows.append(["runs through Harbor", _n(figure["runs through Harbor"]) + " runs", _n(sum(r[6] + r[7] for r in runs)), _usd(figure["runs through Harbor, cost"])])
    cost_rows.append(["**total**", "", "", f"**{_usd(figure['total cost'])}**"])
    if figure["cost per accepted task"] != "":
        cost_rows.append(["per accepted task", "", "", _usd(figure["cost per accepted task"])])
    out += _table(["", "how many", "tokens", "cost"], cost_rows)
    if found["models"][1]:
        stage: dict[str, dict] = {}
        for name, tier, calls, tokens, cost in found["models"][1]:
            row = stage.setdefault(name, {"tokens": 0, "cost": 0.0})
            row[tier], row["tokens"], row["cost"] = row.get(tier, 0) + calls, row["tokens"] + tokens, row["cost"] + cost
        cols = [t for t in TIER_ORDER if any(t in row for row in stage.values())]
        out += ["**Model calls by stage**", ""]
        out += _table(["stage", *(f"{t} calls" for t in cols), "tokens", "cost"],
                      [[name, *(_n(row.get(t, 0)) if row.get(t) else "" for t in cols), _n(row["tokens"]), _usd(row["cost"])]
                       for name, row in sorted(stage.items(), key=lambda kv: _order(kv[0], STAGE_ORDER))])
    if runs:
        out += ["**Runs through Harbor**", ""]
        out += _table(["what for", "agent", "model", "runs", "passed", "errors", "tokens", "cost", "container hours"],
                      [[r[0], r[1], r[2] or "", _n(r[3]), _n(r[4]), _n(r[5]), _n(r[6] + r[7]), _usd(r[8]), r[9]] for r in runs], right={3, 4, 5, 6, 7, 8})
        out += ["The probe's mutant runs are not recorded one by one, so they are not counted here.", ""]
    out += [f"In full: {data('models')}, {data('models_by_benchmark')}, {data('harbor_runs')}.", ""]

    # 3. the people
    out += ["## 3. What people had to do", ""]
    counts = found["decision_counts"][1]
    people_rows = [[kind, _n(n)] for kind, n in counts] or [["none recorded", ""]]
    people_rows.append(["**all decisions by a person**", f"**{_n(figure['decisions by a person'])}**"])
    out += _table(["decision", "times"], people_rows)
    out += [f"Made by {_n(figure['people who made them'])} {'person' if figure['people who made them'] == 1 else 'people'}. "
            f"Each one, with who and when: {data('decisions')}.", ""]
    if found["skips"][1]:
        out += ["**Benchmarks a run left out**", ""]
        out += _table(["benchmark", "who", "why"], [[r[1], r[2], r[4]] for r in found["skips"][1]], right=set())
    if found["adapters"][1]:
        out += ["**Adapters**", ""]
        out += _table(["benchmark", "outcome", "written by", "model rounds", "result", "cost"],
                      [[r[1], r[2], r[3], _n(r[5]), r[6], _usd(r[7])] for r in found["adapters"][1]], right={3, 5})

    # 4. where it comes from
    out += ["## 4. Where these figures come from", ""]
    out += [note for note in notes if not note.startswith("**SIMULATED")] + [""]
    if source:
        out += _table(["", ""], [[k, _n(v)] for k, v in source], right=set())
    out += ["**Files**", ""]
    out += _table(["file", "holds"], [[f"`{DATA}/{name}.csv`", what] for name, (what, _) in TABLES.items()]
                  + ([[f"`{DATA}/source.csv`", "What the tables were made from."]] if source else []), right=set())
    return "\n".join(out)


def write(found: dict[str, Table], folder: Path, title: str, notes: list[str], source: list[list] | None = None) -> Path:
    """Write the report, stats.md, and beside it a folder with one CSV per table. Returns the report's path."""
    data = folder / DATA
    data.mkdir(parents=True, exist_ok=True)
    for old in [*folder.glob("*.csv"), *data.glob("*.csv")]:        # nothing stale from an earlier layout or run
        old.unlink()
    everything = {**found, **({"source": (["what", "value"], source)} if source else {})}
    for name, (head, rows) in everything.items():
        with (data / f"{name}.csv").open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([h.replace(" ", "_").replace(",", "") for h in head])
            writer.writerows(rows)
    path = folder / "stats.md"
    path.write_text(markdown(found, title, notes, source))
    return path
