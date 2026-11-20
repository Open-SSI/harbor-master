"""A made-up benchmark, for the demos, the tests and the pictures."""
from __future__ import annotations

import random
from pathlib import Path

import tomli_w


ABOUT = ("Made-up computational chemistry tasks, for trying Harbor-Master. In each one an agent writes and runs code in a "
         "container and leaves a results file, which a script then checks against a reference. Nothing in it is real.")
# category -> (what its tasks are about, the tasks: a short title and what is asked)
AREAS = {
    "docking": ("Placing small molecules in a protein pocket and scoring how well they bind.", [
        ("Rank ligands against a kinase pocket", "Dock 40 candidate ligands into the ATP pocket of a kinase and rank them by predicted binding affinity."),
        ("Recover a known binding pose", "Redock a co-crystallised inhibitor into its protease and report how far the best pose is from the crystal pose."),
        ("Screen a fragment library", "Dock a library of 200 fragments against an allosteric site and return the ten most promising ones.")]),
    "property prediction": ("Predicting a measurable property of a molecule from its structure.", [
        ("Predict aqueous solubility", "Train a model on measured solubilities and predict the solubility of 150 held-out molecules."),
        ("Estimate melting points", "Predict the melting points of a set of organic crystals and report the mean absolute error."),
        ("Flag likely toxic compounds", "Classify 300 compounds as toxic or not from their structures, on an assay the model has not seen.")]),
    "reaction outcome": ("Saying what a reaction makes, and how much of it.", [
        ("Predict the major product", "Given the reactants and reagents of 80 reactions, predict the major product of each."),
        ("Predict cross-coupling yields", "Predict the yield of 120 palladium-catalysed couplings from their substrates, ligands and bases."),
        ("Pick the selective site", "For 60 substituted aromatics, predict which position a halogenation will favour.")]),
    "retrosynthesis": ("Working backwards from a target molecule to things that can be bought.", [
        ("Plan a route to a drug-like target", "Propose a synthesis route of at most six steps from purchasable building blocks to a given target."),
        ("Find the key disconnection", "For 50 targets, name the bond whose disconnection gives the shortest known route."),
        ("Rank routes by cost", "Given three candidate routes to one target, rank them by the cost of their starting materials.")]),
    "spectra": ("Reading a structure, or a property, out of a measured spectrum.", [
        ("Assign an NMR spectrum", "Assign every peak of a proton NMR spectrum to the hydrogens of a given molecule."),
        ("Identify a compound from its mass spectrum", "Pick the right structure among 20 candidates from a fragmentation mass spectrum."),
        ("Predict infrared bands", "Predict the positions of the strongest infrared absorption bands of 100 small molecules.")]),
}


def fake_benchmark(name: str, count: int, directory: Path = Path("fake"), seed: int = 0) -> Path:
    """Write a synthetic Harbor-format benchmark, for trying the pipeline without real tasks."""
    rng = random.Random(f"{name}-{seed}")
    root = directory / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "benchmark.toml").write_text(tomli_w.dumps({         # what the benchmark is, for the summaries a person is shown
        "name": " ".join(w.capitalize() for w in name.replace("_", "-").split("-")), "description": ABOUT,
        "categories": {area: what for area, (what, _) in AREAS.items()}}))
    for i in range(count):
        d = root / f"t{i:04d}"
        (d / "tests").mkdir(parents=True, exist_ok=True)
        (d / "environment").mkdir(exist_ok=True)
        area = rng.choice(list(AREAS))
        title, asked = rng.choice(AREAS[area][1])
        x = rng.random()
        body = f"{asked} Write and run code in the container, and leave your answer in results.csv."
        if x < 0.10:
            title, asked = f"A question on {area}", f"Answer a multiple choice question on {area}."
            body = f"{asked} Choose one of (A) to (D)."
        elif x < 0.15:
            body += " The write-up is judged by a rubric."
        elif x < 0.18:
            body += " Scoring needs a GPU run."
        (d / "instruction.md").write_text(f"# {title}\n\n{body}\n")
        (d / "task.toml").write_text(tomli_w.dumps({"schema_version": "1.4",
                                                    "task": {"name": f"{name}/t{i:04d}", "title": title, "description": asked},
                                                    "metadata": {"field": area}}))
        (d / "tests" / "test.sh").write_text(f"#!/bin/bash\n# synthetic verifier {i}\ntest -s results.csv\n")
        (d / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n")
        if rng.random() > 0.06:
            (d / "solution").mkdir(exist_ok=True)
            (d / "solution" / "solve.sh").write_text(f"#!/bin/bash\n# synthetic reference solution {i}\n")
    return root
