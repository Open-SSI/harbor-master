"""The commands a user can give during a session, kept in one registry.

To add a command, write a function in `handlers.py` and decorate it with `@command`. Its
group decides how it is treated (DESIGN.md Section 11):

- "view": only shows something, runs at once.
- "control": changes the run, confirmed when it was read from plain text.
- "confirm": spends, discards, or decides, always confirmed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..engine import Session

Handler = Callable[["Session", list[str]], str]


@dataclass
class Command:
    name: str
    args: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return "/" + " ".join([self.name, *self.args])


@dataclass
class Intent:
    """What a typed line was read as."""
    kind: str                      # commands | question | unsupported | unclear
    commands: list[Command] = field(default_factory=list)
    prob: float = 1.0
    reason: str = ""               # for unsupported: why not
    offer: str = ""                # for unsupported: the nearest thing that can be done
    typed: bool = False            # the user typed "/" themself


@dataclass
class Spec:
    name: str
    group: str                     # view | control | confirm
    usage: str
    effect: str = ""               # the consequence shown in a confirmation; "{0}" is the first argument
    run: Handler | None = None     # None: planned, not built yet


COMMANDS: dict[str, Spec] = {}


def command(name: str, group: str, usage: str, effect: str = "") -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        COMMANDS[name] = Spec(name, group, usage, effect, fn)
        return fn
    return register


def planned(name: str, group: str, usage: str) -> None:
    """A command the design names that is not built yet. It answers so, and changes nothing."""
    COMMANDS[name] = Spec(name, group, usage)


def parse(text: str) -> Command:
    parts = text.strip().lstrip("/").split()
    return Command(parts[0].lower() if parts else "help", parts[1:])


def needs_confirmation(intent: Intent) -> bool:
    groups = {COMMANDS[c.name].group for c in intent.commands}
    if "confirm" in groups:
        return True
    return not intent.typed and "control" in groups


def describe(cmd: Command) -> str:
    spec = COMMANDS[cmd.name]
    return spec.effect.format(cmd.args[0] if cmd.args else "it")


def execute(session: "Session", cmd: Command) -> str:
    """Run one command. Returns the line to show. A bad argument is answered, never raised."""
    session.log(f"command {cmd}")
    spec = COMMANDS.get(cmd.name)
    if spec is None:
        return f"There is no /{cmd.name} command. See /help."
    if spec.run is None:
        return f"/{cmd.name} is not built yet."
    return spec.run(session, cmd.args)


def help_text() -> str:
    rows = []
    for group, label in (("view", "View"), ("control", "Control"), ("confirm", "Confirmed")):
        rows.append(f"{label}: " + "  ".join(s.usage for s in COMMANDS.values() if s.group == group and s.run))
    return "\n".join(rows)
