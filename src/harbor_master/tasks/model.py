"""A task and its content digest."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class Task:
    task_id: str            # "<benchmark>/<name>", stable for the life of the task
    benchmark: str
    name: str
    path: Path
    digest: str             # content digest of the task directory; a repair produces a new one
    instruction: str = ""
    meta: dict = field(default_factory=dict)
    has_solution: bool = False
    has_tests: bool = False
    harbor_format: bool = True
    subarea: str = "general"
    origin_digest: str = ""

    def __post_init__(self) -> None:
        self.origin_digest = self.origin_digest or self.digest


HOST_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
ENV_FILE = re.compile(r"^\s*env_file\s*:", re.M)


def needs_from_host(path: Path) -> list[str]:
    """What a task reads from the machine it is run on, rather than ships: an env file its compose file loads, or a
    variable from the host in its task.toml or compose file (Compose's own aside). Such a task needs credentials or
    files that the person running it must supply, such as a token for a gated dataset: anyone else cannot run it as it
    is, so it is not public. [] when it needs nothing."""
    files = [path / "task.toml", *sorted((path / "environment").glob("*compose*.y*ml"))]
    found: set[str] = set()
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        text = "\n".join(line.split("#", 1)[0] for line in text.splitlines())      # what comments mention does not count
        found |= {v for v in HOST_VARIABLE.findall(text) if not v.startswith("COMPOSE_")}
        if ENV_FILE.search(text):
            found.add(f"the env file {f.name} loads")
    return sorted(found)


def digest_dir(path: Path) -> str:
    """Content digest of a directory.

    TODO: switch to Harbor's own task digest, so that IDs do not change at release
    (DESIGN.md Section 7). This stand-in hashes relative paths and file contents.
    """
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(str(f.relative_to(path)).encode())
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()
