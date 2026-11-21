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


QUIT_KEYS = "ctrl+l leave   ctrl+z quit"       # on the bottom border of every box: the run can always be left or ended


class Fits:
    """For a box whose middle part scrolls: on opening, on every resize and as its contents change, the part FLEX (or
    SCROLL inside it) is capped to the room the box's other parts leave, so that its line to type in and its buttons
    are always on the screen, however short the terminal. The box is the screen's first Vertical, at most SHARE of the
    screen tall; CAP, when set, is the most the scrolling part grows to on a tall screen."""
    FLEX, SCROLL, SHARE, CAP = "VerticalScroll", "", 0.92, 0

    def on_mount(self) -> None:
        self.call_after_refresh(self.fit)
        self.set_interval(0.5, self.fit)            # what is shown changes: a correction, a check, a message

    def on_resize(self, event) -> None:
        self.call_after_refresh(self.fit)

    def fit(self) -> None:
        from textual.css.query import NoMatches
        try:
            box = self.query_one(Vertical)
            flex = self.query_one(self.FLEX)
            scroll = self.query_one(self.SCROLL) if self.SCROLL else flex
        except NoMatches:
            return
        outer = lambda w: w.outer_size.height + w.styles.margin.top + w.styles.margin.bottom  # noqa: E731
        others = sum(outer(c) for c in box.children if c is not flex and c.display)
        chrome = box.outer_size.height - box.content_size.height                   # its border and padding
        inside = flex.outer_size.height - scroll.outer_size.height if scroll is not flex else 0     # a frame around it
        room = int(self.size.height * self.SHARE) - box.styles.margin.bottom - chrome - others - inside \
            - flex.styles.margin.top - flex.styles.margin.bottom
        room = max(1, min(room, self.CAP) if self.CAP else room)
        if scroll.styles.max_height is None or scroll.styles.max_height.value != room:
            scroll.styles.max_height = room
        if box.max_scroll_y and self.focused is not None and self.focused is not getattr(self, "_shown", None):
            self._shown = self.focused              # a terminal too short even so: the box scrolls to what has the keyboard
            self.focused.scroll_visible(animate=False)


class ChoiceBox(Fits, ModalScreen[str]):
    """A decision, in a box: what it is about, and one button for each thing that can be done. It opens by
    itself and nothing else can be done on the screen until one is pressed. With `cancel`, esc closes it
    and gives "". Up and down walk the buttons, enter presses the one the keyboard is on.

    With `line` (an option's value, and what to type), the box also has a line to type in: that option needs it,
    and gives "VALUE:what was typed"."""

    DEFAULT_CSS = f"""
    ChoiceBox {{ align: center middle; background: {BG} 70%; }}
    ChoiceBox > Vertical {{ width: 84; max-width: 94%; height: auto; max-height: 90%; border: round {AM}; border-title-color: {AM};
                            padding: 1 2; background: {BG}; overflow-y: auto; scrollbar-size: 0 0; }}
    ChoiceBox VerticalScroll {{ height: auto; margin: 0 0 1 0; }}
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

    CAP, SHARE = 16, 0.88           # its box is at most 90% of the screen

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


__all__ = ["BORDER", "QUIT_KEYS", "SCROLLBARS", "ChoiceBox", "Fits"]
