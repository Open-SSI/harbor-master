"""hm alone, running an hm command in place, and the demo that hm curate --demo and hm view --demo start."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import click
import typer

from .. import tasks as tasks_mod
from ..core import config as config_mod
from ..tui import theme
from .common import app, console


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


# ----- the demos -----
#
# demo-index is made with three made-up contributors beside its owner, and made-up benchmarks at every point a
# benchmark can stand: curated, curating (held by someone now), dropped, and listed (nobody has started it), so that
# the person's own demo sits among other people's work, and the curate screen's first box shows each. Their keys are
# made on this machine, in the index's .hm folder, which git ignores.

PEOPLE = (("Grace Hopper", "Example Institute of Technology", "grace", "lead"),
          ("Alan Turing", "Example University", "alan", ""),
          ("Katherine Johnson", "Example Research Center", "katherine", ""))
# (benchmark, tasks, who, where it is left): curated runs every stage; curating runs to verify and holds the claim;
# dropped is given up before curating, as when no adapter can be made.
BENCHES = (("solubility-bench", 24, "grace", "curated"), ("docking-bench", 18, "alan", "curating"),
           ("spectra-bench", 16, "katherine", "dropped"))
LISTED = (("yield-bench", 12),)     # beside demo-bench: listed, and nobody has started them
DROPPED_WHY = "no adapter could read its vendor files"


def made_up_repo(name: str) -> str:
    """The made-up repository of a made-up benchmark: pasting it in the curate screen's first box finds it on the list."""
    return f"https://github.com/example/{name}"


def people_dir(ws: Path) -> Path:
    return config_mod.local(ws) / "demo-people"


@contextmanager
def acting_as(ws: Path, handle: str):
    """Sign as one of the made-up people: their identity and key, for as long as this lasts."""
    import os

    from ..core import identity as identity_mod
    before = os.environ.get("HM_CONFIG_DIR")
    os.environ["HM_CONFIG_DIR"] = str(people_dir(ws) / handle)
    try:
        yield identity_mod.load()
    finally:
        if before is None:
            os.environ.pop("HM_CONFIG_DIR", None)
        else:
            os.environ["HM_CONFIG_DIR"] = before


def demo_people(ws: Path) -> list[dict]:
    """Make the made-up people's keys, and return their entries for the contributor list."""
    import time

    from ..core import identity as identity_mod
    out = []
    for name, affiliation, handle, role in PEOPLE:
        with acting_as(ws, handle):
            who = identity_mod.create(name, affiliation, f"{handle}@example.org", handle)
        out.append({"name": name, "affiliation": affiliation, "email": who.email, "github": handle, "key": who.public_key,
                    **({"role": role} if role else {}), "added": time.strftime("%Y-%m-%d")})
    return out


def _bench(ws: Path, name: str, count: int, seed: int) -> None:
    from .dev import fake_benchmark
    if name not in tasks_mod.read_registry(ws):
        root = config_mod.local(ws) / "fake"
        fake_benchmark(name, count, directory=root, seed=seed)
        tasks_mod.register(ws, name, root / name, dev=True, github=made_up_repo(name))


def _session(ws: Path, ident, bench: str, until: str = ""):
    from ..engine import Session
    cfg = config_mod.load(ws)
    cfg.select.require_brief = cfg.select.require_signoff = False
    return Session(ws, cfg, ident, {bench: tasks_mod.read_registry(ws)[bench]}, simulate=True, speed=0, no_input=True, until=until)


def _curate(ws: Path, ident, bench: str, verdicts, until: str = "") -> None:
    """A simulated curation of one benchmark, with nobody asked, and `verdicts(n)` deciding each report: None leaves it
    to review. With `until`, each task stops after that stage, as a run that is still going."""
    import asyncio

    from .. import interact
    s = _session(ws, ident, bench, until)
    s.is_reviewer = True
    asyncio.run(s.run())
    for n, task in enumerate(list(s.ready)):
        said = verdicts(n)
        if said:
            interact.decide(s, task, said)


def _hold(ws: Path, ident, bench: str) -> None:
    """A claim on the whole benchmark, as a run that is going holds it, for as long as a claim lasts."""
    import time

    from ..engine.claims import BENCH
    s = _session(ws, ident, bench)
    s.record(bench, "claim", tasks=[BENCH], expires=time.time() + s.cfg.run.claim_hours * 3600)


def _drop(ws: Path, ident, bench: str) -> None:
    """The benchmark given up before curating, as when no adapter can be made: it stays on the list as dropped."""
    _session(ws, ident, bench).record(bench, "decision", digest_override="none", stage="adapt", gate="adapter", tier="frontier",
                                      value="abandoned", rounds=3, outcome="drop", reason=DROPPED_WHY)


def demo_world(ws: Path) -> None:
    """The made-up people's part of the demo: one benchmark curated with some reports still to review, one being curated
    now, one dropped, and one that nobody has started. Made once, and only where the people exist. The claim of the
    one being curated is taken again whenever it has run out, so that it is still being curated when the demo is
    opened days later."""
    from .. import ledger as ledger_mod
    if not people_dir(ws).is_dir():
        return
    root = config_mod.simulated(ws)
    events = ledger_mod.Ledger(root, simulated=True).events
    started = {e.get("benchmark") for e in events if e["type"] in ("claim", "verdict", "decision")}
    for n, (bench, count, handle, left) in enumerate(BENCHES):
        _bench(ws, bench, count, seed=11 + n)
        with acting_as(ws, handle) as who:
            if bench not in started:
                if left == "dropped":
                    _drop(ws, who, bench)
                else:
                    _curate(ws, who, bench, lambda k: None if k % 4 == 3 else "reject the tolerance looks arbitrary" if k % 5 == 4
                            else "accept clean and hard", until="verify" if left == "curating" else "")
            if left == "curating":
                now = ledger_mod.Ledger(root, simulated=True).events
                if ledger_mod.benchmarks.statuses(now, [bench])[bench]["status"] != "curating":
                    _hold(ws, who, bench)
    for n, (bench, count) in enumerate(LISTED):
        _bench(ws, bench, count, seed=31 + n)


def demo_bench(ws: Path, count: int = 10) -> None:
    """The made-up benchmark of the demos, added to this index for simulated runs only. Nothing of it enters the index."""
    from .dev import fake_benchmark
    name = "demo-bench"
    if name not in tasks_mod.read_registry(ws):
        root = config_mod.local(ws) / "fake"
        fake_benchmark(name, count, directory=root, seed=7)
        tasks_mod.register(ws, name, root / name, dev=True, github=made_up_repo(name))
