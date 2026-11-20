"""A walk-through in boxes: one question at a time, each in a box of its own (DESIGN.md Section 11).

`hm init` asks its questions this way, and nothing else does. A box has a line to type the answer in, or only
something to read. Every box says which step it is, and ends in two buttons at its right: Cancel, and Next,
which enter presses. esc goes back a step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Static

from . import theme
from .parts import SCROLLBARS
from .theme import AM, BG, BORDER, FG, MU, PU, SE, TE

Answers = dict[str, object]


@dataclass
class Step:
    key: str
    title: str
    text: str = ""
    kind: str = "text"                                  # text: a typed line | choice: one button each | note: only read
    options: list[tuple[str, str]] = field(default_factory=list)       # (value, label), for a choice
    default: str = ""                                   # what the line holds at first
    optional: bool = False                              # a typed answer that may be left empty
    hint: str = ""                                      # shown dimmed in the empty line
    check: Callable[[str, Answers], str] | None = None  # why a typed answer will not do, or "" when it will
    done: str = ""                                      # the label of the button that goes on, when it is not Next
    prefix: str = ""                                    # shown before a typed line, and not typed: https://github.com/


class WizardApp(App[Answers | None]):
    """Asks the steps in order and returns the answers, or None when it is cancelled."""
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [("escape", "back", "Back"), ("ctrl+q", "cancel", "Cancel"), ("ctrl+t", "theme", "Light or dark")]
    CSS = SCROLLBARS + f"""
    Screen {{ background: {BG}; color: {FG}; align: center middle; }}
    #box {{ width: 84; max-width: 96%; height: auto; max-height: 96%; border: round {PU}; border-title-color: {PU};
            padding: 1 2; background: {BG}; }}
    #title {{ text-style: bold; height: auto; }}
    #text {{ color: {SE}; height: auto; margin: 1 0; }}
    #said {{ color: {AM}; height: auto; }}
    #answer {{ height: auto; }}
    #line-row {{ height: 3; }}
    #prefix {{ width: auto; height: 3; padding: 1 0 0 1; color: {MU}; }}
    Input {{ border: round {PU}; background: {BG}; height: 3; width: 1fr; }}
    Input:focus {{ border: round {PU}; background-tint: {BG} 0%; }}
    #answer Button {{ width: 100%; height: 3; margin: 0; border: round {BORDER}; background: {BG}; color: {FG}; }}
    #answer Button:focus {{ border: round {TE}; color: {TE}; text-style: bold; background-tint: {BG} 0%; }}
    #row {{ height: auto; margin: 1 0 0 0; align-horizontal: right; }}
    #row Button {{ width: auto; min-width: 12; height: 3; margin: 0 0 0 1; border: round {BORDER}; background: {BG}; color: {FG}; }}
    #row #go {{ border: round {TE}; color: {TE}; text-style: bold; }}
    #row Button:focus {{ background-tint: {BG} 0%; text-style: bold underline; }}
    """

    def __init__(self, name: str, steps: list[Step], answers: Answers | None = None):
        super().__init__()
        self.name_, self.steps = name, steps
        self.answers: Answers = dict(answers or {})
        self.at = 0
        self.marked: object = None          # in a choice, the option the keyboard is on

    @property
    def step(self) -> Step:
        return self.steps[self.at]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="box", can_focus=False):      # a box taller than the terminal scrolls, and never hides its buttons
            yield Static(id="title")
            yield Static(id="text")
            yield Vertical(id="answer")
            yield Static(id="said")
            yield Horizontal(id="row")

    def on_mount(self) -> None:
        self._light = theme.light_filter()
        self._filters.append(self._light)
        self.call_after_refresh(self.show)

    def action_theme(self) -> None:
        theme.set_mode("")
        theme.follow(self, self._light)

    async def show(self) -> None:
        step = self.step
        self.query_one("#box").border_title = f"{self.name_}   step {self.at + 1} of {len(self.steps)}"
        self.query_one("#title", Static).update(step.title)
        self.query_one("#text", Static).update(Text(step.text))
        self.query_one("#text").display = bool(step.text)
        self.query_one("#said", Static).update("")
        answer, row = self.query_one("#answer"), self.query_one("#row")
        await answer.remove_children()
        await row.remove_children()
        await row.mount(Button("Cancel", id="cancel"), Button(f"{step.done or 'Next'} ↵", id="go"))
        if step.kind == "text":
            line = Input(value=str(self.answers.get(step.key, step.default) or "").removeprefix(step.prefix), placeholder=step.hint, id="line")
            await answer.mount(Horizontal(Static(step.prefix, id="prefix"), line, id="line-row") if step.prefix else line)
            line.focus()
        elif step.kind == "choice":
            for n, (value, label) in enumerate(step.options):
                await answer.mount(Button(label, id=f"pick-{n}", name=value))
            held = self.answers.get(step.key, step.default)
            at = next((n for n, (value, _) in enumerate(step.options) if value == held), 0)
            self.marked = step.options[at][0]
            self.query_one(f"#pick-{at}", Button).focus()
        else:
            self.query_one("#go", Button).focus()

    def go(self) -> None:
        """What Next does, and enter with it: check the answer this box holds, take it, and show the next box."""
        step, typed = self.step, True
        if step.kind == "choice":       # Next takes the choice the keyboard is on
            typed = self.marked
        if step.kind == "text":
            typed = " ".join(self.query_one("#line", Input).value.split())
            problem = "This one is needed." if not typed and not step.optional else step.check(typed, self.answers) if step.check and typed else ""
            if problem:
                self.query_one("#said", Static).update(problem)
                return
            typed = step.prefix + typed if typed else typed
        self.answers[step.key] = typed
        self.at += 1
        if self.at >= len(self.steps):
            self.exit(self.answers)
        else:
            self.call_after_refresh(self.show)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.go()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.exit(None)
        elif (event.button.id or "").startswith("pick-"):
            self.marked = event.button.name
            self.go()
        else:
            self.go()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if isinstance(event.widget, Button) and (event.widget.id or "").startswith("pick-"):
            self.marked = event.widget.name         # the choice Next would take

    def on_key(self, event: events.Key) -> None:
        """Up and down walk the buttons of a choice, as a list is walked."""
        if event.key in ("up", "down") and isinstance(self.focused, Button):
            event.stop()
            (self.action_focus_previous if event.key == "up" else self.action_focus_next)()

    def action_back(self) -> None:
        if self.at:
            self.at -= 1
            self.call_after_refresh(self.show)

    def action_cancel(self) -> None:
        self.exit(None)


def ask(name: str, steps: list[Step], answers: Answers | None = None) -> Answers | None:
    """Walk a person through the steps in a terminal. None when they cancelled."""
    return WizardApp(name, steps, answers).run()
