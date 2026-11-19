"""Which benchmarks this machine has fetched and registered, kept in the workspace's own folder."""
from __future__ import annotations

import json
from pathlib import Path

REGISTRY = ".hm/tasks/registry.json"


def read_registry(workspace: Path) -> dict[str, dict]:
    path = workspace / REGISTRY
    return json.loads(path.read_text()) if path.exists() else {}


def resolve(workspace: Path, benchmark: str, simulate: bool) -> tuple[dict | None, str]:
    """What a run needs to know about a benchmark, by its name: where its tasks are when it is registered
    here, and its entry on the index's list. None, with the reason, when a run may not take it."""
    from . import supported
    registry = read_registry(workspace)
    entry = supported.find(workspace, benchmark)
    if benchmark not in registry and entry is None:
        return None, f"{benchmark} is not on this index's list of benchmarks, and is not a folder registered here. See: hm benchmarks"
    info = dict(registry[benchmark]) if benchmark in registry else {
        "path": "", "supported": benchmark,
        "license": entry.get("license", ""), "released": entry.get("released", "")}
    if entry is not None:
        info["entry"] = entry       # on the list: the run fetches it, and adapts it, when that is not done yet
    if not simulate and not info.get("supported"):
        return None, f"{benchmark} is not on the list of supported benchmarks, so it only runs with --simulate. See: hm benchmarks"
    return info, ""


def register(workspace: Path, benchmark: str, path: Path, **more) -> None:
    """`more` carries where the benchmark came from: supported (its id on the list), version, commit, dev."""
    reg = read_registry(workspace)
    reg[benchmark] = {"path": str(path.resolve()), **more}
    (workspace / REGISTRY).parent.mkdir(parents=True, exist_ok=True)
    (workspace / REGISTRY).write_text(json.dumps(reg, indent=2) + "\n")
