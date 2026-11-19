"""Having the worker tier write an adapter (DESIGN.md Section 4, stage 2).

The model reads the benchmark's files and returns the adapter's files as text. It has no tool
that writes or runs anything: the code it returns is written out by this module, and run only
by `tasks.adapter`, in a container.
"""
from __future__ import annotations

from pathlib import Path

from .. import skills
from ..core.config import Config
from . import claude

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


def about(entry: dict) -> str:
    keep = ("id", "name", "version", "released", "github", "license", "task_count", "fields", "format", "tasks")
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
    reply = await claude.ask(cfg.tier(tier).model, skill.system,
                             skill.fill(benchmark=about(entry), layout=layout(source_dir), feedback=again),
                             SCHEMA, cwd=source_dir, max_turns=40)
    return reply.data, reply.cost, reply.tokens, skill.tag
