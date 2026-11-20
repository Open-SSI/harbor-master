"""The curate screen (DESIGN.md Section 11), built on Textual.

The run goes on underneath. Nothing is typed here: there are no commands. Whatever the run needs from a
person, it asks in a box that opens by itself and has to be answered: a brief, how many tasks go on, a
verdict on each report, whether to raise the budget, what to do about a trial that failed to run. A few
keys do the rest: a cell's number opens its task, and pause, leave and end have a key each.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Static

from .. import interact
from ..engine import Session
from ..interact import Command, guide
from . import render
from . import theme
from .parts import SCROLLBARS, ChoiceBox
from .theme import BG, BORDER, FG, MU, PU, SE, TE, t
from .widgets import AskScreen, ReviewScreen, SelectionScreen, TaskScreen

KEYS = t(("1-9", SE), (" open a cell   ", MU), ("ctrl+p", SE), (" pause   ", MU), ("ctrl+l", SE), (" leave, it goes on   ", MU),
         ("ctrl+t", SE), (" light/dark   ", MU), ("ctrl+q", SE), (" end", MU))
DETACH = "\x1b_hm-detach\x1b\\"        # tells the relay behind this screen that the user has left it
BOXES = (AskScreen, SelectionScreen, ReviewScreen, ChoiceBox)


def decision(session, request) -> ChoiceBox | None:
    """The box for a request that is a choice between a few things, or None when it is asked another way."""
    if request.kind == "budget":
        more = max(2 * session.budget, 2 * session.spent)
        full = session.spent >= session.budget
        return ChoiceBox("the budget", request.text, [("raise", f"Raise it to ${more:.0f}, and go on"),
                                                      ("end", "End the run here") if full else ("keep", f"Keep it at ${session.budget:.0f}")])
    if request.kind == "failure":
        return ChoiceBox("a trial failed to run", request.text, [("retry", "Retry: run this trial again"),
                                                                 ("discard", "Discard: record the failure, and go on without it")])
    return None


def decided(session, request, said: str) -> None:
    """Carry out what was chosen in a decision box."""
    if request.kind == "budget":
        session.requests[:] = [r for r in session.requests if r is not request]
        if said == "raise":
            interact.execute(session, Command("budget", [str(max(2 * session.budget, 2 * session.spent))]))
        elif said == "end":
            interact.execute(session, Command("quit"))
        session.write_requests()
    elif request.answer is not None and not request.answer.done():
        request.answer.set_result(said)


class SessionApp(App[None]):
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [("ctrl+p", "toggle_pause", "Pause"), ("ctrl+q", "quit_run", "Quit"), ("ctrl+l", "leave", "Leave"),
                ("ctrl+t", "theme", "Light or dark"), ("escape", "close", "Close")]
    CSS = SCROLLBARS + f"""
    Screen {{ background: {BG}; color: {FG}; }}
    #top {{ height: 1fr; scrollbar-size: 0 0; }}
    #bottom {{ height: auto; dock: bottom; }}
    .panel {{ border: round {BORDER}; border-title-color: {MU}; padding: 0 1; height: auto; }}
    #answer {{ border: round {PU}; border-title-color: {PU}; border-subtitle-color: {PU}; display: none; max-height: 12; }}
    #tip {{ height: auto; max-height: 2; padding: 0 2; }}
    #keys {{ height: 1; padding: 0 2; }}
    """

    def __init__(self, session: Session, exit_when_done: bool = False):
        super().__init__()
        self.session = session
        self.exit_when_done = exit_when_done
        self._run: asyncio.Task | None = None
        self._told_finished = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="top", can_focus=False):
            yield Static(id="header", classes="panel")
            yield Static(id="summary", classes="panel")
            yield Static(id="benchmarks", classes="panel")
            yield Static(id="cells", classes="panel")
            yield Static(id="recent", classes="panel")
        with Vertical(id="bottom"):
            yield Static(id="answer", classes="panel")
            yield Static(id="tip")
            yield Static(KEYS, id="keys")

    def on_mount(self) -> None:
        for name in ("summary", "benchmarks", "recent"):
            self.query_one(f"#{name}").border_title = name
        self.query_one("#cells").border_title = "working cells"
        self._light = theme.light_filter()
        self._filters.append(self._light)
        self._run = asyncio.create_task(self.session.run())
        self.set_interval(0.1, self.refresh_view)
        self.refresh_view()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGHUP):         # told to end: close the run's books, then go
            try:
                loop.add_signal_handler(sig, self._end)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        if os.environ.get("HM_CONTROL"):                    # the door another run knocks on, to add a benchmark
            self._door = asyncio.create_task(self._listen(os.environ["HM_CONTROL"]))

    def _end(self) -> None:
        """Told to end from outside. A run that is still going closes its books first, and the screen goes when it has."""
        if self.session.finished:
            self.exit()
        else:
            self.session.kill()

    async def _listen(self, path: str) -> None:
        async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request = json.loads(await reader.readline() or b"{}")
                lines = [interact.execute(self.session, Command("add", [name])) for name in request.get("add", [])]
                reply = {"ok": bool(lines) and all("is added" in line or "already in" in line for line in lines), "text": "\n".join(lines)}
            except (ValueError, OSError) as err:
                reply = {"ok": False, "text": f"The run could not read that: {err}"}
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(answer, path)
        os.chmod(path, 0o600)
        async with server:
            await server.serve_forever()

    # ----- drawing -----

    def refresh_view(self) -> None:
        s = self.session
        asked = next((r for r in s.requests if r.blocking), None)
        if isinstance(self.screen, BOXES):
            pass        # one box at a time: the next opens when this one is answered
        elif asked is not None:
            # Whatever the run needs from the person is put in front of them, in a box that has to be answered.
            box = decision(s, asked)
            if box is not None:
                self.push_screen(box, lambda said, asked=asked: decided(s, asked, said or ""))
            else:
                page = SelectionScreen(s, asked) if asked.kind == "selection" else AskScreen(s, asked)
                self.push_screen(page, lambda said, kind=asked.kind: self.show_result(kind, said or ""))
        elif s.is_reviewer and s.ready:
            # A report is ready: it is put in front of its reviewer, and nothing else is done here until it has a verdict.
            self.push_screen(ReviewScreen(s, list(s.ready)), lambda said: self.show_result("review", said or ""))
        try:
            self._refresh_view()
        except NoMatches:       # a box is open, or the app is closing
            pass

    def _refresh_view(self) -> None:
        theme.follow(self, self._light)
        s, now, width = self.session, time.time(), self.size.width
        self.query_one("#header", Static).update(render.header(s, width))
        self.query_one("#summary", Static).update(render.summary(s, now, width))
        self.query_one("#benchmarks", Static).update(render.benchmarks(s, now))
        self.query_one("#cells", Static).update(render.cells(s, now))
        self.query_one("#recent", Static).update(render.recent(s))
        told = guide.session_tip(s)
        tip = self.query_one("#tip", Static)
        tip.display = bool(told)        # nothing to add: the line is not shown
        tip.update(t(("tip  ", f"bold {TE}"), (told, SE)))
        self._fit()
        if s.leaving:
            s.leaving = False
            self.action_leave()
        if not s.finished:
            self._told_finished = False     # a benchmark was added to a finished run: it is a run again
        if s.finished and not self._told_finished:
            self._told_finished = True
            if s.quitting or self.exit_when_done:
                self.exit()
                return
            self.show_result("finished", interact.execute(s, Command("status")) + "\nThe run is finished. It stays open: "
                             "ctrl+l leaves it, and ctrl+q ends it.")

    def _fit(self) -> None:
        """Keep the working cells on screen. When rows run short, the least needed panels go first."""
        s = self.session
        need = {"header": 4, "summary": 3, "benchmarks": 3 + len(s.benches), "cells": 3 + len(s.cells),
                "recent": 2 + min(3, len(s.recent)) if s.recent else 0}
        room = self.size.height - self.query_one("#bottom").outer_size.height
        shown = {name for name, rows in need.items() if rows}
        for name in ("recent", "summary", "benchmarks", "header"):
            if sum(need[n] for n in shown) <= room:
                break
            shown.discard(name)
        for name in need:
            self.query_one(f"#{name}").display = name in shown

    def show_result(self, title: str, body: str) -> None:
        if not body:
            return
        box = self.query_one("#answer", Static)
        box.border_title, box.border_subtitle = title, "esc to close"
        box.update(render.rows([(t(("hm" if i == 0 else "", PU)), t(line)) for i, line in enumerate(body.splitlines())]))
        box.display = True

    # ----- keys -----

    def on_key(self, event: events.Key) -> None:
        if event.key.isdigit() and event.key != "0" and not isinstance(self.screen, BOXES):
            cell = next((c for c in self.session.cells if c.n == int(event.key)), None)
            if cell is not None and cell.task is not None:
                self.push_screen(TaskScreen(cell.task.task_id, interact.explain(self.session, cell.task.task_id)))

    def action_close(self) -> None:
        self.query_one("#answer").display = False

    def action_theme(self) -> None:
        theme.set_mode("")

    def action_toggle_pause(self) -> None:
        interact.execute(self.session, Command("resume" if self.session.paused else "pause"))

    def action_leave(self) -> None:
        """Leave the screen. Behind a relay the run goes on, and hm curate inside the index's folder opens it again."""
        if not os.environ.get("HM_HOSTED") or self._driver is None:
            self.show_result("cannot leave", "This run is tied to this terminal, so leaving would end it. ctrl+q ends it.")
            return
        self._driver.write(DETACH)
        self._driver.flush()

    def action_quit_run(self) -> None:
        if self.session.finished:
            self.exit()
        elif not isinstance(self.screen, ChoiceBox):
            self.push_screen(ChoiceBox("end the run", "End this run?\nWhat is running is stopped, and its claims are let go. What is "
                                       "recorded stays, and nothing is run twice when you start again.",
                                       [("no", "Keep it going"), ("yes", "End the run")], cancel=True),
                             lambda said: said == "yes" and interact.execute(self.session, Command("quit")))


def run_app(session: Session, exit_when_done: bool = False) -> None:
    SessionApp(session, exit_when_done).run()
