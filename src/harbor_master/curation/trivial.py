"""Submissions that do no work, written by code, which a verifier must reject (DESIGN.md Section 4, stage 3).

A no-op leaves nothing behind. These leave the files the task asks for, with nothing of value in
them: empty, a placeholder, or a copy of a file that was already there. Each replaces the reference
solution's entry point and is run the way a mutant is.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

DATA = "csv|tsv|json|jsonl|txt|npy|npz|yaml|yml|parquet|pkl|h5|hdf5|out|dat|xml|html|md|png|pdf|sdf|pdb|fasta|xyz|cif"
NAMED = re.compile(rf"(?<![\w/.\-])((?:/|\./)?(?:[\w\-]+/)*[\w\-][\w.\-]*\.(?:{DATA}))(?![\w\-])")
PLACEHOLDER = {"json": "{}", "jsonl": "{}", "yaml": "{}", "yml": "{}", "xml": "<a/>", "html": "<html></html>"}
MAX_FILES, MAX_READ = 12, 200_000
ENTRY = "solve.sh"


def outputs(path: Path, instruction: str) -> list[str]:
    """The files a submission must leave: those the instruction names and the tests also name.
    A file the environment already holds is an input, not an output."""
    tests = path / "tests"
    if not tests.is_dir():
        return []
    read = "\n".join(p.read_text(errors="replace") for p in sorted(tests.rglob("*"))
                     if p.is_file() and p.stat().st_size <= MAX_READ)
    checked = set(NAMED.findall(read))
    env = path / "environment"
    given = {p.name for p in env.rglob("*") if p.is_file()} if env.is_dir() else set()
    found = [n for n in dict.fromkeys(NAMED.findall(instruction))
             if (n in checked or any(c.endswith("/" + n.lstrip("./")) for c in checked)) and Path(n).name not in given]
    return found[:MAX_FILES]


def _script(lines: list[str]) -> str:
    return "#!/bin/bash\n" + "\n".join(lines) + "\n"


def submissions(path: Path, instruction: str) -> list[dict]:
    """The trivial submissions for a task, as mutants of its solution. None when it names no output file."""
    outs = outputs(path, instruction)
    if not outs:
        return []
    made = [f"mkdir -p {shlex.quote(str(Path(o).parent))}" for o in outs]
    empty = [f": > {shlex.quote(o)}" for o in outs]
    filled = [f"printf '%s\\n' {shlex.quote(PLACEHOLDER.get(o.rsplit('.', 1)[-1], '0'))} > {shlex.quote(o)}" for o in outs]
    copied = [f"src=$(find {shlex.quote(str(Path(o).parent))} . -maxdepth 2 -type f -name {shlex.quote('*.' + o.rsplit('.', 1)[-1])} "
              f"! -name {shlex.quote(Path(o).name)} 2>/dev/null | head -n 1); "
              f"if [ -n \"$src\" ]; then cp \"$src\" {shlex.quote(o)}; else : > {shlex.quote(o)}; fi" for o in outs]
    files = ", ".join(outs)
    return [
        {"id": "trivial-empty", "kind": "wrong", "what": f"leaves {files} empty", "files": {ENTRY: _script(made + empty)}},
        {"id": "trivial-placeholder", "kind": "wrong", "what": f"writes a placeholder into {files}",
         "files": {ENTRY: _script(made + filled)}},
        {"id": "trivial-copy", "kind": "wrong", "what": f"copies a file that was already there to {files}",
         "files": {ENTRY: _script(made + copied)}},
    ]
