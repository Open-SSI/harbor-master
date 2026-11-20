"""The once-per-benchmark phase: load, triage, a contributor's brief, ranking, selection, adapter.

DESIGN.md Section 4 (stages 0 to 2) and Section 6.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import TYPE_CHECKING

from .. import ledger as ledger_mod
from pathlib import Path

from ..backends import Decision, NotImplementedBackend
from ..curation import gates, leak, ranking
from ..tasks import Task, load_benchmark
from . import claims, prepare
from .state import BENCH, STAGE, BenchState

if TYPE_CHECKING:
    from .session import Session


async def run_shared(s: Session, bench: BenchState) -> None:
    """The benchmark's shared phase. A failure here skips this benchmark and leaves the others running."""
    BENCH.set(bench.id)
    STAGE.set("intake")
    try:
        holder = await claims.screen(s, bench)
        if holder:
            bench.phase, bench.skip = "skipped", "being_screened"
            bench.detail = f"@{holder} is screening this benchmark now. Run again when they are done."
            s.note_recent("rejected", bench.id, bench.detail)
        else:
            try:
                await _run_shared(s, bench)
            finally:
                s.release(bench.id, [claims.SCREEN])
                await s.publish(f"screening of {bench.id} done")      # others must see that it is free again
    except asyncio.CancelledError:
        raise
    except Exception as err:
        bench.phase, bench.skip = "skipped", "error"
        bench.detail = f"stopped at {bench.phase}: {type(err).__name__}: {str(err)[:160]}"
        s.note_recent("error", bench.id, bench.detail)
    if bench.phase == "skipped":        # which benchmark a run left out, and why, is part of the record
        s.note_skip(bench)
    s.flush_usage(bench.id)


async def _run_shared(s: Session, bench: BenchState) -> None:
    if not await prepare.ready(s, bench):       # fetched, and adapted if it is in another format
        s.note_recent("rejected", bench.id, bench.detail)
        return
    bench.phase, bench.detail = "triage", "loading tasks"
    s.explain("triage")
    STAGE.set("triage")
    try:
        bench.tasks = load_benchmark(bench.path, bench.id)
    except ValueError as err:
        bench.phase, bench.detail, bench.skip = "skipped", str(err), "not_harbor_format"
        return
    for task in bench.tasks:
        task.meta["_benchmark_license"] = bench.license
    bench.detail = "reading the benchmark's published results"
    bench.reported = await reported_results(s, bench)
    if s.only:
        wanted = [t for t in bench.tasks if t.name in s.only]
        missing = s.only - {t.name for t in wanted}
        if missing and len(s.benches) == 1:
            raise ValueError(f"no such task in {bench.id}: {', '.join(sorted(missing))}")
        bench.tasks = wanted
    known = ledger_mod.submitted(s.ledger.events)       # one record for the whole benchmark, not one per task
    new = {t.task_id: {"digest": t.digest} for t in bench.tasks
           if known.get(t.task_id, {}).get("digest") != t.digest}
    if new:
        s.record(bench.id, "submission", source=str(bench.path), tasks=new)
    limit = asyncio.Semaphore(16)
    kept: list[Task] = []
    done = 0

    async def one(task: Task) -> None:
        nonlocal done
        async with limit:
            if await triage(s, task):
                kept.append(task)
            else:
                bench.triaged_out += 1
        done += 1
        bench.detail = f"triage {done} of {len(bench.tasks)}"

    await asyncio.gather(*(one(t) for t in bench.tasks))
    kept.sort(key=lambda t: t.name)

    brief = s.ledger.brief(bench.id)
    if brief is None and s.cfg.select.require_brief and kept:
        bench.phase, bench.detail = "brief", "waiting for a brief"
        text = await s.ask("brief", f"{about(bench, kept)}\n\nWhat counts as important in this benchmark? The ranking follows "
                              "your answer. Any contributor may give it.", bench.id)
        if not text:
            bench.phase, bench.detail, bench.skip = "skipped", "no brief, skipped (hm curate in a terminal asks for one in a box)", "no_brief"
            return
        brief = s.record(bench.id, "brief", text=text, examples=[], version=1)

    # A simulation is for watching the stages: every task that passes the gates goes through them, up to top_k.
    # The setting itself is left as it is, so the run's protocol is the one in the file.
    limit_n = ranking.cap(len(bench.tasks), s.cfg.select.top_k, 1.0 if s.sandbox else s.cfg.select.max_fraction)
    while True:
        bench.phase, bench.detail = "ranking", f"scoring {len(kept)} tasks, then comparing the top ones"
        s.explain("rank")
        STAGE.set("rank")
        brief_text = brief["text"] if brief else ""
        ranked = await ranking.rank_two_step(
            kept, lambda t: s.backend.importance(brief_text, t), lambda a, b: s.backend.compare(brief_text, a, b),
            limit_n, s.cfg.select.comparisons_per_task, s.cfg.select.pool_factor)
        # The order tasks are taken in: by rank, round-robin over sub-areas when stratifying. The selection is
        # always the first k of it, so a person can say "the top k" of the list they are shown and mean one thing.
        order = ranking.select([t for t, _ in ranked], lambda t: t.subarea, len(ranked), s.cfg.select.stratify)
        rating = {t.task_id: r for t, r in ranked}
        bench.most = most = min(limit_n, len(order))        # the cap: the most that may go on

        def pick(k: int) -> tuple[list[Task], str]:
            ids = sorted(t.task_id for t in order[:k])
            return order[:k], hashlib.sha256("|".join(ids + [brief["id"] if brief else ""]).encode()).hexdigest()

        chosen, signoff = pick(most)
        approved = False
        for e in s.ledger.events:       # an approval already signed for this ranking, for whichever k it named
            if e["type"] == "decision" and e.get("gate") == "signoff" and e["benchmark"] == bench.id:
                k = int(e.get("selected") or 0)
                if 1 <= k <= most and pick(k)[1] == e["digest"]:
                    (chosen, signoff), approved = pick(k), True
        while True:
            chosen_ids = {t.task_id for t in chosen}
            bench.ranking = [(t.task_id, round(rating[t.task_id], 3), t.task_id in chosen_ids) for t in order]
            if approved or not s.cfg.select.require_signoff or not chosen:
                break
            bench.phase, bench.detail = "signoff", "selection needs approval"
            s.explain("select")
            answer = await s.ask("selection", selection_brief(bench, most), bench.id)
            words = answer.split() if isinstance(answer, str) else []
            if not words or words[0] != "approve":
                break
            k = int(words[1]) if len(words) > 1 and words[1].isdigit() else most
            if 1 <= k <= most:              # anything else is asked again
                chosen, signoff = pick(k)
                s.record(bench.id, "decision", stage="select", gate="signoff", tier="human", value="approved",
                         outcome="pass", selected=len(chosen), signoff=signoff, digest_override=signoff)
                approved = True
        if approved or not s.cfg.select.require_signoff or not chosen:
            break
        if answer is None:
            bench.phase, bench.detail = "skipped", "selection not approved, skipped (run with the screen to approve)"
            bench.skip = "selection_not_approved"
            return
        brief = s.record(bench.id, "brief", text=answer, examples=[], version=int(time.time()))   # re-rank

    # One record for the whole selection: every screened task, its rating, and whether it goes on.
    if not any(e["type"] == "decision" and e.get("gate") == "ranking" and e.get("digest") == signoff
               for e in s.ledger.events):
        s.record(bench.id, "decision", stage="select", gate="ranking", tier="code", outcome="pass",
                 value=f"{len(chosen)} of {len(kept)}", brief=brief["id"][:8] if brief else "",
                 ranking=[[t.name, round(r, 3), int(t.task_id in chosen_ids)] for t, r in ranked],
                 digest_override=signoff)
    bench.not_selected = len(kept) - len(chosen)
    if any(not t.harbor_format for t in chosen):
        bench.phase, bench.detail = "adapt", "building adapter"
        await s.backend.adapt(bench.id, len(chosen))
    bench.selected = len(chosen)
    if s.until == "select":         # this run only screens and ranks
        bench.phase, bench.detail = "ready", ""
        bench.done = len(chosen)
        s.note_recent("checked", bench.id, f"{len(kept)} of {len(bench.tasks)} tasks passed the gates, {len(chosen)} selected, stopped there")
        return
    bench.detail = "claiming tasks"
    free = await claims.take(s, bench, chosen)      # leaves out what is done or held, and locks the rest
    bench.selected = len(free)
    s.queue.extend(free)
    bench.phase, bench.detail = "ready", ""


def described(bench: BenchState) -> dict:
    """What a benchmark says of itself: its entry on the index's list, else a benchmark.toml in its own folder."""
    if bench.entry:
        return bench.entry
    try:
        import tomllib
        return tomllib.loads((bench.path / "benchmark.toml").read_text())
    except (OSError, ValueError):
        return {}


def about(bench: BenchState, kept: list[Task]) -> str:
    """A benchmark for the person asked to say what matters in it, as a few lines to read: what it is, then the
    categories its tasks fall into, with how many in each and, where the benchmark says, what each is about.
    Read from its own description and its tasks, with no model."""
    said = described(bench)
    lines = [str(said.get("name") or bench.id)]
    if said.get("description"):
        lines.append(" ".join(str(said["description"]).split()))
    areas: dict[str, int] = {}
    for t in kept:
        areas[t.subarea] = areas.get(t.subarea, 0) + 1
    top = sorted(areas.items(), key=lambda a: (-a[1], a[0]))
    passed = f"{len(kept)} of its {len(bench.tasks)} tasks passed the gates"
    if len(top) < 2:
        return "\n".join([*lines, "", passed + "."])
    lines += ["", f"{passed}, in {len(top)} categories:"]
    notes, width = said.get("categories") or {}, max(len(a) for a, _ in top[:8])
    for area, n in top[:8]:
        lines.append(f"  {area:<{width}}  {n:>3}   {notes.get(area, '')}".rstrip())
    if len(top) > 8:
        lines.append(f"  and {len(top) - 8} more categories")
    return "\n".join(lines)


def repository(path: Path) -> Path:
    """The benchmark's whole repository, where its results are likely to be, not only its task folder."""
    for folder in (path, *list(path.parents)[:4]):
        if (folder / ".git").exists():
            return folder
    return path


async def reported_results(s: Session, bench: BenchState) -> dict[str, dict]:
    """What the benchmark itself reports for each task: {task name: {"pass_rate", "model"}}.

    Once per benchmark, an agent of the worker tier reads the repository for results given task by
    task: a results table, a leaderboard file, logs of the authors' runs. The record it leaves is
    reused by every later run on the same tasks. A benchmark that reports nothing per task leaves
    an empty one, and all its tasks pass this gate.
    """
    if s.cfg.triage.max_reported_pass_rate >= 1 or not bench.tasks:
        return {}
    digest = hashlib.sha256("|".join(sorted(t.digest for t in bench.tasks)).encode()).hexdigest()
    prior = s.ledger.decision(digest, "triage", "reported_results", s.protocol)
    if prior is not None:
        return prior.get("results") or {}
    try:
        got = await s.backend.reported(bench.id, repository(bench.path), [t.name for t in bench.tasks])
    except (RuntimeError, NotImplementedBackend) as err:
        s.note_recent("error", bench.id, f"no agent could read its published results, so no task is dropped on them: {str(err)[:140]}")
        return {}
    names = {t.name.lower(): t.name for t in bench.tasks}
    results = {}
    for name, seen in got["tasks"].items():
        rate = seen.get("pass_rate")
        if name.lower() in names and isinstance(rate, (int, float)) and 0 <= rate <= 1:
            results[names[name.lower()]] = {"pass_rate": round(float(rate), 3), "model": str(seen.get("model", ""))[:80]}
    limit = s.cfg.triage.max_reported_pass_rate
    over = sum(r["pass_rate"] > limit for r in results.values())
    s.record(bench.id, "decision", stage="triage", gate="reported_results", tier="worker", model=s.cfg.tiers.worker.model,
             value=f"{len(results)} of {len(bench.tasks)} tasks have a reported result, {over} over {limit:.0%}",
             outcome="pass", results=results, source=got.get("source", ""), cost=got.get("cost", 0.0),
             skill=got.get("skill", ""), digest_override=digest)
    if results:
        s.note_recent("checked", bench.id, f"its published results cover {len(results)} of {len(bench.tasks)} tasks: "
                                           f"{over} already solved more than {limit:.0%} of the time")
    return results


async def triage(s: Session, task: Task) -> bool:
    """The admission gates. Code settles what it can, tier D the rest, tier W the unsure ones.

    A task leaves one record: every gate's answer and probability, and whether it passed. A gate
    that had to go up a tier leaves two more, the unsure answer and the one that replaced it,
    because that pair is the label the thresholds are tuned on.
    """
    prior = s.ledger.decision(task.digest, "triage", "gates", s.protocol)
    if prior is not None:
        return prior["outcome"] == "pass"
    answers: dict[str, dict] = {}
    cost, skill = 0.0, ""

    def done(failed: str = "") -> bool:
        s.record(task.benchmark, "decision", task, stage="triage", gate="gates", tier="decision",
                 model=s.cfg.tiers.decision.model, value=answers, outcome="drop" if failed else "pass",
                 reason=f"{failed}:{answers[failed]['value']}" if failed else "", cost=cost, skill=skill)
        return not failed

    released, cutoff = s.benches[task.benchmark].released, s.cfg.triage.released_after
    if released:                    # the release date comes from the benchmark list. An unknown date passes
        answers["recency"] = {"value": released, "tier": "code"}
        if released < cutoff:
            return done("recency")
    seen = s.benches[task.benchmark].reported.get(task.name)
    if seen:                        # what the benchmark's own results say of this task. One with no result passes
        answers["reported"] = {"value": seen["pass_rate"], "tier": "worker", "model": seen.get("model", "")}
        if seen["pass_rate"] > s.cfg.triage.max_reported_pass_rate:
            return done("reported")
    answers["reference_solution"] = {"value": "present" if task.has_solution else "missing", "tier": "code"}
    if not task.has_solution and s.cfg.triage.require_reference_solution:
        return done("reference_solution")
    kind, where = await asyncio.to_thread(leak.find, task.path, task.instruction)
    answers["leak"] = {"value": kind, "tier": "code", **({"where": where[:5]} if where else {})}
    if kind != leak.NONE:           # the answer is within the agent's reach: no model is asked about the rest
        return done("leak")
    wanted = {g: choices for g, (choices, _) in gates.D_GATES.items()}
    decided = await s.backend.decide_gates(task, wanted)       # one request where the backend can
    cost = sum(d.cost for d in decided.values())
    skill = next((d.skill for d in decided.values() if d.skill), "")
    for gate, choices in wanted.items():
        d = decided[gate]
        if d.prob < s.cfg.escalation.gate_min_confidence.get(gate, s.cfg.escalation.min_confidence):
            first = d
            d = await s.backend.confirm(gate, choices, task, first)
            s.record(task.benchmark, "decision", task, stage="triage", gate=gate, tier=first.tier, value=first.value,
                     probability=round(first.prob, 3), outcome="escalate", skill=first.skill)
            s.record(task.benchmark, "decision", task, stage="triage", gate=gate, tier=d.tier, model=d.model,
                     value=d.value, probability=round(d.prob, 3), cost=d.cost, rationale=d.rationale, skill=d.skill,
                     outcome="pass" if gates.passes(gate, d.value, s.cfg) else "drop")
        answers[gate] = {"value": d.value, "p": round(d.prob, 3), "tier": d.tier}
        if not gates.passes(gate, d.value, s.cfg):
            return done(gate)
    return done()


def selection_brief(bench: BenchState, most: int) -> str:
    """What a contributor is asked to approve: how many of the ranked tasks go on to the costly stages."""
    picked = [tid.split("/")[-1] for tid, _, chosen in bench.ranking if chosen]
    return (f"{bench.id}: {len(bench.ranking)} tasks are ranked. Choose how many of the top ones go on to the costly "
            f"stages: any number from 1 to {most}. {len(picked)} is proposed. Ranked highest: {', '.join(picked[:5])}.")
