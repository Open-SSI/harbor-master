"""The session screen (DESIGN.md Section 11), built on Textual.

The run continues underneath whatever the user does. The prompt takes "/" commands, plain
text that maps to commands, and questions.
"""
from __future__ import annotations

import asyncio
import time

from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Input, Static

from .. import interact
from ..engine import Session
from ..interact import Command, Intent, guide
from . import render
from .theme import AM, BG, BORDER, FG, MU, PU, RD, SE, TE, t
from .widgets import Completer, Prompt, TaskScreen

KEYS = t(("1-9", SE), (" open cell   ", MU), ("ctrl+p", SE), (" pause   ", MU), ("esc", SE), (" close panel   ", MU),
         ("/", SE), (" commands   ", MU), ("?", SE), (" help   ", MU), ("ctrl+q", SE), (" quit", MU))


class SessionApp(App[None]):
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [("ctrl+p", "toggle_pause", "Pause"), ("ctrl+q", "quit_run", "Quit")]
    CSS = f"""
    Screen {{ background: {BG}; color: {FG}; }}
    #top {{ height: 1fr; scrollbar-size: 0 0; }}
    #bottom {{ height: auto; dock: bottom; }}
    .panel {{ border: round {BORDER}; border-title-color: {MU}; padding: 0 1; height: auto; }}
    #answer {{ border: round {PU}; border-title-color: {PU}; border-subtitle-color: {PU}; display: none; max-height: 12; }}
    #confirm {{ border: round {AM}; border-title-color: {AM}; border-subtitle-color: {AM}; display: none; }}
    Prompt {{ border: round {PU}; background: {BG}; height: 3; padding: 0 1; }}
    Prompt:focus {{ border: round {PU}; background-tint: {BG} 0%; }}
    Prompt > .input--suggestion {{ color: {MU}; }}
    #tip {{ height: auto; max-height: 2; padding: 0 2; }}
    #keys {{ height: 1; padding: 0 2; }}
    """

    def __init__(self, session: Session, exit_when_done: bool = False):
        super().__init__()
        self.session = session
        self.exit_when_done = exit_when_done
        self.pending: Intent | None = None
        self.pending_text = ""
        self._run: asyncio.Task | None = None
        self._told_finished = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="top"):
            yield Static(id="header", classes="panel")
            yield Static(id="summary", classes="panel")
            yield Static(id="benchmarks", classes="panel")
            yield Static(id="cells", classes="panel")
            yield Static(id="recent", classes="panel")
        with Vertical(id="bottom"):
            yield Static(id="answer", classes="panel")
            yield Static(id="confirm", classes="panel")
            yield Static(id="tip")
            yield Prompt(placeholder="Type a command, an instruction, or a question", id="prompt",
                         suggester=Completer(self.session))
            yield Static(KEYS, id="keys")

    def on_mount(self) -> None:
        for name in ("summary", "benchmarks", "recent", "answer", "confirm"):
            self.query_one(f"#{name}").border_title = name
        self.query_one("#cells").border_title = "working cells"
        self.query_one("#prompt").focus()
        self._run = asyncio.create_task(self.session.run())
        self.set_interval(0.1, self.refresh_view)
        self.refresh_view()

    # ----- drawing -----

    def refresh_view(self) -> None:
        try:
            self._refresh_view()
        except NoMatches:       # a task screen is open, or the app is closing
            pass

    def _refresh_view(self) -> None:
        s, now, width = self.session, time.time(), self.size.width
        self.query_one("#header", Static).update(render.header(s, width))
        self.query_one("#summary", Static).update(render.summary(s, now, width))
        self.query_one("#benchmarks", Static).update(render.benchmarks(s, now))
        self.query_one("#cells", Static).update(render.cells(s, now))
        self.query_one("#recent", Static).update(render.recent(s))
        self.query_one("#tip", Static).update(t(("tip  ", f"bold {TE}"), (guide.session_tip(s), SE)))
        self._fit()
        self._draw_requests()
        if s.finished and not self._told_finished:
            self._told_finished = True
            if s.quitting or self.exit_when_done:
                self.exit()
                return
            blocked = sum(1 for r in s.requests if r.blocking)
            waiting = f" {blocked} request{'s' if blocked != 1 else ''} blocked part of it: see /requests." if blocked else ""
            body = interact.execute(s, Command("status")) + "\nThe run is finished." + waiting
            if s.ready:
                body += f"\n{len(s.ready)} reports are waiting for your verdict. Start with /review."
            self.show_result("finished", body + "\nPress ctrl+q to leave.")

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

    def _draw_requests(self) -> None:
        """Show what the system is waiting on, unless a confirmation is already on screen."""
        s, box = self.session, self.query_one("#confirm", Static)
        n = len(s.requests)
        box.border_subtitle = f"{n} request{'s' if n != 1 else ''} waiting" if n else ""
        if self.pending is not None:
            return
        if not n:
            box.display = False
            return
        r = min(s.requests, key=lambda q: (not q.blocking, q.kind == "verdict"))      # what blocks work comes first
        how = interact.how_to_answer(r)
        box.border_title = "request"
        box.update(render.rows([(t(("?", AM)), t(r.text)), (t(""), t((how, SE)))]))
        box.display = True

    def show_result(self, title: str, body: str, question: str = "") -> None:
        box = self.query_one("#answer", Static)
        rows = [(t(("you", MU)), t(question))] if question else []
        for i, line in enumerate(body.splitlines() or [""]):
            rows.append((t(("hm" if i == 0 else "", PU)), t(line)))
        box.border_title = title
        box.border_subtitle = "esc to close"
        box.update(render.rows(rows))
        box.display = True

    def close_panels(self) -> None:
        self.query_one("#answer").display = False
        if self.pending is not None:
            self.answer_confirmation("n")

    def open_cell(self, n: int) -> None:
        cell = next((c for c in self.session.cells if c.n == n), None)
        if cell is None or cell.task is None:
            self.show_result("cell", f"Cell {n} has no task right now.")
            return
        self.push_screen(TaskScreen(cell.task.task_id, interact.explain(self.session, cell.task.task_id)))

    # ----- input -----

    def on_input_changed(self, event: Input.Changed) -> None:
        """While a "/" command is typed, the bottom line shows what tab would complete it to."""
        prompt = self.query_one(Prompt)
        options, at = prompt.choices()
        if not event.value.startswith("/") or not (options or interact.usage(event.value)):
            self.query_one("#keys", Static).update(KEYS)
            return
        words = [o.rstrip().rsplit(" ", 1)[-1] for o in options]
        room, parts = self.size.width - 34 - len(interact.usage(event.value)), []
        for i, word in enumerate(words):
            if room < len(word) + 3:
                parts.append((f"+{len(words) - i} more   ", MU))
                break
            parts.append((word + "   ", f"bold {PU}" if i == max(at, 0) else SE))
            room -= len(word) + 3
        if words:
            parts.append(("tab", SE))
            parts.append((" completes   " if len(words) == 1 else " next   ", MU))
        self.query_one("#keys", Static).update(t(*parts, (interact.usage(event.value), MU)))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        s = self.session
        if self.pending is not None and text.lower() in ("y", "yes", "n", "no", "e", "edit"):
            self.answer_confirmation(text[0].lower())
            return
        intent = await interact.interpret_async(s, text)
        s.log(f"input {text!r} -> {intent.kind} {' '.join(map(str, intent.commands))}")
        if intent.kind == "unsupported":
            self.show_result("cannot do that", f"{intent.reason}\nInstead: {intent.offer}. Nothing was changed.", text)
        elif intent.kind == "unclear":
            options = " or ".join(str(c) for c in intent.commands)
            self.show_result("not sure", f"I could not read that as a command or a question. Did you mean {options}?\n"
                             "Nothing was changed. Type a / command, or say it another way.", text)
        elif intent.kind == "question":
            self.show_result("answer", "…", text)
            reply = await s.backend.answer(text, interact.context_for(s, text, focused=s.simulate))
            s.log(f"answer {reply!r}")
            self.show_result("answer", reply, text)
            source = "simulated run: from the records, no model" if s.simulate else "small model, read-only"
            self.query_one("#answer").border_subtitle = f"{source} · esc to close"
        elif interact.needs_confirmation(intent):
            self.ask_confirmation(intent, text)
        else:
            self.run_commands(intent, text if not intent.typed else "")

    def ask_confirmation(self, intent: Intent, text: str) -> None:
        self.pending, self.pending_text = intent, text
        box = self.query_one("#confirm", Static)
        rows = [] if intent.typed else [(t(("you", MU)), t(text))]
        for c in intent.commands:
            rows.append((t(("→", AM)), t((str(c), "bold"), ("    " + interact.describe(c), SE))))
        many = len(intent.commands) > 1
        rows.append((t(""), t(f"Run {'all' if many else 'it'}?  ", ("[y]", f"bold {TE}"), " yes   ",
                              ("[n]", f"bold {RD}"), " no   ", ("[e]", f"bold {PU}"), " edit")))
        box.border_title = "confirm"
        box.update(render.rows(rows))
        box.display = True

    def answer_confirmation(self, key: str) -> None:
        intent, text = self.pending, self.pending_text
        if intent is None:
            return
        self.pending = None
        self.query_one("#confirm").display = False
        if key == "y":
            self.run_commands(intent, text if not intent.typed else "")
        elif key == "e":
            prompt = self.query_one("#prompt", Prompt)
            prompt.value = "; ".join(str(c) for c in intent.commands) if len(intent.commands) > 1 else str(intent.commands[0])
            prompt.cursor_position = len(prompt.value)
            self.session.log("confirmation edited")
        else:
            self.session.log(f"declined {' '.join(map(str, intent.commands))}")
            self.show_result("declined", "Nothing was changed.")

    def run_commands(self, intent: Intent, question: str = "") -> None:
        lines = []
        for c in intent.commands:
            result = interact.execute(self.session, c)
            if result.count("\n") >= 7:      # too long for the answer panel: open it full size
                self.push_screen(TaskScreen(str(c), result))
                continue
            lines.append(result if intent.typed and len(intent.commands) == 1 else f"{c}\n{result}")
        if lines:
            view = all(interact.COMMANDS[c.name].group == "view" for c in intent.commands)
            self.show_result("view" if view else "done", "\n".join(lines), question)

    # ----- bindings -----

    def action_toggle_pause(self) -> None:
        name = "resume" if self.session.paused else "pause"
        self.show_result("done", interact.execute(self.session, Command(name)))

    def action_quit_run(self) -> None:
        if self.session.finished:
            self.exit()
        else:
            self.ask_confirmation(Intent("commands", [Command("quit")], typed=True), "")


def run_app(session: Session, exit_when_done: bool = False) -> None:
    SessionApp(session, exit_when_done).run()
