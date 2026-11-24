"""Adapters: code that turns a benchmark in another format into Harbor tasks (DESIGN.md Section 4, stage 2).

An adapter has the layout Harbor's own adapters have, so one written here can be offered to
Harbor later, and one written for Harbor can be used here:

    adapters/<id>/
      README.md  adapter_metadata.json  parity_experiment.json  pyproject.toml
      src/<package>/
        __init__.py  main.py  adapter.py
        task-template/{task.toml, instruction.md, environment/Dockerfile, solution/solve.sh, tests/test.sh}

It lives in the index's repository and is locked with the protocol, because everyone who
curates a benchmark must get the same tasks from it. Its code runs in a container with no
network and the standard library only, with the benchmark's pinned source mounted read-only.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from .intake import find_tasks
from .model import digest_dir

FOLDER = "adapters"
IMAGE = "python:3.12-slim"
FLAGS = ("--source-dir", "--output-dir", "--limit", "--overwrite", "--task-ids")      # Harbor's own flags, and the pinned source
TEMPLATE = ("task.toml", "instruction.md", "environment/Dockerfile", "solution/solve.sh", "tests/test.sh")
TASK_NEEDS = ("task.toml", "instruction.md", "environment/Dockerfile", "tests/test.sh")

MAIN = '''"""Entry point of the adapter. The flags are the ones Harbor's adapters take, plus --source-dir:
the benchmark as it is at its pinned release, which Harbor-Master fetches and passes in.
Do not change this file.
"""
import argparse
from pathlib import Path

from .adapter import Adapter


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True, help="The benchmark at its pinned release")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory to write generated tasks")
    parser.add_argument("--limit", type=int, default=None, help="Generate only the first N tasks")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing tasks")
    parser.add_argument("--task-ids", nargs="+", default=None, help="Only generate these task IDs")
    args = parser.parse_args()
    Adapter(args.output_dir, source_dir=args.source_dir, limit=args.limit, overwrite=args.overwrite, task_ids=args.task_ids).run()


if __name__ == "__main__":
    main()
'''

ADAPTER = '''"""Turns the benchmark into Harbor tasks. Standard library only, no network, and the same output every time."""
from pathlib import Path

TEMPLATE = Path(__file__).parent / "task-template"


class Adapter:
    def __init__(self, output_dir: Path, source_dir: Path, limit: int | None = None, overwrite: bool = False,
                 task_ids: list[str] | None = None, **kwargs):
        self.output_dir, self.source_dir = output_dir, source_dir
        self.limit, self.overwrite, self.task_ids = limit, overwrite, task_ids

    def run(self) -> None:
        """Write one folder per task into self.output_dir: task.toml, instruction.md, environment/Dockerfile,
        solution/solve.sh and tests/test.sh, read from self.source_dir."""
        raise NotImplementedError("the adapter is not written yet")
'''


def package(adapter_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", adapter_id.lower()).strip("_") or "adapter"


def folder(index_dir: Path, adapter_id: str) -> Path:
    return index_dir / FOLDER / adapter_id


def code_dir(adapter_dir: Path) -> Path | None:
    """The adapter's package: the one folder under src/ that has an adapter.py."""
    found = sorted(p.parent for p in (adapter_dir / "src").glob("*/adapter.py"))
    return found[0] if found else None


def scaffold(adapter_dir: Path, entry: dict) -> Path:
    """Write the fixed parts of an adapter for a benchmark, and stubs for the rest. Returns the package folder."""
    pkg = adapter_dir / "src" / package(entry["id"])
    for rel in TEMPLATE:
        (pkg / "task-template" / rel).parent.mkdir(parents=True, exist_ok=True)
        (pkg / "task-template" / rel).touch()
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text(MAIN)
    if not (pkg / "adapter.py").exists():
        (pkg / "adapter.py").write_text(ADAPTER)
    (adapter_dir / "pyproject.toml").write_text(
        f'[project]\nname = "{entry["id"]}-adapter"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = []\n')
    (adapter_dir / "adapter_metadata.json").write_text(json.dumps([{
        "adapter_name": entry["id"], "adapter_builders": [],
        "original_benchmark": [{"split": "test", "size": entry.get("task_count"), "harness": None, "supported_agents": None,
                                "adaptable": True, "notes": None}],
        "harbor_adapter": [{"split": "test", "adapted_benchmark_size": None, "parity_benchmark_size": None,
                            "parity_sampling_rate": None, "registry_benchmark_size": None, "added_agents": [],
                            "parity_matching_agents": [], "parity_unmatching_agents": None, "parity_costs": None, "notes": None}],
    }], indent=2) + "\n")
    (adapter_dir / "parity_experiment.json").write_text("[]\n")       # no parity run has been made
    (adapter_dir / "README.md").write_text(
        f"## {entry.get('name', entry['id'])} → Harbor adapter\n\nTurns {entry.get('name', entry['id'])} {entry.get('version', '')} "
        f"({entry.get('github', '')}, at `{entry.get('commit', '')[:10]}`) into Harbor tasks.\n\n"
        "It reads the benchmark from `--source-dir` and writes one task folder per task into `--output-dir`. It uses the "
        "standard library only, needs no network, and writes the same tasks every time. No parity run against the "
        "original harness has been made.\n")
    return pkg


def digest(adapter_dir: Path) -> str:
    return digest_dir(adapter_dir)[:16]


def structure(adapter_dir: Path) -> list[str]:
    """What is wrong with an adapter before any of it is run."""
    pkg = code_dir(adapter_dir)
    if pkg is None:
        return ["no src/<package>/adapter.py"]
    found = [f"{rel} is missing" for rel in ("main.py", *(f"task-template/{t}" for t in TEMPLATE)) if not (pkg / rel).exists()]
    if (pkg / "main.py").exists():
        text = (pkg / "main.py").read_text()
        found += [f"main.py does not take {flag}" for flag in FLAGS if f'"{flag}"' not in text and f"'{flag}'" not in text]
    for f in sorted(pkg.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError as err:
            found.append(f"{f.relative_to(pkg)} does not parse: {err.msg} at line {err.lineno}")
            continue
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
            for name in names:
                if name.split(".")[0] not in sys.stdlib_module_names:
                    found.append(f"{f.relative_to(pkg)} imports {name}: an adapter may use the standard library only")
    project = adapter_dir / "pyproject.toml"
    if project.exists():
        try:
            if tomllib.loads(project.read_text()).get("project", {}).get("dependencies"):
                found.append("pyproject.toml lists dependencies: an adapter may use the standard library only")
        except tomllib.TOMLDecodeError:
            found.append("pyproject.toml does not parse")
    return found


def generate(adapter_dir: Path, source_dir: Path, out_dir: Path, sandbox: bool = True, timeout: float = 1800) -> tuple[bool, str]:
    """Run the adapter. Returns whether it finished, and the end of what it printed.

    In the sandbox it runs in a container with no network, the adapter and the source read-only,
    and only the output folder to write to. Without it, it runs on this machine as it is.
    """
    pkg = code_dir(adapter_dir)
    if pkg is None:
        return False, "no src/<package>/adapter.py"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    module = f"{pkg.name}.main"
    if sandbox:
        cmd = ["docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
               "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONHASHSEED=0",
               "-v", f"{adapter_dir.resolve()}:/adapter:ro", "-v", f"{source_dir.resolve()}:/source:ro",
               "-v", f"{out_dir.resolve()}:/out", "-w", "/adapter/src", IMAGE,
               "python", "-B", "-m", module, "--source-dir", "/source", "--output-dir", "/out", "--overwrite"]
        cwd = None
    else:
        cmd = [sys.executable, "-B", "-m", module, "--source-dir", str(source_dir.resolve()), "--output-dir", str(out_dir.resolve()), "--overwrite"]
        cwd = pkg.parent
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"})
    except subprocess.TimeoutExpired:
        return False, f"did not finish in {int(timeout)} seconds"
    except FileNotFoundError as err:
        return False, f"could not start: {err}"
    return r.returncode == 0, (r.stdout + r.stderr)[-2000:].strip()


def digests(out_dir: Path) -> dict[str, str]:
    return {d.name: digest_dir(d) for d in find_tasks(out_dir)} if out_dir.is_dir() else {}


def bare_image(dockerfile: Path) -> bool:
    """An environment that is a base image and nothing more: no file copied in, nothing installed or fetched."""
    if not dockerfile.is_file():
        return False
    lines = [ln.strip().split(None, 1)[0].upper() for ln in dockerfile.read_text(errors="replace").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    return bool(lines) and all(word in ("FROM", "WORKDIR", "ENV", "ARG", "LABEL", "USER", "SHELL", "CMD", "ENTRYPOINT")
                               for word in lines)


def fixed_answer(solve: Path) -> bool:
    """A reference solution that only writes a fixed answer: every line that does something is an echo or a printf."""
    if not solve.is_file():
        return False
    acts = [ln.strip() for ln in solve.read_text(errors="replace").splitlines()
            if ln.strip() and not ln.strip().startswith("#") and not ln.strip().startswith(("set ", "mkdir "))]
    return bool(acts) and all(ln.startswith(("echo ", "printf ")) for ln in acts)


def inspect(out_dir: Path, expected: int | None = None) -> tuple[list[str], list[str]]:
    """What is wrong with the generated tasks, and what is only worth knowing."""
    tasks = find_tasks(out_dir) if out_dir.is_dir() else []
    if not tasks:
        return ["it generated no task: no folder with a task.toml"], []
    problems, notes = [], []
    for d in tasks:
        missing = [rel for rel in TASK_NEEDS if not (d / rel).is_file()]
        if missing:
            problems.append(f"{d.name}: no {', '.join(missing)}")
        elif not (d / "instruction.md").read_text().strip():
            problems.append(f"{d.name}: instruction.md is empty")
        else:
            try:
                tomllib.loads((d / "task.toml").read_text())
            except tomllib.TOMLDecodeError as err:
                problems.append(f"{d.name}: task.toml does not parse ({err})")
        if len(problems) >= 12:
            problems.append("and more: only the first are shown")
            break
    if len({d.name for d in tasks}) != len(tasks):
        problems.append("two tasks have the same folder name")
    if expected and len(tasks) != expected:
        problems.append(f"it generated {len(tasks)} tasks, and the benchmark list says {expected}")
    bare = sum(1 for d in tasks if bare_image(d / "environment" / "Dockerfile"))
    fixed = sum(1 for d in tasks if fixed_answer(d / "solution" / "solve.sh"))
    if bare:
        notes.append(f"{bare} of {len(tasks)} tasks run in a bare base image that adds no file and installs nothing")
    if fixed:
        notes.append(f"{fixed} of {len(tasks)} reference solutions write a fixed answer and read no input")
    without = sum(1 for d in tasks if not ((d / "solution").is_dir() and any((d / "solution").iterdir())))
    if without:
        notes.append(f"{without} of {len(tasks)} tasks have no reference solution, so the admission gates will drop them")
    return problems, notes


def check(adapter_dir: Path, source_dir: Path, work: Path, expected: int | None = None, sandbox: bool = True) -> tuple[list[str], list[str], Path]:
    """Everything that can be checked without a model or an agent: the layout, a run, the tasks it writes,
    and that a second run writes the same ones. Returns the problems, the notes, and the generated tasks."""
    out = work / "tasks"
    problems = structure(adapter_dir)
    if problems:
        return problems, [], out
    ok, said = generate(adapter_dir, source_dir, out, sandbox)
    if not ok:
        return [f"it failed when run: {said or 'no output'}"], [], out
    problems, notes = inspect(out, expected)
    if not problems:
        again = work / "again"
        ok, said = generate(adapter_dir, source_dir, again, sandbox)
        first, second = digests(out), digests(again)
        if not ok or first != second:
            changed = sorted(k for k in first.keys() | second.keys() if first.get(k) != second.get(k))
            problems.append(f"a second run did not write the same tasks ({len(changed)} differ, such as {', '.join(changed[:3])}): "
                            "an adapter must write the same output every time")
        shutil.rmtree(again, ignore_errors=True)
    return problems, notes, out
