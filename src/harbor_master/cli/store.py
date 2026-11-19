"""The task store: where the accepted tasks are kept, and bringing copies in and out of it."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from .. import ledger as ledger_mod
from .. import tasks as tasks_mod
from ..core import config as config_mod
from ..tasks import store
from ..tui import theme
from .common import Workspace, app, console, fail, is_owner, need_identity, need_workspace

tasks_app = typer.Typer(invoke_without_command=True, help="The accepted tasks, and the store that keeps the accepted version of each.")
app.add_typer(tasks_app, name="tasks")


def _accepted(ws: Path) -> list[dict]:
    return ledger_mod.index_entries(ledger_mod.Ledger(ws).events)


def _files(ws: Path, entry: dict) -> Path | None:
    """The task's folder among the benchmarks registered here, when it is the accepted version."""
    benchmark, name = entry["task"].split("/", 1)
    info = tasks_mod.read_registry(ws).get(benchmark)
    if not info or not info.get("path") or not Path(info["path"]).is_dir():
        return None
    found = next((d for d in tasks_mod.find_tasks(Path(info["path"])) if d.name == name), None)
    return found if found is not None and tasks_mod.digest_dir(found) == entry["digest"] else None


def make_store(cfg) -> bool:
    """Create the index's task store if it is not there yet, and say how that went. True when it exists now."""
    try:
        there = store.remote(cfg)
        if there.exists():
            console.print(f"[{theme.TE}]The task store is there:[/] {there.where}", highlight=False)
        else:
            there.create()
            console.print(f"[{theme.TE}]Created the task store:[/] {there.where}. It is private: on Hugging Face, give your "
                          "contributors write access to it, and open it to everyone when the index is released.", highlight=False)
        return True
    except store.StoreError as err:
        console.print(f"[{theme.AM}]The task store is named and not created yet:[/] {err}\nThen create it with: /tasks store",
                      highlight=False)
        return False


@tasks_app.command("store")
def tasks_store(name: str = typer.Argument("", help="A Hugging Face dataset repository, as org/name. Owner only: makes it this index's store."),
                workspace: Optional[Path] = Workspace):
    """The store for this index's accepted tasks: whether this machine can use it. The owner names it and creates it here."""
    import re
    ws, ident = need_workspace(workspace), need_identity()
    cfg = config_mod.load(ws)
    if name:
        if not is_owner(ws, ident):
            fail(f"Only the owner names the task store, and @{ident.github} is not the owner of this index.")
        if not store.is_name(name):
            fail("A task store is a Hugging Face dataset repository, written org/name.")
        path = ws / config_mod.ABOUT_NAME
        text = path.read_text() if path.exists() else ""
        line = f"tasks = {json.dumps(name)}"
        path.write_text(re.sub(r'(?m)^tasks\s*=.*$', line, text) if re.search(r'(?m)^tasks\s*=', text) else text.rstrip() + f"\n{line}\n")
        sync = ledger_mod.Sync(ws, ident.name, ident.github)
        try:
            sync.define(f"task store {name}", (config_mod.ABOUT_NAME,))
            if sync.enabled:
                sync.publish(f"task store {name}")
        except ledger_mod.SyncError as err:
            fail(str(err))
        cfg = config_mod.load(ws)
        console.print(f"This index keeps its accepted tasks in {name}." + (" Others get that with /sync." if sync.enabled else ""), highlight=False)
    if not (cfg.about.tasks or "").strip():
        fail("Refused: " + store.not_ready(cfg))
    if is_owner(ws, ident):
        if not make_store(cfg):
            raise typer.Exit(1)
        return
    why = store.not_ready(cfg)
    if why:
        fail(why[0].upper() + why[1:])
    console.print(f"[{theme.TE}]This machine can reach the task store:[/] {store.remote(cfg).where}. "
                  "Whether your token may write to it shows at your first accepted task.", highlight=False)


def _remote(cfg):
    try:
        return store.remote(cfg)
    except store.StoreError as err:
        fail(str(err))


@tasks_app.callback()
def tasks_show(ctx: typer.Context, workspace: Optional[Path] = Workspace):
    """Each accepted task, and where a copy of the accepted version is."""
    if ctx.invoked_subcommand:
        return
    ws = need_workspace(workspace)
    cfg = config_mod.load(ws)
    entries, here, there = _accepted(ws), store.local(ws), _remote(cfg)
    if not entries:
        console.print("No task has been accepted yet, so there is nothing to keep.")
        return
    tb = Table(header_style=theme.MU, border_style=theme.BORDER)
    for col in ("task", "accepted version", "on this machine", "in the store"):
        tb.add_column(col)
    missing = 0
    for e in entries:
        benchmark, name = e["task"].split("/", 1)
        k = store.key(benchmark, name, e["digest"])
        local = "archive" if here.has(k) else "its files" if _files(ws, e) else f"[{theme.AM}]no[/]"
        try:
            kept = "-" if there is None else f"[{theme.TE}]yes[/]" if there.has(k) else f"[{theme.RD}]no[/]"
        except store.StoreError as err:
            fail(str(err))
        missing += kept.endswith("no[/]")
        tb.add_row(e["task"], e["digest"][:12], local, kept)
    console.print(tb)
    per: dict[str, int] = {}
    for e in entries:
        per[e["benchmark"]] = per.get(e["benchmark"], 0) + 1
    console.print(f"{len(entries)} accepted, from {len(per)} benchmark{'s' if len(per) != 1 else ''}: "
                  + ", ".join(f"{b} {n}" for b, n in sorted(per.items(), key=lambda x: -x[1])), highlight=False)
    cut = [s.task_id for s in ledger_mod.fold(ledger_mod.Ledger(ws).events).values() if s.status == "cut"]
    if cut:
        console.print(f"[{theme.MU}]Cut from the index: {', '.join(cut[:8])}{' and more' if len(cut) > 8 else ''}.[/]", highlight=False)
    console.print(f"[{theme.MU}]The owner or a lead makes the final cut with /tasks cut TASK <why>, and undoes one with /tasks restore TASK.[/]")
    console.print(f"[{theme.MU}]The store: {there.where if there else 'none named. The owner sets one with /tasks store ORG/NAME'}.[/]")
    if missing:
        console.print(f"[{theme.AM}]{missing} accepted task{'s are' if missing != 1 else ' is'} not in the store.[/] "
                      "Whoever has the files puts them there with: hm tasks push")


@tasks_app.command("push")
def tasks_push(workspace: Optional[Path] = Workspace):
    """Put every accepted task that this machine has into the store, at the accepted version."""
    ws = need_workspace(workspace)
    cfg = config_mod.load(ws)
    _remote(cfg)
    done, lacking = 0, []
    for e in _accepted(ws):
        benchmark, name = e["task"].split("/", 1)
        try:
            ok, how = store.put(ws, cfg, benchmark, name, e["digest"], _files(ws, e) or Path("/nonexistent"))
        except store.StoreError as err:
            fail(str(err))
        done += ok
        if not ok:
            lacking.append(e["task"])
        console.print(f"  {e['task']}: {how}", highlight=False)
    console.print(f"[{theme.TE}]{done} accepted task{'s' if done != 1 else ''} kept.[/]"
                  + (f" {len(lacking)} could not be: this machine does not have the accepted version of {', '.join(lacking[:4])}." if lacking else ""))
    if lacking:
        raise typer.Exit(1)


def _final(ws: Path, name: str, status: str, value: str, reason: str) -> None:
    """Sign one verdict of the final cut, rewrite the list of accepted tasks, and share both."""
    ident = need_identity()
    me = next((c for c in ledger_mod.load_contributors(ws) if c.get("github") == ident.github), None)
    if not ledger_mod.can_cut(me):
        fail(f"The final cut is made by the owner or a lead, and @{ident.github} is neither. The owner names a lead with: "
             "/contributors role HANDLE lead")
    cfg, sync = config_mod.load(ws), ledger_mod.Sync(ws, ident.name, ident.github)
    try:
        if sync.enabled:
            sync.pull()
    except ledger_mod.SyncError as err:
        fail(str(err))
    states = [s for s in ledger_mod.fold(ledger_mod.Ledger(ws).events).values()
              if s.status == status and (s.task_id == name or s.task_id.split("/", 1)[-1] == name)]
    if len(states) != 1:
        where = "in the index" if status == "accepted" else "among the tasks that were cut"
        fail(f"{name} is in more than one benchmark {where}. Name it as BENCHMARK/TASK." if states else
             f"{name} is not {where}. See: /tasks")
    s = states[0]
    benchmark, task = s.task_id.split("/", 1)
    ledger_mod.Shard(ws, benchmark, ident, ledger_mod.MANUAL, cfg.protocol).append(
        "verdict", s.task_id, s.digest, value=value, reason=reason, reason_code=value, reviewer=ident.github, tier="human")
    ledger_mod.write_index(ws)
    try:
        if value == "cut":
            how = store.drop(ws, cfg, benchmark, task, s.digest)
        else:
            ok, how = store.put(ws, cfg, benchmark, task, s.digest, _files(ws, {"task": s.task_id, "digest": s.digest}) or Path("/nonexistent"))
    except store.StoreError as err:
        how = f"[{theme.AM}]the store was not changed: {str(err)[:200]}[/]. Run this again to finish"
    try:
        if sync.enabled:
            sync.publish(f"{value} {s.task_id}")
    except ledger_mod.SyncError as err:
        fail(str(err))
    left = sum(1 for e in _accepted(ws))
    word = "Cut" if value == "cut" else "Restored"
    console.print(f"[{theme.TE}]{word} {s.task_id}[/], signed by @{ident.github}" + (" and shared" if sync.enabled else "")
                  + f". Store: {how}. {left} task{'s are' if left != 1 else ' is'} in the index now.", highlight=False)


@tasks_app.command("cut")
def tasks_cut(task: str = typer.Argument(..., help="The accepted task to take out of the index."),
              reason: list[str] = typer.Argument(None, help="Why, in a few words."), workspace: Optional[Path] = Workspace):
    """Owner or lead: the final cut. Take an accepted task out of the index, its list and its store."""
    ws = need_workspace(workspace)
    if not reason:
        fail(f"Give a reason: /tasks cut {task} <why>")
    state = next((s for s in ledger_mod.fold(ledger_mod.Ledger(ws).events).values()
                  if s.status == "cut" and (s.task_id == task or s.task_id.split("/", 1)[-1] == task)), None)
    if state is not None:           # cut already: only the store may still hold it, after a failed removal
        try:
            how = store.drop(ws, config_mod.load(ws), *state.task_id.split("/", 1), state.digest)
        except store.StoreError as err:
            fail(str(err))
        console.print(f"{state.task_id} was cut already. Store: {how}.", highlight=False)
        return
    _final(ws, task, "accepted", "cut", " ".join(reason))


@tasks_app.command("restore")
def tasks_restore(task: str = typer.Argument(..., help="A task that was cut."), workspace: Optional[Path] = Workspace):
    """Owner or lead: put a task that was cut back in the index, its list and its store."""
    _final(need_workspace(workspace), task, "cut", "restore", "")


@tasks_app.command("pull")
def tasks_pull(workspace: Optional[Path] = Workspace):
    """Bring the accepted version of every accepted task onto this machine, from the store."""
    ws = need_workspace(workspace)
    cfg = config_mod.load(ws)
    got, lacking = 0, []
    for e in _accepted(ws):
        benchmark, name = e["task"].split("/", 1)
        try:
            path = store.fetch(ws, cfg, benchmark, name, e["digest"])
        except store.StoreError as err:
            fail(str(err))
        got += path is not None
        if path is None:
            lacking.append(e["task"])
    console.print(f"[{theme.TE}]{got} accepted task{'s' if got != 1 else ''} on this machine, each checked against its digest.[/]"
                  + (f" Not in the store: {', '.join(lacking[:6])}." if lacking else ""))
    if lacking:
        raise typer.Exit(1)
