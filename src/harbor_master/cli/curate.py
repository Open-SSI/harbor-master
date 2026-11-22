"""Stage 1, curation: the benchmarks an index may curate, and hm curate, which runs them."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

import typer

from .. import ledger as ledger_mod
from .. import tasks as tasks_mod
from ..core import config as config_mod
from ..tui import theme
from .common import Workspace, app, check_index, console, fail, need_identity, need_workspace


benchmarks_app = typer.Typer(invoke_without_command=True, help="The benchmarks this index may curate, and adding one.")
app.add_typer(benchmarks_app, name="benchmarks")


@benchmarks_app.callback()
def benchmarks(ctx: typer.Context, workspace: Optional[Path] = Workspace):
    """The benchmarks this index may curate. Anyone on the contributor list adds one with hm benchmarks add URL: an agent
    drafts its entry, hm benchmarks revise changes it, and hm benchmarks confirm adds it, signed."""
    if ctx.invoked_subcommand:
        return
    from .views import benchmarks as show
    show(need_workspace(workspace))
    console.print(f"[{theme.MU}]A run works only on these. Anyone on the contributor list adds one: hm benchmarks add URL[/]")

def _show(entry: dict) -> None:
    """The draft, with what it still lacks and how to answer."""
    from ..tasks import adding
    console.print(f"[bold]{entry.get('name') or entry['id']}[/]  [{theme.MU}]{entry['id']}, not added yet[/]", highlight=False)
    from rich.markup import escape
    rows = []
    for key, value in adding.lines(entry):
        if key == "released" and value and value == entry.get("pinned_on"):
            value += "  (the date of the pinned commit: give the real one if it came out earlier)"
        rows.append((key, escape(value)))
    console.print(adding.table(rows, theme.MU, theme.AM, "not known", indent=2), highlight=False)
    lacks = [k for k in adding.lacking(entry) if k in ("name", "description", "paper")]
    if lacks:
        console.print(f"[{theme.AM}]Still needed: {', '.join(lacks)}.[/] Give it with hm benchmarks revise, such as "
                      "hm benchmarks revise paper: https://..." + (" (paper: none when it has none)." if "paper" in lacks else "."), highlight=False)
    else:
        console.print(f"[{theme.TE}]Add it, signed with your key: hm benchmarks confirm.[/] To change something first: hm benchmarks "
                      "revise, then what to change, in plain words or as description: ...", highlight=False)


def _clash(ws: Path, name: str, github: str) -> None:
    from ..tasks import adding
    try:
        adding.clash(ws, name, github)
    except adding.AddError as err:
        fail(str(err))


@benchmarks_app.command("add")
def benchmarks_add(url: str = typer.Argument(..., help="A link to the benchmark: its repository in any form, its paper, its page, or its name."),
                   ref: str = typer.Option("", help="The tag or branch to pin. Default: the newest release."),
                   id_: str = typer.Option("", "--id", help="Its short name here. Default: from the repository's name."),
                   agent: bool = typer.Option(True, "--agent/--no-agent", help="Have the worker agent search the web for it. "
                                              "Without, URL must be the repository's own address, and only the code reads it."),
                   workspace: Optional[Path] = Workspace):
    """Draft a benchmark's entry, to confirm or revise: the worker agent searches the web for its repository and what is
    known about it, and the code downloads it and finds its tasks. Anyone on the contributor list may add one. The curate
    screen does the same in a box, when it starts."""
    from ..tasks import adding
    ws, ident = need_workspace(workspace), need_identity()
    try:
        if agent:
            console.print("The worker agent searches the web for it ...", highlight=False)
            found, _ = asyncio.run(adding.find(ws, ident, url))
            entry = adding.from_found(ws, ident, {**found, **({"release": ref} if ref else {})}, id_)
            if found.get("sources"):
                console.print(f"[{theme.MU}]Found from: {' · '.join(found['sources'])}[/]", highlight=False)
        else:
            entry = adding.draft(ws, ident, url, ref, id_)
    except adding.AddError as err:
        fail(str(err))
    _show(entry)


@benchmarks_app.command("revise")
def benchmarks_revise(words: list[str] = typer.Argument(..., help="What to change, in plain words or as `paper: URL`."),
                      workspace: Optional[Path] = Workspace):
    """Change the benchmark entry that is being added."""
    from ..tasks import adding
    ws = need_workspace(workspace)
    entry, text = adding.kept(ws), " ".join(words)
    if entry is None:
        fail("No benchmark is being added. Start with: hm benchmarks add URL")
    if not adding.direct(ws, entry, text):
        try:
            entry, _ = asyncio.run(adding.refine(ws, need_identity(), entry, text))
        except adding.AddError as err:
            fail(f"{err} Or write it as `paper: https://...` or `description: ...`.")
    _show(entry)


@benchmarks_app.command("confirm")
def benchmarks_confirm(workspace: Optional[Path] = Workspace):
    """Check the drafted benchmark as a whole, with the cheap benchmark-level checks, and add it to this index's list: a
    record signed with your key. A benchmark that a check rejects is not added."""
    from rich.markup import escape

    from ..tasks import adding
    from ..tasks import checks as checks_mod
    ws, ident = need_workspace(workspace), need_identity()
    entry = adding.kept(ws)
    if entry is None:
        fail("No benchmark is being added. Start with: hm benchmarks add URL")
    if adding.lacking(entry):
        fail(f"Not added yet: it still needs {', '.join(adding.lacking(entry))}.")
    cfg = config_mod.load(ws)
    source = adding.source_of(ws, entry)
    found = checks_mod.run(entry, source if source.is_dir() else None, cfg)
    look = {"pass": ("✓", theme.TE), "warn": ("!", theme.AM), "fail": ("✗", theme.RD), "unknown": ("?", theme.MU)}
    for c in found:
        mark, color = look[c.status]
        console.print(f"  [{color}]{mark}[/] {c.label:<38} [{color if c.status in ('fail', 'warn') else theme.MU}]{escape(c.detail)}[/]",
                      highlight=False)
    failed = [c for c in found if c.failed]
    if failed:
        fail(f"Not added: its checks reject it. {checks_mod.why(found)}")
    from ..ledger.store import MANUAL
    ledger_mod.Shard(ws, entry["id"], ident, MANUAL, cfg.protocol).append(      # first, so that it is shared with the entry
        "decision", "", checks_mod.key(entry), stage="benchmark", gate="checks", tier="code",
        value=checks_mod.summary(found), outcome="pass", checks={c.key: [c.status, c.detail[:240]] for c in found})
    try:
        entry = adding.confirm(ws, ident, entry)
    except adding.AddError as err:
        fail(f"Not added yet: {err}")
    sync = ledger_mod.Sync(ws, ident.name, ident.github)
    console.print(f"[{theme.TE}]Added {entry['id']}[/] to this index, signed by @{ident.github}"
                  + (" and shared." if sync.enabled else ".") + " The list is in benchmarks.toml. Curate it with: hm curate",
                  highlight=False)


@benchmarks_app.command("remove")
def benchmarks_remove(name: str, reason: str = typer.Option("", help="Why it is taken off."), workspace: Optional[Path] = Workspace):
    """Take a benchmark off this index's list. Only the person who added it, or the owner, can."""
    from .common import is_owner
    ws, ident = need_workspace(workspace), need_identity()
    entry = tasks_mod.supported.find(ws, name)
    if entry is None:
        fail(f"{name} is not on this index's list. See: hm benchmarks")
    if entry.get("added_by") != ident.github and not is_owner(ws, ident):
        fail(f"{entry['id']} was added by @{entry.get('added_by', '?')}. Only they, or the owner, can take it off.")
    sync = ledger_mod.Sync(ws, ident.name, ident.github)
    ledger_mod.benchmarks.remove(ws, ident, config_mod.load(ws).protocol, entry["id"], reason)
    ledger_mod.benchmarks.write_list(ws)
    if sync.enabled:
        try:
            sync.publish(f"remove benchmark {entry['id']}")
        except ledger_mod.SyncError as err:
            fail(str(err))
    console.print(f"Took {entry['id']} off this index's list. What was recorded about its tasks stays in the ledger.", highlight=False)


@app.command()
def curate(benchmarks: list[str] = typer.Argument(None, help="Benchmarks to run. Default: all registered."),
        simulate: bool = typer.Option(False, help="Use simulated models and trials. Spends nothing."),
        demo: bool = typer.Option(False, help="Try it: a made-up benchmark of 10 tasks, simulated. Spends nothing, and nothing enters the index."),
        speed: float = typer.Option(1.0, help="Simulation only: scale the pretend durations. 0 is instant."),
        cells: int = typer.Option(None, help="Tasks in flight at once."),
        plain: bool = typer.Option(False, help="Print lines, with no full-screen view."),
        no_input: bool = typer.Option(False, help="Never prompt. Skip what needs an answer and record it."),
        only: str = typer.Option("", "--tasks", help="Run only these tasks of the benchmark: names separated by commas."),
        until: str = typer.Option("", help="Stop after this point: select (screen and rank only), verify, or probe. Default: all stages."),
        assume_after: str = typer.Option("", help="Run stages up to this one for real (verify or probe) and assume "
                                         "the rest. Assumed results cost nothing, are marked, and cannot be accepted."),
        foreground: bool = typer.Option(False, "--foreground", hidden=True),      # set by the relay: this process is the run itself
        workspace: Optional[Path] = Workspace):
    """Stage 1, building the index: run its benchmarks through the stages, to the reports a reviewer signs. It opens the
    curate screen, which asks what it needs in boxes. The run goes on when its terminal closes: hm curate inside the
    index's folder shows it again. There is one run per index."""
    from .. import host
    from ..backends import NotImplementedBackend
    from ..engine import Session
    from ..tui import render, run_app, run_plain
    ws, ident = need_workspace(workspace), need_identity()
    if demo:
        from .home import demo_bench, demo_world, people_dir
        if people_dir(ws).is_dir() and not foreground:
            console.print("The other contributors' part of the demo is made up first ...", highlight=False)
            with console.capture():
                demo_world(ws)
        demo_bench(ws)
        simulate = True
    plain = plain or not (sys.stdout.isatty() and sys.stdin.isatty())
    if demo and plain and not benchmarks:       # with no screen to pick in, the demo's own benchmark
        benchmarks = ["demo-bench"]
    ask_first = not benchmarks and not plain    # on the screen, with none named: its first box asks which one
    going = None if foreground else host.active(ws)
    if going is not None:           # one run per index: join the one that is going
        if plain or not going.get("socket"):
            fail(f"A run is going in this index already: {host.describe(going)}. There is one run per index. "
                 + ("Open it in a terminal with: hm curate" if going.get("socket") else "It has no screen: wait for it to end."))
        if going.get("simulate") != simulate and (benchmarks or simulate):
            fail(f"The run that is going is {host.describe(going)}. A {'simulated' if simulate else 'real'} one cannot join it: "
                 "end that run first, with ctrl+z on its screen.")
        for b in benchmarks or []:
            reply = host.tell(ws, {"add": [b]})
            console.print(reply["text"], highlight=False)
            if not reply["ok"]:
                raise typer.Exit(1)
        _view(ws)
        return
    cfg = config_mod.load(ws)
    if cells:
        cfg.run.cells = cells
    registry = tasks_mod.read_registry(ws)
    listed = {e["id"]: e for e in tasks_mod.supported.supported(ws)}
    wanted = [] if ask_first else benchmarks or [*listed, *(b for b in registry if b not in listed and (simulate or registry[b].get("supported")))]
    resolved = {}
    for b in wanted:
        info, why = tasks_mod.resolve(ws, b, simulate)
        if info is None:
            fail(why)
        resolved[b] = info
    if not resolved and not ask_first:
        fail("Nothing to run: this index lists no benchmark yet. Add one with hm benchmarks add URL, or try made-up tasks with hm curate --demo")
    check_index(cfg, ws)
    if not simulate:
        from .common import is_contributor
        if not is_contributor(ws, ident):
            console.print(f"[{theme.RD}]@{ident.github} is not on this index's contributor list, so cannot curate in it.[/] "
                          "Send its owner this line. They paste it in a shell, inside the index's folder:", highlight=False)
            console.print(ident.line(), markup=False, highlight=False, soft_wrap=True)
            raise typer.Exit(1)
    if not simulate and not foreground:        # the keys first: nothing can be spent without them
        from .common import curate_keys, need_keys
        need_keys(lambda: curate_keys(cfg), "A real curation")
    if not simulate:
        from ..tasks import store
        why = store.not_ready(cfg)          # an accepted task must reach the store, so this is asked before anything is spent
        if why:
            fail(f"Refused: {why}")
    if not simulate and not foreground:     # every part tried once, before anything is claimed or spent
        from ..core import access
        console.print("Checking, before anything is spent: your keys, the models, Docker, and each agent in a container ...",
                      highlight=False)

        def said(what: str, why: str, seconds: float) -> None:
            mark = f"[{theme.RD}]✗[/]" if why else f"[{theme.TE}]✓[/]"
            console.print(f"  {mark} {what}  [{theme.MU}]{seconds:.0f} s[/]", highlight=False)
        missing = access.check(cfg, ws, said)
        if missing:
            fail("Nothing was started:\n" + "\n".join(f"- {p}" for p in missing))
    if until not in ("", "select", "verify", "probe") or assume_after not in ("", "verify", "probe"):
        fail("--until takes select, verify or probe. --assume-after takes verify or probe.")
    command = "hm curate " + " ".join([b.split("/")[-1] for b in resolved] + (["--simulate"] if simulate else [])
                                   + ([f"--assume-after {assume_after}"] if assume_after else []))
    if not plain and not foreground:        # everything checks out: start the run behind a relay, and view it from here
        argv = [*(benchmarks or []), *(["--simulate"] if simulate else []), "--speed", str(speed),
                *(["--cells", str(cells)] if cells else []), 
                *(["--no-input"] if no_input else []), *(["--tasks", only] if only else []),
                *(["--until", until] if until else []), *(["--assume-after", assume_after] if assume_after else [])]
        try:
            host.start(ws, argv, command, simulate)
        except RuntimeError as err:
            fail(str(err))
        _view(ws)
        return
    try:
        session = Session(ws, cfg, ident, resolved, simulate=simulate, speed=speed,
                          no_input=no_input or plain, command=command, until=until,
                          assume_after=assume_after, only=[t.strip() for t in only.split(",") if t.strip()])
    except NotImplementedBackend as err:
        fail(str(err))
    mark = None if foreground else host.claim(ws, command, simulate)        # a run without a screen is the one run too
    try:
        if plain:
            asyncio.run(run_plain(session, console))
        else:
            run_app(session)
            console.print(render.final_table(session))
    finally:
        if mark is not None:
            mark.unlink(missing_ok=True)
    console.print(f"[{theme.MU}]Run {session.run_id}: {session.run_dir}[/]")
    if any(r.blocking for r in session.requests):     # something blocked part of the run
        raise typer.Exit(3)


def _view(ws: Path) -> None:
    """Show the run that is going, and say where it stands when the screen is left."""
    from .. import host
    state = host.attach(ws)
    if state == "going":
        here = Path.cwd().resolve() == ws.resolve()
        console.print(f"You left the screen. [{theme.TE}]The run goes on.[/] To get back to it: "
                      + ("[bold]hm curate[/]" if here else f"[bold]cd {ws}[/], then [bold]hm curate[/]"), highlight=False)
    else:
        console.print("The run has ended. hm curate shows it again, and starts a new one.", highlight=False)
