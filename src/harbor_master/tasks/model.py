"""A task and its content digest."""
from __future__ import annotations

import hashlib
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
