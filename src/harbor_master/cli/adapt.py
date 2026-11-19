"""Building the adapter for a benchmark that is not in Harbor format."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import typer

from .. import ledger as ledger_mod
from .. import tasks as tasks_mod
from ..core import config as config_mod
from ..backends import NotImplementedBackend
from ..engine import prepare
from ..tasks import adapter as adapter_mod
from ..tui import theme
from .common import Workspace, app, console, fail, is_owner, need_identity, need_workspace

def source_of(ws: Path, entry: dict) -> Path:
    """The benchmark at its pinned release, fetched if it is not here yet."""
    try:
        return tasks_mod.supported.fetch(entry, config_mod.local(ws) / "sources")
    except (ValueError, subprocess.CalledProcessError) as err:
        fail(str(getattr(err, "stderr", "") or err))


@app.command()
def adapt(name: str = typer.Argument(..., help="A benchmark on this index's list whose format is not harbor."),
          from_: Optional[Path] = typer.Option(None, "--from", help="Take an adapter that exists already, such as one written for "
                                                                    "Harbor, instead of having a model write one."),
          rewrite: bool = typer.Option(False, help="Have the model write the adapter again, over the one that is there."),
          rounds: Optional[int] = typer.Option(None, help="How many times a model may write it. Default: the protocol's [adapt] max_rounds."),
          oracle: Optional[int] = typer.Option(None, help="How many generated tasks to run the reference solution of, through Harbor. "
                                                          "0 skips it. Default: the protocol's [adapt] oracle_tasks."),
          no_sandbox: bool = typer.Option(False, "--no-sandbox", help="Run the adapter's code on this machine as it is, not in a "
                                                                    "container. Only for code you have read."),
          workspace: Optional[Path] = Workspace):
    """Owner only: build the adapter for a benchmark in another format, and check it. A run does this by itself:
    this command is for doing it apart from a run, or again."""
    ws, ident = need_workspace(workspace), need_identity()
    if not is_owner(ws, ident):
        fail(f"An adapter decides what the tasks are for everyone, so only the owner builds one, and @{ident.github} is not the owner.")
    entry = tasks_mod.supported.find(ws, name)
    if entry is None:
        fail(f"{name} is not on this index's list of benchmarks. See: hm benchmarks")
    if entry.get("format", "harbor") == "harbor":
        fail(f"{entry['id']} is in Harbor format already, so it needs no adapter. Fetch it with: hm submit {entry['id']}")
    cfg = config_mod.load(ws)
    if entry["id"] in ledger_mod.abandoned(ledger_mod.Ledger(ws).events):
        console.print(f"[{theme.AM}]{entry['id']} was abandoned.[/] An adapter that passes now reopens it.")
    source = source_of(ws, entry)
    adapter_dir = adapter_mod.folder(ws, entry["id"])
    work = config_mod.local(ws) / "adapted" / entry["id"]
    if from_ is not None:
        if adapter_mod.code_dir(from_) is None:
            fail(f"{from_} is not an adapter: it has no src/<package>/adapter.py.")
        if adapter_dir.exists():
            shutil.rmtree(adapter_dir)
        shutil.copytree(from_, adapter_dir, ignore=shutil.ignore_patterns("__pycache__", ".git", ".venv"))
    pkg = adapter_mod.code_dir(adapter_dir) or adapter_mod.scaffold(adapter_dir, entry)
    by_model = from_ is None and (rewrite or prepare.STUB in (pkg / "adapter.py").read_text())
    oracle = cfg.adapt.oracle_tasks if oracle is None else oracle
    try:
        if by_model:        # the worker model first, then the frontier model, each told what the checks found
            built = asyncio.run(prepare.write_and_check(
                cfg, adapter_dir, entry, source, work, sandbox=not no_sandbox, oracle=oracle, rounds=rounds,
                say=lambda text: console.print(f"[{theme.MU}]{text[0].upper()}{text[1:]} ...[/]")))
        else:
            built = prepare.Built()
            built.problems, built.notes, built.out = asyncio.run(prepare.check(adapter_dir, source, work, entry, not no_sandbox, oracle))
    except (RuntimeError, NotImplementedBackend) as err:
        fail(f"The model did not return an adapter: {err}")
    n = built.tasks
    for p in built.problems:
        console.print(f"[{theme.RD}]x[/] {p}", highlight=False)
    for note in [*built.notes, *([f"The model's notes: {built.said}"] if built.said else [])]:
        console.print(f"[{theme.AM}]![/] {note}", highlight=False)
    if built.cost:
        console.print(f"[{theme.MU}]Writing it cost ${built.cost:.2f} ({built.tokens:,} tokens).[/]")
    if built.problems:
        fail(f"The adapter for {entry['id']} is in {adapter_dir} and does not pass yet. Correct it by hand and run hm adapt {entry['id']} "
             "again, or have the models write it again with --rewrite.")
    shard = ledger_mod.Shard(ws, entry["id"], ident, ledger_mod.MANUAL, cfg.protocol)
    prepare.record(shard.append, cfg, adapter_dir, built, oracle)
    console.print(f"[{theme.TE}]The adapter for {entry['id']} passes:[/] {n} tasks, the same on a second run"
                  + (f", and the reference solution of {min(oracle, n)} passes through Harbor" if oracle else "") + ".")
    console.print(f"[{theme.MU}]It is in {adapter_dir}. Read it, then lock it with the rest: hm protocol lock. "
                  f"After that, everyone gets the same tasks with: hm submit {entry['id']}[/]")
