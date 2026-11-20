"""What the dashboard's buttons show: one table about the index each. They are not commands: `hm open` shows them
to an index's owner and leads, and `hm todo` prints one of them."""
from __future__ import annotations

import time
from pathlib import Path

from rich.table import Table
from rich.text import Text

from .. import ledger as ledger_mod
from .. import skills as skills_mod
from .. import tasks as tasks_mod
from ..core import config as config_mod
from ..ledger import evals
from ..tasks import store
from ..tui import theme
from .common import console


def _table(*columns: tuple[str, str]) -> Table:
    tb = Table(header_style=theme.MU, border_style=theme.BORDER)
    for name, justify in columns:
        tb.add_column(name, justify=justify)
    return tb


def _records(ws: Path, simulated: bool) -> Path:
    if simulated:
        console.print(f"[{theme.AM}]Simulated.[/] These are the records of simulated runs on this machine. None of it is in the index.")
    return config_mod.simulated(ws) if simulated else ws


def status(ws: Path, simulated: bool = False) -> None:
    """Where every benchmark stands: its tasks by stage and outcome."""
    where = _records(ws, simulated)
    led = ledger_mod.Ledger(where, simulated=simulated)
    states = ledger_mod.fold(led.events)
    names = ["in_progress", "not_selected", "dropped", "report_ready", "accepted", "rejected", "contested"]
    tb = _table(("benchmark", "left"), *((n.replace("_", " "), "right") for n in names), ("cost", "right"))
    for b in sorted(set(tasks_mod.read_registry(ws)) | {s.benchmark for s in states.values()}):
        mine = [s for s in states.values() if s.benchmark == b]
        tb.add_row(b, *[str(sum(1 for s in mine if s.status == n)) for n in names],
                   f"${sum(float(e.get('cost', 0) or 0) for e in led.events if e['benchmark'] == b):.2f}")
    console.print(tb)
    console.print(f"[{theme.MU}]{len(led.events)} records in {where / ledger_mod.SHARDS}[/]")


def credits(ws: Path, simulated: bool = False) -> None:
    """Who contributed what: runs, verdicts, repairs, per person."""
    where = _records(ws, simulated)
    people = {c["github"]: f"{c.get('name', '?')} ({c.get('affiliation', '?')}, @{c['github']})"
              for c in ledger_mod.load_contributors(ws) if c.get("github")}
    tb = _table(("contributor", "left"), *((c, "right") for c in ("trials", "tokens", "container hours", "cost", "submissions",
                                                                  "repairs", "briefs", "verdicts")))
    for handle, c in sorted(ledger_mod.credits(ledger_mod.Ledger(where, simulated=simulated).events).items()):
        tb.add_row(people.get(handle, f"@{handle}"), f"{int(c['trials'])}", f"{int(c['tokens']):,}",
                   f"{c['container_seconds'] / 3600:.1f}", f"${c['cost']:.2f}", f"{int(c['submissions'])}",
                   f"{int(c['repairs'])}", f"{int(c['briefs'])}", f"{int(c['verdicts'])}")
    console.print(tb)


def leaderboard(ws: Path, simulated: bool = False) -> None:
    """How each roster configuration does on the accepted tasks."""
    where = _records(ws, simulated)
    cfg = config_mod.load(ws)
    rows, n = ledger_mod.leaderboard(ledger_mod.Ledger(where, simulated=simulated).events, [c.model_dump() for c in config_mod.roster(ws)],
                                     [(e.agent, e.model) for e in cfg.difficulty.suite], evals.read(where, simulated))
    if not n:
        console.print("No task has been accepted yet, so there is nothing to rank.")
        return
    tb = _table(("configuration", "left"), ("agent", "left"), ("effort", "left"), ("score", "right"), ("tasks", "right"),
                ("trials", "right"), ("", "left"))
    for r in rows:
        note = f"includes {r['selection_trials']} trials the tasks were selected on" if r["selection_trials"] else ""
        if not r["complete"] and r["tasks"]:
            note = (note + ", " if note else "") + "incomplete: not ranked"
        tb.add_row(r["name"] or r["model"], f"{r['agent']} {r['version']}".strip(), r["effort"],
                   f"{100 * r['score']:.1f}%" if r["score"] is not None else "-", f"{r['tasks']} of {n}", str(r["trials"]), note)
    console.print(tb)
    console.print(f"[{theme.MU}]Over the {n} accepted task{'s' if n != 1 else ''}. The score is the pass rate on each task, averaged "
                  "over tasks. Trials that failed for an infrastructure reason are left out.[/]")


def todo(ws: Path, simulated: bool = False) -> None:
    """What is left to evaluate: for each roster configuration, the trials done, held by someone, and still free."""
    from .evals import cells_of
    _records(ws, simulated)
    cells = cells_of(ws, simulated)
    if not cells:
        console.print("Nothing to evaluate yet: the index has no accepted task, or its roster is empty.")
        return
    tb = _table(("configuration", "left"), ("trials done", "right"), ("from curation", "right"), ("held now", "right"),
                ("free", "right"), ("given up", "right"))
    left = lambda group: str(sum(len(c.missing) for c in group))      # noqa: E731
    for name in dict.fromkeys(c.config.name for c in cells):
        mine = [c for c in cells if c.config.name == name]
        tb.add_row(name, str(sum(len(c.done) for c in mine)), str(sum(e.get("purpose") == "difficulty" for c in mine for e in c.done.values())),
                   left([c for c in mine if c.open and (c.holder or c.mine)]), left([c for c in mine if c.open and not c.holder and not c.mine]),
                   left([c for c in mine if c.failed]))
    console.print(tb)
    tasks = len({c.task["task"] for c in cells})
    state = "released" if simulated or evals.release(evals.read(ws)) else "not released yet, so nothing can be taken"
    console.print(f"[{theme.MU}]{tasks} accepted task{'s' if tasks != 1 else ''}, {cells[0].trials} trials for each configuration. "
                  f"The index is {state}.[/]", highlight=False)


def tasks(ws: Path, simulated: bool = False) -> None:
    """Each accepted task, and whether the store keeps its accepted version."""
    cfg = config_mod.load(ws)
    entries = ledger_mod.index_entries(ledger_mod.Ledger(ws).events)
    if not entries:
        console.print("No task has been accepted yet.")
        return
    try:
        there, why = store.remote(cfg), "none named in about.toml"
    except store.StoreError as err:
        there, why = None, str(err)
    tb = _table(("task", "left"), ("accepted version", "left"), ("in the store", "left"))
    for e in entries:
        benchmark, name = e["task"].split("/", 1)
        try:
            kept = "-" if there is None else f"[{theme.TE}]yes[/]" if there.has(store.key(benchmark, name, e["digest"])) else f"[{theme.RD}]no[/]"
        except store.StoreError as err:
            kept, why, there = "-", str(err), None
        tb.add_row(e["task"], e["digest"][:12], kept)
    console.print(tb)
    cut = [s.task_id for s in ledger_mod.fold(ledger_mod.Ledger(ws).events).values() if s.status == "cut"]
    console.print(f"{len(entries)} accepted." + (f" Cut from the index: {', '.join(cut[:8])}{' and more' if len(cut) > 8 else ''}." if cut else ""),
                  highlight=False)
    console.print(f"[{theme.MU}]The store: {there.where if there else why}.[/]", highlight=False)


def benchmarks(ws: Path, simulated: bool = False) -> None:
    """The benchmarks this index may curate, from the signed records that added them."""
    from ..engine import prepare
    registry = tasks_mod.read_registry(ws)
    gone = ledger_mod.abandoned(ledger_mod.Ledger(ws).events)
    tb = _table(*((c, "left") for c in ("id", "name", "version", "released", "tasks", "license", "source", "added by", "here")))
    for e in tasks_mod.supported.supported(ws):
        state = f"[{theme.RD}]abandoned[/]" if e["id"] in gone else f"[{theme.TE}]yes[/]" if e["id"] in registry else "a run fetches it"
        tb.add_row(e["id"], e["name"], e["version"], e["released"], str(e.get("task_count", "")), e.get("license", ""),
                   e["github"].replace("https://", ""), "@" + e.get("added_by", "?"), state)
    console.print(tb)
    for b, record in gone.items():      # said in full under the table, where a narrow column cannot cut it
        console.print(f"[{theme.RD}]{b}[/] was {prepare.abandoned_text(b, record)}", highlight=False)


def protocol(ws: Path, simulated: bool = False) -> None:
    """This folder's settings, the locked ones, and any setting that differs."""
    cfg = config_mod.load(ws)
    lock = ledger_mod.protocol.current(ledger_mod.Ledger(ws).events)
    console.print(f"This folder's settings hash to protocol [bold]{cfg.protocol}[/].")
    if lock is None:
        console.print(f"[{theme.AM}]No protocol is locked yet.[/] The owner locks one with: hm lock")
        return
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(lock["time"]))
    console.print(f"Locked: [bold]v{ledger_mod.protocol.version(lock)}[/] ({lock['protocol']}), by @{lock['runner']} on {when}."
                  + (f" {lock['note']}" if lock.get("note") else ""))
    diff = ledger_mod.protocol.differences(lock.get("settings", {}), cfg.protocol_settings())
    if diff:
        console.print(f"[{theme.RD}]They differ, so runs are refused here:[/]")
        for line in diff:
            console.print(f"  {line}")
    else:
        console.print(f"[{theme.TE}]They match. Runs are allowed.[/]")


STATE = {"missing": (theme.RD, "not in the index: hm lock copies the template in"), "same": (theme.TE, "same as the template"),
         "own": (theme.PU, "this index's own wording")}


def skills(ws: Path, simulated: bool = False) -> None:
    """Each curation skill, and whether this index has its own copy of it."""
    tb = _table(("skill", "left"), ("in this index", "left"))
    for name, state in skills_mod.status(ws / "skills"):
        color, words = STATE[state]
        tb.add_row(f"skills/{name}", f"[{color}]{words}[/]")
    console.print(tb)
    for p in skills_mod.problems(ws / "skills"):
        console.print(f"[{theme.RD}]x[/] {p}")
    console.print(f"[{theme.MU}]An index may word any of them its own way: its owner edits the file, then runs hm lock.[/]")


VIEWS = {"leaderboard": leaderboard, "tasks": tasks, "status": status, "todo": todo, "credits": credits,
         "benchmarks": benchmarks, "protocol": protocol, "skills": skills}
SIMULATED = ("leaderboard", "status", "todo", "credits")     # these can show the records of simulated runs


def capture(name: str, ws: Path, simulated: bool, width: int) -> Text:
    """What a view prints, at the width of the box that shows it."""
    before = console._width
    console.width = width
    try:
        with console.capture() as captured:
            VIEWS[name](ws, simulated and name in SIMULATED)
    except Exception as err:            # a view that cannot be read says why, in its box
        return Text(f"Could not be read: {err}")
    finally:
        console._width = before
    return Text.from_ansi(captured.get().rstrip())
