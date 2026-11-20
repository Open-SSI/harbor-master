"""One function per session command. Each returns the line to show the user."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .. import ledger
from ..engine.state import STAGES
from ..tasks import Task
from . import answers
from .registry import command, help_text, planned

if TYPE_CHECKING:
    from ..engine import Session


def _task(session: "Session", args: list[str], name: str) -> tuple[Task | None, str]:
    if not args:
        return None, f"/{name} needs a task ID."
    task = session.find_task(args[0])
    return task, "" if task else f"No task matches {args[0]!r}."


# ----- view: runs at once -----

@command("help", "view", "/help")
def help_(session, args):
    return help_text()


@command("status", "view", "/status")
def status(session, args):
    return answers.progress(session)


@command("cost", "view", "/cost")
def cost(session, args):
    tiers = ", ".join(f"{t} ${c:.2f} ({session.calls[t]} calls)" for t, c in session.cost_by_tier.items())
    return f"Curation: {tiers or 'nothing yet'}. Difficulty trials: ${session.trial_cost:.2f}."


@command("queue", "view", "/queue")
def queue(session, args):
    counts: dict[str, int] = {}
    for t in session.queue:
        counts[t.benchmark] = counts.get(t.benchmark, 0) + 1
    return ", ".join(f"{b}: {n} waiting" for b, n in counts.items()) or "Nothing is waiting."


@command("requests", "view", "/requests")
def requests(session, args):
    return "\n".join(f"{r.id}. {r.text}" for r in session.requests) or "Nothing is waiting on you."


@command("explain", "view", "/explain ID")
def explain(session, args):
    return answers.explain(session, " ".join(args))


for _alias, _usage in (("task", "/task ID"), ("cell", "/cell N")):
    command(_alias, "view", _usage)(explain)


@command("review", "view", "/review [ID]")
def review(session, args):
    """On the screen this opens the reports, one after the other. As text it says what is waiting."""
    return answers.review_briefing(session, args[0] if args else "")


def waiting_reports(session, wanted: str = "") -> list:
    """The reports /review goes through: all that wait for this person, one task's, or one benchmark's."""
    if not wanted:
        return list(session.ready)
    task = session.find_task(wanted)
    if task is not None and task in session.ready:
        return [task]
    return [t for t in session.ready if t.benchmark == wanted or t.benchmark.endswith("/" + wanted)]


WORDS = {"accept": "accept", "a": "accept", "reject": "reject", "r": "reject", "drop": "reject"}


def decide(session, task, line: str) -> str:
    """What a reviewer types under a report they have open: accept [why] or reject why. A verdict is given
    nowhere else, so none is given unread."""
    words = line.split()
    what = WORDS.get(words[0].lower().lstrip("/")) if words else None
    if what is None:
        return "Type accept [why] or reject <why>."
    name, value, need = {"accept": ("accept", "accept", False), "reject": ("reject", "drop", True)}[what]
    return _verdict(session, [task.task_id, *words[1:]], name, value, need)


def report(session, args):
    task, err = _task(session, args, "report")
    return answers.report_text(session, task) if task else err


@command("selection", "view", "/selection benchmark")
def selection(session, args):
    return answers.selection_list(session, args[0]) if args else "Use /selection BENCHMARK"
planned("logs", "view", "/logs ID")
planned("trajectory", "view", "/trajectory ID")


# ----- control: confirmed when read from plain text -----

@command("pause", "control", "/pause [benchmark]", "running stages will finish, no new tasks start")
def pause(session, args):
    target = args[0] if args else ""
    if target in session.benches:
        session.benches[target].paused = True
        session.undo_stack.append(("unpause_bench", target))
        return f"Paused {target}. Running stages will finish."
    session.paused = True
    session.undo_stack.append(("resume", None))
    return "Paused. Running stages will finish."


@command("resume", "control", "/resume", "takes new tasks again")
def resume(session, args):
    session.paused = False
    for b in session.benches.values():
        b.paused = False
    return "Resumed."


@command("cells", "control", "/cells N", "runs {0} tasks at once")
def cells(session, args):
    if not args or not args[0].isdigit() or int(args[0]) < 1:
        return "Give a number of cells: /cells 4"
    session.undo_stack.append(("cells", session.active_cells))
    session.set_cells(int(args[0]))
    return f"Running {args[0]} cells."


@command("priority", "control", "/priority ID", "{0} goes to the front of the queue")
def priority(session, args):
    task, err = _task(session, args, "priority")
    if task is None:
        return err
    if task not in session.queue:
        return f"{task.task_id} is not waiting in the queue."
    session.undo_stack.append(("queue", list(session.queue)))
    session.queue.remove(task)
    session.queue.insert(0, task)
    return f"{task.task_id} is next."


@command("skip", "control", "/skip ID", "{0} is set aside for this run")
def skip(session, args):
    task, err = _task(session, args, "skip")
    if task is None:
        return err
    if task not in session.queue:
        return f"{task.task_id} is not waiting. To stop a running task, use /stop."
    session.undo_stack.append(("queue", list(session.queue)))
    session.queue.remove(task)
    session.benches[task.benchmark].selected -= 1
    return f"Skipped {task.task_id} for this run."


@command("note", "control", "/note ID text", "shown to the fixer and in the report")
def note(session, args):
    task, err = _task(session, args, "note")
    if task is None:
        return err
    session.notes.setdefault(task.task_id, []).append(" ".join(args[1:]))
    return f"Note added to {task.task_id}."


@command("quit", "control", "/quit", "ends after running stages finish, everything is kept for resume")
def quit_(session, args):
    session.quit()
    return "Quitting after the running stages finish. What was waiting for your answer asks again next run."


@command("undo", "control", "/undo", "reverses the last control command")
def undo(session, args):
    if not session.undo_stack:
        return "Nothing to undo."
    what, value = session.undo_stack.pop()
    if what == "resume":
        session.paused = False
    elif what == "unpause_bench":
        session.benches[value].paused = False
    elif what == "cells":
        session.set_cells(value)
    elif what == "queue":
        session.queue[:] = value
    return "Undone."


planned("sync", "control", "/sync")


# ----- confirm: spends, discards, or decides, so always confirmed -----

@command("stop", "confirm", "/stop ID", "its unfinished stage will be discarded, finished stages are kept")
def stop(session, args):
    task, err = _task(session, args, "stop")
    if task is None:
        return err
    cell = session.cell_of(task)
    if cell is None or cell.runner is None:
        return f"{task.task_id} is not running."
    cell.stopping = True
    cell.runner.cancel()
    return f"Stopped {task.task_id} at {STAGES[cell.stage]}. Finished stages are kept."


@command("drop", "confirm", "/drop ID reason", "a credited decision, written to the ledger")
def drop(session, args):
    task, err = _task(session, args, "drop")
    if task is None:
        return err
    reason = " ".join(args[1:]) or "dropped_by_runner"
    cell = session.cell_of(task)
    if task in session.queue:
        session.queue.remove(task)
        session.benches[task.benchmark].selected -= 1
    elif cell is not None and cell.runner is not None:
        cell.stopping = True
        cell.runner.cancel()
    session.record(task.benchmark, "decision", task, stage="manual", gate="drop", tier="human",
                   value="drop", outcome="drop", reason=reason)
    session.note_recent("dropped", task.task_id, f"dropped by @{session.ident.github}: {reason}")
    return f"Dropped {task.task_id}."


@command("budget", "confirm", "/budget N", "the session may spend up to ${0}")
def budget(session, args):
    try:
        value = float(args[0])
    except (IndexError, ValueError):
        return "Give an amount in dollars: /budget 300"
    session.budget = value
    if session.spent < value:
        session.requests[:] = [r for r in session.requests if r.kind != "budget"]
        session.write_requests()
        session.paused = False
    return f"Budget is now ${value:.0f}. Spent so far: ${session.spent:.2f}."


@command("model", "view", "/model")
def model(session, args):
    return ("The tiers and their models are part of the locked protocol, so they cannot change during a curation. "
            "To change one, the owner edits protocol.toml and locks the new protocol: hm lock")


@command("brief", "answer", "/brief benchmark text", "your brief is written to the ledger")
def brief(session, args):
    if len(args) < 2 or args[0] not in session.benches:
        return "Use /brief BENCHMARK followed by the brief text."
    text = " ".join(args[1:])
    waiting = next((r for r in session.requests if r.kind in ("brief", "selection") and r.target == args[0]), None)
    if waiting:
        session.answer_request(waiting, text)       # the benchmark phase records it and ranks again
        return f"Brief recorded for {args[0]}." + (" Ranking again." if waiting.kind == "selection" else "")
    session.record(args[0], "brief", text=text, examples=[], version=int(time.time()))
    return f"Brief recorded for {args[0]}. It applies the next time this benchmark is ranked."


@command("approve", "answer", "/approve benchmark [K]", "the selection is signed in the ledger, and its tasks start")
def approve(session, args):
    waiting = next((r for r in session.requests if r.kind == "selection" and args and
                    (r.target == args[0] or r.target.endswith("/" + args[0]))), None)
    if waiting is None:
        return "No selection is waiting for approval" + (f" for {args[0]}." if args else ". Use /approve BENCHMARK [K]")
    bench = session.benches[waiting.target]
    proposed = sum(1 for r in bench.ranking if r[2])
    if len(args) > 1 and not (args[1].isdigit() and 1 <= int(args[1]) <= bench.most):
        return f"Choose how many of the top tasks go on: a number from 1 to {bench.most}. {proposed} is proposed."
    k = int(args[1]) if len(args) > 1 else proposed
    session.answer_request(waiting, f"approve {k}")
    return f"Selection approved for {waiting.target}: the top {k} of {len(bench.ranking)} tasks go on."


@command("leave", "view", "/leave")
def leave(session, args):
    session.leaving = True
    return "Leaving the screen. The run goes on: open it again with hm curate, inside the index's folder."


@command("add", "control", "/add benchmark", "the benchmark joins this run: fetched, screened, and its tasks queued")
def add(session, args):
    from .. import tasks as tasks_mod
    if not args:
        return "Name a benchmark from the index's list: /add BENCHMARK"
    info, why = tasks_mod.resolve(session.ws, args[0], session.simulate)
    return why if info is None else session.add_benchmark(args[0], info)


def _adapter_request(session, args, kind: str):
    return next((r for r in session.requests if r.kind == kind and (not args or r.target == args[0])), None)


@command("recheck", "control", "/recheck benchmark", "its adapter is checked again: your fix if you made one, otherwise one more try by the frontier model")
def recheck(session, args):
    waiting = _adapter_request(session, args, "adapter")
    if waiting is None:
        return "No adapter is waiting for you" + (f" for {args[0]}." if args else ".")
    session.answer_request(waiting, "retry")
    return f"Checking the adapter for {waiting.target} again."


@command("abandon", "confirm", "/abandon benchmark", "the benchmark is given up for this index until someone builds its adapter by hand")
def abandon(session, args):
    waiting = _adapter_request(session, args, "adapter")
    if waiting is None:
        return "No adapter is waiting for you" + (f" for {args[0]}." if args else ".")
    session.answer_request(waiting, "abandon")
    return (f"{waiting.target} is abandoned: its adapter could not be made to pass. To try again later, "
            f"take it off the list and add it again: hm benchmarks remove {waiting.target}, then hm benchmarks add URL")


@command("lock", "answer", "/lock benchmark", "its adapter is recorded as passing, and its tasks start")
def lock(session, args):
    waiting = _adapter_request(session, args, "lock")
    if waiting is None:
        return "No adapter is waiting for a go-ahead" + (f" for {args[0]}." if args else ".")
    session.answer_request(waiting, "lock")
    return f"The adapter for {waiting.target} may be used."


def _verdict(session, args, name: str, value: str, need_reason: bool) -> str:
    task, err = _task(session, args, name)
    if task is None:
        return err
    if not session.is_reviewer:
        return f"@{session.ident.github} is not on the contributor list, so cannot sign verdicts."
    if task not in session.ready:
        return f"{task.task_id} has no report waiting for your verdict."
    reason = " ".join(args[1:])
    if need_reason and not reason:
        return f"Give a reason: {name} <why>"
    if value == "accept" and any(e.get("assumed") and e.get("digest") == task.digest
                                 for e in session.ledger.by_task.get(task.task_id, [])):
        return (f"{task.task_id} cannot be accepted: some of its results were assumed, not run. "
                "Run those stages for real first.")
    session.sign_verdict(task, value, reason)
    word = {"accept": "Accepted", "drop": "Rejected"}[value]
    session.note_recent("verdict", task.task_id, f"{word.lower()} by @{session.ident.github}" + (f": {reason}" if reason else ""))
    return f"{word} {task.task_id}, signed by @{session.ident.github}. {len(session.ready)} left to review."


@command("included", "view", "/included benchmark")
def included(session, args):
    if not args:
        return "Use /included BENCHMARK"
    bench = args[0] if args[0] in session.benches else next((b for b in session.benches if b.endswith("/" + args[0])), None)
    if bench is None:
        return f"No benchmark matches {args[0]!r}."
    inside = ledger.accepted(session.ledger.events, bench)
    lines = [f"{bench}: {len(inside)} task{'s' if len(inside) != 1 else ''} in the index."]
    for s in inside:
        task = session.find_task(s.task_id)
        lines.append("  " + (answers.report_brief(session, task, s.digest) if task else s.task_id))
    return "\n".join(lines)


def accept(session, args):
    return _verdict(session, args, "accept", "accept", need_reason=False)


def reject(session, args):
    return _verdict(session, args, "reject", "drop", need_reason=True)



@command("kill", "confirm", "/kill", "stops everything now, including running containers")
def kill(session, args):
    session.kill()
    return "Stopped everything."


for _name, _usage in (("restart", "/restart ID"), ("retry", "/retry ID [stage]"), ("escalate", "/escalate ID"),
                      ("limit", "/limit tokens N")):
    planned(_name, "confirm", _usage)


@command("theme", "view", "/theme [light|dark]")
def theme_(session, args):
    from ..tui import theme
    return f"The screen is in {theme.set_mode(args[0] if args else '')} mode now. ctrl+t switches it too."
