"""Where `hm` starts: the home screen, the guide it shows, and a demo to try it on."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click
import typer
from rich.text import Text

from .. import tasks as tasks_mod
from ..core import config as config_mod
from ..interact import guide as guide_mod
from ..tui import theme
from .common import Workspace, app, console, fail, need_identity


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


def run_cli(args: list[str], width: int) -> tuple[int, Text]:
    """Run `hm ARGS` and hand back what it printed, at the width of the panel that will show it."""
    before = console._width
    console.width = width
    try:
        with console.capture() as captured:
            code = invoke(args)
    finally:
        console._width = before
    return code, Text.from_ansi(captured.get().rstrip())


def open_home(start: Path) -> None:
    """The home screen, and from it the run screen and back, until the user leaves."""
    from ..tui.home import HomeApp
    ws, note = None, None
    while True:
        result = HomeApp(run_cli, start, ws, note).run()
        if result is None:
            return
        args, ws = result
        with console.capture() as captured:      # the run screen draws by itself. What the command prints is kept for home
            invoke(["run", *args, "-w", str(ws)])
        note = ("hm run " + " ".join(args), Text.from_ansi(captured.get().rstrip()))


@app.callback(invoke_without_command=True)
def start(ctx: typer.Context):
    """With no command, open the home screen: it shows the steps and what to do next."""
    if ctx.invoked_subcommand is not None:
        return
    if sys.stdout.isatty() and sys.stdin.isatty():
        open_home(Path.cwd())
    else:
        console.print(guide_mod.text(guide_mod.look()), markup=False, highlight=False)
        console.print(f"[{theme.MU}]Every command: hm --help[/]")


@app.command()
def home():
    """Open the home screen: the steps, what to do next, and the commands, in one place."""
    open_home(Path.cwd())


@app.command()
def guide(workspace: Optional[Path] = Workspace):
    """Print the steps and what to do next, as text."""
    console.print(guide_mod.text(guide_mod.look(workspace)), markup=False, highlight=False)


@app.command()
def whoami():
    """Print who you are, and the line to send the owner of an index so that they can add you."""
    ident = need_identity()
    console.print(ident.label(), markup=False, highlight=False)
    console.print(f"/contributors add {ident.github} {ident.public_key}", markup=False, highlight=False, soft_wrap=True)


@app.command()
def demo(count: int = typer.Option(40, help="How many made-up tasks."), workspace: Optional[Path] = Workspace):
    """Try it on made-up tasks: adds a synthetic benchmark for simulated runs. Outside an index, starts one called hm-demo first."""
    from .dev import fake_benchmark
    from .setup import index_new
    need_identity()
    ws = workspace or config_mod.find_workspace()
    if ws is None:
        ws = Path("hm-demo").resolve()
        if not ws.exists():
            index_new("hm-demo", ws, "Demo index", trial=True)
        elif not (ws / config_mod.PROTOCOL_NAME).exists():
            fail(f"{ws} exists and is not an index. Remove it, or run this inside an index.")
    ws = ws.resolve()
    name = "demo-bench"
    if name in tasks_mod.read_registry(ws):
        console.print(f"{name} is already registered in {ws}. Run it with: hm run --simulate")
        return
    root = config_mod.local(ws) / "fake"
    with console.capture():                     # its own message names a path nobody needs
        fake_benchmark(name, count, directory=root, seed=0)
    tasks_mod.register(ws, name, root / name, dev=True)
    console.print(f"Registered [bold]{name}[/] in {ws}: {count} made-up tasks, for simulated runs only. "
                  "Next: hm run --simulate. It stops twice to ask you something, and says how to answer.")
