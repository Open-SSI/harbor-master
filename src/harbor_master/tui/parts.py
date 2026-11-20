"""What every screen shares: scrollbars in the screen's own colors, and the box a decision is made in."""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .theme import AM, BG, BORDER, FG, MU, PU, TE

# Scrollbars, across and down, on everything that scrolls: one cell thin, a quiet thumb on the screen's own ground.
# The toolkit's own are wide and bright blue. A line to type in gets the same quiet frame on every screen and in every box.
SCROLLBARS = f"""
    * {{ scrollbar-size: 1 1; scrollbar-color: {BORDER}; scrollbar-color-hover: {MU}; scrollbar-color-active: {PU};
         scrollbar-background: {BG}; scrollbar-background-hover: {BG}; scrollbar-background-active: {BG};
         scrollbar-corner-color: {BG}; }}
    Input, Input:focus {{ border: round {PU}; background: {BG}; background-tint: {BG} 0%; }}
"""


class ChoiceBox(ModalScreen[str]):
    """A decision, in a box: what it is about, and one button for each thing that can be done. It opens by
    itself and nothing else can be done on the screen until one is pressed. With `cancel`, esc closes it
    and gives "". Up and down walk the buttons, enter presses the one the keyboard is on."""

    DEFAULT_CSS = f"""
    ChoiceBox {{ align: center middle; background: {BG} 70%; }}
    ChoiceBox > Vertical {{ width: 84; max-width: 94%; height: auto; max-height: 90%; border: round {AM}; border-title-color: {AM};
                            padding: 1 2; background: {BG}; }}
    ChoiceBox VerticalScroll {{ height: auto; max-height: 16; margin: 0 0 1 0; }}
    ChoiceBox #text {{ height: auto; color: {FG}; }}
    ChoiceBox Button {{ width: 100%; height: 3; border: round {BORDER}; background: {BG}; color: {FG}; }}
    ChoiceBox Button:focus {{ border: round {TE}; color: {TE}; text-style: bold; background-tint: {BG} 0%; }}
    ChoiceBox #how {{ height: 1; color: {MU}; margin: 1 0 0 0; }}
    """

    def __init__(self, title: str, text: str, options: list[tuple[str, str]], cancel: bool = False):
        super().__init__()
        self._title, self._text, self._options, self._cancel = title, text, options, cancel

    def compose(self) -> ComposeResult:
        head, _, rest = self._text.partition("\n")
        box = Vertical(VerticalScroll(Static(Text.assemble((head, "bold"), "\n" + rest if rest else ""), id="text"), can_focus=False),
                       *(Button(label, id=f"pick-{n}", name=value) for n, (value, label) in enumerate(self._options)),
                       Static("enter chooses" + ("     esc closes" if self._cancel else ""), id="how"))
        box.border_title = self._title
        yield box

    def on_mount(self) -> None:
        self.query_one("#pick-0", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.name)

    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "down"):
            event.stop()
            (self.focus_previous if event.key == "up" else self.focus_next)()
        elif event.key == "escape":
            event.stop()
            if self._cancel:
                self.dismiss("")


__all__ = ["BORDER", "SCROLLBARS", "ChoiceBox"]
