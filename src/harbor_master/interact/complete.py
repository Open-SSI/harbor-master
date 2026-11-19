"""Completing a "/" command as it is typed: the command's name, then its arguments.

What can follow a command is read from its usage line in the registry, so a new command
is completed without any change here. What fits best right now comes first: a benchmark
that is waiting for a brief after /brief, a task whose report is ready after /accept.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .. import ledger
from .registry import COMMANDS

if TYPE_CHECKING:
    from ..engine import Session

ANSWERS = {"brief": ["brief"], "selection": ["approve", "selection"], "verdict": ["review"],     # request kind -> commands
           "replace": ["replace", "included"], "budget": ["budget"]}
BENCH_PHASE = {"brief": "brief", "approve": "signoff", "selection": "signoff"}      # the phase a command answers
VERDICTS = ("accept", "reject", "sendback", "report")
LIMIT = 40


def slots(name: str) -> list[str]:
    """The kinds of argument a command takes, from its usage line: benchmark, ID, N, text."""
    return [word.strip("[]") for word in COMMANDS[name].usage.split()[1:]]


def _benchmarks(session: Session, name: str) -> list[str]:
    first = BENCH_PHASE.get(name)
    return sorted(session.benches, key=lambda b: session.benches[b].phase != first)


def _tasks(session: Session, name: str, slot: str) -> list[str]:
    tasks = [t for b in session.benches.values() for t in b.tasks]
    seen: dict[str, int] = {}
    for t in tasks:
        seen[t.name] = seen.get(t.name, 0) + 1
    short = lambda t: t.name if seen.get(t.name, 0) < 2 else t.task_id      # noqa: E731  (a name two benchmarks share)
    if name == "replace" and slot == "OLD":
        inside = [s.task_id for b in session.benches for s in ledger.accepted(session.ledger.events, b)]
        return [short(t) for t in tasks if t.task_id in inside]
    if name in VERDICTS or name == "replace":
        return [short(t) for t in session.ready] or ([short(t) for t in tasks] if name == "report" else [])
    working = [c.task for c in session.cells if c.task is not None]
    ahead = working + [t for t in session.queue if t not in working]
    return [short(t) for t in ahead] + [short(t) for t in tasks if t not in ahead]


def complete(session: Session, text: str) -> list[str]:
    """The lines that what is typed could become, best first. Empty when it is not a command."""
    if not text.startswith("/"):
        return []
    parts = text[1:].split(" ")
    if len(parts) == 1:
        waiting = [c for r in session.requests for c in ANSWERS.get(r.kind, [])]
        names = [n for n, spec in COMMANDS.items() if spec.run and n.startswith(parts[0].lower())]
        names.sort(key=lambda n: waiting.index(n) if n in waiting else len(waiting))
        return [f"/{n}{' ' if slots(n) else ''}" for n in names]
    name = parts[0].lower()
    if name not in COMMANDS or COMMANDS[name].run is None:
        return []
    kinds, at = slots(name), len(parts) - 2
    if at >= len(kinds):
        return []
    kind, typed = kinds[at], parts[-1].lower()
    options = (_benchmarks(session, name) if kind == "benchmark"
               else _tasks(session, name, kind) if kind in ("ID", "OLD", "NEW") else [])
    options = list(dict.fromkeys(options))
    found = [o for o in options if o.lower().startswith(typed)] or [o for o in options if typed and typed in o.lower()]
    head = " ".join([name, *parts[1:-1]])
    return [f"/{head} {o}{' ' if at + 1 < len(kinds) else ''}" for o in found[:LIMIT]]


def usage(text: str) -> str:
    """The usage line of the command being typed, once its name is complete."""
    name = text[1:].split(" ")[0].lower() if text.startswith("/") else ""
    return COMMANDS[name].usage if name in COMMANDS and COMMANDS[name].run else ""
