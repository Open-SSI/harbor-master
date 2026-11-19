"""The stages a task goes through inside one working cell (DESIGN.md Section 4, stages 3 to 8).

Each stage returns ("ok", ""), ("fix", defect), or ("drop", reason). To add a stage, write one
function with that contract and put it in the tuple in `process`.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import TYPE_CHECKING

from ..backends import NotImplementedBackend
from ..curation import difficulty
from ..tasks import Task
from .state import STAGE, STAGES, CellState

if TYPE_CHECKING:
    from .session import Session


STEPS = [("verify", None), ("probe", None), ("difficulty", None), ("audit", None)]   # filled in below


async def process(s: Session, cell: CellState, task: Task) -> None:
    bench = s.benches[task.benchmark]
    rounds = 0
    while True:
        for name, step in STEPS:
            kind, info = await step(s, cell, task, rounds)
            if kind != "ok":
                break
            if name == s.until:          # this run only goes this far
                s.board(task)["status"] = f"passed {name}, stopped there"
                s.save_board(task)
                s.note_recent("checked", task.task_id, f"passed every stage up to {name}")
                return
        if kind == "ok":
            break
        if kind == "fix":
            rounds += 1
            try:
                if rounds <= s.cfg.fix.max_rounds and await fix(s, cell, task, info, rounds):
                    continue
            except NotImplementedBackend:
                s.board(task)["status"] = f"needs repair: {info}"
                s.save_board(task)
                s.note_recent("to fix", task.task_id, f"{s.board(task).get('why', info)} (repair is not built: left as it is)")
                return
            kind, info = "drop", f"unrepaired:{info}"
            s.record(task.benchmark, "decision", task, stage="fix", gate="repair", tier="code",
                     value=f"{rounds} round{'s' if rounds != 1 else ''}", outcome="drop", reason=info)
        bench.dropped += 1
        s.board(task)["status"] = "dropped"
        s.board(task)["reason"] = info
        s.save_board(task)
        s.note_recent("dropped", task.task_id, s.board(task).get("why", info))
        return
    await report(s, cell, task)
    bench.kept += 1
    s.report_ready(task)
    board = s.board(task)
    s.note_recent("kept", task.task_id, f"{board.get('difficulty', '')}, audit clean, report ready")


def enter(cell: CellState, stage: int, detail: str, attention: bool = False) -> float:
    cell.stage, cell.detail, cell.attention = stage, detail, attention
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
    prior = s.ledger.trial(task.digest, agent, version, model, s.protocol, index, effort)
    if prior is not None:
        return prior
    result = await s.container(run())
    s.trial_cost += result.cost
    return s.record(task.benchmark, "trial", task, purpose=purpose, agent=agent, agent_version=version,
                    agent_version_ran=result.agent_version, model=model, reasoning_effort=effort, trial_index=index,
                    passed=result.passed, reward=result.reward, tokens_in=result.tokens_in,
                    tokens_out=result.tokens_out, cost=result.cost, container_seconds=round(result.seconds, 1),
                    verifier_seconds=round(result.verifier_seconds, 1), task_checksum=result.checksum,
                    artifact=result.artifact, error=result.error)


async def stage_verify(s: Session, cell: CellState, task: Task, rounds: int):
    """The reference solution passes, twice. A no-op fails. The verifier is fast enough."""
    t0 = enter(cell, 3, "oracle and no-op runs")
    oracle, noop = await asyncio.gather(
        trial(s, task, "oracle", "oracle", "", 0, lambda: s.backend.run_oracle(task)),
        trial(s, task, "noop", "nop", "", 0, lambda: s.backend.run_noop(task)))
    again = None
    if oracle["passed"] and not s.simulate:      # a simulation has no second answer to give
        cell.detail = "oracle again, for determinism"
        again = await trial(s, task, "oracle", "oracle", "", 1, lambda: s.backend.run_oracle(task))
    slowest = max(float(e.get("verifier_seconds") or 0) for e in (oracle, noop, again) if e)
    limit = s.cfg.triage.max_verifier_seconds
    s.stage_done(task, 3, t0, oracle_passed=oracle["passed"], noop_passed=noop["passed"],
                 oracle_again=again["passed"] if again else None, verifier_seconds=slowest,
                 errors=[e["error"] for e in (oracle, noop, again) if e and e.get("error")])
    board = s.board(task)
    if any(e and e.get("error") for e in (oracle, noop, again)):
        board["why"] = "a run failed for an infrastructure reason: " + next(e["error"] for e in (oracle, noop, again) if e and e.get("error"))[:160]
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
    return "ok", ""


async def stage_probe(s: Session, cell: CellState, task: Task, rounds: int):
    prior = s.ledger.decision(task.digest, "probe", "verifier", s.protocol)
    if prior is not None:
        return ("ok", "") if prior["outcome"] == "pass" else ("fix", "verifier")
    t0 = enter(cell, 4, "writing mutants")
    written = await s.backend.write_mutants(task)
    mutants, wrong_accepted, right_rejected, detail = written["mutants"], [], [], []
    for i in range(0, len(mutants), 4):
        batch = mutants[i:i + 4]
        accepted = await asyncio.gather(*(s.container(s.backend.run_mutant(task, m)) for m in batch))
        for m, a in zip(batch, accepted):
            detail.append({"id": m["id"], "kind": m["kind"], "what": m.get("what", ""), "verifier_accepted": bool(a),
                           "error": m.get("error", "")})
            if m["kind"] == "wrong" and a:
                wrong_accepted.append(m["id"])
            if m["kind"] == "equivalent" and not a:
                right_rejected.append(m["id"])
        cell.detail = f"{min(i + 4, len(mutants))} of {len(mutants)} mutants run"
    ok = not wrong_accepted and not right_rejected
    s.record(task.benchmark, "decision", task, stage="probe", gate="verifier", tier="worker",
                model=s.cfg.tiers.worker.model, value=f"{len(mutants)} mutants", cost=written["cost"],
             skill=written.get("skill", ""),
                outcome="pass" if ok else "fix", wrong_accepted=wrong_accepted, right_rejected=right_rejected)
    s.stage_done(task, 4, t0, mutants=len(mutants), wrong_accepted=wrong_accepted,
                 right_rejected=right_rejected, detail=detail)
    if ok:
        return "ok", ""
    s.board(task)["why"] = "verifier accepted a wrong mutant" if wrong_accepted else "verifier rejected a correct variant"
    return "fix", "verifier"


async def stage_difficulty(s: Session, cell: CellState, task: Task, rounds: int):
    d = s.cfg.difficulty
    n_total = len(d.suite) * d.trials_per_config
    prior = s.ledger.decision(task.digest, "difficulty", "pass_rate", s.protocol)
    if prior is not None:
        s.board(task)["difficulty"] = prior["value"]
        return ("ok", "") if prior["outcome"] == "pass" else ("drop", "too_easy")
    t0 = enter(cell, 5, f"round 1 of {d.trials_per_config}")
    passes = fails = 0
    result = None
    for r in range(d.trials_per_config):
        cell.detail = f"round {r + 1} of {d.trials_per_config}, {passes} pass{'es' if passes != 1 else ''} so far"
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
    s.stage_done(task, 5, t0, passes=passes, trials=passes + fails, stopped_early=early)
    s.board(task)["difficulty"] = value
    if result == "keep":
        return "ok", ""
    s.board(task)["why"] = f"{value}{', stopped early' if early else ''}"
    return "drop", "too_easy"


async def stage_audit(s: Session, cell: CellState, task: Task, rounds: int):
    prior = s.ledger.decision(task.digest, "audit", "quality", s.protocol)
    if prior is not None:
        return {"accept": ("ok", ""), "fix": ("fix", "design")}.get(prior["value"], ("drop", "broken"))
    t0 = enter(cell, 6, "reading trajectories")
    d = await s.backend.audit(task, rounds)
    tiers = [d.tier]
    if d.value == "unsure":
        s.record(task.benchmark, "decision", task, stage="audit", gate="quality", tier=d.tier, model=d.model,
                    value=d.value, probability=d.prob, outcome="escalate", cost=d.cost, rationale=d.rationale)
        enter(cell, 6, "unsure, asking tier F", attention=True)
        d = await s.backend.second_opinion(task, rounds)
        tiers.append(d.tier)
    outcome = {"accept": "pass", "fix": "fix"}.get(d.value, "drop")
    s.record(task.benchmark, "decision", task, stage="audit", gate="quality", tier=d.tier, model=d.model,
                value=d.value, probability=d.prob, outcome=outcome, cost=d.cost, rationale=d.rationale,
                reason="" if outcome != "drop" else "broken")
    s.stage_done(task, 6, t0, verdict=d.value, tiers=tiers, rationale=d.rationale)
    cell.attention = False
    if d.value == "accept":
        return "ok", ""
    s.board(task)["why"] = d.rationale
    return ("fix", "design") if d.value == "fix" else ("drop", "broken")


async def fix(s: Session, cell: CellState, task: Task, defect: str, round_no: int) -> bool:
    tier = "frontier" if round_no > s.cfg.fix.frontier_after_round else "worker"
    why = s.board(task).get("why", defect)
    t0 = enter(cell, 7, why, attention=True)
    s.note_recent("to fix", task.task_id, why)
    notes = s.notes.get(task.task_id, [])
    result = await s.backend.repair(task, defect, round_no, tier)
    if result["fixed"]:
        old = task.digest
        task.meta.setdefault("_fixed", []).append(defect)
        task.digest = hashlib.sha256(f"{old}|{defect}|{round_no}".encode()).hexdigest()
        s.record(task.benchmark, "repair", task, old_digest=old, new_digest=task.digest, tier=tier,
                    what=result["what"], round=round_no, cost=result["cost"], notes=notes)
    s.stage_done(task, 7, t0, round=round_no, defect=defect, tier=tier, fixed=result["fixed"],
                     what=result["what"])
    cell.attention = False
    return result["fixed"]


async def report(s: Session, cell: CellState, task: Task) -> None:
    if any(e["type"] == "report" and e["digest"] == task.digest for e in s.ledger.by_task.get(task.task_id, [])):
        return
    t0 = enter(cell, 8, "writing the report")
    board = s.board(task)
    written = await s.backend.write_report(task, board)
    board["status"] = "report_ready"
    s.save_board(task)
    path = s.report_path(task)
    banner = "**Recommendation: accept.**"
    if s.assumed:
        banner = ("**ASSUMED RESULTS. These stages were not run: " + ", ".join(sorted(s.assumed)) + ". Their results "
                  "below are made up to exercise the pipeline, and this task cannot be accepted on them.**")
    lines = [f"# {task.task_id}", "", f"Digest `{task.digest[:12]}`, protocol `{s.protocol}`, "
             f"run by @{s.ident.github}.", "", banner, "", "## What the task asks", "",
             task.instruction.strip()[:600] or "(no instruction text)", "", "## Evidence", ""]
    for done in board["stages"]:
        detail = ", ".join(f"{k}: {v}" for k, v in done.items() if k not in ("stage", "seconds"))
        lines.append(f"- **{done['stage']}** ({done['seconds']}s): {detail}")
    if s.notes.get(task.task_id):
        lines += ["", "## Notes", ""] + [f"- {n}" for n in s.notes[task.task_id]]
    text = "\n".join(lines) + "\n"
    path.write_text(text)
    s.record(task.benchmark, "report", task, recommendation="accept", cost=written.get("cost", 0.0),
             location=str(path.relative_to(s.ws)), report_digest=hashlib.sha256(text.encode()).hexdigest()[:16])
    s.stage_done(task, 8, t0)


STEPS[:] = [("verify", stage_verify), ("probe", stage_probe), ("difficulty", stage_difficulty), ("audit", stage_audit)]
