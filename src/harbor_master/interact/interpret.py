"""Reading a line typed at the prompt (DESIGN.md Section 11).

A "/" command is parsed directly. Plain text is meant for the decision tier. Until that
backend exists, the keyword reader here stands in for it, and it returns the same typed
result: commands, a question, unsupported, or unclear.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .. import skills
from .registry import COMMANDS, Command, Intent, parse

if TYPE_CHECKING:
    from ..engine import Session

REFUSALS = [  # (pattern, why not, offered instead)
    (r"\b(threshold|pass rate|trial count|number of trials|suite|difficulty filter)\b.*\b(change|set|raise|lower|use)\b|"
     r"\b(change|set|raise|lower|use)\b.*\b(threshold|pass rate|trials|suite)\b",
     "That is part of the protocol. Changing it mid-run would mix results that cannot be compared.",
     "the owner edits protocol.toml and locks the new protocol: hm protocol lock"),
    (r"\b(delete|remove|overwrite|erase|edit)\b.*\b(ledger|record|records|event)\b",
     "The ledger is append-only.", "a new record that supersedes it (a new verdict, or a run under a new protocol)"),
    (r"\b(accept|approve|mark)\b.*\b(kept|accepted|pass|passed|task|t\d+)\b",
     "Only the stages and a listed reviewer's verdict can accept a task.", "/review, if you are a listed reviewer"),
    (r"\b(edit|change|rewrite|modify)\b.*\b(task|verifier|solution|instruction|tests?)\b",
     "Repairs go through the fix stage, so that they are recorded and re-verified.",
     "/note ID text, to tell the fixer what to look at"),
    (r"\b(contributor|reviewer list|api key|token|identity)\b",
     "That is not a session setting.", "hm keys set NAME, or hm init, outside the session"),
    (r"^\s*(rm|sudo|git|cd|ls|cat|pip|python|bash|sh)\b",
     "The session does not run shell commands.", "/help"),
]


def interpret(session: Session, text: str) -> Intent:
    """Read a line typed at the prompt.

    A "/" command is parsed directly. Plain text is meant for the decision tier (DESIGN.md
    Section 11). Until that backend exists, this keyword reader stands in for it, and it
    returns the same typed result: commands, a question, unsupported, or unclear.
    """
    text = text.strip()
    if text.startswith("/"):
        cmd = parse(text)
        if cmd.name not in COMMANDS:
            return Intent("unsupported", reason=f"There is no /{cmd.name} command.", offer="/help", typed=True)
        return Intent("commands", [cmd], typed=True)
    low = text.lower()
    for pattern, why, offer in REFUSALS:
        if re.search(pattern, low):
            return Intent("unsupported", reason=why, offer=offer)
    if low.endswith("?") or re.match(r"(how|what|why|when|which|who|where|is|are|does|do|can|explain|tell me|describe)\b", low):
        return Intent("question", prob=0.95)
    commands: list[Command] = []
    for part in re.split(r"\s+and\s+|,\s*|;\s*|\s+then\s+", low):
        cmd = _one(session, part)
        if cmd:
            commands.append(cmd)
        elif re.match(r"(tell|show|explain|how|what|why)\b", part):
            return Intent("question", prob=0.9) if not commands else Intent("commands", commands, 0.9)
    if commands:
        return Intent("commands", commands, 0.93)
    return Intent("unclear", [Command("status"), Command("help")], 0.3)


def find_target(session: Session, text: str) -> str | None:
    m = re.search(r"\bcell\s*(\d+)\b", text)
    if m:
        cell = next((c for c in session.cells if c.n == int(m.group(1))), None)
        if cell and cell.task:
            return cell.task.task_id
        return f"cell {m.group(1)}"
    for word in re.findall(r"[\w./@-]+", text):
        task = session.find_task(word)
        if task:
            return task.task_id
    for bench in session.benches:
        if bench.lower() in text or bench.split("/")[-1].lower() in text:
            return bench
    return None


def _one(session: Session, text: str) -> Command | None:
    target = find_target(session, text)
    number = re.search(r"\$?\s*(\d+(?:\.\d+)?)", re.sub(r"\bcell\s*\d+\b|t\d+", "", text))
    args = [target] if target else []
    if re.search(r"\b(pause|hold|wait)\b", text):
        return Command("pause", [a for a in args if not a.startswith("cell ")])
    if re.search(r"\b(resume|continue|carry on|unpause|go on)\b", text):
        return Command("resume")
    if re.search(r"\b(stop|cancel|abort|halt)\b", text) and target:
        return Command("stop", args)
    if re.search(r"\b(kill|stop everything|abort everything)\b", text):
        return Command("kill")
    if re.search(r"\b(skip)\b", text) and target:
        return Command("skip", args)
    if re.search(r"\b(drop|discard|throw out|remove)\b", text) and target:
        return Command("drop", args + ["dropped_by_runner"])
    if re.search(r"\b(budget|spend|spending)\b", text) and number:
        return Command("budget", [number.group(1)])
    if re.search(r"\bcells?\b", text) and number and not target:
        return Command("cells", [number.group(1)])
    if re.search(r"\b(prioriti[sz]e|first|front|next)\b", text) and target:
        return Command("priority", args)
    if re.search(r"\b(quit|exit|finish up|wrap up|end the session)\b", text):
        return Command("quit")
    swap = re.search(r"\breplace\s+(\S+)\s+(?:with|by)\s+(\S+)", text)
    if swap and session.find_task(swap.group(1)) and session.find_task(swap.group(2)):
        return Command("replace", [session.find_task(swap.group(1)).task_id, session.find_task(swap.group(2)).task_id])
    if re.search(r"\b(approve|looks good|go ahead|sign off)\b", text) and target in session.benches:
        return Command("approve", args)
    if re.search(r"\baccept\b", text) and target and target not in session.benches:
        return Command("accept", args)
    if re.search(r"\b(reviews?|reports?|verdicts?)\b", text):
        return Command("report", args) if target and target not in session.benches else Command("review")
    if re.search(r"\b(ranking|selection|selected)\b", text) and target in session.benches:
        return Command("selection", args)
    if re.search(r"\b(status|summary|overview)\b", text):
        return Command("status")
    if re.search(r"\b(cost|spent|how much)\b", text):
        return Command("cost")
    if re.search(r"\b(queue|waiting|pending)\b", text):
        return Command("queue")
    if re.search(r"\b(open|show|go to)\b", text) and target:
        return Command("explain", args)
    return None


NEEDS_TARGET = {"stop", "skip", "drop", "priority", "explain", "approve", "selection"}


async def interpret_async(session: Session, text: str) -> Intent:
    """Read a typed line with the decision model when there is one, and with the keyword reader when not.

    The refusals stay in code: what the session must never do does not depend on a model's reading.
    """
    text = text.strip()
    low = text.lower()
    if text.startswith("/"):
        return interpret(session, text)
    for pattern, why, offer in REFUSALS:
        if re.search(pattern, low):
            return Intent("unsupported", reason=why, offer=offer)
    classify = getattr(session.backend, "classify", None)
    if classify is None or not getattr(session.backend, "jev", False):
        return interpret(session, text)
    skill = skills.load("session/interpret")
    intents = {k: v for k, v in skill.data["intents"].items()
               if k in ("question", "unsupported", "none") or (k in COMMANDS and COMMANDS[k].run)}
    targets = {"none": "The message names no benchmark, working cell or task."}
    resolve: dict[str, str] = {}
    for b in session.benches:
        targets[b] = f"The benchmark {b.split('/')[-1]}."
        resolve[b] = b
    for c in session.cells:
        if c.task:
            targets[f"cell {c.n}"] = f"Working cell {c.n}, which is running the task {c.task.name}."
            resolve[f"cell {c.n}"] = c.task.task_id
    for t in [*session.ready, *session.queue][:80]:
        targets.setdefault(t.task_id, f"The task {t.name}.")
        resolve[t.task_id] = t.task_id
    commands: list[Command] = []
    probs: list[float] = []
    asked_question = False
    try:
        for clause in [c for c in re.split(r"\s+and\s+|;\s*|\s+then\s+", text) if c.strip()]:
            answers = await classify(
                {"message": clause.strip()},        # one clause at a time: the rest of the line would mislead
                {"intent": {"type": "choice", "instructions": skill.data["question"], "criteria": intents},
                 "target": {"type": "choice", "instructions": skill.data["target_question"], "criteria": targets}})
            intent, target = answers.get("intent") or {}, answers.get("target") or {}
            name = intent.get("choice", "none")
            p = float((intent.get("probabilities") or {}).get(name, 0.0))
            if name == "question":
                asked_question = True
                continue
            if name == "unsupported" and p >= 0.6:
                return Intent("unsupported", reason="The session cannot do that.", offer="/help")
            if name in ("none", "unsupported"):
                continue
            args: list[str] = []
            named = find_target(session, clause.lower())      # an exact name in the text beats the model's pick
            chosen = target.get("choice", "none")
            if named and not named.startswith("cell ") and (name in NEEDS_TARGET or name == "pause"):
                args = [named]
            elif chosen != "none" and (name in NEEDS_TARGET or name == "pause"):
                args = [resolve.get(chosen, chosen)]
                p = min(p, float((target.get("probabilities") or {}).get(chosen, 0.0)))
            elif name in NEEDS_TARGET:
                p = 0.0                                   # a command that needs a target, and none was named
            number = re.search(r"\$?\s*(\d+(?:\.\d+)?)", re.sub(r"\bcell\s*\d+\b|t\d+", "", clause))
            if name in ("cells", "budget"):
                args, p = ([number.group(1)], p) if number else ([], 0.0)
            if name == "drop":
                args.append("dropped_by_runner")
            commands.append(Command(name, args))
            probs.append(p)
    except Exception:           # the model is unreachable: fall back, never fail the prompt
        return interpret(session, text)
    if not commands:
        return Intent("question", prob=0.9) if asked_question else Intent("unclear", [Command("status"), Command("help")], 0.3)
    prob = min(probs)
    if prob < float(skill.data.get("min_probability", 0.5)):
        return Intent("unclear", commands, prob)          # shown as "did you mean", never run
    return Intent("commands", commands, prob)
