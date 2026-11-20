"""The eval screen (DESIGN.md Section 9): what `hm eval` shows while an evaluation is going.

It is laid out as the curate screen is: a header, one line of figures, the roster's configurations where
the curate screen has its benchmarks, the working cells, what happened last, and a tip. There is
nothing to type: the evaluation takes cells from the to-do list, runs them, and takes more, by itself. Three
keys pause it, switch light and dark, and end it.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time

from rich.table import Table
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from .. import __version__
from ..engine import Evaluation
from . import theme
from .app import decided, decision
from .parts import SCROLLBARS, ChoiceBox
from .theme import AM, BG, BORDER, FG, MU, PU, RD, SE, TE, clock, t

KEYS = t(("ctrl+p", SE), (" pause   ", MU), ("ctrl+l", SE), (" leave, it goes on   ", MU), ("ctrl+t", SE), (" light/dark   ", MU),
         ("ctrl+q", SE), (" end", MU))
DETACH = "\x1b_hm-detach\x1b\\"        # tells the relay behind this screen that the user has left it
KIND = {"passed": TE, "failed": RD, "claimed": PU, "done": TE}


class EvalApp(App[None]):
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [("ctrl+p", "toggle_pause", "Pause"), ("ctrl+q", "end", "End"), ("ctrl+l", "leave", "Leave"), ("ctrl+t", "theme", "Light or dark")]
    CSS = SCROLLBARS + f"""
    Screen {{ background: {BG}; color: {FG}; }}
    #top {{ height: 1fr; scrollbar-size: 0 0; }}
    #bottom {{ height: auto; dock: bottom; }}
    .panel {{ border: round {BORDER}; border-title-color: {MU}; padding: 0 1; height: auto; }}
    #tip {{ height: auto; max-height: 2; padding: 0 2; }}
    #keys {{ height: 1; padding: 0 2; }}
    """

    def __init__(self, run: Evaluation, title: str = ""):
        super().__init__()
        self.run_, self.title_ = run, title
        self._cells: list = []
        self._read = 0.0

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="top", can_focus=False):
            yield Static(id="header", classes="panel")
            yield Static(id="summary", classes="panel")
            yield Static(id="configs", classes="panel")
            yield Static(id="cells", classes="panel")
            yield Static(id="recent", classes="panel")
        with Vertical(id="bottom"):
            yield Static(id="tip")
            yield Static(KEYS, id="keys")

    def on_mount(self) -> None:
        for name, title in (("summary", "summary"), ("configs", "configurations"), ("cells", "working cells"), ("recent", "recent")):
            self.query_one(f"#{name}").border_title = title
        self._light = theme.light_filter()
        self._filters.append(self._light)
        self._task = asyncio.create_task(self.run_.run())
        self.set_interval(0.2, self.draw)
        self.draw()
        for sig in (signal.SIGTERM, signal.SIGHUP):         # told to end from outside: finish the trials in hand, then go
            try:
                asyncio.get_running_loop().add_signal_handler(sig, self.action_end, True)
            except (NotImplementedError, RuntimeError, ValueError):
                pass

    # ----- drawing -----

    def draw(self) -> None:
        theme.follow(self, self._light)
        run, now = self.run_, time.time()
        s = run.s
        asked = next((r for r in s.requests if r.blocking), None)
        if asked is not None and not isinstance(self.screen, ChoiceBox):       # a trial failed to run: retry it, or discard it
            box = decision(s, asked)
            if box is not None:
                self.push_screen(box, lambda said, asked=asked: decided(s, asked, said or "discard"))
        if isinstance(self.screen, ChoiceBox):
            return
        if now - self._read > 1.0 or run.finished:      # the to-do list is read from the records, once a second
            self._cells, self._read = run.cells(), now
        cells = self._cells
        g = Table.grid(expand=True)
        g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        g.add_column(justify="right", no_wrap=True)
        g.add_row(t(("harbor-master", f"bold {PU}"), (f" v{__version__}", MU), ("   evaluate", f"bold {TE}"),
                    ("   simulated: nothing is spent, and nothing enters the index" if s.sandbox else "", AM)),
                  t(s.ident.name, (f" (@{s.ident.github})", MU)))
        state = (run.state, AM if run.paused else TE)
        g.add_row(t(("index  ", MU), self.title_), t((f"● {state[0]}", state[1])))
        self.query_one("#header", Static).update(g)

        total, done = sum(c.trials for c in cells), sum(len(c.done) for c in cells)
        line = Table.grid(expand=True)
        for _ in range(5):
            line.add_column(ratio=1, no_wrap=True)
        line.add_row(t(("trials  ", MU), str(done), (f" of {total}", MU)),
                     t(("cells  ", MU), str(sum(1 for x in run.slots if x.cell)), (f" of {len(run.slots)}", MU)),
                     t(("containers  ", MU), str(s.containers_used), (f" of {s.cfg.run.max_containers}", MU)),
                     t(("spent  ", MU), f"${s.trial_cost:.2f}"), t(("elapsed  ", MU), clock(now - run.started)))
        self.query_one("#summary", Static).update(line)

        tb = Table(box=None, expand=True, padding=(0, 2, 0, 0), pad_edge=False, header_style=MU, show_edge=False)
        for col, j in (("configuration", "left"), ("trials", "right"), ("passed", "right"), ("yours now", "right"), ("held by others", "right"),
                       ("free", "right"), ("given up", "right")):
            tb.add_column(col, justify=j, no_wrap=True)
        left = lambda group: sum(len(c.missing) for c in group)      # noqa: E731
        for name in dict.fromkeys(c.config.name for c in cells):
            mine = [c for c in cells if c.config.name == name]
            ran = [e for c in mine for e in c.done.values()]
            tb.add_row(name, t(str(len(ran)), (f" of {sum(c.trials for c in mine)}", MU)), t((str(sum(bool(e["passed"]) for e in ran)), TE)),
                       str(left([c for c in mine if c.open and c.mine])), str(left([c for c in mine if c.open and c.holder])),
                       str(left([c for c in mine if c.open and not c.mine and not c.holder])), t((str(left([c for c in mine if c.failed])), RD)))
        self.query_one("#configs", Static).update(tb)

        work = Table(box=None, expand=True, padding=(0, 2, 0, 0), pad_edge=False, header_style=MU, show_edge=False)
        for col, kw in (("#", {"width": 1}), ("task", {"min_width": 18}), ("configuration", {"min_width": 14}), ("now", {"ratio": 1}), ("time", {"justify": "right"})):
            work.add_column(col, no_wrap=True, **kw)
        for slot in run.slots:
            if slot.cell is None:
                work.add_row(t((str(slot.n), MU)), t(("idle", MU)), "", "", "")
            else:
                work.add_row(str(slot.n), slot.cell.task["task"].split("/", 1)[-1], slot.cell.config.name, t((slot.detail, SE)), clock(now - slot.started))
        self.query_one("#cells", Static).update(work)

        log = Table.grid(expand=True, padding=(0, 2, 0, 0))
        log.add_column(no_wrap=True)
        log.add_column(width=7, no_wrap=True)
        log.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        for ts, kind, text in list(run.recent)[:4]:
            log.add_row(t((time.strftime("%H:%M", time.localtime(ts)), MU)), t((kind, KIND.get(kind, SE))), t((text, SE)))
        self.query_one("#recent", Static).update(log)
        self.query_one("#recent").display = bool(run.recent)
        self.query_one("#tip", Static).update(t(("tip  ", f"bold {TE}"), (self.tip(), SE)))

    def tip(self) -> str:
        run = self.run_
        if run.quitting and not run.finished:
            return "Ending: each cell finishes the trial it is in and shares its records. The cells you still hold run out by themselves."
        if run.finished:
            return "The evaluation has ended. ctrl+q closes it. The leaderboard is on the index's screen, and hm todo shows what is left."
        if run.paused:
            return "Paused: the trials that are going finish, and no more cells are taken. ctrl+p goes on."
        if run.state == "taking cells":
            return "Taking cells: the to-do list is every accepted task with every configuration. The next free ones are claimed, and the claim is pushed."
        return ("Each cell is one task with one configuration. Its trials run one after the other, and each is recorded and shared as it ends. "
                "When the cells are done, more are taken. Nothing is needed from you.")

    def action_toggle_pause(self) -> None:
        self.run_.paused = not self.run_.paused

    def action_theme(self) -> None:
        theme.set_mode("")

    def action_leave(self) -> None:
        if os.environ.get("HM_HOSTED") and self._driver is not None:       # behind a relay: the evaluation goes on
            self._driver.write(DETACH)
            self._driver.flush()

    def action_end(self, now: bool = False) -> None:
        run = self.run_
        if run.finished:
            self.exit()
        elif now:
            run.quitting = True          # each cell ends after the trial it is in, and the screen goes when they have
            for r in run.s.requests:     # a question that is still open is answered for the person: nothing is tried again
                if r.answer is not None and not r.answer.done():
                    r.answer.set_result("discard")
            self.set_interval(0.2, lambda: run.finished and self.exit())
        elif not isinstance(self.screen, ChoiceBox):
            self.push_screen(ChoiceBox("end the evaluation", "End this evaluation?\nEach cell finishes the trial it is in and shares its "
                                       "records. The cells you still hold run out by themselves, and are then free for others.",
                                       [("no", "Keep it going"), ("yes", "End it")], cancel=True),
                             lambda said: said == "yes" and self.action_end(True))
