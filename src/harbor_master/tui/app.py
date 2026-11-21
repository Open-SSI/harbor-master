"""The curate screen (DESIGN.md Section 10), built on Textual.

It starts with a box: which benchmark to curate, from the index's list, or a new one by its link. Then the
benchmark goes through its stages, drawn as boxes joined by arrows with the one it is in lit (`stages.py`).
When its tasks are chosen, a box asks how many work cells (1 to 8) and containers run at once, and the
curating stage starts: below the benchmark, one work cell's task goes through its own stages, and keys 1
to 8 choose which cell is shown. Nothing is typed: whatever the run needs from a person, it asks in a box.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import RichLog, Static

from .. import interact
from ..engine import Session
from ..interact import Command
from . import render
from . import stages
from . import theme
from .boxes import CellsBox, ChecksScreen, DraftScreen, PickScreen
from .parts import SCROLLBARS, ChoiceBox
from .theme import BG, BORDER, FG, MU, PU, SE, t
from .widgets import AskScreen, ReviewScreen, SelectionScreen, TaskScreen

KEYS = t(("1-8", SE), (" cell   ", MU), ("enter", SE), (" task   ", MU), ("pgup/pgdn", SE), (" log   ", MU),
         ("ctrl+l", SE), (" leave   ", MU), ("ctrl+t", SE), (" theme   ", MU), ("ctrl+q", SE), (" end", MU))
LOG_MIN = 5         # rows the log needs to be worth showing: its frame and three lines
DETACH = "\x1b_hm-detach\x1b\\"        # tells the relay behind this screen that the user has left it
BOXES = (AskScreen, SelectionScreen, ReviewScreen, ChoiceBox, PickScreen, DraftScreen, ChecksScreen, CellsBox)


def decision(session, request) -> ChoiceBox | None:
    """The box for a request that is a choice between a few things, or None when it is asked another way."""
    if request.kind == "rejected":         # the workflow decided: the person only confirms
        title = "a benchmark is dropped" if " is dropped." in request.text.split("\n")[0] else "a benchmark is rejected"
        return ChoiceBox(title, request.text, [("ok", "Confirm")])
    if request.kind == "hung":             # a trial was stopped: run it again, or drop its task
        return ChoiceBox(f"a trial is stuck   {request.target}", request.text, [("retry", "Retry it"), ("drop", "Drop the task")])
    return None


def finished_note(s: Session) -> str:
    """What the box for the next benchmark says of the ones this run took: curated, rejected (and why), or let go."""
    said = []
    for b in s.benches.values():
        if b.phase == "skipped":
            why = b.detail.removeprefix("dropped: ").rstrip(".")
            said.append(f"{b.id} is {'dropped' if b.detail.startswith('dropped') else 'left out of this run'}: {why}."
                        if why else f"{b.id} is left out of this run.")
        elif stages.bench_stage(s, b)[0] == stages.DONE:
            said.append(f"{b.id} is curated.")
        else:
            said.append(f"{b.id} is let go: it is listed again, and what was done stays.")
    return " ".join(said) + " Pick the next one, or ctrl+q to end."


def decided(session, request, said: str) -> None:
    """Carry out what was chosen in a decision box."""
    if request.answer is not None and not request.answer.done():
        request.answer.set_result(said)


class SessionApp(App[None]):
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [("ctrl+q", "quit_run", "Quit"), ("ctrl+l", "leave", "Leave"),
                ("ctrl+t", "theme", "Light or dark"), ("escape", "close", "Close"),
                ("pageup", "log(-1)", "Log back"), ("pagedown", "log(1)", "Log on"), ("home", "log_end(0)", "Log start"),
                ("end", "log_end(1)", "Log end")]
    CSS = SCROLLBARS + f"""
    Screen {{ background: {BG}; color: {FG}; }}
    #top {{ height: 1fr; }}
    #bottom {{ height: auto; dock: bottom; }}
    .panel {{ border: round {BORDER}; border-title-color: {MU}; padding: 0 1; height: auto; }}
    #answer {{ border: round {PU}; border-title-color: {PU}; border-subtitle-color: {PU}; display: none; max-height: 12; }}
    #keys {{ height: 1; padding: 0 2; }}
    #recent {{ height: 1fr; min-height: {LOG_MIN}; background: {BG}; }}
    """

    def __init__(self, session: Session, exit_when_done: bool = False):
        super().__init__()
        self.session = session
        self.exit_when_done = exit_when_done
        self._run: asyncio.Task | None = None
        self._told_finished = False
        self.shown = 1              # the work cell drawn under the benchmark

    def compose(self) -> ComposeResult:
        with Vertical(id="top"):
            yield Static(id="header", classes="panel")
            yield Static(id="summary", classes="panel")
            yield Static(id="benchmarks", classes="panel")
            yield Static(id="cells", classes="panel")
            # The log: every event of the run, oldest first, in the room the panels above leave. It scrolls; it follows
            # the newest line unless it has been scrolled back.
            log = RichLog(id="recent", classes="panel", wrap=True, max_lines=2000, auto_scroll=False)
            log.can_focus = False
            yield log
        with Vertical(id="bottom"):
            yield Static(id="answer", classes="panel")
            yield Static(KEYS, id="keys")

    def on_mount(self) -> None:
        self.query_one("#summary").border_title = "summary"
        self.query_one("#recent").border_title = "log"
        self._logged: tuple | None = None       # the newest event written to the log
        self._light = theme.light_filter()
        self._filters.append(self._light)
        self.session.on_screen = True
        self._run = asyncio.create_task(self.session.run())
        self.set_interval(0.1, self.refresh_view)
        self.refresh_view()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGHUP):         # told to end: close the run's books, then go
            try:
                loop.add_signal_handler(sig, self._end)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        try:                                                # the relay says the last terminal closed without leaving
            loop.add_signal_handler(signal.SIGUSR1, lambda: self._nothing_to_keep() and self._end())
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
        if isinstance(self.screen, CellsBox) and self.screen.request not in s.requests:
            self.screen.dismiss("")             # answered some other way: the box goes
        elif isinstance(self.screen, PickScreen) and s.benches and not s.finished:
            self.screen.dismiss("")             # a benchmark joined the run as the box opened: it is not needed
        elif isinstance(self.screen, BOXES):
            pass        # one box at a time: the next opens when this one is answered
        elif not s.benches and not s.quitting:
            self.push_screen(PickScreen(s), self.picked)         # the first thing: which benchmark
        elif asked is not None and asked.kind == "cells":
            self.push_screen(CellsBox(asked, s.active_cells, s.max_containers), lambda said, asked=asked: decided(s, asked, said or ""))
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
        names = ", ".join(s.benches) or "none yet"
        self.query_one("#benchmarks").border_title = f"benchmark   {names}"
        self.query_one("#benchmarks", Static).update(stages.benchmarks(s, width - 4, now))
        cells = self.query_one("#cells", Static)
        curating = any(b.phase == "ready" and b.selected for b in s.benches.values())
        cells.display = curating                                  # the work cells appear with the curating stage
        if curating:
            self.shown = min(self.shown, s.active_cells)
            cells.border_title = stages.cells_title(s, self.shown)
            cells.update(stages.cell_view(s, self.shown, width - 4, now))
        self._log()
        self._fit()
        if s.leaving:
            s.leaving = False
            self.action_leave()
        if not s.finished:
            self._told_finished = False     # a benchmark was added to a finished run: it is a run again
        if s.finished and not self._told_finished:
            if s.quitting or self.exit_when_done:
                self._told_finished = True
                self.exit()
                return
            # Everything is done: the box for the next benchmark opens, once no other box is open (the last review's
            # box may still be), so that it is never missed.
            if not isinstance(self.screen, BOXES):
                self._told_finished = True
                self.push_screen(PickScreen(s, finished_note(s)), self.picked)

    def _log(self) -> None:
        """Write the events the log does not have yet, oldest first. It follows them only while it is at its end."""
        s, log = self.session, self.query_one("#recent", RichLog)
        new = []
        for entry in s.recent:              # newest first
            if entry is self._logged:
                break
            new.append(entry)
        if not new:
            return
        at_end = log.scroll_y >= log.max_scroll_y        # the log follows the newest line only from its end
        for entry in reversed(new):
            log.write(render.log_row(entry), scroll_end=at_end)     # scrolled once the line is laid out, not before
        self._logged = new[0]
        self._count = getattr(self, "_count", 0) + len(new)
        log.border_subtitle = f"{self._count} event{'s' if self._count != 1 else ''}"

    def action_log(self, pages: int) -> None:
        log = self.query_one("#recent", RichLog)
        log.scroll_to(y=log.scroll_y + pages * max(1, log.scrollable_content_region.height - 1), animate=False)

    def action_log_end(self, end: int) -> None:
        log = self.query_one("#recent", RichLog)
        (log.scroll_end if end else log.scroll_home)(animate=False)

    def _fit(self) -> None:
        """Keep the stages on screen. When rows run short, the least needed panels go first: the log, the figures, the
        header, and last the benchmark's stages, once a work cell's stages are under them. The log takes whatever room
        is left, and is shown only when that is at least LOG_MIN rows."""
        s = self.session
        cells = self.query_one("#cells").display
        need = {"header": 4, "summary": 3, "benchmarks": 2 + 7 * max(1, len(s.benches)), "cells": 12 if cells else 0,
                "recent": LOG_MIN}
        room = self.size.height - self.query_one("#bottom").outer_size.height
        shown = {name for name, rows in need.items() if rows}
        for name in ("recent", "summary", "header", *(["benchmarks"] if cells else [])):
            if sum(need[n] for n in shown) <= room:
                break
            shown.discard(name)
        for name in need:
            if name != "cells":
                self.query_one(f"#{name}").display = name in shown

    def show_result(self, title: str, body: str) -> None:
        if not body:
            return
        box = self.query_one("#answer", Static)
        box.border_title, box.border_subtitle = title, "esc"
        box.update(render.rows([(t(("hm" if i == 0 else "", PU)), t(line)) for i, line in enumerate(body.splitlines())]))
        box.display = True
        if getattr(self, "_hide", None) is not None:
            self._hide.stop()
        self._hide = self.set_timer(8, self.action_close)        # it says its piece, then goes

    # ----- keys -----

    def picked(self, said) -> None:
        """What the first box chose: a listed benchmark, which joins the run, or a new one found from a link, whose draft
        opens next, filled in."""
        if isinstance(said, tuple) and said[0] == "draft":
            self.push_screen(DraftScreen(self.session, said[1], said[2]), self.drafted)
            return
        said = said or ""
        if said.startswith("pick:"):
            told = interact.execute(self.session, Command("add", [said.removeprefix("pick:")]))
            if "is added" not in told:          # only a refusal is worth a line
                self.show_result("curate", told)

    def drafted(self, said) -> None:
        """What the add box and the checks box came to: the checks to ask, the draft again, a benchmark added (it joins the
        run), or one rejected and not added (the first box again, saying so)."""
        if isinstance(said, tuple) and said[0] == "check":
            self.push_screen(ChecksScreen(self.session, said[1], said[2]), self.drafted)
            return
        if isinstance(said, tuple) and said[0] == "draft":
            self.push_screen(DraftScreen(self.session, said[1], said[2]), self.drafted)
            return
        said = said or ""
        if said.startswith("added:"):
            self.picked("pick:" + said.removeprefix("added:"))
        elif said.startswith("rejected:"):
            self.push_screen(PickScreen(self.session, f"{said.removeprefix('rejected:')} was not added: its checks reject it."), self.picked)
        else:
            self.push_screen(PickScreen(self.session), self.picked)

    def on_key(self, event: events.Key) -> None:
        if isinstance(self.screen, BOXES):
            return
        if event.key.isdigit() and 1 <= int(event.key) <= self.session.active_cells:
            self.shown = int(event.key)
            self._refresh_view()
        elif event.key == "enter":
            cell = next((c for c in self.session.cells if c.n == self.shown), None)
            if cell is not None and cell.task is not None:
                self.push_screen(TaskScreen(cell.task.task_id, interact.explain(self.session, cell.task.task_id)))

    def action_close(self) -> None:
        self.query_one("#answer").display = False

    def action_theme(self) -> None:
        theme.set_mode("")


    def _nothing_to_keep(self) -> bool:
        """A run that is not worth keeping with nobody watching: a simulated one, or one that has taken no benchmark yet.
        It would only wait at a box that nobody sees."""
        return self.session.simulate or not self.session.benches

    def action_leave(self) -> None:
        """Leave the screen. Behind a relay the run goes on, and hm curate inside the index's folder opens it again.
        A simulated run, or one that has taken no benchmark, ends instead: nothing of it is left running."""
        if self._nothing_to_keep():
            self._end_now()
            return
        if not os.environ.get("HM_HOSTED") or self._driver is None:
            self.show_result("cannot leave", "This run is tied to this terminal, so leaving would end it. ctrl+q ends it.")
            return
        self._driver.write(DETACH)
        self._driver.flush()

    def action_quit_run(self) -> None:
        if self.session.finished:
            self.exit()
        elif not (isinstance(self.screen, ChoiceBox) and self.screen._title == "end the run"):     # over any other box too
            self.push_screen(ChoiceBox("end the run", "End this run?\nWhat is running is stopped, and its claims are let go. What is "
                                       "recorded stays, and nothing is run twice when you start again.",
                                       [("no", "Keep it going"), ("yes", "End the run")], cancel=True),
                             lambda said: said == "yes" and self._end_now())

    def _end_now(self) -> None:
        """The box's "End the run": nothing more is taken, nothing waits for an answer, and what is running is stopped:
        each Harbor job is interrupted, so that it takes its containers down. The run then closes its books."""
        interact.execute(self.session, Command("quit"))
        self.session.kill()


def run_app(session: Session, exit_when_done: bool = False) -> None:
    SessionApp(session, exit_when_done).run()
