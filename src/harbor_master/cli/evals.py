"""Stage 2, the evaluation of a released index: hm release opens it, hm eval takes cells and runs them, hm todo shows what is left."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from .. import ledger as ledger_mod
from .. import tasks as tasks_mod
from ..core import config as config_mod
from ..ledger import evals
from ..tui import theme
from .common import Workspace, app, check_lock, console, fail, is_owner, need_identity, need_workspace


@app.command()
def release(workspace: Optional[Path] = Workspace):
    """Owner only, once: release the index as version 1.0.0. Its tasks are fixed, curation closes, and evaluation opens.
    The roster is not fixed: its owner adds configurations as new models come out, and each adds its cells to the to-do list."""
    from ..interact import guide
    from .results import lock_protocol
    ws, ident = need_workspace(workspace), need_identity()
    if not is_owner(ws, ident):
        fail(f"Only the owner releases an index, and @{ident.github} is not the owner of this one.")
    if evals.release(evals.read(ws)):
        fail("This index is released already. hm todo shows what is left to evaluate.")
    if guide.look(ws).lock != "locked":
        fail("Lock the index first, so that what is released is what its folder holds now: hm lock")
    index = ledger_mod.index_entries(ledger_mod.Ledger(ws).events)
    if not index:
        fail("No task has been accepted yet, so there is nothing to release.")
    cfg = config_mod.load(ws)
    lock_protocol(ws, "release", "release")
    ledger_mod.write_index(ws)
    evals.append(ws, ident, "release", version="1.0.0", tasks=len(index), index=evals.file_digest(ws / ledger_mod.INDEX_FILE),
                 trials=cfg.eval.trials)
    sync = ledger_mod.Sync(ws, ident.name, ident.github)
    sync._commit("release the index as 1.0.0")
    if sync.enabled:
        sync.publish("release the index as 1.0.0")
    n = len(config_mod.roster(ws))
    console.print(f"[{theme.TE}]Released as v1.0.0[/]: {len(index)} tasks, {n} configurations on the roster, {cfg.eval.trials} trials each. "
                  "Curation is closed, and evaluation is open to everyone on the contributor list: hm eval. The roster can still grow: "
                  "edit roster.toml, then hm sync.", highlight=False)


def _view(ws: Path) -> None:
    """Show the run that is going in this index, and say where it stands when its screen is left."""
    from .. import host
    going = host.attach(ws) == "going"
    console.print("It goes on in the background. hm eval inside this folder shows it again." if going else
                  "It has ended. What is left: hm todo", highlight=False)


def cells_of(ws: Path, simulated: bool, me: str = "", machine: str = "") -> list[evals.Cell]:
    root = config_mod.simulated(ws) if simulated else ws
    led = ledger_mod.Ledger(root, simulated=simulated)
    events = evals.read(root, simulated)
    cfg = config_mod.load(ws)
    return evals.cells(ledger_mod.index_entries(led.events), config_mod.roster(ws) or (config_mod.demo_roster(cfg) if simulated else []), cfg.eval,
                       evals.agent_trials(led.events, events), events, time.time(), me, machine)


@app.command()
def todo(workspace: Optional[Path] = Workspace):
    """What is left to evaluate: for each roster configuration, the trials done, held by someone, and still free."""
    from .views import todo as show
    ws = need_workspace(workspace, say=True)
    real = ledger_mod.index_entries(ledger_mod.Ledger(ws).events)
    show(ws, simulated=not real and bool(ledger_mod.index_entries(ledger_mod.Ledger(config_mod.simulated(ws), simulated=True).events)))
    console.print(f"[{theme.MU}]Take some with: hm eval[/]", highlight=False)


@app.command(name="eval")
def eval_(take: int = typer.Option(None, help="How many cells to take: each is one task with one configuration. Default: as many as this machine runs at once."),
          config: str = typer.Option("", help="Only this configuration from the roster."),
          simulate: bool = typer.Option(False, help="Use simulated trials on the tasks accepted in simulated runs. Spends nothing."),
          demo: bool = typer.Option(False, help="Try it: the made-up benchmark's tasks, evaluated simulated. Spends nothing, and nothing enters the index."),
          plain: bool = typer.Option(False, help="No screen: take once, run, print a table, and end."),
          speed: float = typer.Option(1.0, hidden=True), foreground: bool = typer.Option(False, hidden=True),
          workspace: Optional[Path] = Workspace):
    """Stage 2, evaluating the released index: take the next cells from the to-do list, hold them for a few hours, and run their trials.
    In a terminal it opens the eval screen, and goes on taking cells until none is free. It goes on when its
    terminal closes: hm eval inside the index's folder shows it again."""
    from .. import engine
    from ..core import keys
    from ..tasks import store
    import sys

    from .. import host
    ws, ident = need_workspace(workspace), need_identity()
    if demo:
        from .home import demo_eval
        demo_eval(ws)
        simulate = True
    cfg = config_mod.load(ws)
    screen = not plain and (foreground or (sys.stdout.isatty() and sys.stdin.isatty()))
    if screen and not foreground and host.active(ws):       # one is going in this index: show it
        _view(ws)
        return
    me = next((c for c in ledger_mod.load_contributors(ws) if c.get("github") == ident.github), None)
    if not simulate:
        if not ledger_mod.can_evaluate(me):
            fail(f"@{ident.github} is not on this index's contributor list yet, so cannot evaluate it. hm init prints the line to send its owner.")
        if not evals.release(evals.read(ws)):
            fail("This index is not released yet, so its evaluation is not open. Its owner opens it with: hm release")
        check_lock(ledger_mod.protocol.current(ledger_mod.Ledger(ws).events), cfg, ws)
    roster = [c for c in (config_mod.roster(ws) or (config_mod.demo_roster(cfg) if simulate else [])) if config in ("", c.name)]
    if not roster:
        fail(f"{config} is not on the roster." if config else "The roster is empty: its owner lists the configurations in roster.toml.")
    if not simulate:        # a configuration this machine has no credential for is left to others
        able = [c for c in roster if any(keys.get(n) for n in keys.needs(c.agent, c.model, c.key)) or not keys.needs(c.agent, c.model, c.key)]
        for c in roster:
            if c not in able:
                console.print(f"[{theme.MU}]Left to others: {c.name}. None of its keys is in your keys file ({', '.join(keys.needs(c.agent, c.model, c.key))}).[/]", highlight=False)
        roster = able
        if not roster:
            fail("This machine has a key for none of the roster's configurations.")
    registry = tasks_mod.read_registry(ws)
    index = ledger_mod.index_entries(ledger_mod.Ledger(config_mod.simulated(ws) if simulate else ws, simulated=simulate).events)
    here: dict[str, tasks_mod.Task] = {}                # the tasks of the benchmarks on this machine
    for b, info in registry.items():
        if info.get("path") and Path(info["path"]).is_dir():
            here.update({t.task_id: t for t in tasks_mod.load_benchmark(Path(info["path"]), b)})

    def fetch(entry: dict) -> tasks_mod.Task:
        """The accepted version of a task: the files here when they are that version, else the stored one."""
        mine = here.get(entry["task"])
        if mine is not None and (simulate or mine.digest == entry["digest"]):
            return mine
        path = store.fetch(ws, cfg, *entry["task"].split("/", 1), entry["digest"])
        if path is None:
            raise store.StoreError(f"the accepted version of {entry['task']} is not on this machine and not in the task store")
        return tasks_mod.load_task(path, entry["benchmark"])

    wanted = {b: registry.get(b) or {"path": "", "supported": b} for b in sorted({e["benchmark"] for e in index})}
    if screen and not foreground:       # everything checks out: start it behind a relay, and view it from here
        try:
            host.start(ws, [*(["--take", str(take)] if take else []), *(["--config", config] if config else []),
                            *(["--simulate"] if simulate else []), "--speed", str(speed)], "hm eval", simulate, "eval")
        except RuntimeError as err:
            fail(str(err))
        _view(ws)
        return
    session = engine.Session(ws, cfg, ident, wanted, simulate=simulate, speed=speed if screen else 0.0, no_input=not screen, command="hm eval")
    if screen:
        from ..tui.evals import EvalApp
        EvalApp(engine.Evaluation(session, index, roster, fetch, take or cfg.run.cells), cfg.about.title or cfg.about.name or ws.name).run()
        return
    rows = asyncio.run(engine.evaluate(session, index, roster, take or cfg.run.cells, fetch))
    if not rows:
        left = [c for c in cells_of(ws, simulate, ident.github, session.machine) if c.open]
        console.print("Nothing is left to evaluate." if not left else
                      f"Nothing is free now: {len(left)} cell{'s are' if len(left) != 1 else ' is'} held by others, for at most {cfg.eval.claim_hours:g} hours. See: hm todo")
        return
    tb = Table(header_style=theme.MU, border_style=theme.BORDER)
    for col, j in (("task", "left"), ("configuration", "left"), ("passed", "right"), ("new trials", "right"), ("", "left")):
        tb.add_column(col, justify=j)
    for r in sorted(rows, key=lambda r: (r["task"], r["config"])):
        tb.add_row(r["task"].split("/", 1)[-1], r["config"], f"{r['passes']} of {r['trials']}", str(r["new"]), r["note"])
    console.print(tb)
    console.print(f"[{theme.MU}]{sum(r['new'] for r in rows)} new trials, ${session.trial_cost:.2f}. Take more with hm eval. What is left: hm todo[/]", highlight=False)
