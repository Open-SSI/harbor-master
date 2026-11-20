"""Intake: detect what was submitted and load its tasks (DESIGN.md Section 4, stage 0)."""
from __future__ import annotations

import tomllib
from pathlib import Path

from .model import Task, digest_dir

SKIP = {".git", "archive", "node_modules", "__pycache__", "solution", "tests", "environment"}


def find_tasks(path: Path) -> list[Path]:
    """Every task directory under `path`, at any depth. A task is a directory with a task.toml."""
    found: list[Path] = []

    def walk(d: Path) -> None:
        if (d / "task.toml").exists():
            found.append(d)
            return
        for child in sorted(p for p in d.iterdir() if p.is_dir() and p.name not in SKIP and not p.name.startswith(".")):
            walk(child)

    walk(path)
    return found


def detect(path: Path) -> str:
    """"harbor_task", "harbor_dataset", or "foreign" (needs an adapter)."""
    if (path / "task.toml").exists():
        return "harbor_task"
    return "harbor_dataset" if path.is_dir() and find_tasks(path) else "foreign"


def load_task(path: Path, benchmark: str) -> Task:
    meta: dict = {}
    if (path / "task.toml").exists():
        meta = tomllib.loads((path / "task.toml").read_text())
    md = meta.get("metadata", {})
    instruction = (path / "instruction.md").read_text() if (path / "instruction.md").exists() else ""
    solution = path / "solution"
    return Task(
        task_id=f"{benchmark}/{path.name}", benchmark=benchmark, name=path.name, path=path,
        digest=digest_dir(path), instruction=instruction, meta=meta,
        has_solution=solution.is_dir() and any(solution.iterdir()),
        has_tests=(path / "tests").is_dir() and any((path / "tests").iterdir()),
        subarea=str(md.get("field") or md.get("domain") or (md.get("tags") or ["general"])[0]),
    )


def load_benchmark(path: Path, benchmark: str) -> list[Task]:
    found = find_tasks(path) if path.is_dir() else []
    if not found:
        raise ValueError(f"{path} is not in Harbor format. A benchmark in another format needs an adapter, which a real run of hm curate writes.")
    return [load_task(d, benchmark) for d in found]
