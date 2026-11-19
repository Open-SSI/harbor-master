"""Curation: submit, brief, run, review, and what a run is waiting on."""
from __future__ import annotations

import asyncio
import subprocess
import json
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.markdown import Markdown
from rich.table import Table

from .. import ledger as ledger_mod
from .. import tasks as tasks_mod
from ..core import config as config_mod
from ..tui import theme
from .common import Simulated, Workspace, app, check_lock, console, fail, need_identity, need_workspace, records


@app.command()
def benchmarks(workspace: Optional[Path] = Workspace):
    """The benchmarks this index may curate, from its benchmarks.toml."""
    ws = need_workspace(workspace)
    registry = tasks_mod.read_registry(ws)
    tb = Table(header_style=theme.MU, border_style=theme.BORDER)
    for col in ("id", "name", "version", "released", "tasks", "license", "source", "in this workspace"):
        tb.add_column(col)
    gone = ledger_mod.abandoned(ledger_mod.Ledger(ws).events)
    for e in tasks_mod.supported.supported(ws):
        here = e["id"] in registry
        state = (f"[{theme.RD}]abandoned[/]" if e["id"] in gone else f"[{theme.TE}]yes[/]" if here
                 else "not yet: a run fetches it")
        tb.add_row(e["id"], e["name"], e["version"], e["released"], str(e.get("task_count", "")), e.get("license", ""),
                   e["github"].replace("https://", ""), state)
    console.print(tb)
    for b, record in gone.items():      # said in full under the table, where a narrow column cannot cut it
        from ..engine import prepare
        console.print(f"[{theme.RD}]{b}[/] was {prepare.abandoned_text(b, record)}", highlight=False)
    console.print(f"[{theme.MU}]hm run works only on these. To add one, the owner adds an entry to benchmarks.toml and locks it.[/]")


@app.command()
def submit(name: str = typer.Argument(..., help="A benchmark ID from `hm benchmarks`. With --dev, a local path."),
           dev: bool = typer.Option(False, help="Register a local folder for simulated runs only."),
           field: str = typer.Option("general", help="With --dev: the field this benchmark belongs to."),
           no_sandbox: bool = typer.Option(False, "--no-sandbox", help="For a benchmark that needs an adapter: run the adapter's code "
                                                                     "on this machine as it is, not in a container."),
           workspace: Optional[Path] = Workspace):
    """Fetch a supported benchmark at its pinned release and register it in this workspace."""
    ws = need_workspace(workspace)
    need_identity()
    if dev:
        path = Path(name)
        benchmark = path.resolve().name
        try:
            found = tasks_mod.load_benchmark(path, benchmark)
        except ValueError as err:
            fail(str(err))
        tasks_mod.register(ws, benchmark, path, field, dev=True)
        console.print(f"Registered [bold]{benchmark}[/] for simulated runs only: {len(found)} task{'s' if len(found) != 1 else ''}.")
        return
    entry = tasks_mod.supported.find(ws, name)
    if entry is None:
        known = ", ".join(e["id"] for e in tasks_mod.supported.supported(ws)) or "none yet"
        fail(f"{name} is not on this index's list of benchmarks ({known}). See: hm benchmarks. "
             "A local folder can be registered for simulated runs with --dev.")
    foreign = entry.get("format", "harbor") != "harbor"
    if foreign:                                          # not Harbor tasks yet: the index's adapter writes them
        from ..tasks import adapter as adapter_mod
        adapter_dir = adapter_mod.folder(ws, entry["id"])
        if adapter_mod.code_dir(adapter_dir) is None:
            fail(f"{entry['id']} is not in Harbor format, and this index has no adapter for it yet. Its owner builds one with: "
                 f"hm adapt {entry['id']}")
        wrong = adapter_mod.structure(adapter_dir)
        if wrong:
            fail(f"The adapter for {entry['id']} cannot be used: {'; '.join(wrong[:3])}")
    console.print(f"Fetching {entry['name']} {entry['version']} ({entry['ref']}) from {entry['github']} ...")
    try:
        path = tasks_mod.supported.fetch(entry, config_mod.local(ws) / "sources")
    except (ValueError, subprocess.CalledProcessError) as err:
        fail(str(getattr(err, "stderr", "") or err))
    benchmark = entry["id"]
    more = {}
    if foreign:
        console.print(f"Writing the tasks with the index's adapter ({adapter_mod.FOLDER}/{benchmark}) ...")
        out = config_mod.local(ws) / "adapted" / benchmark / "tasks"
        ok, said = adapter_mod.generate(adapter_dir, path, out, sandbox=not no_sandbox)
        problems, _ = adapter_mod.inspect(out, entry.get("task_count")) if ok else ([f"the adapter failed: {said or 'no output'}"], [])
        if problems:
            fail(f"The adapter for {benchmark} did not write usable tasks: {'; '.join(problems[:3])}")
        known = ledger_mod.submitted(ledger_mod.Ledger(ws).events)
        mine = adapter_mod.digests(out)
        differ = [t for t, d in mine.items() if known.get(f"{benchmark}/{t}", {}).get("digest") not in (None, d)]
        if differ:
            console.print(f"[{theme.AM}]{len(differ)} of these tasks differ from the ones the ledger has under the same names.[/] "
                          "That is expected only after the adapter changed.")
        path, more = out, {"adapter": adapter_mod.digest(adapter_dir)}
    found = tasks_mod.load_benchmark(path, benchmark)
    tasks_mod.register(ws, benchmark, path, ", ".join(entry.get("fields", [])) or "general", supported=entry["id"], license=entry.get("license", ""),
                       version=entry["version"], released=entry["released"], commit=entry["commit"], github=entry["github"], **more)
    console.print(f"Registered [bold]{benchmark}[/]: {entry['name']} {entry['version']}, released {entry['released']}, "
                  f"{len(found)} tasks at commit {entry['commit'][:10]}" + (", written by the index's adapter." if more else "."))


@app.command()
def brief(benchmark: str, text: str = typer.Option(None), workspace: Optional[Path] = Workspace):
    """Record the field lead's brief: what counts as important in this field and this benchmark."""
    ws, ident = need_workspace(workspace), need_identity()
    if benchmark not in tasks_mod.read_registry(ws):
        fail(f"{benchmark} is not registered. See: hm status")
    text = text or typer.prompt("What counts as important in this field and this benchmark?")
    cfg = config_mod.load(ws)
    shard = ledger_mod.Shard(ws, benchmark, ident, ledger_mod.MANUAL, cfg.protocol)
    shard.append("brief", text=text, examples=[], version=int(time.time()))
    console.print(f"Brief recorded for {benchmark}, credited to @{ident.github}.")


@app.command()
def run(benchmarks: list[str] = typer.Argument(None, help="Benchmarks to run. Default: all registered."),
        simulate: bool = typer.Option(False, help="Use simulated models and trials. Spends nothing."),
        speed: float = typer.Option(1.0, help="Simulation only: scale the pretend durations. 0 is instant."),
        cells: int = typer.Option(None, help="Tasks in flight at once."),
        budget: float = typer.Option(None, help="Spending limit in dollars."),
        plain: bool = typer.Option(False, help="Print lines, with no full-screen view."),
        no_input: bool = typer.Option(False, help="Never prompt. Skip what needs an answer and record it."),
        only: str = typer.Option("", "--tasks", help="Run only these tasks of the benchmark: names separated by commas."),
        until: str = typer.Option("", help="Stop after this point: select (screen and rank only), verify, or probe. Default: all stages."),
        assume_after: str = typer.Option("", help="Run stages up to this one for real (verify or probe) and assume "
                                         "the rest. Assumed results cost nothing, are marked, and cannot be accepted."),
        workspace: Optional[Path] = Workspace):
    """Run benchmarks through the stages, to the report a reviewer reads."""
    from ..backends import NotImplementedBackend
    from ..engine import Session
    from ..tui import render, run_app, run_plain
    ws, ident = need_workspace(workspace), need_identity()
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
        if b not in registry and b not in listed:
            fail(f"{b} is not on this index's list of benchmarks, and is not a folder registered here. See: hm benchmarks")
        entry = listed.get(b)       # on the list: the run fetches it, and adapts it, when that is not done yet
        info = dict(registry[b]) if b in registry else {
            "path": "", "field": ", ".join(entry.get("fields", [])) or "general", "supported": b,
            "license": entry.get("license", ""), "released": entry.get("released", "")}
        resolved[b] = {**info, **({"entry": entry} if entry else {})}
        if not simulate and not resolved[b].get("supported"):
            fail(f"{b} is not on the list of supported benchmarks, so it only runs with --simulate. See: hm benchmarks")
    gone = ledger_mod.abandoned(ledger_mod.Ledger(ws).events)
    for b in benchmarks or []:          # asked for by name: refused outright. Otherwise the run leaves it out and says so
        if b in gone:
            from ..engine import prepare
            fail(f"Refused: {b} was {prepare.abandoned_text(b, gone[b])}")
    if not resolved:
        fail("Nothing to run: this index lists no benchmark yet. Its owner adds one to benchmarks.toml. To try made-up tasks: hm demo")
    lock = ledger_mod.protocol.current(ledger_mod.Ledger(ws).events)
    if lock is None and not simulate:
        fail("No protocol is locked in this ledger, so a real run has no fixed settings to run under. "
             "The owner locks them with: hm protocol lock")
    check_lock(lock, cfg, ws)
    if not simulate:
        mine = ledger_mod.fields_of(ledger_mod.load_contributors(ws), ident.github)
        if mine is not None and not mine:
            fail(f"@{ident.github} has no fields on the contributor list, so cannot curate. See: hm contributors")
    plain = plain or not sys.stdout.isatty()
    if until not in ("", "select", "verify", "probe") or assume_after not in ("", "verify", "probe"):
        fail("--until takes select, verify or probe. --assume-after takes verify or probe.")
    command = "hm run " + " ".join([b.split("/")[-1] for b in resolved] + (["--simulate"] if simulate else [])
                                   + ([f"--assume-after {assume_after}"] if assume_after else []))
    try:
        session = Session(ws, cfg, ident, resolved, simulate=simulate, speed=speed,
                          no_input=no_input or plain, command=command, until=until,
                          assume_after=assume_after, only=[t.strip() for t in only.split(",") if t.strip()])
    except NotImplementedBackend as err:
        fail(str(err))
    if plain:
        asyncio.run(run_plain(session, console))
    else:
        run_app(session)
        console.print(render.final_table(session))
    console.print(f"[{theme.MU}]Run {session.run_id}: {session.run_dir}[/]")
    if any(r.blocking for r in session.requests):     # something blocked part of the run
        raise typer.Exit(3)


@app.command()
def review(benchmark: str = typer.Argument(None), simulated: bool = Simulated, workspace: Optional[Path] = Workspace):
    """Read each finished report and record accept, drop, or send back, with a reason."""
    ws, ident = need_workspace(workspace), need_identity()
    cfg = config_mod.load(ws)
    if not any(c["github"] == ident.github for c in ledger_mod.active(ledger_mod.load_contributors(ws))):
        fail(f"@{ident.github} is not on the contributor list, so cannot sign a verdict.")
    ledger_dir = records(ws, simulated)
    led = ledger_mod.Ledger(ledger_dir, simulated=simulated)
    todo = [s for s in ledger_mod.fold(led.events).values() if s.status == "report_ready"
            and (benchmark is None or s.benchmark.endswith(benchmark))
            and not any(v["runner"] == ident.github and v["digest"] == s.digest for v in s.verdicts)]
    if not todo:
        console.print("No reports are waiting for your verdict.")
        return
    shards: dict[str, ledger_mod.Shard] = {}
    stamp = ledger_mod.MANUAL
    for i, s in enumerate(todo, 1):
        report = next((e for e in reversed(led.by_task[s.task_id]) if e["type"] == "report"), None)
        console.rule(f"{i} of {len(todo)}  {s.task_id}")
        path = ws / report["location"] if report and report.get("location") else None
        console.print(Markdown(path.read_text()) if path and path.exists() else "(the report file is not on this machine)")
        choice = typer.prompt("accept / drop / back / skip / quit", default="skip").strip().lower()
        if choice.startswith("q"):
            break
        value = {"a": "accept", "d": "drop", "b": "send_back"}.get(choice[:1])
        if value is None:
            continue
        shard = shards.setdefault(s.benchmark, ledger_mod.Shard(ledger_dir, s.benchmark, ident, stamp, cfg.protocol))

        def sign(task_id: str, digest: str, value: str, reason: str) -> None:
            led.add(shard.append("verdict", task_id, digest, value=value, reason=reason, tier="human",
                                 reason_code=value, reviewer=ident.github, simulated=simulated))
            ledger_mod.write_index(ledger_dir, simulated)       # the accepted list follows every verdict
            if value == "accept" and not simulated:             # keep the version that was accepted, for later benchmark runs
                from ..tasks import store
                info = tasks_mod.read_registry(ws).get(task_id.split("/", 1)[0], {})
                files = next((d for d in tasks_mod.find_tasks(Path(info["path"])) if d.name == task_id.split("/", 1)[1]), None) \
                    if info.get("path") and Path(info["path"]).is_dir() else None
                try:
                    _, how = store.put(ws, cfg, *task_id.split("/", 1), digest, files or Path("/nonexistent"))
                except store.StoreError as err:
                    how = f"not kept: {err}"
                console.print(f"[{theme.MU}]Accepted version {how}.[/]", highlight=False)

        if value == "accept":
            inside = ledger_mod.accepted(led.events, s.benchmark)
            if len(inside) >= cfg.index.max_per_benchmark:
                console.print(f"[{theme.AM}]{s.benchmark} already has {len(inside)} tasks in the index, the most one "
                              f"benchmark may have.[/] To include {s.task_id.split('/')[-1]}, choose the one it replaces:")
                for n, old in enumerate(inside, 1):
                    console.print(f"  {n}. {old.task_id.split('/')[-1]}  ({old.passes} of {old.trials} trials passed)")
                pick = typer.prompt("Replace which number? (blank keeps the index as it is)", default="", show_default=False)
                if not pick.strip().isdigit() or not 1 <= int(pick) <= len(inside):
                    console.print("Left as it is. This report still waits for a verdict.")
                    continue
                old = inside[int(pick) - 1]
                why = typer.prompt("Reason", default="", show_default=False)
                new_name, old_name = s.task_id.split("/")[-1], old.task_id.split("/")[-1]
                sign(old.task_id, old.digest, "replaced", f"replaced by {new_name}" + (f": {why}" if why else ""))
                sign(s.task_id, s.digest, "accept", f"replaces {old_name}" + (f": {why}" if why else ""))
                console.print(f"[{theme.TE}]{new_name} is in the index in place of {old_name}, signed by @{ident.github}.[/]")
                continue
        reason = typer.prompt("Reason", default="" if value == "accept" else None, show_default=False)
        sign(s.task_id, s.digest, value, reason)
        console.print(f"[{theme.TE}]Recorded {value} for {s.task_id}, signed by @{ident.github}.[/]")


@app.command(name="eval")
def eval_(benchmarks: list[str] = typer.Argument(None, help="Benchmarks whose tasks to run. Default: all registered."),
          config: str = typer.Option("", help="A configuration from the roster, by name. See: hm roster"),
          agent: str = typer.Option("", help="Or name one yourself: the agent harness, such as claude-code or codex."),
          model: str = typer.Option("", help="The model the agent runs."),
          version: str = typer.Option("", help="The agent's version. Pin it."),
          effort: str = typer.Option("", help="Reasoning effort, such as max."),
          only: str = typer.Option("", "--tasks", help="Task names, separated by commas. Default: the accepted tasks."),
          trials: int = typer.Option(None, help="Trials per task. Default: trials_per_config in the protocol."),
          simulate: bool = typer.Option(False, help="Use simulated trials. Spends nothing."),
          workspace: Optional[Path] = Workspace):
    """Benchmark an agent on tasks. Leaves the same records as the difficulty filter does."""
    from ..core.config import RosterEntry, roster
    from ..engine import Session, evaluate
    ws, ident = need_workspace(workspace), need_identity()
    if config:
        entry = next((c for c in roster(ws) if c.name == config), None)
        if entry is None:
            fail(f"{config} is not on the roster. See: hm roster")
    elif agent and model:
        entry = RosterEntry(name=f"{agent}:{model}", agent=agent, model=model, reasoning_effort=effort, version=version)
    else:
        fail("Name a configuration from the roster with --config, or give --agent and --model.")
    agent, model = entry.agent, entry.model
    cfg = config_mod.load(ws)
    registry = tasks_mod.read_registry(ws)
    led = ledger_mod.Ledger(config_mod.simulated(ws) if simulate else ws, simulated=simulate)
    lock = ledger_mod.protocol.current(ledger_mod.Ledger(ws).events)
    if not simulate and lock is None:
        fail("Benchmarking needs a locked protocol. The owner locks one with: hm protocol lock")
    check_lock(lock, cfg, ws)
    from ..tasks import store
    names = {t.strip() for t in only.split(",") if t.strip()}
    index = [e for e in ledger_mod.index_entries(led.events) if not benchmarks or e["benchmark"] in benchmarks]
    here: dict[str, tasks_mod.Task] = {}                # the tasks of the benchmarks registered on this machine
    for b, info in registry.items():
        if (not benchmarks or b in benchmarks) and info.get("path") and Path(info["path"]).is_dir():
            here.update({t.task_id: t for t in tasks_mod.load_benchmark(Path(info["path"]), b)})
    chosen, lacking = [], []
    if names:           # tasks named by hand, to try an agent on them. An accepted one must still be the accepted version
        version = {e["task"]: e["digest"] for e in index}
        chosen = [t for t in here.values() if t.name in names]
        lacking = [t.task_id for t in chosen if not simulate and version.get(t.task_id, t.digest) != t.digest]
    for e in ([] if names else index):
        mine = here.get(e["task"])
        if mine is not None and (simulate or mine.digest == e["digest"]):
            chosen.append(mine)
            continue
        try:            # not here, or not this version: the store has the one that was accepted
            path = None if simulate else store.fetch(ws, cfg, *e["task"].split("/", 1), e["digest"])
        except store.StoreError as err:
            fail(str(err))
        if path is None:
            lacking.append(e["task"])
        else:
            chosen.append(tasks_mod.load_task(path, e["benchmark"]))
    if lacking:
        fail(f"Refused: the accepted version of {', '.join(lacking[:5])}{' and more' if len(lacking) > 5 else ''} is not on this machine "
             "and not in the task store. Running another version would give results for a different task. See: hm tasks")
    if not chosen:
        fail("No tasks to run. Name some with --tasks, or accept tasks into the index first.")
    wanted = {b: registry.get(b) or {"path": "", "field": "", "supported": b} for b in sorted({t.benchmark for t in chosen})}
    if not simulate and not all(v.get("supported") for v in wanted.values()):
        fail("Only supported benchmarks can be benchmarked for real. See: hm benchmarks")
    session = Session(ws, cfg, ident, wanted, simulate=simulate, speed=0.0, no_input=True,
                      command=f"hm eval --agent {agent} --model {model}")
    rows = asyncio.run(evaluate(session, entry, chosen, trials or cfg.difficulty.trials_per_config))
    tb = Table(header_style=theme.MU, border_style=theme.BORDER)
    for col, j in (("task", "left"), ("passed", "right"), ("new trials", "right"), ("", "left")):
        tb.add_column(col, justify=j)
    for r in sorted(rows, key=lambda r: r["task"]):
        tb.add_row(r["task"].split("/", 1)[-1], f"{r['passes']} of {r['trials']}" if r["trials"] else "-",
                   str(r["new"]), r["note"])
    console.print(tb)
    done = [r for r in rows if r["trials"]]
    if done:
        console.print(f"{agent} with {model}: {sum(r['passes'] for r in done)} of {sum(r['trials'] for r in done)} "
                      f"trials passed on {len(done)} tasks. Run {session.run_id}: {session.run_dir}")


@app.command()
def claims(simulated: bool = Simulated, workspace: Optional[Path] = Workspace):
    """Which tasks someone is working on now, so that nobody runs them twice."""
    ws = need_workspace(workspace)
    led = ledger_mod.Ledger(records(ws, simulated), simulated=simulated)
    now, rows = time.time(), 0
    for b in led.benchmarks():
        by: dict[tuple[str, float], int] = {}
        for task_id, (runner, expires) in led.claims(b, now).items():
            if task_id.startswith("__"):
                console.print(f"{b}: being screened by @{runner}")
                rows += 1
                continue
            by[(runner, expires)] = by.get((runner, expires), 0) + 1
        for (runner, expires), n in by.items():
            rows += 1
            console.print(f"{b}: {n} task{'s' if n != 1 else ''} held by @{runner} until "
                          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(expires))}")
    if not rows:
        console.print("Nobody holds a claim right now.")


@app.command()
def requests(workspace: Optional[Path] = Workspace):
    """List what the latest run was left waiting on."""
    ws = need_workspace(workspace)
    runs = sorted((config_mod.local(ws) / "runs").glob("*/requests.json"))
    items = json.loads(runs[-1].read_text()) if runs else []
    if not items:
        console.print("Nothing is waiting on you.")
        return
    for r in items:
        how = f"  [{theme.MU}]answer with: hm brief {r['target']}[/]" if r["kind"] == "brief" else ""
        console.print(f"[{theme.AM}]{r['kind']}[/]  {r['text']}{how}")
