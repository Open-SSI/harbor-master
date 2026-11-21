"""Getting a benchmark ready for a run with no one's help: fetched at its pinned release, and
turned into Harbor tasks by the index's adapter if it is in another format (DESIGN.md Section 4, stage 2).

Where the adapter is missing, the run of whoever takes the benchmark builds it. The worker model writes it, the checks run,
and a failing one goes back with its failures. If the worker's rounds fail, the decision model, Jev, judges whether the
benchmark can be adapted at all, as a typed choice: if it can, the frontier model tries next; if not, a person is shown
why, and either drops the benchmark or gives the frontier model a hint and lets it try. If the frontier model fails too, the benchmark is dropped,
the person is told why, and nobody can add it to the index again.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
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
    why = ("no adapter could be made to pass, by the worker model or the frontier model" if record.get("stage") == "adapt"
           else record.get("value") or "a person confirmed it")
    return (f"dropped by @{record['runner']} on {when}: {why}"
            + (f" ({record['reason'][:160]})" if record.get("reason") else "") + ". Anyone may curate it again")


PERMANENT = ("environment", "adapter", "no_task_passed", "not_harbor_format")     # what drops a benchmark, until someone tries it again


async def confirm_drop(s: Session, bench: BenchState, why: str, gate: str, record: bool = True) -> str:
    """A benchmark is rejected before curating. The workflow decides what follows from what rejected it: a lasting reason
    (no public image, no adapter any model could write, no task that passes the gates) drops it, and a passing one (a fetch
    or a model that failed) leaves it out of this run, listed for the next. A box says so, and the person only confirms.
    A dropped benchmark may be curated again by anyone. With nobody there, nothing is dropped. Returns "drop" or "keep"."""
    if s.no_input:
        return "keep"
    lasting = gate in PERMANENT
    bench.phase, bench.detail, bench.confirmed = "review", "a box asks you to confirm", "asked"
    what = ("dropped: it stays on the index's list as dropped, and anyone may curate it again" if lasting else
            "left out of this run: it stays listed, and the next run tries it again")
    answer = await s.ask("rejected", f"{bench.id} is rejected. {why.rstrip('.')}.\n\nConfirm, and it is {what}.", bench.id, ["ok"])
    if answer is None:
        return "keep"
    bench.confirmed = "drop" if lasting else "keep"
    if lasting and record:      # the adapter's own drop record says why, when it is the adapter
        s.record(bench.id, "decision", stage="benchmark", gate=gate, tier="human", value=why[:200], outcome="drop",
                 reason=(why[:1].lower() + why[1:]).splitlines()[0][:160])     # read as it is wherever it is shown as dropped
    return bench.confirmed


def approved(events, digest: str) -> bool:
    """An adapter may be used when someone on the contributor list signed that this very version passed its checks."""
    return any(e["type"] == "decision" and e.get("stage") == "adapt" and e.get("outcome") == "pass" and e.get("digest") == digest
               for e in events)


def foreign(entry: dict | None) -> bool:
    return entry is not None and entry.get("format", "harbor") != "harbor"


def work_dir(ws: Path, benchmark: str) -> Path:
    return config_mod.local(ws) / "adapted" / benchmark


async def oracle_problems(out: Path, n: int, jobs: Path, limits: dict | None = None) -> list[str]:
    """Run the reference solution of `n` generated tasks, spread over the set, through Harbor. Each must pass its own test.
    The first few by name would often all come from one source, and say nothing of the others."""
    from ..backends import harbor
    found, made = [], tasks_mod.find_tasks(out)
    picked = sorted({made[i * len(made) // n] for i in range(n)}) if made and n else []     # spread over the set, not its first few:
    for task_dir in picked:                                                                  # tasks sorted by name group by source
        if not (task_dir / "solution").is_dir():
            continue
        result = await harbor.run(task_dir, jobs, "oracle", **(limits or {}))
        if not result.passed:
            found.append(f"{task_dir.name}: " + (unpullable(result.error, task_dir) or slow_build(result.error, limits) or
                         "its reference solution does not pass its own test" + (f" ({result.error[:500]})" if result.error else
                                                                                 verifier_said(result.artifact))))
    return found


def verifier_said(trial: str, lines: int = 12, limit: int = 1800) -> str:
    """The end of what the test wrote, so that whoever fixes the adapter can see why the reference solution failed:
    the test's own output, and the log files it left, newest first."""
    folder = Path(trial) / "verifier" if trial else None
    if folder is None or not folder.is_dir():
        return ""
    files = sorted((f for f in folder.iterdir() if f.is_file() and f.name != "reward.txt" and f.stat().st_size),
                   key=lambda f: (f.name != "test-stdout.txt", -f.stat().st_mtime))
    parts = []
    for f in files[:4]:
        tail = f.read_text(errors="replace").strip().splitlines()[-lines:]
        parts.append(f"--- {f.name}, last lines:\n" + "\n".join(line[:200] for line in tail))
    empty = [f.name for f in folder.iterdir() if f.is_file() and not f.stat().st_size]
    if empty:
        parts.append("empty: " + ", ".join(sorted(empty)))
    return ("\nWhat the test wrote:\n" + "\n".join(parts))[:limit] if parts else ""


PULL = re.compile(r"pull access denied|repository does not exist|manifest unknown|requested access to the resource is denied", re.I)


def unpullable(error: str, task_dir: Path) -> str:
    """When a task never ran because the image its Dockerfile starts from cannot be pulled, say that, and what follows from it.
    The reference solution was never tried, so this is not a broken task: it is an environment the adapter cannot use."""
    if not error or not PULL.search(error):
        return ""
    dockerfile = task_dir / "environment" / "Dockerfile"
    base = next((line.split()[1] for line in (dockerfile.read_text().splitlines() if dockerfile.is_file() else [])
                 if line.strip().upper().startswith("FROM ") and len(line.split()) > 1), "its base image")
    return (f"the image it starts from, {base}, cannot be pulled: it is private or does not exist, so the task never ran and its "
            "reference solution was never tried. Every task that uses such an image fails the same way. Build the environment "
            "from public sources instead: a public base image, the project's source at the pinned commit, and its dependencies, "
            "installed in the Dockerfile. It must build within the protocol's limit, 300 seconds by default: prefer prebuilt "
            "packages to compiling from source.")


SLOW = re.compile(r"EnvironmentStartTimeoutError|Environment start timed out|VerifierTimeoutError", re.I)


def slow_build(error: str, limits: dict | None = None) -> str:
    """When a task never finished because its environment did not build, or its verifier did not finish, within the
    protocol's limits, say which. Such a task is dropped: nobody waits longer for it."""
    if not error or not SLOW.search(error):
        return ""
    limits = limits or {}
    if "Verifier" in error:
        return f"its verifier did not finish within {int(limits.get('max_verifier_seconds') or 300)} seconds, the protocol's limit"
    return (f"its environment did not build within {int(limits.get('max_build_seconds') or 300)} seconds, the protocol's limit. "
            "Use a lighter environment: a base image that already has what the task needs, prebuilt packages instead of "
            "compiling from source, and only what the task uses")


def limits_of(cfg: Config) -> dict:
    """The protocol's caps on building an environment and on running a verifier."""
    return {"max_build_seconds": cfg.triage.max_build_seconds, "max_verifier_seconds": cfg.triage.max_verifier_seconds}


async def check(adapter_dir: Path, source: Path, work: Path, entry: dict, sandbox: bool, oracle: int,
                limits: dict | None = None) -> tuple[list[str], list[str], Path]:
    """Every check that needs no model: the layout, a run, the tasks, a second run, and a few reference solutions."""
    problems, notes, out = await asyncio.to_thread(adapter_mod.check, adapter_dir, source, work, entry.get("task_count"), sandbox)
    if not problems and oracle:
        problems = await oracle_problems(out, oracle, work / "harbor", limits)
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
        built.problems, built.notes, built.out = await check(adapter_dir, source, work, entry, sandbox, oracle, limits_of(cfg))
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

async def checked(s: Session, bench: BenchState, entry: dict, source: Path) -> bool:
    """The benchmark-level checks (tasks/checks.py), asked once for each pinned release: when it was added, or here,
    before its images are looked up and its adapter is built. One that fails rejects it: a box says why, the person
    confirms, and it is dropped for good, on the list as dropped. With nobody to confirm, it is only left out of this
    run. The image check is left to what follows, which asks about it in a box of its own."""
    from ..tasks import checks as checks_mod
    key = checks_mod.key(entry)
    prior = s.ledger.decision(key, "benchmark", "checks", s.protocol)
    if prior is not None and prior.get("outcome") == "pass":
        bench.checked = "passed"
        return True
    bench.phase, bench.detail = "check", "cheap checks on the whole benchmark, before anything costly"
    found = await asyncio.to_thread(checks_mod.run, entry, source, s.cfg, True)

    def keep(outcome: str) -> None:
        failed = [c for c in found if c.failed]
        s.record(bench.id, "decision", stage="benchmark", gate="checks", tier="code", value=checks_mod.summary(found),
                 outcome=outcome, checks={c.key: [c.status, c.detail[:240]] for c in found}, digest_override=key,
                 **({"reason": f"its checks reject it: {failed[0].detail}"[:160]} if failed else {}))
    bench.checked = "rejected" if any(c.failed for c in found) else f"{len(found)} checked"
    if not any(c.failed for c in found):
        keep("pass")
        return True
    why = checks_mod.why(found)
    bench.phase, bench.detail = "check_review", "its checks reject it: a box asks you to confirm"
    said = await s.ask("rejected", f"{bench.id} is rejected by its checks. {why}\n\nConfirm, and it is dropped: it stays on the "
                       "index's list as dropped, and nobody curates it.", bench.id, ["ok"])
    if said is None:
        return _skip(bench, f"its checks reject it, and nobody was there to confirm: left out of this run. {why}", "checks_failed")
    keep("drop")
    return _skip(bench, f"dropped: its checks reject it. {why}", "checks_failed")


def _skip(bench: BenchState, why: str, code: str) -> bool:
    bench.phase, bench.detail, bench.skip = "skipped", why, code
    return False


async def _downloading(bench: BenchState, folder: Path) -> None:
    """While a benchmark downloads, how much of it is here, every two seconds: a large repository takes minutes, and
    the screen says that it moves."""
    def size() -> int:
        total = 0
        for root, _, files in os.walk(folder):
            for name in files:
                with contextlib.suppress(OSError):
                    total += os.lstat(os.path.join(root, name)).st_size
        return total
    while True:
        await asyncio.sleep(2)
        got = await asyncio.to_thread(size)
        if got and bench.phase == "fetch":
            bench.detail = f"downloading it at its pinned release · {got / 1e6:,.0f} MB so far"


async def ready(s: Session, bench: BenchState) -> bool:
    """Make a listed benchmark's tasks available to this run. False means it is skipped, with the reason on it."""
    entry, ws = bench.entry, s.ws
    if entry is None or (not foreign(entry) and bench.path != Path() and bench.path.exists()):
        return True
    real = ledger_mod.Ledger(ws).events             # the index's own ledger, whatever kind of run this is
    bench.phase, bench.detail = "fetch", "downloading it at its pinned release"
    STAGE.set("adapt")
    sources = config_mod.local(ws) / "sources"
    watching = asyncio.create_task(_downloading(bench, sources / entry["id"]))
    try:
        source = await asyncio.to_thread(tasks_mod.supported.fetch, entry, sources)
    except (ValueError, subprocess.CalledProcessError, OSError) as err:
        return _skip(bench, f"could not be fetched: {str(getattr(err, 'stderr', '') or err).strip()[:160]}", "fetch_failed")
    finally:
        watching.cancel()
    if not await checked(s, bench, entry, source):       # the cheap checks on the whole benchmark, before anything costly
        return False
    if not s.simulate:          # before anything is adapted or screened: can its tasks run here at all
        from ..tasks import environments
        bench.detail = "checking that its tasks' container images are public"
        tasks_dir = source / entry.get("tasks", ".") if not foreign(entry) else source     # its tasks', not its archive's
        found = await asyncio.to_thread(environments.check, tasks_dir if tasks_dir.is_dir() else source)
        why = environments.verdict(found)
        if why:
            if await confirm_drop(s, bench, why, "environment") == "drop":
                return _skip(bench, "dropped: its tasks run in images that are not public", "environment_private")
            return _skip(bench, "its tasks run in images that are not public: left out of this run", "environment_private")
        partly = environments.partly(found)
        if partly:
            s.note_recent("checked", bench.id, partly[:220])
            entry = {**entry, "environments": partly}       # told to whoever writes its adapter
        bench.phase, bench.detail = "fetch", "fetched"
    more = {"supported": entry["id"], "license": entry.get("license", ""), "version": entry.get("version", ""),
            "released": entry.get("released", ""), "commit": entry.get("commit", ""), "github": entry.get("github", "")}
    if not foreign(entry):
        bench.path = source
        tasks_mod.register(ws, bench.id, source, **more)
        return True

    adapter_dir, work = adapter_mod.folder(ws, bench.id), work_dir(ws, bench.id)
    sandbox, oracle = s.cfg.run.sandbox != "none", 0 if s.simulate else s.cfg.adapt.oracle_tasks
    trying = s.sandbox      # a simulated run, whose records are kept apart: it builds nothing and needs no lock
    have = adapter_mod.code_dir(adapter_dir) is not None and STUB not in (adapter_mod.code_dir(adapter_dir) / "adapter.py").read_text()
    if trying and not have:
        return _skip(bench, "it is not in Harbor format and has no adapter yet. A real run builds one", "no_adapter")
    if not trying and not (have and approved(real, adapter_mod.digest(adapter_dir))):
        if not await _build_and_share(s, bench, entry, source, adapter_dir, work, sandbox, oracle, have):
            return False
    bench.phase, bench.detail = "adapt", "the index's adapter writes the tasks"
    out = work / "tasks"
    ok, said = await asyncio.to_thread(adapter_mod.generate, adapter_dir, source, out, sandbox)
    problems, _ = adapter_mod.inspect(out, entry.get("task_count")) if ok else ([f"it failed when run: {said or 'no output'}"], [])
    if problems:
        return _skip(bench, f"its adapter did not write usable tasks: {problems[0][:160]}", "adapter_failed")
    bench.path = out
    tasks_mod.register(ws, bench.id, out, adapter=adapter_mod.digest(adapter_dir), **more)
    return True


async def _build_and_share(s: Session, bench: BenchState, entry: dict, source: Path, adapter_dir: Path, work: Path,
                           sandbox: bool, oracle: int, have: bool) -> bool:
    """Get an adapter that passes, asking a person only when the models cannot. The signed record that it passed
    is what lets every later run use it, so it needs no lock from the owner."""
    bench.phase = "adapt"
    say = lambda text: setattr(bench, "detail", text)  # noqa: E731

    def usage(tier: str, cost: float, tokens: int) -> None:
        s.backend.usage(tier, cost, tokens)
        bench.cost += cost

    def drop(built: Built, reason: str) -> None:
        record(lambda *a, **k: s.record(bench.id, a[0], digest_override=k.pop("digest"), **k), s.cfg, adapter_dir, built, oracle,
               "drop", reason[:300])

    def failures(built: Built) -> str:
        return "; ".join(p[:400] for p in built.problems[:2])

    worker_rounds = max(1, s.cfg.adapt.frontier_after_round)
    frontier_rounds = max(1, s.cfg.adapt.max_rounds - s.cfg.adapt.frontier_after_round)
    try:
        feedback = ""
        if have:        # someone wrote or changed it: check what is there before spending on a model
            say("checking the adapter that is there")
            built = Built()
            built.problems, built.notes, built.out = await check(adapter_dir, source, work, entry, sandbox, oracle, limits_of(s.cfg))
            feedback = "\n".join(built.problems)
        if not have or built.problems:
            built = await write_and_check(s.cfg, adapter_dir, entry, source, work, sandbox=sandbox, oracle=oracle, say=say, usage=usage,
                                          feedback=feedback, rounds=worker_rounds, tier="worker")
        if built.problems:
            feedback = "\n".join(built.problems)
            say("the decision model judges whether it can be adapted at all")
            try:
                choice, prob, cost, tokens, tag = await writer.judge(s.cfg, entry, source, built.problems, built.said)
                usage("decision", cost, tokens)
            except (RuntimeError, NotImplementedBackend) as err:     # no judgment: the stronger model tries, as for "can"
                choice, prob, tag = "", 0.0, ""
                s.note_recent("error", bench.id, f"the decision model could not judge the adapter: {str(err)[:120]}")
            s.record(bench.id, "decision", stage="adapt", gate="adaptable", value=choice or "declined", outcome="judged", probability=round(prob, 3),
                     tier="decision", model=s.cfg.tiers.decision.model, skill=tag,
                     digest_override=adapter_mod.digest(adapter_dir))        # a judgment of this version of the adapter
            if choice == "cannot_be_adapted" and prob >= s.cfg.escalation.min_confidence:   # sure no adapter could work: dropped
                said = await confirm_drop(s, bench, f"The worker model could not adapt it, and the decision model judges that no "
                                          f"adapter could (probability {prob:.0%}).\nWhat failed: {failures(built)}", "adapter", record=False)
                if said != "drop":
                    return _skip(bench, "the decision model judges it cannot be adapted: left out of this run", "adapter_failed")
                drop(built, f"judged not adaptable ({prob:.0%}): {failures(built)}")
                return _skip(bench, "dropped: the decision model judges it cannot be adapted", "abandoned")
            else:
                s.note_recent("to fix", bench.id, "the worker model's adapter does not pass, and it can be adapted: the frontier model tries")
            bench.phase = "adapt"
            more = await write_and_check(s.cfg, adapter_dir, entry, source, work, sandbox=sandbox, oracle=oracle, say=say, usage=usage,
                                         feedback=feedback, rounds=frontier_rounds, tier="frontier")
            more.cost, more.tokens, more.rounds = more.cost + built.cost, more.tokens + built.tokens, more.rounds + built.rounds
            built = more
            if built.problems:          # the strongest model failed too: a person confirms that it is dropped for good
                reason = failures(built)
                said = await confirm_drop(s, bench, f"Neither the worker model nor the frontier model could adapt it.\nWhy: {reason}", "adapter",
                                          record=False)
                if said != "drop":
                    return _skip(bench, "its adapter failed, and you kept it listed: it is left out of this run", "adapter_failed")
                drop(built, f"the frontier model: {reason}")
                return _skip(bench, "dropped: neither the worker model nor the frontier model could adapt it", "abandoned")
    except (RuntimeError, NotImplementedBackend) as err:
        return _skip(bench, f"the model did not return an adapter: {str(err)[:160]}", "model_error")

    s.note_recent("checked", bench.id, f"its adapter passes: {built.tasks} tasks, the same on a second run"
                  + (f", written by the {built.tier} model in {built.rounds} round{'s' if built.rounds != 1 else ''} for ${built.cost:.2f}"
                     if built.tier != "human" else ""))
    if s.cfg.adapt.require_review:
        bench.phase, bench.detail = "review", "its adapter waits for your go-ahead"
        answer = await s.ask("lock", f"The adapter for {bench.id} passes its checks: {built.tasks} tasks. It is in {adapter_dir}. "
                             "Read it, then let its tasks be used.", bench.id)
        if answer != "lock":
            return _skip(bench, f"its adapter passes and waits for your go-ahead: /lock {bench.id} in a run", "adapter_not_locked")
    record(lambda *a, **k: s.record(bench.id, a[0], digest_override=k.pop("digest"), **k), s.cfg, adapter_dir, built, oracle)
    bench.phase, bench.detail = "adapt", "sharing the adapter"
    if s.sync.enabled:
        async with s._sync_lock:
            await asyncio.to_thread(s.sync.define, f"adapter for {bench.id}", (f"{adapter_mod.FOLDER}/{bench.id}",))
        await s.publish(f"adapter for {bench.id}")
    return True
