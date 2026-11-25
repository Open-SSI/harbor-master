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

from ..backends import NotImplementedBackend
from ..backends.base import Decision
from ..curation import gates, ranking
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
        try:
            holder = await claims.claim(s, bench)
        except claims.ClaimError as err:
            bench.phase, bench.skip, bench.detail = "skipped", "claim_failed", str(err)
            s.note_recent("rejected", bench.id, bench.detail)
            s.note_skip(bench)
            return
        if holder:
            bench.phase, bench.skip = "skipped", "claimed"
            bench.detail = f"@{holder} is curating this benchmark. It is theirs until they are done."
            s.note_recent("rejected", bench.id, bench.detail)
        else:
            await _run_shared(s, bench)
    except asyncio.CancelledError:
        raise
    except Exception as err:
        bench.phase, bench.skip = "skipped", "error"
        bench.detail = f"stopped at {bench.phase}: {type(err).__name__}: {str(err)[:160]}"
        s.note_recent("error", bench.id, bench.detail)
    if bench.phase == "skipped":        # which benchmark a run left out, and why, is part of the record
        s.note_skip(bench)
    s.flush_usage(bench.id)


ASK_FIRST = ("fetch_failed", "adapter_failed", "model_error", "not_harbor_format", "no_task_passed")   # rejections a person confirms


def approved_before(s: Session, bench: BenchState, kept: list[Task], brief: dict | None):
    """A selection a person approved earlier for this benchmark and this brief, perhaps under an earlier protocol, whose
    tasks all still pass the gates: (the tasks it chose, the ranking it was approved on, its digest), or None. Settings
    that change how tasks are ranked do not ask again for a selection that was approved; a new brief does."""
    by_name = {t.name: t for t in kept}
    rankings = {e["digest"]: e for e in s.ledger.events
                if e["type"] == "decision" and e.get("gate") == "ranking" and e["benchmark"] == bench.id}
    signoffs = [e for e in s.ledger.events if e["type"] == "decision" and e.get("gate") == "signoff"
                and e["benchmark"] == bench.id and e.get("outcome") == "pass"]
    for e in sorted(signoffs, key=lambda e: -e["time"]):
        listed = (rankings.get(e["digest"]) or {}).get("ranking") or []
        names = [name for name, _, picked in listed if picked]
        if not names or any(name not in by_name for name in names):
            continue
        ids = sorted(by_name[name].task_id for name in names)
        if hashlib.sha256("|".join(ids + [brief["id"] if brief else ""]).encode()).hexdigest() != e["digest"]:
            continue                    # approved on another brief
        ranked = [(by_name[name], float(r)) for name, r, _ in listed if name in by_name]
        floor = min((r for _, r in ranked), default=0.0)
        ranked += [(t, floor - 1.0) for t in kept if t.name not in {name for name, _, _ in listed}]   # new since: below
        return [by_name[name] for name in names], ranked, e["digest"]
    return None


async def rejected(s: Session, bench: BenchState) -> None:
    """A benchmark turned away before curating: unless a person already decided, a box asks them to confirm, and whether
    that it is dropped (not taken again) or left out of this run."""
    if bench.skip in ASK_FIRST and not bench.confirmed:
        why, code = bench.detail, bench.skip
        if await prepare.confirm_drop(s, bench, why[:1].upper() + why[1:], code) == "drop":
            why = f"dropped: {why}"              # the code stays what rejected it, so that it is shown at that stage
        bench.phase, bench.detail = "skipped", why
    s.note_recent("rejected", bench.id, bench.detail)


async def _run_shared(s: Session, bench: BenchState) -> None:
    if not await prepare.ready(s, bench):       # fetched, and adapted if it is in another format
        await rejected(s, bench)
        return
    bench.phase, bench.detail = "triage", "loading tasks"
    s.explain("triage")
    STAGE.set("triage")
    try:
        bench.tasks = load_benchmark(bench.path, bench.id)
    except ValueError as err:
        bench.phase, bench.detail, bench.skip = "skipped", str(err), "not_harbor_format"
        await rejected(s, bench)
        return
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
    if not kept and bench.tasks and not s.only:      # every task failed a gate: the benchmark is rejected, if a person agrees
        bench.phase, bench.skip = "skipped", "no_task_passed"
        bench.detail = f"none of its {len(bench.tasks)} tasks passed the admission gates"
        await rejected(s, bench)
        return
    if any(e["type"] == "decision" and e.get("stage") in ("adapt", "benchmark") and e.get("outcome") == "drop"
           and e["benchmark"] == bench.id for e in s.ledger.events):
        # Tried again after it was dropped: what already has an outcome is not chosen again, so the rest is ranked, and
        # the selection is asked again. With nothing left, it stays dropped.
        done = claims.completed(s, bench.id)
        kept = [t for t in kept if t.task_id not in done]
        if not kept:
            why = "every task that passed the gates already has an outcome"
            s.record(bench.id, "decision", stage="benchmark", gate="nothing_left", tier="code", value=why, outcome="drop",
                     reason=f"nothing is left to curate: {why}")
            bench.phase, bench.detail, bench.skip = "skipped", f"dropped: nothing is left to curate, as {why}", "none_accepted"
            s.note_recent("dropped", bench.id, f"nothing is left to curate: {why}")
            s.tell("rejected", f"{bench.id} is dropped. Nothing is left to curate in it: {why}.\n\nConfirm. It stays on "
                   "the index's list as dropped.", bench.id)
            return

    brief = s.ledger.brief(bench.id)
    if brief is None and s.cfg.select.require_brief and kept:
        bench.phase, bench.detail = "brief", "waiting for a brief"
        text = await s.ask("brief", f"{about(bench, kept, dropped_by(s, bench, kept))}\n\nWhat counts as important in this benchmark? The ranking follows "
                              "your answer. Any contributor may give it.", bench.id)
        if not text:
            bench.phase, bench.detail, bench.skip = "skipped", "no brief, skipped (hm curate in a terminal asks for one in a box)", "no_brief"
            return
        brief = s.record(bench.id, "brief", text=text, examples=[], version=1)

    # A simulation is for watching the stages: every task that passes the gates goes through them, up to top_k.
    # The setting itself is left as it is, so the run's protocol is the one in the file.
    # Tasks named for this run (--tasks) were chosen by a person: the share of the benchmark is not applied to them.
    limit_n = ranking.cap(len(bench.tasks), s.cfg.select.top_k, 1.0 if s.sandbox or s.only else s.cfg.select.max_fraction)
    while True:
        reused = approved_before(s, bench, kept, brief)
        if reused is not None:      # approved before, perhaps under other settings: it stands, and nothing is ranked again
            chosen, ranked, signoff = reused
            chosen_ids = {t.task_id for t in chosen}
            bench.most = len(chosen)
            bench.ranking = [(t.task_id, round(r, 3), t.task_id in chosen_ids) for t, r in ranked]
            s.note_recent("checked", bench.id, f"the selection approved before stands: {len(chosen)} tasks, not ranked again")
            break
        bench.phase, bench.detail = "ranking", f"scoring {len(kept)} tasks, then comparing the top ones"
        s.explain("rank")
        STAGE.set("rank")
        brief_text = brief["text"] if brief else ""
        ranked = await ranking.rank_two_step(
            kept, lambda t: s.backend.importance(brief_text, t), lambda a, b: s.backend.compare(brief_text, a, b),
            limit_n, s.cfg.select.comparisons_per_task, s.cfg.select.pool_factor, concurrency=RANK_AT_ONCE)
        # The order tasks are taken in: by rank, round-robin over sub-areas when stratifying. The selection is
        # always the first k of it, so a person can say "the top k" of the list they are shown and mean one thing.
        order = ranking.select([t for t, _ in ranked], lambda t: t.subarea, len(ranked), s.cfg.select.stratify)
        rating = {t.task_id: r for t, r in ranked}
        bench.most = most = min(limit_n, len(order))        # the cap: the most that may go on

        def pick(at: list[int]) -> tuple[list[Task], str]:
            """The tasks at these ranked positions (from 1), and the digest a signoff on them carries."""
            tasks_at = [order[i - 1] for i in at if 1 <= i <= len(order)]
            ids = sorted(t.task_id for t in tasks_at)
            return tasks_at, hashlib.sha256("|".join(ids + [brief["id"] if brief else ""]).encode()).hexdigest()

        chosen, signoff = pick(list(range(1, most + 1)))
        approved = False
        for e in s.ledger.events:       # an approval already signed for this ranking, for whichever tasks it named
            if e["type"] == "decision" and e.get("gate") == "signoff" and e["benchmark"] == bench.id:
                at = [int(i) for i in e.get("positions") or []] or list(range(1, int(e.get("selected") or 0) + 1))
                if at and len(at) <= most and pick(at)[1] == e["digest"]:
                    (chosen, signoff), approved = pick(at), True
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
            at, why = ranking.positions(" ".join(words[1:]), len(order), most) if len(words) > 1 else (list(range(1, most + 1)), "")
            if not why:                     # anything else is asked again
                chosen, signoff = pick(at)
                s.record(bench.id, "decision", stage="select", gate="signoff", tier="human", value="approved",
                         outcome="pass", selected=len(chosen), positions=at, signoff=signoff, digest_override=signoff)
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
    bench.chosen = [t.task_id for t in chosen]
    free = await claims.take(s, bench, chosen)      # leaves out what already has an outcome
    bench.selected = len(free)
    if free and not s.asked_cells and s.on_screen and not s.no_input:     # the curating stage starts: how much runs at once
        s.asked_cells = True
        bench.phase, bench.detail = "cells", "how many work cells and containers"
        said = await s.ask("cells", f"{len(free)} task{'s' if len(free) != 1 else ''} of {bench.id} chosen. How many run at once?\n"
                           "Each work cell takes one task through its stages; each trial runs in a container.", bench.id)
        if said:
            cells, _, boxes = str(said).partition(",")
            s.set_cells(int(cells), int(boxes) if boxes else None)
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


WHY_DROPPED = {
    "reported": "already solved: its own published results show a model passing over {cap:.0%}",
    "format": "not an agent task: {value}",
    "verifier": "not graded by code: {value}",
    "verifier_cost": "too costly to grade",
    "duplicate": "a duplicate of another task",
    "credentials": "not public: it needs credentials from whoever runs it, such as a token for a gated dataset",
}


def dropped_by(s: Session, bench: BenchState, kept: list[Task]) -> dict[str, int]:
    """Why the tasks that did not pass the gates were left out, as reasons a person can read, with how many each."""
    passed, why = {t.task_id for t in kept}, {}
    for t in bench.tasks:
        if t.task_id in passed:
            continue
        d = s.ledger.decision(t.digest, "triage", "gates", s.protocol)
        gate, _, value = (d or {}).get("reason", "").partition(":")
        said = WHY_DROPPED.get(gate, "{gate}").format(cap=s.cfg.triage.max_reported_pass_rate, gate=gate or "no record",
                                                     value=value.replace("_", " "))
        why[said] = why.get(said, 0) + 1
    return why


def about(bench: BenchState, kept: list[Task], dropped: dict[str, int] | None = None) -> str:
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
    left = []
    if dropped:
        width = max(len(str(n)) for n in dropped.values())
        left = ["", f"{sum(dropped.values())} were left out:",
                *(f"  {n:>{width}}  {why}" for why, n in sorted(dropped.items(), key=lambda w: -w[1]))]
    if len(top) < 2:
        return "\n".join([*lines, "", passed + ".", *left])
    lines += ["", f"{passed}, in {len(top)} categories:"]
    notes, width = said.get("categories") or {}, max(len(a) for a, _ in top[:8])
    import textwrap
    indent = 2 + width + 2 + 3 + 3                  # a long note wraps under itself, not under the categories
    for area, n in top[:8]:
        row = f"  {area:<{width}}  {n:>3}   {notes.get(area, '')}".rstrip()
        lines.append(textwrap.fill(row, width=94, subsequent_indent=" " * indent, break_on_hyphens=False))
    if len(top) > 8:
        lines.append(f"  and {len(top) - 8} more categories")
    return "\n".join([*lines, *left])


def repository(path: Path) -> Path:
    """The benchmark's whole repository, where its results are likely to be, not only its task folder."""
    for folder in (path, *list(path.parents)[:4]):
        if (folder / ".git").exists() or (folder / ".hm-pinned").exists():
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


RANK_AT_ONCE = 32        # ranking's requests to the decision model in flight at once: small and cheap, so many

# Gates where the decision model's word alone never drops a task: it is sent to the worker agent, which reads the
# task's files. On verifier_cost, the decision model reads only the start of long tests and calls many cheap verifiers
# costly; a wrong "cheap" costs one measured run in verify, a wrong "costly" loses the task for good.
CONFIRM_DROP = {"verifier_cost"}
GRADING = {"verifier", "verifier_cost"}         # the gates that need the test code to answer


def unconfirmed_drop(prior: dict) -> bool:
    """A recorded screening drop that rests on the decision model alone, on a gate that needs the worker's word."""
    if prior.get("outcome") != "drop":
        return False
    gate = str(prior.get("reason", "")).split(":")[0]
    said = (prior.get("value") or {}).get(gate, {})
    return gate in CONFIRM_DROP and (said.get("tier") == "decision" or not said.get("evidence"))     # alone, or with nothing cited


async def triage(s: Session, task: Task) -> bool:
    """The admission gates. Code settles what it can, tier D the rest, tier W the unsure ones.

    A task leaves one record: every gate's answer and probability, and whether it passed. A gate
    that had to go up a tier leaves two more, the unsure answer and the one that replaced it,
    because that pair is the label the thresholds are tuned on.
    """
    prior = s.ledger.decision(task.digest, "triage", "gates", s.protocol)
    # A pass decided before the gates read the task's test code is screened again on how it is graded: a grader that
    # calls a model passed as "programmatic" when only the test files' names were shown.
    stale = prior is not None and prior.get("outcome") == "pass" and not prior.get("read_tests")
    if prior is not None and not unconfirmed_drop(prior) and not stale:
        return prior["outcome"] == "pass"
    # A drop the decision model made alone on a gate that needs the worker's word is screened again, keeping the
    # answers it had: that gate goes to the worker, and the gates after it are asked.
    again_on = GRADING if stale else CONFIRM_DROP
    kept = {g: a for g, a in (prior or {}).get("value", {}).items() if g in gates.D_GATES and g not in again_on}
    answers: dict[str, dict] = {}
    cost, skill = 0.0, ""
    redo, more = prior is not None, {"read_tests": True}
    level = int((prior or {}).get("redo") or 1) + 1     # each screening again beside the ones before, under its own key

    def done(failed: str = "") -> bool:
        tier = "worker" if any(a.get("tier") == "worker" for a in answers.values()) else "decision"
        if redo:
            more["redo"] = level         # beside the earlier screening's record, which keeps its own key
        s.record(task.benchmark, "decision", task, stage="triage", gate="gates", tier=tier, **more,
                 model=s.cfg.tiers.decision.model, value=answers, outcome="drop" if failed else "pass",
                 reason=f"{failed}:{answers[failed]['value']}" if failed else "", cost=cost, skill=skill)
        return not failed

    # Recency and license are the benchmark's, checked once for the whole of it (tasks/checks.py): not asked per task.
    seen = s.benches[task.benchmark].reported.get(task.name)
    if seen:                        # what the benchmark's own results say of this task. One with no result passes
        answers["reported"] = {"value": seen["pass_rate"], "tier": "worker", "model": seen.get("model", "")}
        if seen["pass_rate"] > s.cfg.triage.max_reported_pass_rate:
            return done("reported")
    from ..tasks.model import needs_from_host
    needs = needs_from_host(task.path)
    if needs:                       # credentials only the person running it has: anyone else cannot run it
        answers["credentials"] = {"value": "needs " + ", ".join(needs[:4]), "tier": "code"}
        return done("credentials")
    # A task with no reference solution goes on: if it is selected, its work cell writes one (engine/stages.py).
    answers["reference_solution"] = {"value": "present" if task.has_solution else "missing: a frontier trial must pass it", "tier": "code"}
    wanted = {g: choices for g, (choices, _) in gates.D_GATES.items()}
    if redo:
        decided = {g: Decision(a["value"], float(a.get("p", 1.0)), a.get("tier", "decision"), "", 0.0, 0) for g, a in kept.items()}
        if not stale:               # the unconfirmed drop: its first answer goes to the worker
            first = prior["value"].get("verifier_cost") or {}
            decided["verifier_cost"] = Decision(first.get("value", ""), float(first.get("p", 0.0)), "decision", "", 0.0, 0)
        rest = {g: c for g, c in wanted.items() if g not in decided}
        decided.update(await s.backend.decide_gates(task, rest) if rest else {})
    else:
        decided = await s.backend.decide_gates(task, wanted)       # one request where the backend can
    cost = sum(d.cost for d in decided.values())
    skill = next((d.skill for d in decided.values() if d.skill), "")
    for gate, choices in wanted.items():
        d = decided[gate]
        if gate in kept and redo:          # answered before, and not what is screened again
            answers[gate] = kept[gate]
            if not gates.passes(gate, d.value, s.cfg):
                return done(gate)
            continue
        drop_alone = gate in CONFIRM_DROP and d.tier == "decision" and not gates.passes(gate, d.value, s.cfg)
        if drop_alone or d.prob < s.cfg.escalation.gate_min_confidence.get(gate, s.cfg.escalation.min_confidence):
            first = d
            d = await s.backend.confirm(gate, choices, task, first)
            again = {"redo": level} if redo else {}      # screened again: beside the earlier screenings' records
            if not redo:                # a screening again starts from the first answer, which is recorded already
                s.record(task.benchmark, "decision", task, stage="triage", gate=gate, tier=first.tier, value=first.value,
                         probability=round(first.prob, 3), outcome="escalate", skill=first.skill)
            s.record(task.benchmark, "decision", task, stage="triage", gate=gate, tier=d.tier, model=d.model,
                     value=d.value, probability=round(d.prob, 3), cost=d.cost, rationale=d.rationale, skill=d.skill,
                     evidence=d.evidence, outcome="pass" if gates.passes(gate, d.value, s.cfg) else "drop", **again)
        answers[gate] = {"value": d.value, "p": round(d.prob, 3), "tier": d.tier}
        if d.evidence:
            answers[gate]["evidence"] = d.evidence
        if not gates.passes(gate, d.value, s.cfg):
            return done(gate)
    return done()


def selection_brief(bench: BenchState, most: int) -> str:
    """What a contributor is asked to approve: which of the ranked tasks go on to the costly stages."""
    picked = [tid.split("/")[-1] for tid, _, chosen in bench.ranking if chosen]
    return (f"{bench.id}: {len(bench.ranking)} tasks are ranked. Type the positions of the tasks that go on to the costly "
            f"stages, such as 1, 2, 3, 5 or 1-13, 16: at most {most}. The top {len(picked)} are proposed. Ranked highest: "
            f"{', '.join(picked[:5])}.")
