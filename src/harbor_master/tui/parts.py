"""What every screen shares: scrollbars in the screen's own colors, and the box a decision is made in."""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from .theme import AM, BG, BORDER, FG, MU, PU, TE

# Scrollbars, across and down, on everything that scrolls: one cell thin, a quiet thumb on the screen's own ground.
# The toolkit's own are wide and bright blue. A line to type in gets the same quiet frame on every screen and in every box,
# and a button is a frame and its words: green when it has the keyboard or the pointer, never a filled block.
SCROLLBARS = f"""
    * {{ scrollbar-size: 1 1; scrollbar-color: {BORDER}; scrollbar-color-hover: {MU}; scrollbar-color-active: {PU};
         scrollbar-background: {BG}; scrollbar-background-hover: {BG}; scrollbar-background-active: {BG};
         scrollbar-corner-color: {BG}; }}
    Input, Input:focus {{ border: round {PU}; background: {BG}; background-tint: {BG} 0%; }}
    Button, Button.-style-default, Button:hover, Button.-style-default:hover, Button:focus, Button.-style-default:focus,
    Button.-active, Button.-style-default.-active {{ background: {BG}; background-tint: {BG} 0%; tint: {BG} 0%; text-style: bold; }}
    Button, Button.-style-default {{ border: round {BORDER}; color: {FG}; }}
    Button:hover, Button.-style-default:hover, Button:focus, Button.-style-default:focus,
    Button.-active, Button.-style-default.-active {{ border: round {TE}; color: {TE}; }}
    Button:disabled, Button.-style-default:disabled {{ border: round {BORDER}; color: {MU}; text-style: none; }}
"""


QUIT_KEYS = "ctrl+l leave   ctrl+q quit"       # on the bottom border of every box: the run can always be left or ended


class ChoiceBox(ModalScreen[str]):
    """A decision, in a box: what it is about, and one button for each thing that can be done. It opens by
    itself and nothing else can be done on the screen until one is pressed. With `cancel`, esc closes it
    and gives "". Up and down walk the buttons, enter presses the one the keyboard is on.

    With `line` (an option's value, and what to type), the box also has a line to type in: that option needs it,
    and gives "VALUE:what was typed"."""

    DEFAULT_CSS = f"""
    ChoiceBox {{ align: center middle; background: {BG} 70%; }}
    ChoiceBox > Vertical {{ width: 84; max-width: 94%; height: auto; max-height: 90%; border: round {AM}; border-title-color: {AM};
                            padding: 1 2; background: {BG}; }}
    ChoiceBox VerticalScroll {{ height: auto; max-height: 16; margin: 0 0 1 0; }}
    ChoiceBox #text {{ height: auto; color: {FG}; }}
    ChoiceBox Button, ChoiceBox Button.-style-default {{ width: 100%; height: 3; border: round {BORDER}; background: {BG}; color: {FG}; }}
    ChoiceBox Button:focus {{ border: round {TE}; color: {TE}; text-style: bold; background-tint: {BG} 0%; }}
    ChoiceBox Input {{ margin: 0 0 1 0; }}
    ChoiceBox #said {{ height: auto; color: {AM}; }}
    """

    def __init__(self, title: str, text: str, options: list[tuple[str, str]], cancel: bool = False, line: tuple[str, str] | None = None):
        super().__init__()
        self._title, self._text, self._options, self._cancel, self._line = title, text, options, cancel, line

    def compose(self) -> ComposeResult:
        head, _, rest = self._text.partition("\n")
        typed = [Input(placeholder=self._line[1], id="line"), Static(id="said")] if self._line else []
        box = Vertical(VerticalScroll(Static(Text.assemble((head, "bold"), "\n" + rest if rest else ""), id="text"), can_focus=False),
                       *typed, *(Button(label, id=f"pick-{n}", name=value) for n, (value, label) in enumerate(self._options)))
        box.border_title = self._title
        if not self._cancel:                # the box that asks whether to end the run has esc to go back instead
            box.border_subtitle = QUIT_KEYS
        yield box

    def on_mount(self) -> None:
        # With a line to type in, the keyboard starts on it: typing goes there, and enter sends it with its option.
        # It never starts on a button that drops something.
        (self.query_one("#line", Input) if self._line else self.query_one("#pick-0", Button)).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self._line and event.button.name == self._line[0]:
            typed = self.query_one("#line", Input).value.strip()
            if not typed:
                self.query_one("#said", Static).update(f"Type {self._line[1]} first.")
                self.query_one("#line", Input).focus()
                return
            self.dismiss(f"{event.button.name}:{typed}")
            return
        self.dismiss(event.button.name)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()                    # enter on the line presses its option
        self.query_one(f"#pick-{[v for v, _ in self._options].index(self._line[0])}", Button).press()

    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "down"):
            event.stop()
            (self.focus_previous if event.key == "up" else self.focus_next)()
        elif event.key == "escape":
            event.stop()
            if self._cancel:
                self.dismiss("")


__all__ = ["BORDER", "QUIT_KEYS", "SCROLLBARS", "ChoiceBox"]
