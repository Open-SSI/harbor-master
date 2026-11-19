"""Getting a benchmark ready for a run with no one's help: fetched at its pinned release, and
turned into Harbor tasks by the index's adapter if it is in another format (DESIGN.md Section 4, stage 2).

Where the adapter is missing, the owner's run builds it: the worker model writes it, the checks
run, and a failing one goes back with its failures, to the frontier model after a few rounds.
A person is asked only when that still fails, and then for one of two things: to abandon the
benchmark, or to fix the adapter and have it checked again.
"""
from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .. import ledger as ledger_mod
from .. import tasks as tasks_mod
from ..backends import NotImplementedBackend
from ..backends import adapter as writer
from ..core import config as config_mod
from ..tasks import adapter as adapter_mod
from .state import STAGE

if TYPE_CHECKING:
    from ..core.config import Config
    from .session import Session
    from .state import BenchState

STUB = "the adapter is not written yet"


@dataclass
class Built:
    """What building or checking an adapter came to."""
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    out: Path = Path()
    cost: float = 0.0
    tokens: int = 0
    skill: str = ""
    tier: str = "human"         # who wrote the version that was checked last
    rounds: int = 0
    said: str = ""              # the model's own notes

    @property
    def tasks(self) -> int:
        return len(tasks_mod.find_tasks(self.out)) if self.out.is_dir() else 0


def abandoned_text(benchmark: str, record: dict) -> str:
    """What anyone is told who tries to run a benchmark that was given up."""
    import time
    when = time.strftime("%Y-%m-%d", time.localtime(record["time"]))
    return (f"abandoned by @{record['runner']} on {when}: no adapter could be made to pass"
            + (f" ({record['reason'][:120]})" if record.get("reason") else "") + f". Its owner reopens it with: hm adapt {benchmark}")


def foreign(entry: dict | None) -> bool:
    return entry is not None and entry.get("format", "harbor") != "harbor"


def work_dir(ws: Path, benchmark: str) -> Path:
    return config_mod.local(ws) / "adapted" / benchmark


async def oracle_problems(out: Path, n: int, jobs: Path) -> list[str]:
    """Run the reference solution of the first `n` generated tasks through Harbor. Each must pass its own test."""
    from ..backends import harbor
    found = []
    for task_dir in tasks_mod.find_tasks(out)[:n]:
        if not (task_dir / "solution").is_dir():
            continue
        result = await harbor.run(task_dir, jobs, "oracle")
        if not result.passed:
            found.append(f"{task_dir.name}: its reference solution does not pass its own test"
                         + (f" ({result.error[:300]})" if result.error else ""))
    return found


async def check(adapter_dir: Path, source: Path, work: Path, entry: dict, sandbox: bool, oracle: int) -> tuple[list[str], list[str], Path]:
    """Every check that needs no model: the layout, a run, the tasks, a second run, and a few reference solutions."""
    problems, notes, out = await asyncio.to_thread(adapter_mod.check, adapter_dir, source, work, entry.get("task_count"), sandbox)
    if not problems and oracle:
        problems = await oracle_problems(out, oracle, work / "harbor")
    return problems, notes, out


async def write_and_check(cfg: Config, adapter_dir: Path, entry: dict, source: Path, work: Path, *, sandbox: bool, oracle: int,
                          say: Callable[[str], None] = lambda text: None, usage: Callable[[str, float, int], None] = lambda *a: None,
                          feedback: str = "", rounds: int | None = None, tier: str = "") -> Built:
    """Have a model write the adapter until it passes: the worker tier first, then the frontier tier.
    `tier` names one tier for every round, for a retry that goes straight to the stronger model."""
    pkg = adapter_mod.code_dir(adapter_dir) or adapter_mod.scaffold(adapter_dir, entry)
    built = Built()
    for round_no in range(1, max(1, rounds or cfg.adapt.max_rounds) + 1):
        built.tier = tier or ("frontier" if round_no > cfg.adapt.frontier_after_round else "worker")
        built.rounds = round_no
        say(f"round {round_no}: the {built.tier} model writes the adapter")
        data, cost, tokens, built.skill = await writer.write_adapter(cfg, entry, source, feedback, built.tier)
        usage(built.tier, cost, tokens)
        built.cost, built.tokens, built.said = built.cost + cost, built.tokens + tokens, data.get("notes", "")
        writer.write_files(pkg, data["files"])
        say(f"round {round_no}: checking the adapter")
        built.problems, built.notes, built.out = await check(adapter_dir, source, work, entry, sandbox, oracle)
        if not built.problems:
            break
        feedback = "\n".join(built.problems)
    return built


def record(append: Callable[..., dict], cfg: Config, adapter_dir: Path, built: Built, oracle: int, outcome: str = "pass", reason: str = "") -> dict:
    """The record an adapter leaves: who wrote it, how many tasks, what it cost, and whether it passed."""
    model = cfg.tier(built.tier).model if built.tier in ("worker", "frontier") else ""
    return append("decision", digest=adapter_mod.digest(adapter_dir) if adapter_dir.exists() else "none", stage="adapt", gate="adapter",
                  tier=built.tier, model=model, value=f"{built.tasks} tasks" if outcome == "pass" else "abandoned", outcome=outcome,
                  cost=built.cost, skill=built.skill, oracle_checked=oracle, rounds=built.rounds, reason=reason)


# ----- inside a run -----

def _skip(bench: BenchState, why: str, code: str) -> bool:
    bench.phase, bench.detail, bench.skip = "skipped", why, code
    return False


async def ready(s: Session, bench: BenchState) -> bool:
    """Make a listed benchmark's tasks available to this run. False means it is skipped, with the reason on it."""
    entry, ws = bench.entry, s.ws
    if entry is None or (not foreign(entry) and bench.path != Path() and bench.path.exists()):
        return True
    real = ledger_mod.Ledger(ws).events             # the index's own ledger, whatever kind of run this is
    gone = ledger_mod.abandoned(real).get(bench.id)
    if gone is not None:                            # given up: nobody's run touches it until its owner reopens it
        return _skip(bench, abandoned_text(bench.id, gone), "abandoned")
    bench.phase, bench.detail = "fetch", "fetching it at its pinned release"
    STAGE.set("adapt")
    try:
        source = await asyncio.to_thread(tasks_mod.supported.fetch, entry, config_mod.local(ws) / "sources")
    except (ValueError, subprocess.CalledProcessError, OSError) as err:
        return _skip(bench, f"could not be fetched: {str(getattr(err, 'stderr', '') or err).strip()[:160]}", "fetch_failed")
    more = {"supported": entry["id"], "license": entry.get("license", ""), "version": entry.get("version", ""),
            "released": entry.get("released", ""), "commit": entry.get("commit", ""), "github": entry.get("github", "")}
    if not foreign(entry):
        bench.path = source
        tasks_mod.register(ws, bench.id, source, bench.field, **more)
        return True

    adapter_dir, work = adapter_mod.folder(ws, bench.id), work_dir(ws, bench.id)
    sandbox, oracle = s.cfg.run.sandbox != "none", 0 if s.simulate else s.cfg.adapt.oracle_tasks
    trying = s.sandbox      # a simulated run, whose records are kept apart: it builds nothing and needs no lock
    have = adapter_mod.code_dir(adapter_dir) is not None and STUB not in (adapter_mod.code_dir(adapter_dir) / "adapter.py").read_text()
    lock = ledger_mod.protocol.current(real)
    name = f"{adapter_mod.FOLDER}/{bench.id}"
    locked = have and lock is not None and (lock.get("registry") or {}).get(name) == adapter_mod.digest(adapter_dir)
    if trying and not have:
        return _skip(bench, "it is not in Harbor format and has no adapter yet. The owner's real run builds one, or: hm adapt " + bench.id, "no_adapter")
    if not trying and not locked:
        if not s.is_owner:
            return _skip(bench, "it is not in Harbor format, and the owner has not locked its adapter yet. Try again after: hm sync", "adapter_not_locked")
        if not await _build_and_lock(s, bench, entry, source, adapter_dir, work, sandbox, oracle, have, lock):
            return False
    bench.phase, bench.detail = "adapt", "the index's adapter writes the tasks"
    out = work / "tasks"
    ok, said = await asyncio.to_thread(adapter_mod.generate, adapter_dir, source, out, sandbox)
    problems, _ = adapter_mod.inspect(out, entry.get("task_count")) if ok else ([f"it failed when run: {said or 'no output'}"], [])
    if problems:
        return _skip(bench, f"its adapter did not write usable tasks: {problems[0][:160]}", "adapter_failed")
    bench.path = out
    tasks_mod.register(ws, bench.id, out, bench.field, adapter=adapter_mod.digest(adapter_dir), **more)
    return True


async def _build_and_lock(s: Session, bench: BenchState, entry: dict, source: Path, adapter_dir: Path, work: Path,
                          sandbox: bool, oracle: int, have: bool, lock: dict | None) -> bool:
    """The owner's run: get an adapter that passes, asking a person only when the models cannot, then lock it."""
    bench.phase = "adapt"
    say = lambda text: setattr(bench, "detail", text)  # noqa: E731

    def usage(tier: str, cost: float, tokens: int) -> None:
        s.backend.usage(tier, cost, tokens)
        bench.cost += cost

    try:
        if have:        # someone wrote or changed it: check what is there before spending on a model
            say("checking the adapter that is there")
            built = Built()
            built.problems, built.notes, built.out = await check(adapter_dir, source, work, entry, sandbox, oracle)
        else:
            built = await write_and_check(s.cfg, adapter_dir, entry, source, work, sandbox=sandbox, oracle=oracle, say=say, usage=usage)
        while built.problems:
            before = adapter_mod.digest(adapter_dir)
            bench.phase, bench.detail = "review", "its adapter needs you"
            shown = "; ".join(p[:140] for p in built.problems[:2])
            answer = await s.ask("adapter", f"The adapter for {bench.id} does not pass after {built.rounds} round{'s' if built.rounds != 1 else ''}"
                                 f"{' by the models' if built.rounds else ''}: {shown}. It is in {adapter_dir}.", bench.id)
            if answer is None:
                return _skip(bench, "its adapter does not pass yet, and nothing could ask you. Run with the screen, or: hm adapt " + bench.id, "adapter_needs_person")
            if answer == "abandon":
                record(lambda *a, **k: s.record(bench.id, a[0], digest_override=k.pop("digest"), **k), s.cfg, adapter_dir, built, oracle,
                       "drop", built.problems[0][:200])
                return _skip(bench, f"abandoned: its adapter could not be made to pass. Only hm adapt {bench.id} reopens it", "abandoned")
            bench.phase = "adapt"
            if adapter_mod.digest(adapter_dir) != before:       # fixed by hand: check that
                say("checking your fix")
                cost, tokens = built.cost, built.tokens
                built = Built(cost=cost, tokens=tokens, rounds=built.rounds)
                built.problems, built.notes, built.out = await check(adapter_dir, source, work, entry, sandbox, oracle)
            else:                                               # nothing changed: one more try by the stronger model
                more = await write_and_check(s.cfg, adapter_dir, entry, source, work, sandbox=sandbox, oracle=oracle, say=say, usage=usage,
                                             feedback="\n".join(built.problems), rounds=1, tier="frontier")
                more.cost, more.tokens, more.rounds = more.cost + built.cost, more.tokens + built.tokens, built.rounds + 1
                built = more
    except (RuntimeError, NotImplementedBackend) as err:
        return _skip(bench, f"the model did not return an adapter: {str(err)[:160]}", "model_error")

    record(lambda *a, **k: s.record(bench.id, a[0], digest_override=k.pop("digest"), **k), s.cfg, adapter_dir, built, oracle)
    s.note_recent("checked", bench.id, f"its adapter passes: {built.tasks} tasks, the same on a second run"
                  + (f", written by the {built.tier} model in {built.rounds} round{'s' if built.rounds != 1 else ''} for ${built.cost:.2f}"
                     if built.tier != "human" else ""))
    if s.cfg.adapt.require_review:
        bench.phase, bench.detail = "review", "its adapter waits for your lock"
        answer = await s.ask("lock", f"The adapter for {bench.id} passes its checks: {built.tasks} tasks. It is in {adapter_dir}. "
                             "Read it, then lock it so that its tasks can be used.", bench.id)
        if answer != "lock":
            return _skip(bench, f"its adapter passes and is not locked. Lock it with /lock {bench.id} in a run, or: hm protocol lock", "adapter_not_locked")
    bench.phase, bench.detail = "adapt", "locking the adapter"
    name = f"{adapter_mod.FOLDER}/{bench.id}"
    registry = {**((lock or {}).get("registry") or {}), name: adapter_mod.digest(adapter_dir)}       # this adapter, and nothing else new
    event = ledger_mod.protocol.write(s.ws, s.ident, s.cfg, f"adapter for {bench.id}", registry)
    s.ledger.add(event)
    if s.sync.enabled:
        async with s._sync_lock:
            await asyncio.to_thread(s.sync.define, f"adapter for {bench.id}", (name,))
        await s.publish(f"adapter for {bench.id}")
    return True
