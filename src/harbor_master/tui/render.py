"""Rich renderables for each region of the session screen (DESIGN.md Section 10)."""
from __future__ import annotations

import time

from rich.table import Table
from rich.text import Text

from .. import __version__
from ..engine import STAGES, Session
from .theme import AM, BORDER, KIND_COLOR, MU, PU, RD, SE, TE, clock, pulse, spinner, t


def header(s: Session, width: int = 120) -> Table:
    g = Table.grid(expand=True)
    g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    g.add_column(justify="right", no_wrap=True)
    who = f" ({s.ident.affiliation}, @{s.ident.github})" if width >= 100 else f" (@{s.ident.github})"
    g.add_row(t(("harbor-master", f"bold {PU}"), (f" v{__version__}", MU)), t(s.ident.name, (who, MU)))
    state = (("paused", AM) if s.paused else ("finished", TE) if s.finished else ("running", TE))
    g.add_row(t(("$ ", MU), s.command or "/run"), t((f"  ● {state[0]}", state[1])))
    return g


def summary(s: Session, now: float, width: int = 120) -> Table:
    """One line. On a narrow terminal the labels shorten, so that nothing is cut off."""
    busy = sum(1 for c in s.cells if c.task)
    k = lambda n: f"{n / 1000:.1f}k" if n >= 1000 else str(n)  # noqa: E731
    wide = width >= 110
    sep = " of " if wide else "/"
    items = [
        t(("cells  ", MU), str(busy), (f"{sep}{s.active_cells}", MU)),
        t(("containers  " if wide else "boxes  ", MU), str(s.containers_used), (f"{sep}{s.max_containers}", MU)),
        t(("spent  ", MU), f"${s.spent:.2f}"),
        t(("elapsed  " if wide else "", MU), clock(now - s.started)),
        t(("calls  " if wide else "", MU), ("D ", TE), k(s.calls["decision"]), (" W ", PU), k(s.calls["worker"]),
          (" F ", AM), k(s.calls["frontier"])),
    ]
    g = Table.grid(expand=True, padding=(0, 2, 0, 0), pad_edge=False)
    for i in range(len(items)):
        g.add_column(justify="right" if i == len(items) - 1 else "left", no_wrap=True)
    g.add_row(*items)
    return g


def bar(frac: float, width: int = 22) -> Text:
    full = round(max(0.0, min(1.0, frac)) * width)
    return t(("█" * full, PU), ("░" * (width - full), "#34343f"), (f" {round(frac * 100):>3}%", SE))


def benchmarks(s: Session, now: float) -> Table:
    tb = Table(box=None, expand=True, padding=(0, 2, 0, 0), pad_edge=False, header_style=MU, show_edge=False)
    tb.add_column("benchmark", no_wrap=True)
    tb.add_column("progress", width=30, no_wrap=True)
    for name in ("queued", "kept", "drop", "cost"):
        tb.add_column(name, justify="right")
    for b in s.benches.values():
        if b.phase == "ready":
            progress = bar(b.done / b.selected) if b.selected else t(("nothing selected", MU))
            if b.paused:
                progress = t(("paused  ", AM), progress)
            cols = [t((str(b.queued), SE)), t((str(b.kept), TE)), t((str(b.dropped), RD))]
        else:
            color = AM if b.phase in ("brief", "signoff", "review", "skipped") else PU
            mark = "!" if b.phase == "skipped" else spinner(now)
            progress = t((f"{mark} ", color), (b.detail or b.phase, color))
            cols = [t(("-", MU))] * 3
        tb.add_row(b.id, progress, *cols, f"${b.cost:.2f}")
    return tb


TO_COME = "#4b4b57"
NAME_WIDTH = max(len(name) for name in STAGES)


def stage_marks(stage: int, color: str, now: float) -> Text:
    """The stages as one track: done ones solid, the running one breathing, the ones to come faint."""
    return t(("●" * stage, TE), ("●", pulse(color, now)), ("•" * (len(STAGES) - stage - 1), TO_COME))


def cells(s: Session, now: float) -> Table:
    tb = Table(box=None, expand=True, padding=(0, 2, 0, 0), pad_edge=False, header_style=MU, show_edge=False)
    tb.add_column("", width=1)
    tb.add_column("#", width=1)
    tb.add_column("task", min_width=18, no_wrap=True)
    tb.add_column("stages", width=10, no_wrap=True)
    tb.add_column("now", ratio=1, no_wrap=True)
    tb.add_column("time", justify="right")
    for c in s.cells:
        if c.task is None:
            idle = "off" if c.n > s.active_cells else "idle"
            tb.add_row(t(("·", MU)), t((str(c.n), MU)), t((idle, MU)), "", "", "")
            continue
        color = AM if c.attention else PU
        name = STAGES[c.stage]
        now_text = t((f"{name:<{NAME_WIDTH}}", f"bold {color}"), ("  " + c.detail, SE))     # one color, and details in a column
        tb.add_row(t((spinner(now), color)), t((str(c.n), MU)), c.task.task_id.split("/", 1)[-1][-28:],
                   stage_marks(c.stage, color, now), now_text, t((clock(now - c.started), SE)))
    return tb


def rows(items: list[tuple[Text, Text]], label_width: int = 4) -> Table:
    """Labelled lines whose text wraps under itself, not under the label."""
    g = Table.grid(expand=True, padding=(0, 1, 0, 0))
    g.add_column(width=label_width, no_wrap=True)
    g.add_column(ratio=1)
    for label, text in items:
        g.add_row(label, text)
    return g


def recent(s: Session, n: int = 3) -> Table:
    g = Table.grid(expand=True, padding=(0, 2, 0, 0), pad_edge=False)
    g.add_column(no_wrap=True)
    g.add_column(width=7, no_wrap=True)
    g.add_column(no_wrap=True)
    g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    for ts, kind, task_id, text in list(s.recent)[:n]:
        g.add_row(t((time.strftime("%H:%M", time.localtime(ts)), MU)), t((kind, KIND_COLOR.get(kind, SE))),
                  task_id.split("/", 1)[-1], t((text, SE)))
    return g


def log_row(entry: tuple[float, str, str, str]) -> Table:
    """One event in the curate screen's log: when, what kind, which task, and what happened, in full. A long line wraps
    under its own words, not under the time."""
    ts, kind, task_id, text = entry
    g = Table.grid(expand=True, padding=(0, 2, 0, 0), pad_edge=False)
    g.add_column(no_wrap=True)
    g.add_column(width=8, no_wrap=True)
    g.add_column(width=12, no_wrap=True, overflow="ellipsis")
    g.add_column(ratio=1)
    g.add_row(t((time.strftime("%H:%M:%S", time.localtime(ts)), MU)), t((kind, KIND_COLOR.get(kind, SE))),
              task_id.split("/", 1)[-1], t((text, SE)))
    return g


def recent_line(entry: tuple[float, str, str, str]) -> Text:
    ts, kind, task_id, text = entry
    return t((time.strftime("%H:%M:%S", time.localtime(ts)) + " ", MU), (f"{kind:<9}", KIND_COLOR.get(kind, SE)),
             f"{task_id}  ", (text, SE))


def final_table(s: Session) -> Table:
    tb = Table(header_style=MU, border_style=BORDER)
    for name in ("benchmark", "tasks", "failed a gate", "under the cap", "selected", "kept", "dropped", "cost"):
        tb.add_column(name, justify="left" if name == "benchmark" else "right")
    for b in s.benches.values():
        tb.add_row(b.id, str(len(b.tasks)), str(b.triaged_out), str(b.not_selected), str(b.selected),
                   t((str(b.kept), TE)), t((str(b.dropped), RD)), f"${b.cost:.2f}")
    return tb
