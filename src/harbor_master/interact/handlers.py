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


@command("review", "view", "/review [benchmark]")
def review(session, args):
    return answers.review_briefing(session, args[0] if args else "")


@command("report", "view", "/report ID")
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
            "To change one, the owner edits protocol.toml and locks the new protocol: hm protocol lock")


@command("brief", "confirm", "/brief benchmark text", "the lead's brief is written to the ledger")
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


@command("approve", "confirm", "/approve benchmark", "the selection is signed in the ledger, and its tasks start")
def approve(session, args):
    waiting = next((r for r in session.requests if r.kind == "selection" and args and
                    (r.target == args[0] or r.target.endswith("/" + args[0]))), None)
    if waiting is None:
        return "No selection is waiting for approval" + (f" for {args[0]}." if args else ". Use /approve BENCHMARK")
    session.answer_request(waiting, "approve")
    return f"Selection approved for {waiting.target}."


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
        return f"Give a reason: /{name} {task.name} <why>"
    if value == "accept" and any(e.get("assumed") and e.get("digest") == task.digest
                                 for e in session.ledger.by_task.get(task.task_id, [])):
        return (f"{task.task_id} cannot be accepted: some of its results were assumed, not run. "
                "Run those stages for real first.")
    if value == "accept":
        limit = session.cfg.index.max_per_benchmark
        inside = ledger.accepted(session.ledger.events, task.benchmark)
        if len(inside) >= limit:
            names = ", ".join(s.task_id.split("/")[-1] for s in inside)
            short = task.benchmark.split("/")[-1]
            session.ask_replacement(task, f"{task.benchmark} already has {len(inside)} tasks in the index, the most one "
                                    f"benchmark may have. To include {task.name}, choose the one it replaces: {names}.")
            return (f"{short} is full ({len(inside)} of {limit}), so {task.name} was not accepted. "
                    f"Choose one to replace: /replace OLD {task.name}. Compare them with /included {short}.")
    session.sign_verdict(task, value, reason)
    word = {"accept": "Accepted", "drop": "Rejected", "send_back": "Sent back"}[value]
    session.note_recent("verdict", task.task_id, f"{word.lower()} by @{session.ident.github}" + (f": {reason}" if reason else ""))
    return f"{word} {task.task_id}, signed by @{session.ident.github}. {len(session.ready)} left to review."


@command("replace", "confirm", "/replace OLD NEW [reason]", "your signed verdict: {0} leaves the index, the new task takes its place")
def replace(session, args):
    if len(args) < 2:
        return "Use /replace OLD NEW [reason]: OLD leaves the index and NEW takes its place."
    old, new = session.find_task(args[0]), session.find_task(args[1])
    if old is None or new is None:
        return f"No task matches {args[0 if old is None else 1]!r}."
    if not session.is_reviewer:
        return f"@{session.ident.github} is not on the contributor list, so cannot sign verdicts."
    if new not in session.ready:
        return f"{new.task_id} has no report waiting for your verdict."
    state = next((s for s in ledger.accepted(session.ledger.events, new.benchmark) if s.task_id == old.task_id), None)
    if state is None:
        return f"{old.task_id} is not in the index for {new.benchmark}, so it cannot be replaced. See /included."
    reason = " ".join(args[2:])
    session.sign_verdict(old, "replaced", f"replaced by {new.name}" + (f": {reason}" if reason else ""), digest=state.digest)
    session.sign_verdict(new, "accept", f"replaces {old.name}" + (f": {reason}" if reason else ""))
    session.note_recent("verdict", new.task_id, f"accepted by @{session.ident.github}, replacing {old.name}")
    return f"{new.name} is in the index in place of {old.name}, signed by @{session.ident.github}. {len(session.ready)} left to review."


@command("included", "view", "/included benchmark")
def included(session, args):
    if not args:
        return "Use /included BENCHMARK"
    bench = args[0] if args[0] in session.benches else next((b for b in session.benches if b.endswith("/" + args[0])), None)
    if bench is None:
        return f"No benchmark matches {args[0]!r}."
    inside = ledger.accepted(session.ledger.events, bench)
    lines = [f"{bench}: {len(inside)} of {session.cfg.index.max_per_benchmark} places in the index are taken."]
    for s in inside:
        task = session.find_task(s.task_id)
        lines.append("  " + (answers.report_brief(session, task, s.digest) if task else s.task_id))
    return "\n".join(lines)


@command("accept", "confirm", "/accept ID [reason]", "your signed verdict: {0} enters the index")
def accept(session, args):
    return _verdict(session, args, "accept", "accept", need_reason=False)


@command("reject", "confirm", "/reject ID reason", "your signed verdict: {0} is kept out of the index")
def reject(session, args):
    return _verdict(session, args, "reject", "drop", need_reason=True)


@command("sendback", "confirm", "/sendback ID reason", "your signed verdict: {0} returns for repair in a later run")
def sendback(session, args):
    return _verdict(session, args, "sendback", "send_back", need_reason=True)


@command("kill", "confirm", "/kill", "stops everything now, including running containers")
def kill(session, args):
    session.kill()
    return "Stopped everything."


for _name, _usage in (("restart", "/restart ID"), ("retry", "/retry ID [stage]"), ("escalate", "/escalate ID"),
                      ("limit", "/limit tokens N")):
    planned(_name, "confirm", _usage)
