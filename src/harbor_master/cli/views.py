"""What the dashboard's buttons show: one table about the index each. They are not commands: `hm view` shows them
to an index's owner and leads, and `hm benchmarks` prints one of them."""
from __future__ import annotations

from pathlib import Path

from rich.table import Table
from rich.text import Text

from .. import ledger as ledger_mod
from .. import skills as skills_mod
from .. import tasks as tasks_mod
from ..core import config as config_mod
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
    events = ledger_mod.Ledger(ws).events
    gone = ledger_mod.abandoned(events)
    listed = tasks_mod.supported.supported(ws)
    told = ledger_mod.benchmarks.statuses(events, [e["id"] for e in listed])
    color = {"listed": theme.SE, "curating": theme.PU, "curated": theme.TE, "dropped": theme.RD}
    tb = _table(*((c, "left") for c in ("id", "status", "name", "version", "released", "tasks", "license", "source", "added by")))
    for e in listed:
        st = told[e["id"]]
        state = f"[{color[st['status']]}]{st['status']}[/]" + (f" by @{st['by']} until {st['until']}" if st["status"] == "curating" else "")
        tb.add_row(e["id"], state, e["name"], e["version"], e["released"], str(e.get("task_count", "")), e.get("license", ""),
                   e["github"].replace("https://", ""), "@" + e.get("added_by", "?"))
    console.print(tb)
    for b, record in gone.items():      # said in full under the table, where a narrow column cannot cut it
        console.print(f"[{theme.RD}]{b}[/] was {prepare.abandoned_text(b, record)}", highlight=False)


STATE = {"missing": (theme.RD, "not in the index: the tool's template is used"), "same": (theme.TE, "same as the template"),
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
    console.print(f"[{theme.MU}]An index may word any of them its own way: its owner edits the file, and it is shared from the owner's next hm command.[/]")


VIEWS = {"tasks": tasks, "status": status, "credits": credits, "benchmarks": benchmarks, "skills": skills}
SIMULATED = ("status", "credits")     # these can show the records of simulated runs


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
