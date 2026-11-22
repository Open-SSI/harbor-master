"""The curate screen's two pictures of where things stand (DESIGN.md Section 10).

The benchmark's stages, as boxes joined by arrows: fetch, adapt, screen, rank, select, curate, review. The one it is
in is lit, the ones behind it are ticked, the ones ahead are faint. Under it, the same picture for one work cell: the
task it holds, through verify, probe, the worker's tries, audit, fix, then difficulty, once, and report. Keys 1 to 8 choose the cell shown.
"""
from __future__ import annotations

import re
import time

from rich.console import Group
from rich.text import Text

from ..engine import STAGES, Session
from ..engine.state import STAGE_NO, BenchState, CellState
from .theme import AM, MU, PU, RD, SE, TE, blend, clock, spinner, t

FAINT = "#4a4a56"

# The benchmark's stages: name, and what it does, in a few words.
BENCH_STAGES = [
    ("fetch", "at its pinned release"),
    ("check", "the whole benchmark, cheaply"),
    ("adapt", "into Harbor tasks"),
    ("screen", "admission gates"),
    ("rank", "by the brief"),
    ("select", "the top tasks"),
    ("curate", "in the work cells"),
    ("review", "sign each report"),
    ("curated", "every task has an outcome"),
]
FETCH, CHECK, ADAPT, SCREEN, RANK, SELECT, CURATE, REVIEW, DONE = range(len(BENCH_STAGES))   # where each box is

# A task's stages in a work cell, by their index in STAGES, and what each does.
TASK_STAGES = [(STAGE_NO[name], name, what) for name, what in (
    ("verify", "solution audited: valid?"),
    ("probe", "tests stated in instruction?"),
    ("worker", "solved twice = too easy"),
    ("audit", "task's fault, or the agent's?"),
    ("fix", "repair, then check again"),
    ("difficulty", "kept if ≤ 3 of 6 pass"),
    ("report", "audit, then recommend"),
)]
_REACHED: dict[tuple[int, str], int] = {}      # the furthest stage each benchmark reached, for one that stopped on the way


ARROWS = {4: (" ──▶", " ──▷"), 3: (" ─▶", " ─▷"), 2: ("─▶", "─▷"), 1: ("▶", "▷")}     # done, not yet: by how wide


def _fit(boxes: list[tuple[str, str, str]], width: int) -> tuple[int, int]:
    """The inside width of each box and the width of each arrow, so that the boxes and their arrows fit the panel on
    one line. The widest arrows are kept while every label fits (a box's mark gives way first, see strip); then the
    arrows give way, before any label is cut. On a panel too narrow even for the shortest arrows, the boxes shrink, down
    to 4."""
    n = len(boxes)
    need = max(len(label) for label, _, _ in boxes)
    for a in (4, 3, 2, 1):
        w = min(14, (width - a * (n - 1)) // n - 2)
        if w >= need or a == 1:
            return max(4, w), a
    return 4, 1


def strip(boxes: list[tuple[str, str, str]], width: int, now: float, loop: tuple[int, int, str, bool] | None = None) -> Group:
    """Boxes joined by arrows. Each is (label, line under it, state): done, now, wait, todo, skip or fail. `loop` draws an
    arrow back from one box to an earlier one, under the strip: (from, to, words, lit)."""
    w, a = _fit(boxes, width)
    look = {"done": (TE, TE, "✓ "), "now": (PU, f"bold {PU}", spinner(now) + " "), "wait": (AM, f"bold {AM}", "? "),
            "alert": (AM, f"bold {AM}", spinner(now) + " "),
            "todo": (FAINT, MU, ""), "skip": (FAINT, FAINT, "– "), "fail": (RD, f"bold {RD}", "✗ ")}
    top, mid, bot, sub = Text(), Text(), Text(), Text()
    centre = lambda i: i * (w + 2 + a) + (w + 2) // 2      # noqa: E731
    for i, (label, under, state) in enumerate(boxes):
        edge, ink, mark = look[state]
        if state == "now":
            edge = blend(PU, "#ffffff", 0.25 + 0.25 * abs(((now * 1.2) % 2) - 1))    # the lit box breathes
        # The label is never cut while the mark can give way: first its space, then the mark itself (the box's color
        # still says its state).
        name = next((x for x in (mark + label, mark.strip() + label, label) if len(x) <= w), label[:w])
        top.append("╭" + "─" * w + "╮", style=edge)
        mid.append("│", style=edge)
        mid.append(name.center(w), style=ink)
        mid.append("│", style=edge)
        bot.append("╰" + "─" * w + "╯", style=edge)
        # The line under a box may reach into the gaps on either side of it, so that "not needed" is not cut under a
        # narrow box; it never reaches the line under the next box.
        words = under[:w + 1 + a]
        at = max(len(sub.plain), centre(i) - len(words) // 2)
        sub.append(" " * (at - len(sub.plain)))
        sub.append(words, style=SE if state in ("done", "now", "wait", "alert") else FAINT)
        if i < len(boxes) - 1:
            done = state == "done"
            top.append(" " * a)
            mid.append(ARROWS[a][0 if done else 1], style=TE if done else FAINT)
            bot.append(" " * a)
    if loop is None:
        return Group(top, mid, bot, sub)
    start, end, words, lit = loop
    x0, x1 = centre(end), centre(start)
    span = x1 - x0 - 1
    label = f" {words} "[:span]
    left = (span - len(label)) // 2
    back = Text(" " * x0)
    back.append("╰" + "─" * left + label + "─" * (span - left - len(label)) + "╯", style=AM if lit else FAINT)
    arrow = Text(" " * x0)
    arrow.append("▲", style=AM if lit else FAINT)
    arrow.append(" " * (x1 - x0 - 1) + "│", style=AM if lit else FAINT)
    return Group(top, mid, bot, sub, arrow, back)


# ----- the benchmark -----

def bench_stage(s: Session, b: BenchState) -> tuple[int, str]:
    """Which of BENCH_STAGES a benchmark is in, and how: now, wait (a box asks a person), fail, or done (DONE)."""
    phase = b.phase
    if phase == "skipped":
        at = {"claimed": FETCH, "claim_failed": FETCH, "fetch_failed": FETCH, "checks_failed": CHECK, "environment_private": CHECK,
              "abandoned": ADAPT, "adapter_needs_person": ADAPT, "no_adapter": ADAPT, "adapter_failed": ADAPT, "adapter_not_locked": ADAPT,
              "model_error": ADAPT, "not_harbor_format": ADAPT, "no_task_passed": SCREEN, "no_brief": RANK, "selection_not_approved": SELECT,
              "none_accepted": REVIEW}
        return at.get(b.skip, _REACHED.get((id(s), b.id), FETCH)), "fail"
    if phase == "ready":
        working = b.queued or any(c.task is not None and c.task.benchmark == b.id for c in s.cells)
        if not b.chosen and not working:            # nothing was chosen: nothing to curate
            return DONE, "done"
        if working:
            index, how = CURATE, "now"
        elif any(task.benchmark == b.id for task in s.ready):
            index, how = REVIEW, "wait"
        else:
            index, how = DONE, "done"
    else:
        index, how = {"queued": (FETCH, "todo"), "fetch": (FETCH, "now"), "check": (CHECK, "now"), "check_review": (CHECK, "wait"),
                      "adapt": (ADAPT, "now"), "review": (ADAPT, "wait"), "triage": (SCREEN, "now"), "brief": (RANK, "wait"),
                      "ranking": (RANK, "now"), "signoff": (SELECT, "wait"), "cells": (CURATE, "wait")}.get(phase, (FETCH, "now"))
    key = (id(s), b.id)
    _REACHED[key] = max(_REACHED.get(key, 0), index)
    return index, how


def _foreign(b: BenchState) -> bool:
    return bool(b.entry) and b.entry.get("format", "harbor") != "harbor"


def bench_boxes(s: Session, b: BenchState, index: int, how: str) -> list[tuple[str, str, str]]:
    kept = len(b.tasks) - b.triaged_out
    review = sum(1 for task in s.ready if task.benchmark == b.id)
    running = sum(1 for c in s.cells if c.task is not None and c.task.benchmark == b.id)
    # under a box there are 12 columns: "17/20 3 left", "20/20 done"
    done = f"{b.settled}/{len(b.chosen)} {len(b.left)} left" if b.left else f"{b.settled}/{len(b.chosen)} done"
    under = [f"{len(b.tasks)} tasks" if b.tasks else "", b.checked, "" if _foreign(b) else "not needed",
             f"{kept} kept" if b.tasks and index > SCREEN else "", f"{len(b.ranking)} ranked" if b.ranking else "",
             f"{len(b.chosen)} chosen" if b.chosen else "",
             (f"{running} working" if running else done) if b.chosen else "",
             f"{review} to review" if review else "",
             (f"{len(b.left)} left" if b.left else f"{b.kept} kept") if index == DONE else ""]
    boxes = []
    for i, (name, _) in enumerate(BENCH_STAGES):
        if (i == ADAPT and not _foreign(b)) or (i == CHECK and not b.entry):   # in Harbor format; not on the list: made up
            state = "skip"
        elif i == DONE:         # through, with tasks left for the next run: not curated yet, and it says so in amber
            state = ("wait" if b.left else "done") if index == DONE else "todo"
        elif index == DONE or i < index:
            state = "done"
        elif i == index:
            state = how if how != "done" else "done"
        else:
            state = "todo"
        boxes.append((name, under[i], state))
    return boxes


def _names(left: list[tuple[str, str]], most: int = 3) -> str:
    names = [tid.split("/", 1)[-1] for tid, _ in left]
    return ", ".join(names[:most]) + (f" and {len(names) - most} more" if len(names) > most else "")


_STANDING: dict[tuple, dict[str, int]] = {}
ORDER = ("working", "waiting", "to review", "accepted", "rejected", "contested", "dropped", "left for retry")
STANDING_COLOR = {"working": PU, "waiting": MU, "to review": AM, "accepted": TE, "rejected": RD, "contested": AM,
                  "dropped": RD, "left for retry": AM}


def standing(s: Session, b: BenchState) -> dict[str, int]:
    """Every chosen task in exactly one state, so that they add up to all of them: working in a cell, waiting for one,
    to review (its report waits for a verdict), accepted, rejected, dropped, or left for retry (this run let it go with no
    outcome). Outcomes come from the ledger, whichever run reached them. Counted again only when something changed."""
    working = {c.task.task_id for c in s.cells if c.task is not None and c.task.benchmark == b.id}
    left = {tid for tid, _ in b.left}
    key = (id(s), b.id, len(s.ledger.events), tuple(sorted(left)), tuple(sorted(working)), tuple(b.chosen))
    if key not in _STANDING:
        from ..ledger import fold
        states = fold(e for e in s.ledger.events if e.get("benchmark") == b.id)
        got = dict.fromkeys(ORDER, 0)
        for tid in b.chosen:
            status = states[tid].status if tid in states else ""
            if tid in working:
                got["working"] += 1
            elif tid in left:
                got["left for retry"] += 1
            else:
                got[{"accepted": "accepted", "rejected": "rejected", "contested": "contested", "report_ready": "to review",
                     "dropped": "dropped"}.get(status, "waiting")] += 1
        _STANDING.clear()
        _STANDING[key] = {name: n for name, n in got.items() if n}
    return _STANDING[key]


def standing_text(s: Session, b: BenchState) -> Text:
    """'20 tasks: 4 working · 10 waiting · 1 accepted · 4 dropped · 1 left for retry', nothing that is zero."""
    out = Text(f"{len(b.chosen)} tasks: ", style=SE)
    for i, (name, n) in enumerate(standing(s, b).items()):
        if i:
            out.append(" · ", style=MU)
        out.append(f"{n} {name}", style=STANDING_COLOR.get(name, SE))
    return out


def standing_line(s: Session, b: BenchState) -> str:
    """The same, as plain words, for a box."""
    return f"{len(b.chosen)} tasks: " + " · ".join(f"{n} {name}" for name, n in standing(s, b).items())


def bench_view(s: Session, b: BenchState, width: int, now: float) -> Group:
    index, how = bench_stage(s, b)
    lines: list = [strip(bench_boxes(s, b, index, how), width, now), Text()]
    if index == DONE and b.left:        # this run is through, but not every chosen task has an outcome
        said = t(("● not finished   ", f"bold {AM}"))
        said.append_text(standing_text(s, b))
        said.append(f" ({_names(b.left)})", style=AM)
    elif index == DONE:
        said = t(("✓ curated   ", f"bold {TE}"))
        said.append_text(standing_text(s, b) if b.chosen else t(("nothing was chosen", MU)))
    else:
        name, what = BENCH_STAGES[index]
        color = {"now": PU, "wait": AM, "fail": RD, "todo": MU}[how]
        verb = {"now": "", "wait": "your turn: ", "fail": "stopped: ", "todo": "waiting to start"}[how]
        said = t((f"● {name}", f"bold {color}"), (f" · {what}", SE))
        if index == CURATE and b.chosen:    # every chosen task, in one state each: they add up to all of them
            said.append("   ")
            said.append_text(standing_text(s, b))
        else:
            said.append("   " + verb + b.detail, style=color if how != "now" else MU)
    lines.append(said)
    return Group(*lines)


def benchmarks(s: Session, width: int, now: float) -> Group | Text:
    if not s.benches:
        return t(("No benchmark yet: the box asks which one to curate.", MU))
    parts: list = []
    for b in s.benches.values():
        if len(s.benches) > 1:
            parts.append(t((b.id, "bold")))
        parts.append(bench_view(s, b, width, now))
    return Group(*parts)


# ----- the work cells -----

def tabs(s: Session, shown: int) -> Text:
    """One tab per work cell that takes tasks: its number, its task and its stage. The one shown is lit."""
    out = Text()
    for c in s.cells[:s.active_cells]:
        what = f"{c.task.name} · {STAGES[c.stage]}" if c.task is not None else "idle"
        label = f" {c.n}  {what} "
        style = f"bold {BG_LIT} on {PU}" if c.n == shown else (SE if c.task is not None else MU)
        out.append(label, style=style)
        out.append("  ")
    return out


BG_LIT = "#16161d"


def task_boxes(s: Session, c: CellState) -> list[tuple[str, str, str]]:
    board = s.blackboards.get(c.task.task_id, {}) if c.task is not None else {}
    seen = [x.get("stage") for x in board.get("stages", [])]
    seconds = {x.get("stage"): x.get("seconds", 0) for x in board.get("stages", [])}
    boxes = []
    for index, name, _ in TASK_STAGES:
        if c.task is None:
            state, under = "todo", ""
        elif index == c.stage:
            state, under = ("alert" if c.attention or name == "fix" else "now"), "now"
        elif name in seen:
            state, under = "done", f"{seconds.get(name, 0):.0f}s"
        elif name == "fix" and c.stage > index:             # the task passed with nothing to repair
            state, under = "skip", "not needed"
        elif index < c.stage:       # passed by this version in an earlier run: a repair would have sent it back to verify
            state, under = "done", "earlier run"
        else:
            state, under = "todo", ""
        boxes.append((name, under, state))
    return boxes


def cell_view(s: Session, shown: int, width: int, now: float) -> Group:
    cell = next((c for c in s.cells if c.n == shown), s.cells[0])
    parts: list = [tabs(s, cell.n), Text()]
    if cell.task is not None:
        info = cell.task.meta.get("task", {})
        text = re.sub(r"<!--.*?-->", "", cell.task.instruction, flags=re.S)      # no canary comment as a title
        title = info.get("description") or next((line.lstrip("# ").strip() for line in text.splitlines() if line.strip()), "")
        title = title if len(title) <= 80 else title[:77].rstrip() + "..."
        parts.append(t((cell.task.name, "bold"), (f"  {title}" if title else "", SE), (f"   {clock(now - cell.started)}", MU)))
    else:
        parts.append(t(("waiting for a task" if s.queue or not s.benches else "nothing left for this cell", MU)))
    fixing = cell.task is not None and cell.stage == STAGE_NO["fix"]
    parts.append(strip(task_boxes(s, cell), width, now, loop=(4, 0, "repaired: back to verify", fixing)))
    if cell.task is not None:
        what = next((w for i, _, w in TASK_STAGES if i == cell.stage), "")
        parts.append(t((f"● {STAGES[cell.stage]}", f"bold {AM if cell.attention else PU}"), (f" · {what}", SE),
                       (f"   {cell.detail}" if cell.detail else "", MU)))
    return Group(*parts)


def cells_title(s: Session, shown: int) -> str:
    busy = sum(1 for c in s.cells[:s.active_cells] if c.task is not None)
    keys = f"1-{s.active_cells}" if s.active_cells > 1 else "1"
    return f"work cell {shown}/{s.active_cells} · {busy} busy · {keys} to switch"


__all__ = ["BENCH_STAGES", "TASK_STAGES", "bench_stage", "benchmarks", "cell_view", "cells_title", "strip", "time"]
