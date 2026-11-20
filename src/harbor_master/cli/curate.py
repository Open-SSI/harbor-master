"""Stage 1, curation: the benchmarks an index may curate, and hm curate, which runs them."""
from __future__ import annotations

import asyncio
import subprocess
import json
import sys
from pathlib import Path
from typing import Optional

import typer

from .. import ledger as ledger_mod
from .. import tasks as tasks_mod
from ..core import config as config_mod
from ..tui import theme
from .common import Workspace, app, check_lock, console, fail, need_identity, need_workspace


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

DRAFT = "benchmark-draft.json"      # in .hm: the entry being added, until it is confirmed


def _may_add(ws: Path, ident) -> None:
    from .common import is_contributor
    if not is_contributor(ws, ident):
        fail(f"@{ident.github} is not on this index's contributor list yet, so cannot add a benchmark to it. See: hm contributors")


def _clash(ws: Path, name: str, github: str) -> None:
    """Refuse a benchmark that is on the list already, by its ID or by its repository."""
    same = lambda a, b: a.lower().rstrip("/").removesuffix(".git") == b.lower().rstrip("/").removesuffix(".git")  # noqa: E731
    found = next((e for e in tasks_mod.supported.supported(ws) if same(e["github"], github)), None)
    if found:
        fail(f"That repository is on this index's list already, as {found['id']}, added by @{found.get('added_by', '?')} "
             f"on {found.get('added', '?')}. See: hm benchmarks")
    found = tasks_mod.supported.find(ws, name)
    if found:
        fail(f"{found['id']} is on this index's list already, for another repository ({found['github']}). "
             "Give this one another name with --id.")


def _draft(ws: Path) -> dict:
    path = config_mod.local(ws) / DRAFT
    if not path.exists():
        fail("No benchmark is being added. Start with: hm benchmarks add URL")
    return json.loads(path.read_text())


def _keep(ws: Path, entry: dict) -> None:
    """Save the draft and show it, with what it still lacks and how to answer."""
    path = config_mod.local(ws) / DRAFT
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(entry, indent=2) + "\n")
    console.print(f"[bold]{entry.get('name', entry['id'])}[/]  [{theme.MU}]{entry['id']}, not added yet[/]", highlight=False)
    for key in ("description", "paper", "released", "license", "homepage"):
        value = entry.get(key, "")
        if key == "released" and value == entry.get("pinned_on"):
            value += "  (the date of the pinned commit: reply `released: YYYY-MM-DD` if the benchmark came out earlier)"
        console.print(f"  [{theme.MU}]{key:<13}[/]{value or f'[{theme.AM}]not known[/]'}", highlight=False, soft_wrap=False)
    tasks = f"{entry['task_count']} Harbor tasks under {entry['tasks']}" if entry.get("format") == "harbor" else \
        "not Harbor task folders: the first run writes an adapter"
    console.print(f"  [{theme.MU}]{'pinned':<13}[/]{entry['ref']} ({entry['commit'][:10]}), from {entry['github']}", highlight=False)
    console.print(f"  [{theme.MU}]{'tasks':<13}[/]{tasks}", highlight=False)
    lacks = [k for k in ledger_mod.benchmarks.missing(entry) if k in ("name", "description", "paper")]
    if lacks:
        console.print(f"[{theme.AM}]Still needed: {', '.join(lacks)}.[/] Give it with hm benchmarks revise, such as "
                      "hm benchmarks revise paper: https://..." + (" (paper: none when it has none)." if "paper" in lacks else "."), highlight=False)
    else:
        console.print(f"[{theme.TE}]Add it, signed with your key: hm benchmarks confirm.[/] To change something first: hm benchmarks "
                      "revise, then what to change, in plain words or as description: ...", highlight=False)


def _agent(ws: Path, entry: dict, note: str = "") -> str:
    """Have the agent fill in the draft. Returns why it did not, or "" when it did."""
    from ..backends import NotImplementedBackend, describe
    try:
        data, cost = asyncio.run(describe.fill(config_mod.load(ws), entry, config_mod.local(ws) / "sources" / entry["id"], note))
    except (RuntimeError, NotImplementedBackend) as err:
        return str(err)
    describe.merge(entry, data)
    console.print(f"[{theme.MU}]An agent read the repository and filled this in (${cost:.2f}). Check it.[/]", highlight=False)
    return ""


@benchmarks_app.command("add")
def benchmarks_add(url: str = typer.Argument(..., help="The address of the benchmark's git repository."),
                   ref: str = typer.Option("", help="The tag or branch to pin. Default: the newest tag, else the main branch."),
                   id_: str = typer.Option("", "--id", help="Its short name here. Default: from the repository's name."),
                   agent: bool = typer.Option(True, "--agent/--no-agent", help="Have an agent read the repository and draft the entry."),
                   workspace: Optional[Path] = Workspace):
    """Draft a benchmark's entry from its repository, to confirm or revise. Anyone on the contributor list may add one."""
    from ..backends import describe
    ws, ident = need_workspace(workspace), need_identity()
    _may_add(ws, ident)
    github = url.rstrip("/").removesuffix(".git")
    sync = ledger_mod.Sync(ws, ident.name, ident.github)
    try:
        if sync.enabled:            # the list as everyone has it now, so that what someone else just added is seen
            sync.pull()
        _clash(ws, id_ or github, github)
        ref, commit = tasks_mod.supported.pin(url, ref)
        entry = tasks_mod.supported.describe(url, ref, commit, config_mod.local(ws) / "sources", id_)
    except (ValueError, subprocess.CalledProcessError, ledger_mod.SyncError) as err:
        fail(str(getattr(err, "stderr", "") or err).strip())
    entry["pinned_on"] = entry["released"]          # until someone says when it came out, the pinned commit's date stands in
    describe.merge(entry, describe.guess(config_mod.local(ws) / "sources" / entry["id"]))
    why = _agent(ws, entry) if agent else ""
    if why:
        console.print(f"[{theme.AM}]No agent filled this in[/] ({why[:160]}). What the code could read is below.", highlight=False)
    _keep(ws, entry)


@benchmarks_app.command("revise")
def benchmarks_revise(words: list[str] = typer.Argument(..., help="What to change, in plain words or as `paper: URL`."),
                      workspace: Optional[Path] = Workspace):
    """Change the benchmark entry that is being added."""
    from ..backends import describe
    ws = need_workspace(workspace)
    entry, text = _draft(ws), " ".join(words)
    if not describe.direct(entry, text):
        why = _agent(ws, entry, text)
        if why:
            fail(f"No agent could read that ({why[:160]}). Write it as `paper: https://...` or `description: ...`.")
    _keep(ws, entry)


@benchmarks_app.command("confirm")
def benchmarks_confirm(workspace: Optional[Path] = Workspace):
    """Add the drafted benchmark to this index's list: a record signed with your key."""
    ws, ident = need_workspace(workspace), need_identity()
    _may_add(ws, ident)
    entry = _draft(ws)
    lacks = ledger_mod.benchmarks.missing(entry)
    if lacks:
        fail(f"Not added yet: it still needs {', '.join(lacks)}. Give it with: hm benchmarks revise {lacks[0]}: ...")
    sync = ledger_mod.Sync(ws, ident.name, ident.github)
    try:
        if sync.enabled:
            sync.pull()
        _clash(ws, entry["id"], entry["github"])
        entry.pop("pinned_on", None)
        ledger_mod.benchmarks.add(ws, ident, config_mod.load(ws).protocol, entry)
        ledger_mod.benchmarks.write_list(ws)
        if sync.enabled:
            sync.publish(f"add benchmark {entry['id']}")
    except ledger_mod.SyncError as err:
        fail(str(err))
    (config_mod.local(ws) / DRAFT).unlink(missing_ok=True)
    console.print(f"[{theme.TE}]Added {entry['id']}[/] to this index, signed by @{ident.github}"
                  + (" and shared." if sync.enabled else ".") + " The list is in benchmarks.toml. Run it with: hm curate",
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
        budget: float = typer.Option(None, help="Spending limit in dollars."),
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
        from .home import demo_bench
        demo_bench(ws)
        benchmarks, simulate = ["demo-bench"], True
    if not simulate and ledger_mod.evals.release(ledger_mod.evals.read(ws)):
        fail("This index is released, so its curation is closed. Its evaluation is open: hm eval")
    plain = plain or not (sys.stdout.isatty() and sys.stdin.isatty())
    going = None if foreground else host.active(ws)
    if going is not None:           # one run per index: join the one that is going
        if plain or not going.get("socket"):
            fail(f"A run is going in this index already: {host.describe(going)}. There is one run per index. "
                 + ("Open it in a terminal with: hm curate" if going.get("socket") else "It has no screen: wait for it to end."))
        if going.get("simulate") != simulate and (benchmarks or simulate):
            fail(f"The run that is going is {host.describe(going)}. A {'simulated' if simulate else 'real'} one cannot join it: "
                 "end that run first, with ctrl+q on its screen.")
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
    if budget:
        cfg.run.budget_usd = budget
    registry = tasks_mod.read_registry(ws)
    listed = {e["id"]: e for e in tasks_mod.supported.supported(ws)}
    wanted = benchmarks or [*listed, *(b for b in registry if b not in listed and (simulate or registry[b].get("supported")))]
    resolved = {}
    for b in wanted:
        info, why = tasks_mod.resolve(ws, b, simulate)
        if info is None:
            fail(why)
        resolved[b] = info
    gone = ledger_mod.abandoned(ledger_mod.Ledger(ws).events)
    for b in benchmarks or []:          # asked for by name: refused outright. Otherwise the run leaves it out and says so
        if b in gone:
            from ..engine import prepare
            fail(f"Refused: {b} was {prepare.abandoned_text(b, gone[b])}")
    if not resolved:
        fail("Nothing to run: this index lists no benchmark yet. Add one with hm benchmarks add URL, or try made-up tasks with hm curate --demo")
    lock = ledger_mod.protocol.current(ledger_mod.Ledger(ws).events)
    if not simulate and not plain and not foreground:       # the owner changed a list or a setting: lock it here, not in another step
        from ..interact import guide as guide_mod
        from .common import is_owner
        if is_owner(ws, ident) and guide_mod.look(ws).lock != "locked":
            from .results import lock_protocol
            from .views import protocol
            protocol(ws)
            if typer.confirm("A real run needs these locked. Lock them now, signed with your key?", default=True):
                lock_protocol(ws)
                lock = ledger_mod.protocol.current(ledger_mod.Ledger(ws).events)
    if lock is None and not simulate:
        fail("No protocol is locked in this ledger, so a real run has no fixed settings to run under. "
             "The owner locks them in a shell: hm lock")
    check_lock(lock, cfg, ws)
    if not simulate:
        from .common import is_contributor
        if not is_contributor(ws, ident):
            fail(f"@{ident.github} is not on this index's contributor list, so cannot curate in it. See: hm contributors")
    if not simulate:
        from ..tasks import store
        why = store.not_ready(cfg)          # an accepted task must reach the store, so this is asked before anything is spent
        if why:
            fail(f"Refused: {why}")
    if until not in ("", "select", "verify", "probe") or assume_after not in ("", "verify", "probe"):
        fail("--until takes select, verify or probe. --assume-after takes verify or probe.")
    command = "/run " + " ".join([b.split("/")[-1] for b in resolved] + (["--simulate"] if simulate else [])
                                   + ([f"--assume-after {assume_after}"] if assume_after else []))
    if not plain and not foreground:        # everything checks out: start the run behind a relay, and view it from here
        argv = [*(benchmarks or []), *(["--simulate"] if simulate else []), "--speed", str(speed),
                *(["--cells", str(cells)] if cells else []), *(["--budget", str(budget)] if budget else []),
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
        console.print(f"[{theme.TE}]The run goes on[/] in the background. hm curate inside this folder shows it again.", highlight=False)
    else:
        console.print("The run has ended. hm curate shows it again, and starts a new one.", highlight=False)
