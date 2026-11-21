"""The stages a task goes through inside one working cell (DESIGN.md Section 4).

First the task is made a valid one: verify runs its reference solution and a no-op, and a task with no reference
solution, or one that fails, goes to a repair. Before a repair is paid for, the decision model says what would make it
valid (write the solution, repair the solution, the verifier or the environment) or that nothing would, and then it is
dropped at once.

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
from ..backends.base import TaskDropped
from ..core.config import SuiteEntry
from ..curation import difficulty, trivial
from ..tasks import Task
from ..tasks.model import digest_dir
from .state import STAGE, STAGE_NO, STAGES, CellState

if TYPE_CHECKING:
    from .session import Session


STEPS = [("verify", None), ("probe", None), ("worker", None), ("audit", None), ("difficulty", None)]   # filled in below


async def process(s: Session, cell: CellState, task: Task) -> None:
    bench = s.benches[task.benchmark]
    rounds = 0
    while True:
        for name, step in STEPS:
            try:
                kind, info = await step(s, cell, task, rounds)
            except TaskDropped as gone:      # a trial hung, and the person chose to drop the task
                s.board(task)["why"] = gone.why
                kind, info = "drop", gone.reason
            if kind != "ok":
                break
            if name == s.until:          # this run only goes this far
                s.board(task)["status"] = f"passed {name}, stopped there"
                s.save_board(task)
                s.note_recent("checked", task.task_id, f"passed every stage up to {name}")
                return
        if kind == "ok":
            break
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
    """The reference solution passes, twice. A no-op fails. The verifier is fast enough. A task with no reference
    solution has one written first, as a repair."""
    t0 = enter(cell, STAGE_NO["verify"], "oracle and no-op runs")
    if not task.has_solution:
        s.board(task)["why"] = "it has no reference solution"
        if not s.cfg.triage.require_reference_solution:
            return "drop", "no_reference_solution"
        return "fix", "solution"
    oracle, noop = await asyncio.gather(
        trial(s, task, "oracle", "oracle", "", 0, lambda: s.backend.run_oracle(task)),
        trial(s, task, "noop", "nop", "", 0, lambda: s.backend.run_noop(task)))
    again = None
    if oracle["passed"] and not s.simulate:      # a simulation has no second answer to give
        cell.detail = "oracle again, for determinism"
        again = await trial(s, task, "oracle", "oracle", "", 1, lambda: s.backend.run_oracle(task))
    slowest = max(float(e.get("verifier_seconds") or 0) for e in (oracle, noop, again) if e)
    limit = s.cfg.triage.max_verifier_seconds
    s.stage_done(task, STAGE_NO["verify"], t0, oracle_passed=oracle["passed"], noop_passed=noop["passed"],
                 oracle_again=again["passed"] if again else None, verifier_seconds=slowest,
                 errors=[e["error"] for e in (oracle, noop, again) if e and e.get("error")])
    board = s.board(task)
    if any(e and e.get("error") for e in (oracle, noop, again)):
        error = next(e["error"] for e in (oracle, noop, again) if e and e.get("error"))
        from .prepare import SLOW, limits_of, slow_build, unpullable
        if SLOW.search(error):          # over the protocol's limit on building or verifying: dropped, not repaired
            reason = "verifier_too_slow" if "Verifier" in error else "environment_too_slow"
            board["why"] = slow_build(error, limits_of(s.cfg))
            s.record(task.benchmark, "decision", task, stage="verify", gate="build_time" if reason == "environment_too_slow" else "verifier_time",
                     tier="code", value=error[:120], outcome="drop", reason=reason)
            return "drop", reason
        board["why"] = (unpullable(error, task.path) or "a run failed for an infrastructure reason: " + error[:160])
        return "fix", "oracle"
    if not oracle["passed"]:
        board["why"] = "the reference solution fails its own tests"
        return "fix", "oracle"
    if noop["passed"]:
        board["why"] = "a no-op run passes"
        return "fix", "oracle"
    if again is not None and not again["passed"]:
        board["why"] = "the reference solution passed once and failed once: the verifier is not deterministic"
        return "fix", "oracle"
    if slowest > limit:
        board["why"] = f"the verifier took {slowest:.0f}s, over the {limit}s limit"
        s.record(task.benchmark, "decision", task, stage="verify", gate="verifier_time", tier="code",
                 value=round(slowest, 1), outcome="drop", reason="verifier_too_slow")
        return "drop", "verifier_too_slow"
    return await trivial_submissions(s, cell, task)


async def trivial_submissions(s: Session, cell: CellState, task: Task):
    """The verifier rejects a submission that did no work: the task's output files left empty, filled
    with a placeholder, or copied from a file that was already there. Written by code, no model."""
    prior = s.ledger.decision(task.digest, "verify", "trivial", s.protocol)
    if prior is not None:
        return ("ok", "") if prior["outcome"] == "pass" else ("fix", "verifier")
    tries = trivial.submissions(task.path, task.instruction)
    if not tries:                   # the task names no output file that its tests also name
        return "ok", ""
    t0 = enter(cell, STAGE_NO["verify"], "trivial submissions")
    accepted = await asyncio.gather(*(s.container(s.backend.run_mutant(task, m, "verify")) for m in tries))
    passed = [m for m, a in zip(tries, accepted) if a]
    s.record(task.benchmark, "decision", task, stage="verify", gate="trivial", tier="code", value=f"{len(tries)} tried",
             outcome="fix" if passed else "pass", accepted=[m["id"] for m in passed])
    s.stage_done(task, STAGE_NO["verify"], t0, trivial_tried=len(tries), trivial_accepted=[m["id"] for m in passed])
    if not passed:
        return "ok", ""
    s.board(task)["why"] = f"the verifier accepts a submission that did no work: one that {passed[0]['what']}"
    return "fix", "verifier"


async def stage_probe(s: Session, cell: CellState, task: Task, rounds: int):
    """The verifier is tested from both sides before any agent trial: by mutants of the solution, and by
    asking whether the instruction states everything the tests check."""
    kind, info = await mutants(s, cell, task)
    return (kind, info) if kind != "ok" else await coverage(s, cell, task)


async def coverage(s: Session, cell: CellState, task: Task):
    """An unstated check is the commonest reason a task that looks hard is only unclear. Finding it here
    costs one request. Finding it at the audit costs the frontier trials that came before."""
    prior = s.ledger.decision(task.digest, "probe", "coverage", s.protocol)
    if prior is not None and prior["outcome"] == "pass":
        return "ok", ""
    if prior is not None and prior["outcome"] == "fix" and prior.get("tier") != "decision" and prior.get("rationale"):
        s.board(task)["why"] = prior["rationale"]           # confirmed before, with what is missing: the repair is told it
        return "fix", "design"
    t0 = enter(cell, STAGE_NO["probe"], "instruction against the tests")
    d = await s.backend.covers(task)
    tiers = [d.tier]
    # The decision model's "unstated" names nothing, however sure it is, and it reads only the start of long tests. So it
    # is never acted on as it is: the worker agent reads the task's files, and confirms it, saying what is not stated
    # (which is what the repair is then told to add), or overturns it.
    if d.prob < s.cfg.escalation.min_confidence or d.value == "unstated":
        s.record(task.benchmark, "decision", task, stage="probe", gate="coverage", tier=d.tier, model=d.model,
                 value=d.value, probability=round(d.prob, 3), outcome="escalate", cost=d.cost, skill=d.skill)
        d = await s.backend.covers(task, d)
        tiers.append(d.tier)
    ok = d.value != "unstated"
    s.record(task.benchmark, "decision", task, stage="probe", gate="coverage", tier=d.tier, model=d.model,
             value=d.value, probability=round(d.prob, 3), outcome="pass" if ok else "fix", cost=d.cost,
             rationale=d.rationale, skill=d.skill)
    s.stage_done(task, STAGE_NO["probe"], t0, coverage=d.value, tiers=tiers, rationale=d.rationale)
    if ok:
        return "ok", ""
    s.board(task)["why"] = d.rationale or "the tests check something the instruction does not state"
    return "fix", "design"


async def mutants(s: Session, cell: CellState, task: Task):
    prior = s.ledger.decision(task.digest, "probe", "verifier", s.protocol)
    if prior is not None:
        return ("ok", "") if prior["outcome"] == "pass" else ("fix", "verifier")
    t0 = enter(cell, STAGE_NO["probe"], "writing mutants")
    written = await s.backend.write_mutants(task)
    mutants, wrong_accepted, right_rejected, detail, broken = written["mutants"], [], [], [], []
    for i in range(0, len(mutants), 4):
        batch = mutants[i:i + 4]
        accepted = await asyncio.gather(*(s.container(s.backend.run_mutant(task, m)) for m in batch))
        for m, a in zip(batch, accepted):
            detail.append({"id": m["id"], "kind": m["kind"], "what": m.get("what", ""), "verifier_accepted": bool(a),
                           "error": m.get("error", ""), **({"broken": m["broken"]} if m.get("broken") else {})})
            if a is None:               # the variant itself did not run: it counts for nothing
                broken.append(m["id"])
                continue
            if m["kind"] == "wrong" and a:
                wrong_accepted.append(m["id"])
            if m["kind"] == "equivalent" and not a:
                right_rejected.append(m["id"])
        cell.detail = f"{min(i + 4, len(mutants))} of {len(mutants)} mutants run"
    ok = not wrong_accepted and not right_rejected
    s.record(task.benchmark, "decision", task, stage="probe", gate="verifier", tier="worker",
                model=s.cfg.tiers.worker.model, value=f"{len(mutants)} mutants", cost=written["cost"],
             skill=written.get("skill", ""),
                outcome="pass" if ok else "fix", wrong_accepted=wrong_accepted, right_rejected=right_rejected, broken=broken)
    s.stage_done(task, STAGE_NO["probe"], t0, mutants=len(mutants) - len(broken), wrong_accepted=wrong_accepted,
                 right_rejected=right_rejected, broken=broken, detail=detail)
    if ok:
        return "ok", ""
    s.board(task)["why"] = "verifier accepted a wrong mutant" if wrong_accepted else "verifier rejected a correct variant"
    return "fix", "verifier"


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
    got = await asyncio.gather(*(
        trial(s, task, "cheap_solve", entry.agent, entry.model, i,
              lambda i=i: s.backend.run_trial(task, entry, i, d.cheap_agent_hours), version=entry.version)
        for i in range(d.cheap_trials)))
    return list(got) if all(e["passed"] and not e.get("error") for e in got) else None


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
    for r in range(s.cfg.difficulty.trials):
        cell.detail = f"{passes + fails} of {n_total} trials run · {passes} passed"
        got = await asyncio.gather(*(
            trial(s, task, "difficulty", entry.agent, entry.model, r,
                  lambda entry=entry, r=r: s.backend.run_trial(task, entry, r),
                  version=entry.version, effort=entry.reasoning_effort)
            for entry in d.suite))
        passes += sum(bool(e["passed"]) for e in got)
        fails += sum(not e["passed"] for e in got)
        result = difficulty.outcome(passes, fails, n_total, d.max_pass_rate)
        if result and d.early_stop:
            break
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
    v, trivial = last("verify", "oracle_passed"), last("verify", "trivial_tried")
    if v and v.get("oracle_passed") and not v.get("noop_passed"):
        tried = trivial.get("trivial_tried", 0) if trivial and not trivial.get("trivial_accepted") else 0
        out.append("Its reference solution passes its tests and a no-op fails"
                   + (f", and none of {tried} trivial answers is accepted." if tried else "."))
    fixes = [d for d in board["stages"] if d["stage"] == "fix" and d.get("fixed")]
    if fixes:
        said = "; ".join(d.get("what") or f"its {d.get('defect', 'files')} was mended" for d in fixes)
        out.append(f"{'One repair was' if len(fixes) == 1 else f'{len(fixes)} repairs were'} made: {said}.")
    probe = last("probe", "mutants")
    if probe and probe.get("mutants"):
        wrong, right = probe.get("wrong_accepted") or [], probe.get("right_rejected") or []
        out.append(f"The verifier told right from wrong on all {probe['mutants']} probe answers." if not (wrong or right) else
                   f"On {probe['mutants']} probe answers the verifier still accepts {len(wrong)} wrong and rejects {len(right)} right.")
    worker, hard = last("worker", "cheap_solve"), last("difficulty", "passes")
    cheap = "The cheap worker model failed it" if worker and worker["cheap_solve"] == "failed" else ""
    if hard and hard.get("trials"):
        p, n = hard["passes"], hard["trials"]
        frontier = (f"no frontier trial of {n} passed" if p == 0 else f"{p} of {n} frontier trials passed, so it is solvable but hard")
        out.append(f"{cheap}, and {frontier}." if cheap else frontier[:1].upper() + frontier[1:] + ".")
    elif cheap:
        out.append(cheap + ".")
    return out


def progress(stage: str, r: dict) -> str:
    """One line for the log on what a stage of a task found, as it ends: what a person watching wants to know."""
    n = lambda k: len(r.get(k) or [])  # noqa: E731
    if stage == "verify" and "oracle_passed" in r:
        if r.get("errors"):
            return f"a run failed: {str(r['errors'][0])[:90]}"
        return ("reference solution passes, a no-op fails" if r["oracle_passed"] and not r.get("noop_passed") else
                "reference solution fails its own tests" if not r["oracle_passed"] else "a no-op passes: the tests check too little") + \
               (f", in {r['verifier_seconds']:.0f} s" if r.get("verifier_seconds") else "")
    if stage == "verify" and "trivial_tried" in r:
        return (f"none of {r['trivial_tried']} trivial answers is accepted" if not n("trivial_accepted") else
                f"{n('trivial_accepted')} of {r['trivial_tried']} trivial answers are accepted")
    if stage == "probe" and "mutants" in r:
        wrong, right = n("wrong_accepted"), n("right_rejected")
        left_out = f" ({n('broken')} whose own script failed left out)" if n("broken") else ""
        return (f"the verifier tells right from wrong on all {r['mutants']} probe answers{left_out}" if not (wrong or right) else
                f"of {r['mutants']} probe answers, {wrong} wrong accepted and {right} right rejected{left_out}")
    if stage == "probe" and "coverage" in r:
        return "the instruction states everything the tests check" if r["coverage"] == "covered" else \
            "the tests check something the instruction does not state"
    if stage == "worker":
        return {"failed": "the worker model failed it: not too easy", "passed": f"the worker model solved it every time: too easy",
                "off": "the worker model's tries are switched off"}.get(r.get("cheap_solve", ""), "")
    if stage == "audit":
        verdict = str(r.get("verdict", "?")).replace("_", " ")         # what is to be fixed has a line of its own
        return f"audit: {verdict}" + (f". {str(r['rationale'])[:110]}" if r.get("rationale") and verdict == "accept" else "")
    if stage == "fix":
        return (f"repaired the {r.get('defect', 'task')}" + (f": {r['what']}" if r.get("what") else "") if r.get("fixed") else
                f"round {r.get('round', '?')} could not repair the {r.get('defect', 'task')}")
    if stage == "difficulty" and r.get("trials"):
        return f"{r['passes']} of {r['trials']} frontier trials passed" + (", stopped early" if r.get("stopped_early") else "")
    if stage == "report":
        return "report written"
    return ""


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
