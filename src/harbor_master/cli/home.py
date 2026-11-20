"""hm alone, running an hm command in place, and the demos that hm init, hm curate --demo and hm eval --demo start."""
from __future__ import annotations

from pathlib import Path

import click
import typer

from .. import tasks as tasks_mod
from ..core import config as config_mod
from ..tui import theme
from .common import app, console, need_identity


def invoke(args: list[str]) -> int:
    """Run `hm ARGS` in this process, and return its exit code."""
    try:
        code = typer.main.get_command(app).main(args=args, prog_name="hm", standalone_mode=False)
    except click.exceptions.Exit as err:
        code = err.exit_code
    except click.ClickException as err:        # a wrong option or a missing argument
        console.print(f"[{theme.RD}]{err.format_message()}[/]")
        code = 2
    except click.Abort:
        code = 1
    return code if isinstance(code, int) else 0


def _version(asked: bool) -> None:
    if asked:
        from .. import __version__
        console.print(f"hm {__version__}", highlight=False)
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def start(ctx: typer.Context,
          version: bool = typer.Option(False, "--version", "-V", callback=_version, is_eager=True, help="Show the version and exit.")):
    """hm alone does nothing: it says which commands there are."""
    if ctx.invoked_subcommand is None:
        from . import USAGE
        console.print(USAGE, markup=False, highlight=False, soft_wrap=True)
        raise typer.Exit(2)


def demo_bench(ws: Path, count: int = 10) -> None:
    """The made-up benchmark of the demos, added to this index for simulated runs only. Nothing of it enters the index."""
    from .dev import fake_benchmark
    name = "demo-bench"
    if name not in tasks_mod.read_registry(ws):
        root = config_mod.local(ws) / "fake"
        fake_benchmark(name, count, directory=root, seed=7)
        tasks_mod.register(ws, name, root / name, dev=True)


def demo_eval(ws: Path) -> None:
    """What the eval demo starts from: the made-up benchmark, curated in a simulated run with its tasks accepted, so
    that there is something to evaluate. Simulated, on this machine only."""
    import asyncio

    from .. import interact
    from .. import ledger as ledger_mod
    from ..engine import Session
    root = config_mod.simulated(ws)
    if ledger_mod.index_entries(ledger_mod.Ledger(root, simulated=True).events):
        return
    demo_bench(ws)
    cfg = config_mod.load(ws)
    cfg.select.require_brief = cfg.select.require_signoff = False        # nobody is asked anything: this only makes tasks to evaluate
    s = Session(ws, cfg, need_identity(), {"demo-bench": tasks_mod.read_registry(ws)["demo-bench"]}, simulate=True, speed=0, no_input=True)
    s.is_reviewer = True
    asyncio.run(s.run())
    for task in list(s.ready):
        interact.decide(s, task, "accept for the demo")
