"""Having the worker tier write an adapter (DESIGN.md Section 4, stage 2).

The model reads the benchmark's files and returns the adapter's files as text. It has no tool
that writes or runs anything: the code it returns is written out by this module, and run only
by `tasks.adapter`, in a container.
"""
from __future__ import annotations

from pathlib import Path

from .. import skills
from ..core.config import Config
from . import sessions

SKILL = "adapt/write-adapter"
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["files", "task_count", "notes"],
    "properties": {
        "files": {"type": "array", "description": "Complete files, with paths relative to the adapter's package.",
                  "items": {"type": "object", "additionalProperties": False, "required": ["path", "content"],
                            "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}},
        "task_count": {"type": "integer", "description": "How many tasks the adapter will write."},
        "notes": {"type": "string", "description": "What could not be carried over, and what a reviewer should check by hand."},
    },
}


def layout(source_dir: Path, limit: int = 120) -> str:
    """The top of the benchmark's file tree, as a starting point for the model."""
    files = sorted(str(p.relative_to(source_dir)) for p in source_dir.rglob("*") if p.is_file() and ".git" not in p.parts)
    return "\n".join(files[:limit]) + (f"\n... and {len(files) - limit} more files" if len(files) > limit else "")


def harbor_guide(limit: int = 12000) -> str:
    """The parts of Harbor's own adapter tutorial that are about making the tasks (its sections 1 to 3), read from the
    installed Harbor, so that they are the version this machine runs. "" when Harbor does not ship it."""
    try:
        from harbor.cli.adapter_review import _REVIEW_CHECKLIST as tutorial
    except Exception:           # another version of Harbor, or none: the skill's own rules stand alone
        return ""
    start, end = tutorial.find("### 1. Understand the Original Benchmark"), tutorial.find("### 4.")
    if start < 0 or end <= start:
        return ""
    return ("HARBOR'S OWN ADAPTER GUIDE, the parts about making the tasks. Skip what is about pull requests, parity runs, "
            "Harbor's own repository and its datasets: those are not yours to do. Where it says to run the oracle, the "
            "checks do that after you answer.\n\n" + tutorial[start:end].strip()[:limit])


def about(entry: dict) -> str:
    keep = ("id", "name", "version", "released", "github", "license", "task_count", "format", "tasks", "environments")
    return "\n".join(f"{k}: {entry[k]}" for k in keep if entry.get(k) not in (None, "", []))


def write_files(package: Path, files: list[dict]) -> list[str]:
    """Write what the model returned into the adapter's package. Only adapter.py and files under
    task-template/ are taken: the entry point and everything outside the package are not the model's to write."""
    written = []
    for f in files:
        rel = Path(f["path"])
        if rel.is_absolute() or ".." in rel.parts or not (rel == Path("adapter.py") or rel.parts[0] == "task-template"):
            continue
        target = package / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["content"])
        written.append(str(rel))
    return written


async def write_adapter(cfg: Config, entry: dict, source_dir: Path, feedback: str = "", tier: str = "worker") -> tuple[dict, float, int, str]:
    """One session of the worker tier, or of the frontier tier when the worker's adapter keeps failing.
    Returns its typed reply, what it cost, its tokens, and the skill's tag."""
    skill = skills.load(SKILL)
    again = ("A first version of the adapter did not pass these checks. Return every file again, corrected:\n" + feedback) if feedback else ""
    reply = await sessions.ask(cfg.tier(tier), skill.system,
                             skill.fill(benchmark=about(entry), layout=layout(source_dir), guide=harbor_guide(), feedback=again),
                             SCHEMA, cwd=source_dir, max_turns=40)
    return reply.data, reply.cost, reply.tokens, skill.tag


FIDELITY = "adapt/check-fidelity"
FIDELITY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "found"],
    "properties": {"verdict": {"type": "string", "enum": ["faithful", "inputs_missing", "setting_changed"]},
                   "found": {"type": "string", "description": "The files read, what the benchmark gives and asks, what the tasks do."}},
}


def sample(out_dir: Path, n: int = 3, limit: int = 1500) -> str:
    """A few generated tasks, spread over the set, as text: each one's instruction, environment and reference solution."""
    from ..tasks.intake import find_tasks
    made = find_tasks(out_dir)
    picked = sorted({made[i * len(made) // n] for i in range(n)}) if made else []
    parts = []
    for d in picked:
        parts.append(f"### {d.name}")
        for rel in ("instruction.md", "environment/Dockerfile", "solution/solve.sh"):
            f = d / rel
            parts.append(f"--- {rel}\n" + (f.read_text(errors="replace")[:limit] if f.is_file() else "(none)"))
        parts.append("--- files: " + ", ".join(sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())[:30]))
    return "\n".join(parts)


async def fidelity(cfg: Config, entry: dict, source_dir: Path, out_dir: Path, notes: list[str]) -> tuple[str, float, int]:
    """The worker tier reads the benchmark and a few of the tasks, and says whether the tasks are the benchmark's own.
    Returns a problem ("" when they are), the cost and the tokens."""
    skill = skills.load(FIDELITY)
    reply = await sessions.ask(cfg.tier("worker"), skill.system,
                               skill.fill(benchmark=about(entry), notes="\n".join(notes) or "nothing", tasks=sample(out_dir)),
                               FIDELITY_SCHEMA, cwd=source_dir, max_turns=20)
    verdict, found = reply.data.get("verdict", ""), reply.data.get("found", "")
    said = {"inputs_missing": "the tasks do not give the solver the data, files or tools the benchmark gives it",
            "setting_changed": "the tasks ask something other than the benchmark's own setting"}.get(verdict, "")
    return (f"{said}: {found[:1200]}" if said else ""), reply.cost, reply.tokens


JUDGE = "adapt/judge"


async def judge(cfg: Config, entry: dict, source_dir: Path, problems: list[str], notes: str) -> tuple[str, float, float, int, str]:
    """Ask the decision model, Jev, whether the benchmark can be adapted at all, as a typed choice with its probability.
    Returns the choice ("" when it declined), the probability, the cost, the tokens and the skill's tag."""
    from . import jev
    skill = skills.load(JUDGE)
    asked = {g: jev.choice(v["question"], v["criteria"]) for g, v in skill.data.items()}
    state = {"benchmark": about(entry), "files": layout(source_dir, 80), "failures": "\n".join(problems[:12]),
             "worker_notes": notes[:4000]}
    answers, cost, tokens = await jev.decisions(cfg.tiers.decision.model, state, asked)
    answer = answers.get("adaptable") or {}
    choice = answer.get("choice", "") if answer.get("type") == "choice" else ""
    prob = float((answer.get("probabilities") or {}).get(choice, answer.get("confidence") or 0.0)) if choice else 0.0
    return choice, prob, cost, tokens, skill.tag
