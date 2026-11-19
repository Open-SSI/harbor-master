"""Is the answer within the agent's reach? A scan of the task's own files, by code (DESIGN.md Section 4).

Three things are looked for, none of which needs a model or a container:
- a file of the reference solution that is also in the agent's environment, byte for byte;
- the tests' code, or a file of expected answers, that is also in the environment;
- an image build that reaches out of its folder for solution/ or tests/, or an instruction that
  quotes most of the solution.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

MIN_BYTES = 64                      # shorter files match by chance: an empty __init__.py, a one-line config
MAX_BYTES = 20 * 1024 * 1024        # larger ones are data, and are not read
CODE = {".py", ".sh", ".r", ".jl", ".js", ".ts", ".ipynb"}
ANSWERS = re.compile(r"expected|answer|ground[_-]?truth|gold|reference|solution", re.I)
REACH = re.compile(r"^\s*(?:COPY|ADD)\s+[^\n#]*\.\./(solution|tests)\b", re.I | re.M)
CONTEXT = re.compile(r"^\s*context:\s*[\"']?\.\.[\"']?/?\s*$", re.M)
NONE = "none"


def _files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return [p for p in sorted(folder.rglob("*")) if p.is_file() and "__pycache__" not in p.parts
            and MIN_BYTES <= p.stat().st_size <= MAX_BYTES]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lines(text: str) -> list[str]:
    """The lines of a solution that say something: no blanks, no comments, nothing short."""
    out = (" ".join(line.split()) for line in text.splitlines())
    return [line for line in out if len(line) >= 30 and not line.startswith(("#", "//"))]


def find(path: Path, instruction: str) -> tuple[str, list[str]]:
    """The kind of leak and where it is, or ("none", [])."""
    if not path.is_dir():
        return NONE, []
    env, rel = path / "environment", lambda p: str(p.relative_to(path))
    inside = {_sha(p): rel(p) for p in _files(env)}
    same = [f"{rel(p)} = {inside[h]}" for p in _files(path / "solution") if (h := _sha(p)) in inside]
    if same:
        return "solution_in_image", same
    same = [f"{rel(p)} = {inside[h]}" for p in _files(path / "tests")
            if (p.suffix.lower() in CODE or ANSWERS.search(p.name)) and (h := _sha(p)) in inside]
    if same:
        return "tests_in_image", same
    for f in (p for p in sorted(env.rglob("*")) if p.is_file()) if env.is_dir() else ():
        name = f.name.lower()
        if not name.startswith(("dockerfile", "docker-compose", "compose")) or f.stat().st_size > MAX_BYTES:
            continue
        text = f.read_text(errors="replace")
        reach = REACH.search(text)
        if reach:
            return f"{reach.group(1)}_in_image", [f"{rel(f)} copies ../{reach.group(1)}"]
        if not name.startswith("dockerfile") and CONTEXT.search(text):
            return "solution_in_image", [f"{rel(f)} builds from the task's whole folder"]
    said = " ".join(instruction.split())
    lines = [ln for p in _files(path / "solution") if p.suffix.lower() in CODE | {".txt", ".md", ".json", ".csv"}
             for ln in _lines(p.read_text(errors="replace"))]
    quoted = [ln for ln in lines if ln in said]
    if len(lines) >= 3 and 2 * len(quoted) >= len(lines):
        return "solution_in_instruction", [f"{len(quoted)} of the solution's {len(lines)} lines are in the instruction"]
    return NONE, []
