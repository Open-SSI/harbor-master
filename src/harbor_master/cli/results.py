"""Reading the ledger: status, credits, the leaderboard, verification."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import typer

from rich.table import Table

from .. import ledger as ledger_mod
from .. import templates
from .. import tasks as tasks_mod
from ..core import config as config_mod
from ..tui import theme
from .common import (Simulated, Workspace, app, console, fail, index_app, is_owner, ledger_app, mention_simulated, need_identity,
                     need_workspace, records)


def _people(ledger_dir: Path) -> dict[str, str]:
    return {c["github"]: f"{c.get('name', '?')} ({c.get('affiliation', '?')}, @{c['github']})"
            for c in ledger_mod.load_contributors(ledger_dir) if c.get("github")}


@app.command()
def status(simulated: bool = Simulated, workspace: Optional[Path] = Workspace):
    """Where every benchmark stands, from the ledger."""
    ws = need_workspace(workspace)
    where = records(ws, simulated)
    led = ledger_mod.Ledger(where, simulated=simulated)
    states = ledger_mod.fold(led.events)
    names = ["in_progress", "not_selected", "dropped", "report_ready", "accepted", "rejected", "replaced", "contested"]
    tb = Table(header_style=theme.MU, border_style=theme.BORDER)
    tb.add_column("benchmark")
    for n in names:
        tb.add_column(n.replace("_", " "), justify="right")
    tb.add_column("cost", justify="right")
    registry = tasks_mod.read_registry(ws)
    for b in sorted(set(registry) | {s.benchmark for s in states.values()}):
        mine = [s for s in states.values() if s.benchmark == b]
        tb.add_row(b, *[str(sum(1 for s in mine if s.status == n)) for n in names],
                   f"${sum(float(e.get('cost', 0) or 0) for e in led.events if e['benchmark'] == b):.2f}")
    console.print(tb)
    console.print(f"[{theme.MU}]{len(led.events)} events in {where / ledger_mod.SHARDS}[/]")
    mention_simulated(ws, simulated)


@app.command()
def credits(simulated: bool = Simulated, workspace: Optional[Path] = Workspace):
    """Contributions per person, computed from the ledger."""
    ws = need_workspace(workspace)
    ledger_dir = records(ws, simulated)
    people = _people(ws)
    tb = Table(header_style=theme.MU, border_style=theme.BORDER)
    tb.add_column("contributor")
    for col in ("trials", "tokens", "container hours", "cost", "submissions", "repairs", "briefs", "verdicts"):
        tb.add_column(col, justify="right")
    for handle, c in sorted(ledger_mod.credits(ledger_mod.Ledger(ledger_dir, simulated=simulated).events).items()):
        tb.add_row(people.get(handle, f"@{handle}"), f"{int(c['trials'])}", f"{int(c['tokens']):,}",
                   f"{c['container_seconds'] / 3600:.1f}", f"${c['cost']:.2f}", f"{int(c['submissions'])}",
                   f"{int(c['repairs'])}", f"{int(c['briefs'])}", f"{int(c['verdicts'])}")
    console.print(tb)


@app.command()
def leaderboard(simulated: bool = Simulated, workspace: Optional[Path] = Workspace):
    """How each agent configuration does on the accepted tasks, computed from the ledger."""
    from ..core.config import roster
    ws = need_workspace(workspace)
    cfg = config_mod.load(ws)
    led = ledger_mod.Ledger(records(ws, simulated), simulated=simulated)
    rows, n = ledger_mod.leaderboard(led.events, [c.model_dump() for c in roster(ws)],
                                     [(e.agent, e.model) for e in cfg.difficulty.suite])
    if not n:
        console.print("No task has been accepted yet, so there is nothing to rank. Accept tasks with: hm review")
        mention_simulated(ws, simulated)
        return
    tb = Table(header_style=theme.MU, border_style=theme.BORDER)
    for col, j in (("configuration", "left"), ("agent", "left"), ("effort", "left"), ("score", "right"),
                   ("tasks", "right"), ("trials", "right"), ("", "left")):
        tb.add_column(col, justify=j)
    for r in rows:
        note = "tasks were selected against it" if r["selected_against"] else ""
        if not r["complete"] and r["tasks"]:
            note = (note + ", " if note else "") + "incomplete: not ranked"
        score = f"{100 * r['score']:.1f}%" if r["score"] is not None else "-"
        tb.add_row(r["name"] or r["model"], f"{r['agent']} {r['version']}".strip(), r["effort"], score,
                   f"{r['tasks']} of {n}", str(r["trials"]), note)
    console.print(tb)
    console.print(f"[{theme.MU}]Over the {n} accepted task{'s' if n != 1 else ''} in {ledger_mod.INDEX_FILE}. The score is the pass "
                  "rate on each task, averaged over tasks. Trials that failed for an infrastructure reason are left out.[/]")


@app.command(name="roster")
def roster_cmd(workspace: Optional[Path] = Workspace):
    """The agent configurations to benchmark on the index, from its roster.toml."""
    from ..core import keys
    from ..core.config import roster
    ws = need_workspace(workspace)
    tb = Table(header_style=theme.MU, border_style=theme.BORDER)
    for col in ("name", "agent", "version", "model", "effort", "needs", "key found"):
        tb.add_column(col)
    for c in roster(ws):
        found = f"[{theme.TE}]yes[/]" if keys.found(c.key, c.agent) else f"[{theme.RD}]no[/]"
        tb.add_row(c.name, c.agent, c.version, c.model, c.reasoning_effort, c.key, found)
    console.print(tb)
    console.print(f"[{theme.MU}]Run one with: hm eval --config NAME. What is still to run: hm todo[/]")


@app.command()
def todo(simulated: bool = Simulated, workspace: Optional[Path] = Workspace):
    """What is still to run: each roster configuration on each accepted task."""
    from ..core.config import roster
    ws = need_workspace(workspace)
    cfg = config_mod.load(ws)
    led = ledger_mod.Ledger(records(ws, simulated), simulated=simulated)
    index = ledger_mod.index_entries(led.events)
    if not index:
        console.print("No task has been accepted yet, so nothing is waiting to be benchmarked.")
        return
    n = cfg.difficulty.trials_per_config
    tb = Table(header_style=theme.MU, border_style=theme.BORDER)
    for col, j in (("configuration", "left"), ("trials done", "right"), ("trials to run", "right"), ("tasks untouched", "right")):
        tb.add_column(col, justify=j)
    for c in roster(ws):
        done = [sum(1 for i in range(n) if led.trial(t["digest"], c.agent, c.version, c.model, cfg.protocol, i,
                                                     c.reasoning_effort)) for t in index]
        tb.add_row(c.name, str(sum(done)), str(len(index) * n - sum(done)), str(sum(1 for d in done if d == 0)))
    console.print(tb)
    console.print(f"[{theme.MU}]{len(index)} accepted task{'s' if len(index) != 1 else ''}, {n} trials each. Run one with: hm eval --config NAME[/]")


@index_app.command("build")
def index_build(publish: bool = typer.Option(False, help="Owner only: also push index.toml to the shared index."),
                workspace: Optional[Path] = Workspace):
    """Write index.toml, the list of accepted tasks, from the signed verdicts."""
    ws, ident = need_workspace(workspace), need_identity()
    ledger_dir = ws
    path = ledger_mod.write_index(ledger_dir)
    n = len(ledger_mod.index_entries(ledger_mod.Ledger(ledger_dir).events))
    console.print(f"{path}: {n} accepted task{'s' if n != 1 else ''}.")
    if publish:
        if not is_owner(ws, ident):
            fail("Only the owner publishes index.toml, so that one person writes that file.")
        sync = ledger_mod.Sync(ledger_dir, ident.name, ident.github)
        if not sync.enabled:
            fail("This index is not shared. Connect it with: hm index connect URL")
        sync.publish_index(f"index: {n} accepted tasks")
        console.print(f"[{theme.TE}]Published.[/]")


@ledger_app.command("verify")
def ledger_verify(simulated: bool = Simulated, workspace: Optional[Path] = Workspace):
    """Check every shard: schema, hash chain, signatures, repeats, and what accepted tasks must have."""
    ws = need_workspace(workspace)
    ledger_dir = records(ws, simulated)
    fields = {b: info.get("field", "") for b, info in tasks_mod.read_registry(ws).items()}
    problems = ledger_mod.verify(ledger_dir, fields, config_mod.load(ws).index.max_per_benchmark, simulated=simulated, index_dir=ws)
    n = len(ledger_mod.Ledger(ledger_dir, simulated=simulated).events)
    if problems:
        for p in problems[:50]:
            console.print(f"[{theme.RD}]x[/] {p}")
        fail(f"{len(problems)} problem{'s' if len(problems) != 1 else ''} in {n} events.")
    console.print(f"[{theme.TE}]The ledger is sound:[/] {n} events in {len(ledger_mod.shard_paths(ledger_dir))} shards.")


@app.command()
def sync(workspace: Optional[Path] = Workspace):
    """Pull the shared index and push this workspace's new records."""
    ws = need_workspace(workspace)
    ident = need_identity()
    cfg = config_mod.load(ws)
    ledger_dir = ws
    s = ledger_mod.Sync(ledger_dir, ident.name, ident.github)
    if not s.enabled:
        fail("This index is not shared yet. Connect it with: hm index connect URL")
    try:
        s.pull()
        problems = ledger_mod.verify(ledger_dir, max_per_benchmark=cfg.index.max_per_benchmark)
        if problems:
            for p in problems[:20]:
                console.print(f"[{theme.RD}]x[/] {p}")
            fail(f"Not pushed: the ledger has {len(problems)} problem{'s' if len(problems) != 1 else ''}.")
        if is_owner(ws, ident):         # the description is the owner's to change at any time
            s.define("describe the index", only=("about.toml", "README.md", ".gitignore"))
        s.publish(f"sync by @{ident.github}")
    except ledger_mod.SyncError as err:
        fail(str(err))
    console.print(f"[{theme.TE}]Synced.[/] {len(ledger_mod.Ledger(ledger_dir).events)} events in the shared ledger.")


protocol_app = typer.Typer(invoke_without_command=True, help="The locked settings a curation runs under.")
app.add_typer(protocol_app, name="protocol")


@protocol_app.callback()
def protocol_show(ctx: typer.Context, workspace: Optional[Path] = Workspace):
    """Show this workspace's protocol, the locked one, and any setting that differs."""
    if ctx.invoked_subcommand:
        return
    ws = need_workspace(workspace)
    cfg = config_mod.load(ws)
    lock = ledger_mod.protocol.current(ledger_mod.Ledger(ws).events)
    console.print(f"This workspace's settings hash to protocol [bold]{cfg.protocol}[/].")
    if lock is None:
        console.print(f"[{theme.AM}]No protocol is locked in this ledger.[/] The owner locks one with: hm protocol lock")
        return
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(lock["time"]))
    console.print(f"Locked: [bold]{lock['protocol']}[/], by @{lock['runner']} on {when}." + (f" {lock['note']}" if lock.get("note") else ""))
    diff = ledger_mod.protocol.differences(lock.get("settings", {}), cfg.protocol_settings())
    here = templates.digests(ws)
    diff += [f"{name}: not the locked copy" for name, digest in (lock.get("registry") or {}).items() if here.get(name) != digest]
    if not diff:
        console.print(f"[{theme.TE}]They match. Runs are allowed.[/]")
        return
    console.print(f"[{theme.RD}]They differ, so runs are refused here:[/]")
    for line in diff:
        console.print(f"  {line}")


@protocol_app.command("lock")
def protocol_lock(note: str = typer.Option("", help="Why the protocol is being set or changed."),
                  workspace: Optional[Path] = Workspace):
    """Owner only: lock this index's settings, benchmark list, roster and contributor list as they are here."""
    ws, ident = need_workspace(workspace), need_identity()
    cfg = config_mod.load(ws)
    if not is_owner(ws, ident):
        fail(f"Only the owner can lock the protocol, and @{ident.github} is not the owner on the contributor list.")
    sync = ledger_mod.Sync(ws, ident.name, ident.github)
    if sync.enabled:
        sync.pull()
    lock = ledger_mod.protocol.current(ledger_mod.Ledger(ws).events)
    registry, requires = templates.digests(ws), cfg.requires.harbor_master
    if lock is not None and (lock["protocol"], lock.get("registry"), lock.get("requires", requires)) == (cfg.protocol, registry, requires):
        console.print(f"Protocol {cfg.protocol} and the index's files are already the locked ones.")
        return
    shard = ledger_mod.Shard(ws, ledger_mod.protocol.BENCH, ident, ledger_mod.MANUAL, cfg.protocol)
    shard.append("protocol", settings=cfg.protocol_settings(), registry=registry, requires=requires, note=note,
                 replaces=lock["protocol"] if lock and lock["protocol"] != cfg.protocol else "")
    sync.define(f"lock protocol {cfg.protocol}")        # the files travel with the lock that covers them
    if sync.enabled:
        sync.publish(f"lock protocol {cfg.protocol}")
    same = lock is not None and lock["protocol"] == cfg.protocol
    console.print(f"[{theme.TE}]Locked protocol {cfg.protocol}.[/] " + (
        "The settings are the same as before, so earlier records stand. The lists are the new ones." if same else
        "Every run on this index must now use these settings."
        + (f" It replaces {lock['protocol']}: tasks finished under that one keep their records, and may be "
           "run again under the new one." if lock else "")))
