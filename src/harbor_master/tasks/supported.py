"""The benchmarks an index may curate: what its signed records list, and reading a new one from its repository."""
from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path

REQUIRED = ("id", "name", "version", "released", "github", "ref", "commit", "tasks")


def supported(index_dir: Path) -> list[dict]:
    from ..ledger import benchmarks
    return benchmarks.entries(index_dir)


def find(index_dir: Path, name: str) -> dict | None:
    name = name.split("/")[-1].lower()
    return next((e for e in supported(index_dir) if e["id"].lower() == name), None)


def fetch(entry: dict, sources: Path) -> Path:
    """Clone the benchmark at its pinned commit, or check an existing clone is at it. Returns the tasks folder."""
    repo = sources / entry["id"]
    git = lambda *a, cwd=repo: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)  # noqa: E731
    if not (repo / ".git").exists():
        sources.mkdir(parents=True, exist_ok=True)
        address = entry["github"] if Path(entry["github"]).exists() else entry["github"] + ".git"      # a folder is cloned as it is
        git("clone", "--quiet", "--depth", "1", "--branch", entry["ref"], address, str(repo), cwd=sources)
    head = git("rev-parse", "HEAD").stdout.strip()
    if head != entry["commit"]:         # pinned to a branch that has moved on: ask for the commit itself
        got = subprocess.run(["git", "fetch", "--quiet", "--depth", "1", "origin", entry["commit"]], cwd=repo, capture_output=True)
        if got.returncode == 0 and subprocess.run(["git", "checkout", "--quiet", "--detach", entry["commit"]], cwd=repo,
                                                  capture_output=True).returncode == 0:
            head = git("rev-parse", "HEAD").stdout.strip()
    if head != entry["commit"]:
        raise ValueError(f"{repo} is at {head[:10]}, not the pinned commit {entry['commit'][:10]} ({entry['ref']}). "
                         "Remove that folder and submit again.")
    return repo / entry["tasks"]


# ----- adding one: the owner gives an address, and the entry is worked out from the repository -----

LICENSES = (("Apache License", "Apache-2.0"), ("MIT License", "MIT"), ("BSD 3-Clause", "BSD-3-Clause"), ("BSD 2-Clause", "BSD-2-Clause"),
            ("Attribution-NonCommercial 4.0", "CC-BY-NC-4.0"), ("Attribution 4.0", "CC-BY-4.0"), ("CC0 1.0", "CC0-1.0"),
            ("GNU GENERAL PUBLIC LICENSE", "GPL"), ("Mozilla Public License", "MPL-2.0"))


def pin(url: str, ref: str = "") -> tuple[str, str]:
    """The release to pin, and its commit: the tag or branch that was named, else the newest tag, else the main branch."""
    try:
        out = subprocess.run(["git", "ls-remote", "--symref", url], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise ValueError(f"{url} did not answer.") from None
    if out.returncode:
        raise ValueError(f"Could not read {url}: {(out.stderr.strip().splitlines() or ['git failed'])[-1]}")
    rows = [line.split("\t") for line in out.stdout.splitlines() if "\t" in line]
    refs = {name: sha for sha, name in rows if not sha.startswith("ref:")}
    tags = {n[10:]: refs.get(n + "^{}", sha) for n, sha in refs.items() if n.startswith("refs/tags/") and not n.endswith("^{}")}
    heads = {n[11:]: sha for n, sha in refs.items() if n.startswith("refs/heads/")}
    if ref:
        if ref not in tags and ref not in heads:
            raise ValueError(f"{url} has no tag or branch called {ref}. It has: {', '.join([*tags, *heads][:12]) or 'none'}")
        return ref, tags.get(ref) or heads[ref]
    if tags:
        newest = max(tags, key=lambda t: [int(n) for n in re.findall(r"\d+", t)])
        return newest, tags[newest]
    main = next((sha[len("ref: refs/heads/"):] for sha, name in rows if name == "HEAD" and sha.startswith("ref: refs/heads/")), "")
    main = main if main in heads else next((h for h in ("main", "master") if h in heads), next(iter(heads), ""))
    if not main:
        raise ValueError(f"{url} has no tag and no branch.")
    return main, heads[main]


def describe(url: str, ref: str, commit: str, sources: Path, id_: str = "") -> dict:
    """The entry for a benchmark, read from its repository at the pinned commit: where its tasks are, and in what format."""
    name = url.rstrip("/").removesuffix(".git").split("/")[-1]
    entry = {"id": id_ or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"), "name": name,
             "version": ref.lstrip("v") if re.search(r"\d", ref) else commit[:7], "released": "",
             "github": url.rstrip("/").removesuffix(".git"), "ref": ref, "commit": commit, "tasks": ".", "format": "harbor"}
    repo = fetch(entry, sources)
    entry["released"] = subprocess.run(["git", "log", "-1", "--format=%cs"], cwd=repo, capture_output=True, text=True).stdout.strip()
    homes = Counter(str(p.parent.parent.relative_to(repo)) for p in repo.rglob("task.toml") if ".git" not in p.parts)
    if homes:
        entry["tasks"], entry["task_count"] = homes.most_common(1)[0]
    else:
        entry["format"] = "other"       # not Harbor task folders: the first run builds an adapter for it
    for file in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
        if (repo / file).is_file():
            head = (repo / file).read_text(errors="replace")[:600]
            entry["license"] = next((spdx for words, spdx in LICENSES if words.lower() in head.lower()), "")
            break
    return {k: v for k, v in entry.items() if v != ""}
