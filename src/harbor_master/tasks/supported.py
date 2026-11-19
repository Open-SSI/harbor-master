"""The benchmarks an index may curate: benchmarks.toml in the index repository."""
from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

REQUIRED = ("id", "name", "version", "released", "github", "ref", "commit", "tasks")


def supported(index_dir: Path) -> list[dict]:
    path = index_dir / "benchmarks.toml"
    entries = tomllib.loads(path.read_text()).get("benchmark", []) if path.exists() else []
    for e in entries:
        missing = [k for k in REQUIRED if not e.get(k)]
        if missing:
            raise ValueError(f"benchmarks.toml: {e.get('id', '?')} is missing {', '.join(missing)}")
    return entries


def find(index_dir: Path, name: str) -> dict | None:
    name = name.split("/")[-1].lower()
    return next((e for e in supported(index_dir) if e["id"].lower() == name), None)


def fetch(entry: dict, sources: Path) -> Path:
    """Clone the benchmark at its pinned commit, or check an existing clone is at it. Returns the tasks folder."""
    repo = sources / entry["id"]
    git = lambda *a, cwd=repo: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)  # noqa: E731
    if not (repo / ".git").exists():
        sources.mkdir(parents=True, exist_ok=True)
        git("clone", "--quiet", "--depth", "1", "--branch", entry["ref"], entry["github"] + ".git", str(repo), cwd=sources)
    head = git("rev-parse", "HEAD").stdout.strip()
    if head != entry["commit"]:
        raise ValueError(f"{repo} is at {head[:10]}, not the pinned commit {entry['commit'][:10]} ({entry['ref']}). "
                         "Remove that folder and submit again.")
    return repo / entry["tasks"]
