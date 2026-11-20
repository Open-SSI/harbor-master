"""The task store: where the accepted tasks are kept, and bringing copies in and out of it."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .. import ledger as ledger_mod
from .. import tasks as tasks_mod
from ..core import config as config_mod
from ..tasks import store
from ..tui import theme
from .common import Workspace, app, console, fail, need_identity, need_workspace



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
        why = there.writable()      # every contributor must be able to put an accepted task there, and get one back
        if why:
            console.print(f"[{theme.AM}]{why}[/] Give the token that access, then run hm lock again.", highlight=False)
        return not why
    except store.StoreError as err:
        console.print(f"[{theme.AM}]The task store is named and not created yet:[/] {err}\nhm lock creates it once that is fixed.",
                      highlight=False)
        return False


def _final(ws: Path, name: str, status: str, value: str, reason: str) -> None:
    """Sign one verdict of the final cut, rewrite the list of accepted tasks, and share both."""
    ident = need_identity()
    me = next((c for c in ledger_mod.load_contributors(ws) if c.get("github") == ident.github), None)
    if not ledger_mod.can_cut(me):
        fail(f"The final cut is made by the owner or a lead, and @{ident.github} is neither. The owner names a lead with: "
             "hm contributors role HANDLE lead")
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


@app.command("cut")
def tasks_cut(task: str = typer.Argument(..., help="The accepted task to take out of the index."),
              reason: list[str] = typer.Argument(None, help="Why, in a few words."), workspace: Optional[Path] = Workspace):
    """Owner or lead: the final cut. Take an accepted task out of the index, its list and its store."""
    ws = need_workspace(workspace)
    if not reason:
        fail(f"Give a reason: hm cut {task} <why>")
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


@app.command("restore")
def tasks_restore(task: str = typer.Argument(..., help="A task that was cut."), workspace: Optional[Path] = Workspace):
    """Owner or lead: put a task that was cut back in the index, its list and its store."""
    _final(need_workspace(workspace), task, "cut", "restore", "")
