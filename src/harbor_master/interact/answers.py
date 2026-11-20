"""Answers built from the records: why a task is where it is, and what a question is about.

Reference solutions and probe mutants are never included.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..engine import estimate
from ..engine.state import STAGES
from .registry import COMMANDS

if TYPE_CHECKING:
    from ..engine import Session


def explain(session: Session, token: str) -> str:
    """Why a task is where it is, from its records."""
    m = re.fullmatch(r"(?:cell\s*)?(\d+)", token.strip())
    task = None
    if m:
        cell = next((c for c in session.cells if c.n == int(m.group(1))), None)
        task = cell.task if cell else None
    task = task or session.find_task(token)
    if task is None:
        return f"No task or cell matches {token!r}."
    board = session.blackboards.get(task.task_id, {})
    lines = [f"{task.task_id}  ({board.get('status', 'waiting')})"]
    for e in session.ledger.by_task.get(task.task_id, []):
        if e["type"] == "decision" and e.get("stage") == "triage":
            continue
        what = {"decision": f"{e.get('stage')}: {e.get('value')} -> {e.get('outcome')} [{e.get('tier')}]",
                "trial": f"trial {e.get('purpose')} {e.get('model') or e.get('agent')}: "
                         f"{'pass' if e.get('passed') else 'fail'}",
                "repair": f"repair by {e.get('tier')}: {e.get('what')}",
                "report": "report written"}.get(e["type"])
        if what:
            lines.append("  " + what)
    if board.get("why"):
        lines.append(f"  why: {board['why']}")
    return "\n".join(lines)


def how_to_answer(r) -> str:
    """The command that answers a request."""
    short = r.target.split("/")[-1]
    return {"brief": f"Answer with: /brief {r.target} <what is important>",
            "selection": f"Approve the top K with /approve {short} K. See the whole list with /selection {short}. "
                         f"To rank differently, give a new brief: /brief {r.target} <text>",
            "verdict": "Nothing enters the index until you sign. Start with /review",
            "adapter": f"Fix the files and check them again with /recheck {short} (with no fix, the frontier model tries once more). "
                       f"Or give the benchmark up with /abandon {short}",
            "lock": f"Lock it with /lock {short}",
            "budget": "Answer with: /budget N, or /quit"}.get(r.kind, "")


def ready_to_type(r) -> str:
    """What the empty prompt shows, dimmed, while a request waits: the command that answers it, with what to fill in.
    It is a hint and nothing more. The user types the answer: none is put in the prompt for them."""
    short = r.target.split("/")[-1]
    return {"brief": f"/brief {r.target} <what matters most in this benchmark, in a sentence or two>",
            "selection": f"/approve {short} <how many of the top tasks>   or   /brief {r.target} <a new brief, to rank again>",
            "verdict": "/review   to read each report and accept or reject it",
            "adapter": f"/recheck {short}   or   /abandon {short}",
            "lock": f"/lock {short}",
            "budget": "/budget <dollars>   or   /quit"}.get(r.kind, "")


def waiting(session: Session) -> str:
    """What the run needs from the user now, and how to give it."""
    lines = [f"{r.text}\n  {how_to_answer(r)}"
             for r in sorted(session.requests, key=lambda q: (not q.blocking, q.kind == "verdict"))]
    if session.paused and not any(r.kind == "budget" for r in session.requests):
        lines.append("The run is paused. Continue it with /resume.")
    if lines:
        return ("The run is finished. " if session.finished else "") + "Waiting on you:\n" + "\n".join(lines)
    busy = sum(1 for c in session.cells if c.task is not None)
    if session.finished or (not busy and not session.queue
                            and all(b.phase in ("ready", "skipped") for b in session.benches.values())):
        return "The run is finished and nothing is waiting on you. Leave with /quit, then see: /status"
    return (f"Nothing is needed from you right now. {busy} of {len(session.cells)} cells are working "
            f"and {len(session.queue)} tasks are queued.")


def bench_line(b) -> str:
    """Where one benchmark stands, in words that fit its phase."""
    n, passed = len(b.tasks), len(b.tasks) - b.triaged_out
    gates = f"{passed} of {n} tasks passed the admission gates"
    if b.phase == "queued":
        return f"{b.id}: not started yet."
    if b.phase == "triage":
        return f"{b.id}: checking the admission gates ({b.detail})."
    if b.phase == "skipped":
        return f"{b.id}: skipped. {b.detail}"
    if b.phase == "brief":
        return f"{b.id}: {gates}. Waiting for a brief before they are ranked."
    if b.phase == "ranking":
        return f"{b.id}: {gates}. Ranking them now."
    if b.phase == "signoff":
        return f"{b.id}: {gates}, {sum(1 for r in b.ranking if r[2])} selected. Waiting for the selection to be approved."
    if b.phase in ("fetch", "adapt"):
        return f"{b.id}: getting it ready ({b.detail or b.phase})."
    if b.phase == "review":
        return f"{b.id}: {b.detail or 'its adapter needs you'}."
    return (f"{b.id}: {b.selected} of {n} tasks selected ({b.triaged_out} failed a gate, {b.not_selected} under "
            f"the cap). {b.kept} kept, {b.dropped} dropped, {b.queued} still to do.")


def progress(session: Session) -> str:
    return "\n".join([bench_line(b) for b in session.benches.values()]
                     + [f"Spent ${session.spent:.2f} of ${session.budget:.0f}."])


def timing(session: Session) -> str:
    busy = [c for c in session.cells if c.task is not None]
    if not busy:
        return "No task is in a working cell right now, so there is nothing to time."
    lines = []
    for c in busy:
        est = estimate(session, c)
        lines.append(f"Cell {c.n}, {c.task.name}: in {STAGES[c.stage]}, "
                     + (f"about {_span(est)} to finish." if est else "too little history yet to estimate."))
    return "\n".join(lines + [f"{len(session.queue)} more tasks are queued."])


def terms(session: Session) -> dict[str, str]:
    """Short explanations of the words the screen uses, keyed by the word a question would contain."""
    sel, cfg = session.cfg.select, session.cfg
    return {
        "brief": "A brief is a few sentences, from any contributor, on what makes a task important in this benchmark. "
                 "Ranking orders the tasks by it. Give one with /brief BENCHMARK text.",
        "select": f"The selection is the top-ranked tasks that go on to the costly stages: at most {sel.top_k} per "
                  f"benchmark, and at most {sel.max_fraction:.0%} of it. The lead approves it with /approve BENCHMARK.",
        "cap": f"The cap is the most tasks one benchmark may send to the costly stages: the smaller of {sel.top_k} and "
               f"{sel.max_fraction:.0%} of its tasks.",
        "gate": "An admission gate is a cheap check a task must pass before it is ranked: it has a reference solution, "
                "a program can verify it quickly, it is not multiple choice, its license allows use, and it is recent.",
        "cell": "A working cell carries one task through verify, probe, difficulty (once), audit and report. Cells run in parallel.",
        "verdict": "A verdict is a reviewer's signed decision on a finished task. /review shows each report, and under it "
                   "you type accept or reject <why>.",
        "simulat": "A simulated run invents every result and spends nothing. No model is called, so answers here are "
                   "put together from the run's records by the words in your question.",
    }


TOPICS = (      # which records a question is about, by its words
    ("waiting", r"next|now what|what now|what (should|do|can|must) i|to ?do\b|wait|stuck|block|need|my turn|proceed|continue|nothing happen"),
    ("time", r"how long|\bwhen\b|\beta\b|finish|time left|remaining|\btake\b|how soon"),
    ("cost", r"cost|spen[dt]|budget|money|\$|price|token"),
    ("progress", r"progress|status|going|state\b|doing|happen|kept|dropped|how many|where (is|are) (it|we|things)"),
    ("help", r"help|command|how (do|can) i|what can (i|you)"),
)
HELP = ("Type / to see the commands, or /help for the list. You can also ask: what should I do next, how is it going, "
        "how long until cell 2 finishes, what is TASK about, why was TASK dropped, how much has been spent.")


def about_target(session: Session, question: str) -> str:
    """The records of the task or benchmark a question names. Empty when it names none."""
    found = find_target(session, question.lower())
    task = session.find_task(found) if found else None
    parts: list[str] = []
    if task is not None:
        cell = session.cell_of(task)
        if cell is not None:
            est = estimate(session, cell)
            parts.append(f"{task.name} is in {STAGES[cell.stage]} ({cell.detail}).")
            parts.append(f"About {_span(est)} to finish." if est else "Too little history yet to estimate the time.")
        elif task in session.queue:
            parts.append(f"{task.name} is waiting in the queue, position {session.queue.index(task) + 1}.")
        else:
            parts.append(explain(session, task.task_id))
        text = " ".join(task.instruction.split())
        if text:
            parts.append("The task: " + (text[:320] + ("..." if len(text) > 320 else "")))
    elif found in session.benches:
        parts.append(bench_line(session.benches[found]))
    return "\n".join(parts)


def context_for(session: Session, question: str, focused: bool = False) -> str:
    """The records attached to a question, gathered by code. Solutions are never included.

    A model gets all of them and picks. With `focused`, there is no model to pick (a simulated
    run), so only the records that the question's words point at are returned, as the answer.
    """
    q = question.lower()
    sections = {"target": about_target(session, question), "waiting": waiting(session), "progress": progress(session),
                "time": timing(session), "cost": COMMANDS["cost"].run(session, []), "help": HELP}
    asked = [text for word, text in terms(session).items() if word in q]
    if not focused:
        return "\n\n".join([text for name, text in sections.items() if text and name != "help"] + asked)
    explaining = bool(asked) and re.search(r"what('s| is| are| does)|mean|explain|define", q)
    names = [name for name, pattern in TOPICS if re.search(pattern, q)]
    picked = (asked if explaining else []) + ([sections["target"]] if sections["target"] else [])
    picked += [sections[n] for n in names if not (sections["target"] and n in ("progress", "time"))]
    if picked:
        return "\n\n".join(dict.fromkeys(picked))
    return ("A simulated run has no model, so it cannot answer a free question. Here is where the run stands.\n\n"
            + sections["waiting"] + "\n\n" + sections["progress"] + "\n\n" + HELP)


def _span(est: tuple[float, float]) -> str:
    lo, hi = est
    if hi < 90:
        return f"{max(1, round(lo))} to {max(2, round(hi))} seconds"
    return f"{max(1, round(lo / 60))} to {max(2, round(hi / 60))} minutes"


def report_brief(session: Session, task, digest: str | None = None) -> str:
    """One line on a finished task, for the reviewer: how hard it was, what the audit said, what was fixed."""
    events = [e for e in session.ledger.by_task.get(task.task_id, []) if e.get("digest") == (digest or task.digest)]
    hard = next((e["value"] for e in reversed(events) if e["type"] == "decision" and e.get("stage") == "difficulty"), "?")
    audit = next((e for e in reversed(events) if e["type"] == "decision" and e.get("stage") == "audit"
                  and e.get("outcome") == "pass"), None)
    repairs = [e for e in session.ledger.by_task.get(task.task_id, []) if e["type"] == "repair"]
    parts = [f"{hard} by frontier agents", f"audit accepted by the {audit['tier']} tier" if audit else "audit: see report"]
    if repairs:
        parts.append(f"repaired {len(repairs)} time{'s' if len(repairs) != 1 else ''} ({repairs[-1].get('what', '')})")
    return f"{task.name}: " + ", ".join(parts) + "."


def review_briefing(session: Session, benchmark: str = "") -> str:
    """What is waiting for this user's verdict, and how to go and check it."""
    if not session.is_reviewer:
        return f"@{session.ident.github} is not on the contributor list, so cannot sign verdicts."
    ready = [t for t in session.ready if not benchmark or t.benchmark == benchmark or t.benchmark.endswith("/" + benchmark)]
    if not ready:
        return "No reports are waiting for your verdict."
    lines = [f"{len(ready)} report{'s are' if len(ready) != 1 else ' is'} ready. Each task passed every stage, "
             "and is recommended for the index. Nothing enters the index until you sign."]
    lines += ["  " + report_brief(session, t) for t in ready[:8]]
    if len(ready) > 8:
        lines.append(f"  and {len(ready) - 8} more.")
    lines.append("/review opens them one after the other. Under each report you type accept [why] or reject <why>.")
    return "\n".join(lines)


def report_text(session: Session, task) -> str:
    event = next((e for e in reversed(session.ledger.by_task.get(task.task_id, [])) if e["type"] == "report"), None)
    path = session.ws / event["location"] if event and event.get("location") else None
    if path is None or not path.exists():
        return "The report file is not on this machine.\n\n" + explain(session, task.task_id)
    return path.read_text()


def task_line(task, limit: int = 110) -> str:
    """One short sentence on what a task asks: its own description, else the start of its instruction."""
    said = " ".join(str((task.meta.get("task") or {}).get("description") or task.instruction).lstrip("# ").split())
    first = said.split(". ")[0].rstrip(".")
    return (first[:limit].rstrip() + "...") if len(first) > limit else first


def selection_list(session: Session, benchmark: str) -> str:
    bench = session.benches.get(benchmark) or next(
        (b for name, b in session.benches.items() if name.endswith("/" + benchmark)), None)
    if bench is None or not bench.ranking:
        return f"No ranking yet for {benchmark!r}."
    rows = [f"{bench.id}: {sum(1 for r in bench.ranking if r[2])} of {len(bench.ranking)} selected. "
            "A star marks a selected task. They are listed in the order they are taken, so the selection is the top of the list."]
    for i, (tid, rating, chosen) in enumerate(bench.ranking[:40], 1):
        rows.append(f"  {i:>3} {'*' if chosen else ' '} {tid.split('/')[-1]:<12} {rating:+.2f}")
    if len(bench.ranking) > 40:
        rows.append(f"  and {len(bench.ranking) - 40} more.")
    return "\n".join(rows)


def find_target(session, text: str) -> str | None:
    """The task, cell or benchmark that a line of text is about, if it names one."""
    m = re.search(r"\bcell\s*(\d+)\b", text)
    if m:
        cell = next((c for c in session.cells if c.n == int(m.group(1))), None)
        return cell.task.task_id if cell and cell.task else f"cell {m.group(1)}"
    for word in re.findall(r"[\w./@-]+", text):
        task = session.find_task(word)
        if task:
            return task.task_id
    return next((b for b in session.benches if b.lower() in text or b.split("/")[-1].lower() in text), None)
