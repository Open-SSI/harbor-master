"""What a run can be told to do, kept in one registry. No screen has a prompt: the boxes and keys of the
curate screen run these, and so does the relay.

To add one, write a function in `handlers.py` and decorate it with `@command`.
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
class Spec:
    name: str
    group: str                     # view | control | answer | confirm
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


# What the run screen itself does: only what steers or shows the run that is going. What the run asks of a person
# (a brief, a selection, a verdict, an adapter) is asked in a box. The other commands stay in the registry, because
# those boxes and the relay run them.
RUN_SCREEN = ("help", "status", "explain", "review", "pause", "resume", "leave", "quit")


def shown(name: str) -> bool:
    """Whether a command is one of the run screen's own."""
    return name in RUN_SCREEN and name in COMMANDS and COMMANDS[name].run is not None


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
    about = {"help": "this list", "status": "where every benchmark and task stands, and what was spent",
             "explain": "why a task is where it is (or press its cell's number)", "review": "read the reports that wait, and accept or reject each",
             "pause": "hold the run, or one benchmark", "resume": "go on",
             "leave": "leave the screen: the run goes on", "quit": "end the run"}
    rows = [f"  {COMMANDS[n].usage:<22} {about.get(n, '')}" for n in RUN_SCREEN if shown(n)]
    return "\n".join(["On this screen:", *rows, "When the run needs something from you, it asks in a box of its own."])
