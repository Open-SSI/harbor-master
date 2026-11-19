"""The prompt and the task screen."""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Input, Static

from .. import interact
from ..interact import Command
from .theme import BG, PU

if TYPE_CHECKING:
    from .app import SessionApp


class Completer(Suggester):
    """The rest of a "/" command, shown dimmed after the cursor."""

    def __init__(self, session):
        super().__init__(use_cache=False, case_sensitive=True)
        self.session = session

    async def get_suggestion(self, value: str) -> str | None:
        return next((c.rstrip() for c in interact.complete(self.session, value) if c.startswith(value)), None)


class Prompt(Input):
    """The prompt. With nothing typed, a few single keys act at once. Tab completes a "/" command."""

    options: list[str] = []     # what tab is going through
    chosen = -1

    def choices(self) -> tuple[list[str], int]:
        """The completions of what is typed, and which one is in the prompt (-1 for none yet)."""
        if len(self.options) > 1 and 0 <= self.chosen < len(self.options) and self.options[self.chosen] == self.value:
            return self.options, self.chosen        # tab is going through several: stay on that list
        return interact.complete(self.app.session, self.value), -1  # type: ignore[attr-defined]

    def complete(self, step: int = 1) -> None:
        """Take the next completion. Tab again moves on to the one after."""
        options, at = self.choices()
        if not options:
            return
        self.options, self.chosen = options, (at + step) % len(options)
        self.value = options[self.chosen]
        self.cursor_position = len(self.value)

    async def _on_key(self, event: events.Key) -> None:
        app: SessionApp = self.app  # type: ignore[assignment]
        if event.key in ("tab", "shift+tab") and self.value.startswith("/"):
            event.stop()
            event.prevent_default()
            self.complete(1 if event.key == "tab" else -1)
            return
        if self.value == "":
            if app.pending is not None and event.key in ("y", "n", "e"):
                event.stop()
                event.prevent_default()
                app.answer_confirmation(event.key)
                return
            if event.key.isdigit() and event.key != "0":
                event.stop()
                event.prevent_default()
                app.open_cell(int(event.key))
                return
            if event.character == "?":
                event.stop()
                event.prevent_default()
                app.show_result("help", interact.execute(app.session, Command("help")))
                return
        if event.key == "escape":
            event.stop()
            app.close_panels()
            return
        await super()._on_key(event)


class TaskScreen(ModalScreen[None]):
    """One task: where it is and why."""

    BINDINGS = [("escape", "dismiss", "Back")]
    DEFAULT_CSS = f"""
    TaskScreen {{ align: center middle; background: {BG} 70%; }}
    TaskScreen > VerticalScroll {{ width: 90%; height: 80%; border: round {PU}; padding: 1 2; background: {BG}; }}
    """

    def __init__(self, title: str, body: str):
        super().__init__()
        self._title, self._body = title, body

    def compose(self) -> ComposeResult:
        box = VerticalScroll(Static(self._body))
        box.border_title = self._title
        box.border_subtitle = "esc to go back"
        yield box
