"""Tools for developing Harbor-Master itself."""
from __future__ import annotations

import random
from pathlib import Path

import tomli_w
import typer

from .common import console, dev_app


AREAS = ["docking", "property prediction", "reaction outcome", "retrosynthesis", "spectra"]


@dev_app.command("fake-benchmark")
def fake_benchmark(name: str, count: int, directory: Path = typer.Option(Path("fake"), "--dir"), seed: int = 0):
    """Write a synthetic Harbor-format benchmark, for trying the pipeline without real tasks."""
    rng = random.Random(f"{name}-{seed}")
    root = directory / name
    for i in range(count):
        d = root / f"t{i:04d}"
        (d / "tests").mkdir(parents=True, exist_ok=True)
        (d / "environment").mkdir(exist_ok=True)
        area = rng.choice(AREAS)
        x = rng.random()
        body = f"Task {i} of {name}, on {area}. Write and run code in the container to produce results.csv."
        if x < 0.10:
            body = f"Task {i}: a multiple choice question on {area}. Choose one of (A) to (D)."
        elif x < 0.15:
            body += " The write-up is judged by a rubric."
        elif x < 0.18:
            body += " Scoring needs a GPU run."
        (d / "instruction.md").write_text(body + "\n")
        (d / "task.toml").write_text(tomli_w.dumps({"schema_version": "1.4", "task": {"name": f"{name}/t{i:04d}"},
                                                    "metadata": {"field": area}}))
        (d / "tests" / "test.sh").write_text(f"#!/bin/bash\n# synthetic verifier {i}\n")
        (d / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n")
        if rng.random() > 0.06:
            (d / "solution").mkdir(exist_ok=True)
            (d / "solution" / "solve.sh").write_text(f"#!/bin/bash\n# synthetic reference solution {i}\n")
    console.print(f"Wrote {count} synthetic tasks to {root}. Register with: hm submit {root}")
