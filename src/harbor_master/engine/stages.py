"""The stages a task goes through inside one working cell (DESIGN.md Section 4).

First the task is checked without running anything: verify audits its reference solution, and probe its tests against
its instruction, each the same way (`audited`). A task with no reference solution has one written, as a repair. Before
a repair is paid for, the decision model says what would make it valid (write the solution, repair the solution, the
verifier or the environment) or that nothing would, and then it is dropped at once.

Every cheap check comes first, and they may loop through a repair: verify, probe, the worker agent's
tries, and the audit of how it failed. A repair makes a new version of the task, and that version goes
through all of them again. Only a task that passes them all reaches the difficulty run, the costly one:
the frontier agents' trials, last, once, and never part of the loop. Then the report.

Each stage returns ("ok", ""), ("fix", defect), or ("drop", reason). To add a stage, write one
function with that contract and put it in the tuple in `process`.
"""
from __future__ import annotations

import contextlib

import asyncio
import hashlib
import time
from typing import TYPE_CHECKING

from ..backends import NotImplementedBackend
from ..backends.base import CHECKS, Decision, TaskDropped, Unreachable
from ..core.config import SuiteEntry
from ..curation import difficulty
from ..tasks import Task
from ..tasks.model import digest_dir
from .state import STAGE, STAGE_NO, STAGES, CellState

if TYPE_CHECKING:
    from .session import Session


STEPS = [("verify", None), ("probe", None), ("worker", None), ("audit", None), ("difficulty", None)]   # filled in below


def recall(s: Session, task: Task) -> None:
    """Hand the backend the trials already recorded for this version of the task, by this run or an earlier one, so that
    the audits read them even when the stages that ran them are not run again."""
    for e in s.ledger.by_task.get(task.task_id, []):
        if e["type"] == "trial" and e.get("digest") == task.digest:
            s.backend.remember(task, e)


async def process(s: Session, cell: CellState, task: Task) -> None:
    bench = s.benches[task.benchmark]
    rounds = 0
    while True:
        recall(s, task)
        for name, step in STEPS:
            kind, info = await attempt(s, cell, task, name, step, rounds)
            if kind != "ok":
                break
            if name == s.until:          # this run only goes this far
                s.board(task)["status"] = f"passed {name}, stopped there"
                s.save_board(task)
                s.note_recent("checked", task.task_id, f"passed every stage up to {name}")
                return
        if kind == "ok":
            break
        if kind == "restart":           # a repair after an error made a new version: every check runs on it again
            continue
        if kind == "fix" and info in ("oracle", "solution"):     # not yet a valid task: is it worth a repair?
            kind, info = await diagnosed(s, task, info)
        if kind == "fix":
            rounds += 1
            try:
                fixed = False
                while rounds <= s.cfg.fix.max_rounds and not fixed:     # a round that could not repair it hands it on:
                    fixed = await fix(s, cell, task, info, rounds)      # the worker model first, then the frontier model
                    if not fixed and rounds < s.cfg.fix.max_rounds:
                        rounds += 1
                    elif not fixed:
                        break
                if fixed:
                    continue
            except NotImplementedBackend:
                s.note_recent("to fix", task.task_id, f"{s.board(task).get('why', info)} (repair is not built)")
            kind, info = "drop", f"unrepaired:{info}"
            s.record(task.benchmark, "decision", task, stage="fix", gate="repair", tier="code",
                     value=f"{rounds} round{'s' if rounds != 1 else ''}", outcome="drop", reason=info)
        if any(tid == task.task_id for tid, _ in bench.left):     # left out of this run only, with no outcome
            s.board(task)["status"] = "left for the next run"
            s.save_board(task)
            return
        bench.dropped += 1                  # dropped by the workflow: no report, no box, and the cell takes the next task
        s.board(task)["status"] = "dropped"
        s.board(task)["reason"] = info
        s.save_board(task)
        s.note_recent("dropped", task.task_id, s.board(task).get("why", info))
        return
    await report(s, cell, task)
    bench.kept += 1
    s.report_ready(task)
    board = s.board(task)
    s.note_recent("kept", task.task_id, f"{board.get('difficulty', '')}, report ready, it recommends {board.get('recommendation', 'accept')}")


async def attempt(s: Session, cell: CellState, task: Task, name: str, step, rounds: int) -> tuple[str, str]:
    """One stage. A stage before the difficulty run that errors (an image that does not build, a session that gave no
    answer) is not left for later: the frontier model reads the error and repairs the task if the task causes it, and the
    stage runs again. A second error in the same stage drops the task. An error in the difficulty run is the run's own,
    and so is a model that could not be reached (Unreachable): the task is left for the next run, neither repaired nor
    dropped."""
    try:
        return await step(s, cell, task, rounds)
    except TaskDropped as gone:          # a trial hung, and the person chose to drop the task
        s.board(task)["why"] = gone.why
        return "drop", gone.reason
    except (NotImplementedBackend, Unreachable):
        raise
    except Exception as err:
        if name == "difficulty":
            raise
        said = f"{type(err).__name__}: {str(err)[:600]}"
        errored = task.meta.setdefault("_errored", [])
        if name in errored:
            s.board(task)["why"] = f"the {name} stage failed again after the frontier model looked at it: {said[:160]}"
            s.record(task.benchmark, "decision", task, stage=name, gate="error", tier="code", value=said[:200],
                     outcome="drop", reason=f"error:{name}")
            return "drop", f"error:{name}"
        errored.append(name)
        s.note_recent("error", task.task_id, f"{name}: {said[:140]}. The frontier model looks at it, then the stage runs again")
        s.board(task)["why"] = f"the {name} stage failed with an error: {said}"
        try:
            fixed = await fix(s, cell, task, "error", s.cfg.fix.frontier_after_round + 1)     # always the frontier tier
        except NotImplementedBackend:
            fixed = False
        if fixed:
            return "restart", ""
        return await attempt(s, cell, task, name, step, rounds)


async def diagnosed(s: Session, task: Task, defect: str) -> tuple[str, str]:
    """The decision model's answer on a task that is not yet valid, before a repair is paid for: ("drop", why) when it
    is sure nothing would make the task valid, else ("fix", defect), with what it judged added for the repair."""
    board = s.board(task)
    try:
        d = await s.backend.diagnose(task, defect, board.get("why", defect))
    except NotImplementedBackend:
        return "fix", defect
    s.record(task.benchmark, "decision", task, stage="verify", gate="diagnose", tier=d.tier, model=d.model, value=d.value or "declined",
             probability=round(d.prob, 3), outcome="drop" if d.value == "drop" else "pass", cost=d.cost, rationale=d.rationale[:400],
             skill=d.skill)
    if d.value == "drop" and d.prob >= s.cfg.escalation.min_confidence:
        board["why"] = f"{board.get('why', defect)}; the decision model judges nothing would make it valid: {d.rationale}"
        return "drop", "not_valid"
    if d.value:
        board["why"] = f"{board.get('why', defect)}. The decision model judges: {d.value.replace('_', ' ')}. {d.rationale}".strip()
    return "fix", defect


def enter(cell: CellState, stage: int, detail: str, attention: bool = False) -> float:
    cell.stage, cell.detail, cell.attention = stage, detail, attention
    if cell.on_stage is not None:
        cell.on_stage(STAGES[stage])
    STAGE.set(STAGES[stage])        # model calls from here on are counted under this stage
    return time.time()


async def trial(s: Session, task: Task, purpose: str, agent: str, model: str, index: int, run,
                version: str = "", effort: str = "") -> dict:
    """Run one slot, or reuse it when the ledger already has it. Returns the trial's ledger event.

    Every run of an agent or of the verifier goes through here, whatever it is for: the reference
    solution, a no-op, a difficulty trial during curation, or a benchmark trial afterwards. So they
    all leave the same record, with the same fields, and the same Harbor folder behind it.

    The slot is keyed by the pinned version. The event also records the version that
    actually ran, as the backend read it from the agent, so a drifted build shows up.
    """
    prior = s.ledger.trial(task.digest, agent, version, model, index, effort)
    if prior is not None:
        return prior
    result = await s.container(run())
    s.trial_cost += result.cost
    from .prepare import PULL, SLOW
    while result.error and not PULL.search(result.error) and not SLOW.search(result.error) and await s.failed(
            task.name, f"{purpose} trial {index + 1}" + (f" of {agent} with {model}" if model else ""), result.error):
        result = await s.container(run())       # the person chose to try it again
        s.trial_cost += result.cost
    from . import claims
    with contextlib.suppress(Exception):        # a finished trial is progress: the benchmark's claim is kept alive
        await claims.renew(s)
    return s.record(task.benchmark, "trial", task, purpose=purpose, agent=agent, agent_version=version,
                    agent_version_ran=result.agent_version, model=model, reasoning_effort=effort, trial_index=index,
                    passed=result.passed, reward=result.reward, tokens_in=result.tokens_in,
                    tokens_out=result.tokens_out, cost=result.cost, container_seconds=round(result.seconds, 1),
                    verifier_seconds=round(result.verifier_seconds, 1), task_checksum=result.checksum,
                    artifact=result.artifact, error=result.error)


async def stage_verify(s: Session, cell: CellState, task: Task, rounds: int):
    """Is the reference solution valid: does it solve the task without reading the tests, hard-coding answers or faking
    the work? Nothing is run: it is audited (see `audited`). A task with no reference solution has one written first,
    as a repair."""
    if not task.has_solution:
        enter(cell, STAGE_NO["verify"], "it has no reference solution")
        s.board(task)["why"] = "it has no reference solution"
        if not s.cfg.triage.require_reference_solution:
            return "drop", "no_reference_solution"
        return "fix", "solution"
    return await audited(s, cell, task, "solution")


async def stage_probe(s: Session, cell: CellState, task: Task, rounds: int):
    """Does the instruction state everything the tests check? An unstated check is the commonest reason a task that looks
    hard is only unclear. It is audited (see `audited`)."""
    return await audited(s, cell, task, "coverage")


async def audited(s: Session, cell: CellState, task: Task, check: str) -> tuple[str, str]:
    """One of the checks before any agent trial, the same way for each (CHECKS in backends/base.py):

    1. The worker agent reads the task's files and writes what it found.
    2. The decision model decides from that, with a probability.
    3. Unsure (under the escalation threshold, or no answer), or against the worker's own verdict, however sure: the
       frontier agent audits again, shown the worker's audit, and the decision model decides from both.
    4. A decision to reject goes to a person in a box, who drops the task or lets it go on, but only when the last agent
       to audit gave the same verdict: one that found nothing decisive is not overruled by the decision model alone.

    Each step is recorded. A decision already made for this version of the task, under this protocol or an earlier one, is used as it is:
    a person's answer stands, and a rejection nobody answered is asked again."""
    c = CHECKS[check]
    stage = c["stage"]
    prior = s.ledger.decision(task.digest, stage, check, s.protocol)
    if prior is not None and prior["outcome"] != "escalate":
        if prior["outcome"] == "pass":
            return "ok", ""
        if prior.get("tier") == "human":
            s.board(task)["why"] = f"{c['says']}, and a person confirmed the drop"
            return "drop", f"{check}_rejected"
        verdicts = " ".join(f"The {t} agent's verdict: {v}." for t, v in (prior.get("verdicts") or {}).items())
        return await confirm_drop(s, task, check, f"{verdicts} The decision model decided {prior.get('value') or 'nothing'}."
                                  f"\n\nThe {prior.get('tier', 'last')} agent found:\n{prior.get('rationale', '')}")
    t0 = enter(cell, STAGE_NO[stage], c["doing"])
    floor = s.cfg.escalation.gate_min_confidence.get(check, s.cfg.escalation.min_confidence)
    if prior is not None and prior.get("tier") == "worker":     # run again after an error: the worker escalated it already
        first = Decision(value=prior.get("verdict") or "", prob=0.0, tier="worker", model=prior.get("model", ""),
                         rationale=prior.get("rationale", ""), skill=prior.get("skill", ""))
        audits = [first]
        escalate = True
        cell.detail = "run again: the frontier agent audits what the worker found"
    else:
        first = await s.backend.check_audit(task, check, "worker")
        audits = [first]
        d = await s.backend.check_decide(task, check, audits)
        escalate = not d.value or d.prob < floor or d.value != first.value
        if escalate:
            s.record(task.benchmark, "decision", task, stage=stage, gate=check, tier="worker", model=first.model, value=d.value,
                     probability=round(d.prob, 3), outcome="escalate", cost=first.cost + d.cost,
                     rationale=first.rationale[:2000], skill=first.skill, verdict=first.value)
            cell.detail = ("the decision went against the worker's verdict" if d.value and d.value != first.value else
                           "the decision was unsure") + ": the frontier agent audits it"
    if escalate:
        audits.append(await s.backend.check_audit(task, check, "frontier", [first]))
        d = await s.backend.check_decide(task, check, audits)
    last = audits[-1]
    ok = bool(d.value) and d.value != c["reject"]
    # A rejection rests on a decisive point the agent that read the files found itself. The decision model reads only its
    # audit: rejecting against that agent's own verdict, or giving no answer, does not put the task in front of a person.
    overruled = not ok and bool(last.value) and last.value != c["reject"]
    ok = ok or overruled
    verdicts = {a.tier: a.value for a in audits}
    s.record(task.benchmark, "decision", task, stage=stage, gate=check, tier=last.tier, model=last.model, value=d.value,
             probability=round(d.prob, 3), outcome="pass" if ok else "reject", cost=last.cost + d.cost,
             rationale=last.rationale[:2000], skill=last.skill, verdicts=verdicts, overruled=overruled)
    if overruled:
        s.note_recent("checked", task.task_id, f"the decision model said {d.value or 'nothing'}, but the {last.tier} agent, "
                      f"which read the files, found nothing decisive: it goes on")
    s.stage_done(task, STAGE_NO[stage], t0, check=check, value=d.value or "no answer", probability=round(d.prob, 3),
                 tiers=[a.tier for a in audits], rationale=last.rationale)
    if ok:
        return "ok", ""
    said = " ".join(f"The {tier} agent's verdict: {value}." for tier, value in verdicts.items())
    said += (f" The decision model decided {d.value}, at p={d.prob:.2f}." if d.value else
             " The decision model gave no answer, even after the frontier agent's audit.")
    return await confirm_drop(s, task, check, f"{said}\n\nThe {last.tier} agent found:\n{last.rationale}")


async def confirm_drop(s: Session, task: Task, check: str, found: str) -> tuple[str, str]:
    """A check decided to reject the task. A person says whether it is dropped or goes on. With nobody to ask it is
    dropped for this run, and nothing is signed: the next run asks again."""
    c = CHECKS[check]
    s.board(task)["why"] = c["says"]
    s.note_recent("to drop", task.task_id, f"{c['says']}: a person decides")
    text = (f"{task.task_id}: {c['says']}.\n\n{found.strip()[:2400]}\n\n"
            "Drop drops the task. Continue ignores this, and the task goes on to its next stage.")
    said = await s.ask("drop", text, task.task_id, ["drop", "continue"])
    if said is None:                # nobody to ask: left out of this run only. The agent's rejection stands, and with no
        # outcome recorded the next run takes the task again and asks
        if ("decision", task.digest, c["stage"], check, s.protocol, "code") not in s.ledger.taken:   # once: asked again later
            s.record(task.benchmark, "decision", task, stage=c["stage"], gate=check, tier="code", value="unconfirmed",
                     outcome="unconfirmed", reason=f"{check}_rejected")
        s.benches[task.benchmark].left.append((task.task_id, f"{c['says']}: nobody was there to confirm the drop"))
        return "drop", f"{check}_rejected"
    s.record(task.benchmark, "decision", task, stage=c["stage"], gate=check, tier="human", value=said,
             outcome="drop" if said == "drop" else "pass", reason=f"{check}_rejected" if said == "drop" else "")
    if said == "drop":
        s.board(task)["why"] = f"{c['says']}, and a person confirmed the drop"
        return "drop", f"{check}_rejected"
    s.note_recent("checked", task.task_id, f"{c['says']}, but a person let it go on")
    return "ok", ""


async def until_settled(chains, stop: asyncio.Event) -> int:
    """Runs `chains` (coroutines, each a series of trials) side by side until all of them end or one sets `stop`
    because the result can no longer change. What is still running is then cancelled, which stops its Harbor job and
    removes its containers; a trial stopped so is not recorded. An error in one (a hung trial the person dropped)
    stops the others and is raised. Returns how many were stopped."""
    running = {asyncio.ensure_future(c) for c in chains}
    stopper = asyncio.ensure_future(stop.wait())
    try:
        while running:
            done, _ = await asyncio.wait(running | {stopper}, return_when=asyncio.FIRST_COMPLETED)
            running -= done
            for t in done - {stopper}:
                if t.exception() is not None:
                    raise t.exception()
            if stopper in done:
                break
        return len(running)
    finally:
        stopper.cancel()
        for t in running:
            t.cancel()
        await asyncio.gather(*running, return_exceptions=True)


async def cheap_solve(s: Session, cell: CellState, task: Task) -> list[dict] | None:
    """The worker tier's model tries first, `cheap_trials` times side by side, before any frontier agent runs.
    Returns its trials when every one passed: a task a smaller model solves each time is too easy, and the
    suite's trials are never spent on it. One pass among failures is not enough to say so."""
    d = s.cfg.difficulty
    if not d.cheap_solve or d.cheap_trials < 1:
        return None
    version = next((e.version for e in d.suite if e.agent == d.cheap_agent), "")
    entry = SuiteEntry(agent=d.cheap_agent, model=s.cfg.tiers.worker.model, version=version)
    if any((e.agent, e.model, e.reasoning_effort) == (entry.agent, entry.model, entry.reasoning_effort) for e in d.suite):
        return None                 # the very configuration the suite runs: there is no cheaper try to make
    cell.detail = f"{d.cheap_trials} tries by {entry.model}"
    got, failed = [], asyncio.Event()

    async def one(i: int) -> None:
        e = await trial(s, task, "cheap_solve", entry.agent, entry.model, i,
                        lambda: s.backend.run_trial(task, entry, i, d.cheap_agent_hours), version=entry.version)
        got.append(e)
        if not e["passed"] or e.get("error"):     # one failure settles it: the task is not solved every time
            failed.set()
    stopped = await until_settled([one(i) for i in range(d.cheap_trials)], failed)
    if stopped:
        s.note_recent("checked", task.task_id, f"the worker agent failed a try: its other {stopped} stopped, not needed")
    return sorted(got, key=lambda e: e["trial_index"]) if not failed.is_set() else None


async def stage_worker(s: Session, cell: CellState, task: Task, rounds: int):
    """The worker agent tries the task, `cheap_trials` times side by side. A task it solves every time is too easy,
    and no frontier trial is ever spent on it. One pass among failures is not enough to say so. Cheap, so it is part
    of the loop: a repaired task is tried again. How it failed is what the audit reads next."""
    prior = s.ledger.decision(task.digest, "worker", "worker_solve", s.protocol)
    if prior is not None:
        if prior["outcome"] == "drop":
            s.board(task)["difficulty"] = prior["value"]
            return "drop", "solved_by_worker"
        return "ok", ""
    d = s.cfg.difficulty
    t0 = enter(cell, STAGE_NO["worker"], "the worker agent tries it")
    solved = await cheap_solve(s, cell, task)
    if solved is None:
        s.record(task.benchmark, "decision", task, stage="worker", gate="worker_solve", tier="code",
                 value="not solved every time", outcome="pass")
        s.stage_done(task, STAGE_NO["worker"], t0, cheap_solve="failed" if d.cheap_solve else "off")
        return "ok", ""
    n, model = len(solved), solved[0]["model"]
    value = f"solved by the worker agent, {n} of {n} tries"
    s.record(task.benchmark, "decision", task, stage="worker", gate="worker_solve", tier="code", value=value,
             outcome="drop", cheap_trials=n, model=model, reason="solved_by_worker")
    s.stage_done(task, STAGE_NO["worker"], t0, cheap_solve="passed", cheap_trials=n)
    s.board(task)["difficulty"] = value
    s.board(task)["why"] = f"{model} solved it in {'each of its ' + str(n) + ' tries' if n > 1 else 'one try'}, so no frontier trial was run"
    return "drop", "solved_by_worker"


async def stage_difficulty(s: Session, cell: CellState, task: Task, rounds: int):
    """The frontier agents' trials: last, once per task, and never part of the repair loop. Only a task that passed
    every cheap check gets here, so the result is measured on the version that is reported and accepted."""
    d = s.cfg.difficulty
    n_total = len(d.suite) * s.cfg.difficulty.trials
    prior = s.ledger.decision(task.digest, "difficulty", "pass_rate", s.protocol)
    if prior is not None:
        s.board(task)["difficulty"] = prior["value"]
        return ("ok", "") if prior["outcome"] == "pass" else ("drop", prior.get("reason") or "too_easy")
    t0 = enter(cell, STAGE_NO["difficulty"], f"0 of {n_total} trials run")
    passes = fails = 0
    result = None
    settled = asyncio.Event()
    cell.detail = f"0 of {n_total} trials run · 0 passed"

    async def one(entry, r):
        """One trial, counted on the screen and told in the log the moment it ends."""
        nonlocal passes, fails, result
        e = await trial(s, task, "difficulty", entry.agent, entry.model, r,
                        lambda: s.backend.run_trial(task, entry, r), version=entry.version, effort=entry.reasoning_effort)
        passes, fails = passes + bool(e["passed"]), fails + (not e["passed"])
        cell.detail = f"{passes + fails} of {n_total} trials run · {passes} passed"
        minutes = (e.get("container_seconds") or 0) / 60
        s.note_recent("checked", task.task_id, f"difficulty try {r + 1} of {d.trials} by {entry.agent} ({entry.model}): "
                      + ("passed" if e["passed"] else "failed") + (f" after {minutes:.0f} min" if minutes >= 1 else "")
                      + (f", {e['error'][:80]}" if e.get("error") else ""))
        result = difficulty.outcome(passes, fails, n_total, d.max_pass_rate)
        if result and d.early_stop:     # checked after every trial: once it cannot change, nothing more is run
            settled.set()

    taken = [0] * len(d.suite)          # how many of each agent's tries have been started

    def turn(after: int) -> int | None:
        """The agent to try next after `after`: the next one in the suite that has tries left, `after` itself last."""
        for k in range(1, len(d.suite) + 1):
            j = (after + k) % len(d.suite)
            if taken[j] < d.trials:
                return j
        return None

    async def slot(j: int | None):
        """One trial at a time, as many slots as agents. When a trial ends, its slot starts the other agent's next
        try, so the agents take turns: Astra ends, Claude Code starts; Claude Code ends, Astra starts."""
        while j is not None and not settled.is_set():
            r = taken[j]
            taken[j] += 1
            await one(d.suite[j], r)
            j = turn(j)
    starts = range(len(d.suite)) if d.trials > 0 else []     # each slot starts with its own agent
    stopped = await until_settled([slot(j) for j in starts], settled)
    if stopped:
        s.note_recent("checked", task.task_id, f"settled at {passes} of {passes + fails} passed: "
                      f"{stopped} running trial{'s' if stopped > 1 else ''} stopped, not needed")
    result = result or ("drop" if passes > difficulty.max_passes(n_total, d.max_pass_rate) else "keep")
    value = f"{passes} of {passes + fails} passed"
    early = passes + fails < n_total
    s.record(task.benchmark, "decision", task, stage="difficulty", gate="pass_rate", tier="code",
                value=value, outcome="pass" if result == "keep" else "drop", passes=passes,
                trials=passes + fails, stopped_early=early, reason="" if result == "keep" else "too_easy")
    s.stage_done(task, STAGE_NO["difficulty"], t0, passes=passes, trials=passes + fails, stopped_early=early)
    s.board(task)["difficulty"] = value
    if result == "keep":
        return "ok", ""
    s.board(task)["why"] = f"{value}{', stopped early' if early else ''}"
    return "drop", "too_easy"


async def stage_audit(s: Session, cell: CellState, task: Task, rounds: int):
    prior = s.ledger.decision(task.digest, "audit", "quality", s.protocol)
    if prior is not None:
        return {"accept": ("ok", ""), "fix": ("fix", "design")}.get(prior["value"], ("drop", "broken"))
    t0 = enter(cell, STAGE_NO["audit"], "reading how the worker agent failed")
    d = await s.backend.audit(task, rounds)
    tiers = [d.tier]
    if d.value == "unsure":
        s.record(task.benchmark, "decision", task, stage="audit", gate="quality", tier=d.tier, model=d.model,
                    value=d.value, probability=d.prob, outcome="escalate", cost=d.cost, rationale=d.rationale)
        enter(cell, STAGE_NO["audit"], "unsure, asking tier F", attention=True)
        d = await s.backend.second_opinion(task, rounds)
        tiers.append(d.tier)
    outcome = {"accept": "pass", "fix": "fix"}.get(d.value, "drop")
    s.record(task.benchmark, "decision", task, stage="audit", gate="quality", tier=d.tier, model=d.model,
                value=d.value, probability=d.prob, outcome=outcome, cost=d.cost, rationale=d.rationale,
                reason="" if outcome != "drop" else "broken")
    s.stage_done(task, STAGE_NO["audit"], t0, verdict=d.value, tiers=tiers, rationale=d.rationale)
    cell.attention = False
    if d.value == "accept":
        return "ok", ""
    s.board(task)["why"] = d.rationale
    return ("fix", "design") if d.value == "fix" else ("drop", "broken")


async def fix(s: Session, cell: CellState, task: Task, defect: str, round_no: int) -> bool:
    tier = "frontier" if round_no > s.cfg.fix.frontier_after_round else "worker"
    why = s.board(task).get("why", defect)
    t0 = enter(cell, STAGE_NO["fix"], why, attention=True)
    s.note_recent("to fix", task.task_id, why)
    notes = s.notes.get(task.task_id, [])
    result = await s.backend.repair(task, defect, round_no, tier, why)
    if result["fixed"]:
        old = task.digest
        task.meta.setdefault("_fixed", []).append(defect)
        if result.get("path"):          # a real repair: a new copy of the task, and its own digest
            task.path = result["path"]
            task.digest = digest_dir(task.path)
            if (task.path / "instruction.md").is_file():
                task.instruction = (task.path / "instruction.md").read_text()
            solution, tests = task.path / "solution", task.path / "tests"
            task.has_solution = solution.is_dir() and any(solution.iterdir())
            task.has_tests = tests.is_dir() and any(tests.iterdir())
        else:
            task.digest = hashlib.sha256(f"{old}|{defect}|{round_no}".encode()).hexdigest()
            task.has_solution = task.has_solution or defect == "solution"      # a simulated repair writes no file
        s.record(task.benchmark, "repair", task, old_digest=old, new_digest=task.digest, tier=tier,
                    what=result["what"], round=round_no, cost=result["cost"], notes=notes)
    s.stage_done(task, STAGE_NO["fix"], t0, round=round_no, defect=defect, tier=tier, fixed=result["fixed"],
                     what=result["what"])
    cell.attention = False
    return result["fixed"]


async def report(s: Session, cell: CellState, task: Task) -> None:
    """The report a person signs a verdict on, for a task the workflow kept: its frontier failures are audited first.
    A task the workflow dropped gets none."""
    if any(e["type"] == "report" and e["digest"] == task.digest for e in s.ledger.by_task.get(task.task_id, [])):
        return
    t0 = enter(cell, STAGE_NO["report"], "auditing the failed trials")
    recall(s, task)
    board = s.board(task)
    recommend, why = "accept", ""
    try:                    # no repair can follow the difficulty run: what the audit finds goes to the reviewer
        d = await s.backend.final_audit(task)
        recommend = {"accept": "accept", "broken": "reject"}.get(d.value, "check")
        why = d.rationale
        board["final_audit"] = {"verdict": d.value, "rationale": d.rationale}
        s.record(task.benchmark, "decision", task, stage="report", gate="final_audit", tier=d.tier, model=d.model, value=d.value,
                 probability=d.prob, outcome="pass", recommendation=recommend, cost=d.cost, rationale=d.rationale)
        if recommend == "reject":
            s.note_recent("to fix", task.task_id, "its failures look like the task's fault: reject")
    except NotImplementedBackend:
        recommend, why = "check", "The frontier trials were not audited: that audit is not built yet. Read them before signing."
    cell.detail = "writing the report"
    written = await s.backend.write_report(task, board)
    board["status"] = "report_ready"
    s.save_board(task)
    path = s.report_path(task)
    lead = {"accept": "**Recommendation: accept.**",
            "reject": "**Recommendation: reject.** The frontier agents' failures look like the task's fault, not its difficulty.",
            "check": "**Recommendation: read the frontier trials before signing.**"}[recommend]
    banner = " ".join([lead, *_reasons(board), why.strip()]).strip()
    if s.assumed:
        banner = ("**ASSUMED RESULTS. These stages were not run: " + ", ".join(sorted(s.assumed)) + ". Their results "
                  "below are made up to exercise the pipeline, and this task cannot be accepted on them.**")
    lines = [f"# {task.task_id}", "", banner, "", "## What the task asks", "",
             task.instruction.strip()[:600] or "(no instruction text)", "", "## Evidence", ""]
    for done in board["stages"]:
        lines.append(f"- **{done['stage']}** ({done['seconds']}s): {_evidence(done)}")
    lines += _earlier(s, task, {done["stage"] for done in board["stages"]})
    if s.notes.get(task.task_id):
        lines += ["", "## Notes", ""] + [f"- {n}" for n in s.notes[task.task_id]]
    text = "\n".join(lines) + "\n"
    path.write_text(text)
    board["recommendation"] = recommend
    s.record(task.benchmark, "report", task, recommendation=recommend, cost=written.get("cost", 0.0),
             location=str(path.relative_to(s.ws)), report_digest=hashlib.sha256(text.encode()).hexdigest()[:16])
    s.stage_done(task, STAGE_NO["report"], t0)


def _reasons(board: dict) -> list[str]:
    """Why the recommendation, in a few sentences from what the stages found: that the task is valid, what was
    repaired, how the verifier stood up to wrong answers, and how hard it was for the cheap and the frontier agents."""
    def last(stage, key):
        return next((d for d in reversed(board["stages"]) if d["stage"] == stage and key in d), None)
    out = []
    sol, cov = last("verify", "check"), last("probe", "check")
    if sol and sol.get("value") == "valid":
        out.append("An audit found its reference solution valid" + (", and that its instruction states everything its tests check."
                                                                    if cov and cov.get("value") == "covered" else "."))
    fixes = [d for d in board["stages"] if d["stage"] == "fix" and d.get("fixed")]
    if fixes:
        said = "; ".join(d.get("what") or f"its {d.get('defect', 'files')} was mended" for d in fixes)
        out.append(f"{'One repair was' if len(fixes) == 1 else f'{len(fixes)} repairs were'} made: {said}.")
    worker, hard = last("worker", "cheap_solve"), last("difficulty", "passes")
    cheap = "The cheap worker model failed it" if worker and worker["cheap_solve"] == "failed" else ""
    if hard and hard.get("trials"):
        p, n = hard["passes"], hard["trials"]
        frontier = (f"no frontier trial of {n} passed" if p == 0 else f"{p} of {n} frontier trials passed, so it is solvable but hard")
        out.append(f"{cheap}, and {frontier}." if cheap else frontier[:1].upper() + frontier[1:] + ".")
    elif cheap:
        out.append(cheap + ".")
    return out


def _sentences(text: str, most: int = 2, limit: int = 320) -> str:
    """The first sentences of a model's explanation, whole: the log wraps a long line, so it is never cut mid-word."""
    import re
    parts = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    found = [p for p in parts if not re.match(r"I (only |also )?(read|opened|looked|checked|did not|didn't|went|reviewed|inspected)\b", p)]
    parts = found or parts          # what the model read is no finding: the line shows what it found
    out = ""
    for part in parts[:most]:
        if out and len(out) + len(part) > limit:
            break
        out = f"{out} {part}".strip()
    return out if len(out) <= limit else out[:limit].rsplit(" ", 1)[0] + " ..."


def progress(stage: str, r: dict) -> str:
    """One line for the log on what a stage of a task found, as it ends: what a person watching wants to know."""
    n = lambda k: len(r.get(k) or [])  # noqa: E731
    if stage in ("verify", "probe") and "check" in r:
        c = CHECKS[r["check"]]
        by = " and ".join(dict.fromkeys(r.get("tiers") or [])) or "an agent"
        verdict = c["says"] if r["value"] == c["reject"] else ("no answer: a person decides" if r["value"] == "no answer" else
                  {"valid": "the reference solution is valid", "covered": "the instruction states everything the tests check"}.get(r["value"], r["value"]))
        return f"{verdict} (audited by the {by} agent, decided at p={r.get('probability', 0):.2f})"
    if stage == "worker":
        return {"failed": "the worker model failed it: not too easy", "passed": f"the worker model solved it every time: too easy",
                "off": "the worker model's tries are switched off"}.get(r.get("cheap_solve", ""), "")
    if stage == "audit":
        verdict = str(r.get("verdict", "?")).replace("_", " ")         # what is to be fixed has a line of its own
        return f"audit: {verdict}" + (f". {_sentences(str(r['rationale']))}" if r.get("rationale") and verdict == "accept" else "")
    if stage == "fix" and r.get("defect") == "error" and not r.get("fixed"):
        return "the frontier model found nothing in the task to change: the stage runs again"
    if stage == "fix":
        return (f"repaired the {r.get('defect', 'task')}" + (f": {r['what']}" if r.get("what") else "") if r.get("fixed") else
                f"round {r.get('round', '?')} could not repair the {r.get('defect', 'task')}")
    if stage == "difficulty" and r.get("trials"):
        return f"{r['passes']} of {r['trials']} frontier trials passed" + (", stopped early" if r.get("stopped_early") else "")
    if stage == "report":
        return "report written"
    return ""


def _earlier(s: Session, task: Task, shown: set[str]) -> list[str]:
    """The results of the stages this run did not do again for this version of the task, from the ledger: the standing
    decision of each gate, and every trial. A task that resumed in a later run is reported with all its evidence."""
    lines, seen = [], set()
    for e in s.ledger.by_task.get(task.task_id, []):
        if e.get("digest") != task.digest:
            continue
        if e["type"] == "decision" and e.get("stage") not in shown and (e.get("stage"), e.get("gate")) not in seen:
            standing = s.ledger.decision(task.digest, e.get("stage", ""), e.get("gate", ""), s.protocol)
            if standing is None or standing.get("outcome") == "escalate" or e.get("gate") == "error":
                continue
            seen.add((e.get("stage"), e.get("gate")))
            value = standing.get("value")
            value = ", ".join(f"{k} {v.get('value') if isinstance(v, dict) else v}" for k, v in value.items()) if isinstance(value, dict) else value
            lines.append(f"- **{standing['stage']}** ({standing['gate']}, an earlier run): {value or standing.get('outcome')}"
                         f", {standing.get('outcome')} ({standing.get('tier')})")
        elif e["type"] == "trial" and "trial" not in shown:
            minutes = (e.get("container_seconds") or 0) / 60
            lines.append(f"- **trial** ({e.get('purpose')}, an earlier run): {e.get('agent')} with {e.get('model')}, "
                         + ("passed" if e.get("passed") else "failed") + (f" after {minutes:.0f} min" if minutes >= 1 else "")
                         + (f", {str(e.get('error'))[:80]}" if e.get("error") else ""))
    return lines


def _evidence(done: dict) -> str:
    """One stage's results, as a reader takes them in: yes and no, lists as lists, nothing empty. Per-item detail
    (every mutant, every trial) is in the ledger, not here."""
    shown = []
    for k, v in done.items():
        if k in ("stage", "seconds") or v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, dict) or (isinstance(v, list) and any(isinstance(x, dict) for x in v)):
            continue
        v = ("yes" if v else "no") if isinstance(v, bool) else ", ".join(map(str, v)) if isinstance(v, list) else v
        shown.append(f"{k.replace('_', ' ')} {v}")
    return "; ".join(shown) or "done"


STEPS[:] = [("verify", stage_verify), ("probe", stage_probe), ("worker", stage_worker), ("audit", stage_audit),
            ("difficulty", stage_difficulty)]
