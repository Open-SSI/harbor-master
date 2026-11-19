"""Which benchmarks this machine has fetched and registered, kept in the workspace's own folder."""
from __future__ import annotations

import json
from pathlib import Path

REGISTRY = ".hm/tasks/registry.json"


def read_registry(workspace: Path) -> dict[str, dict]:
    path = workspace / REGISTRY
    return json.loads(path.read_text()) if path.exists() else {}


def register(workspace: Path, benchmark: str, path: Path, field_name: str, **more) -> None:
    """`more` carries where the benchmark came from: supported (its id on the list), version, commit, dev."""
    reg = read_registry(workspace)
    reg[benchmark] = {"path": str(path.resolve()), "field": field_name, **more}
    (workspace / REGISTRY).parent.mkdir(parents=True, exist_ok=True)
    (workspace / REGISTRY).write_text(json.dumps(reg, indent=2) + "\n")
