"""The benchmarks an index may curate: what its signed records list, and reading a new one from its repository."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REQUIRED = ("id", "name", "version", "released", "github", "ref", "commit", "tasks")
PINNED = ".hm-pinned"       # in a source folder fetched as an archive: the commit it holds


def supported(index_dir: Path) -> list[dict]:
    from ..ledger import benchmarks
    return benchmarks.entries(index_dir)


def find(index_dir: Path, name: str) -> dict | None:
    name = name.split("/")[-1].lower()
    return next((e for e in supported(index_dir) if e["id"].lower() == name), None)


def _github(url: str) -> tuple[str, str] | None:
    m = re.fullmatch(r"https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?", url.strip())
    return (m.group(1), m.group(2)) if m else None


def _huggingface(url: str) -> str | None:
    """"ORG/NAME" for a Hugging Face dataset's address, else None."""
    m = re.fullmatch(r"https?://huggingface\.co/datasets/([\w.-]+/[\w.-]+?)(?:\.git)?/?", url.strip())
    return m.group(1) if m else None


def _dataset(name: str, commit: str, repo: Path) -> bool:
    """A Hugging Face dataset's files at one revision: each file as it is, not the pointer git keeps for a large one."""
    try:
        with urllib.request.urlopen(f"https://huggingface.co/api/datasets/{name}/revision/{commit}", timeout=60) as r:
            files = [s["rfilename"] for s in json.load(r).get("siblings", [])]
        with tempfile.TemporaryDirectory(dir=repo.parent) as tmp:
            for rel in files:
                target = (Path(tmp) / "d" / rel).resolve()
                if Path(tmp).resolve() not in target.parents:
                    raise OSError(f"{rel} is outside the dataset")
                target.parent.mkdir(parents=True, exist_ok=True)
                with urllib.request.urlopen(f"https://huggingface.co/datasets/{name}/resolve/{commit}/{rel}", timeout=120) as response, \
                        target.open("wb") as out:
                    shutil.copyfileobj(response, out, 1 << 20)
            shutil.move(str(Path(tmp) / "d"), str(repo))
        (repo / PINNED).write_text(commit + "\n")
        return True
    except (OSError, ValueError, KeyError):
        shutil.rmtree(repo, ignore_errors=True)
        return False


def _archive(owner: str, name: str, commit: str, repo: Path) -> bool:
    """The repository's files at one commit, from GitHub's archive of it: much faster than a clone of a large
    repository, and it holds exactly the pinned commit. False when it could not be had."""
    url = f"https://codeload.github.com/{owner}/{name}/tar.gz/{commit}"
    try:
        with tempfile.TemporaryDirectory(dir=repo.parent) as tmp:
            pack = Path(tmp) / "source.tar.gz"
            with urllib.request.urlopen(url, timeout=120) as response, pack.open("wb") as out:
                shutil.copyfileobj(response, out, 1 << 20)
            with tarfile.open(pack) as tar:
                tar.extractall(Path(tmp) / "x", filter="data")
            top = next((Path(tmp) / "x").iterdir())         # GitHub puts everything in one folder, NAME-COMMIT
            shutil.move(str(top), str(repo))
        (repo / PINNED).write_text(commit + "\n")
        return True
    except (OSError, tarfile.TarError, StopIteration):
        shutil.rmtree(repo, ignore_errors=True)
        return False


def _at(repo: Path) -> str:
    """The commit a source folder holds, or "" when it holds none: a clone that was cut off holds none."""
    if (repo / PINNED).is_file():
        return (repo / PINNED).read_text().strip()
    got = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=repo, capture_output=True, text=True)
    return got.stdout.strip() if got.returncode == 0 else ""


def fetch(entry: dict, sources: Path) -> Path:
    """The benchmark at its pinned commit, or a check that an existing copy is at it. Returns the tasks folder.
    A GitHub repository is fetched as an archive of the commit, and a Hugging Face dataset file by file at its revision.
    Anything else, or a download that cannot be had, is cloned."""
    repo = sources / entry["id"]
    git = lambda *a, cwd=repo: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)  # noqa: E731
    if repo.exists() and not _at(repo):         # a fetch that was cut off: start again
        shutil.rmtree(repo)
    if not repo.exists():
        sources.mkdir(parents=True, exist_ok=True)
        where, dataset = _github(entry["github"]), _huggingface(entry["github"])
        got = (_archive(*where, entry["commit"], repo) if where else
               _dataset(dataset, entry["commit"], repo) if dataset else False)
        if not got and dataset:
            raise ValueError(f"Could not download the dataset {dataset} at {entry['commit'][:10]} from Hugging Face.")
        if not got:
            address = entry["github"] if Path(entry["github"]).exists() else entry["github"] + ".git"      # a folder is cloned as it is
            git("clone", "--quiet", "--depth", "1", "--branch", entry["ref"], address, str(repo), cwd=sources)
    head = _at(repo)
    if head != entry["commit"] and (repo / ".git").exists():      # pinned to a branch that has moved on: ask for the commit itself
        got = subprocess.run(["git", "fetch", "--quiet", "--depth", "1", "origin", entry["commit"]], cwd=repo, capture_output=True)
        if got.returncode == 0 and subprocess.run(["git", "checkout", "--quiet", "--detach", entry["commit"]], cwd=repo,
                                                  capture_output=True).returncode == 0:
            head = _at(repo)
    if head != entry["commit"]:
        raise ValueError(f"{repo} is at {head[:10] or 'no commit'}, not the pinned commit {entry['commit'][:10]} ({entry['ref']}). "
                         "Remove that folder and submit again.")
    return repo / entry["tasks"]


def committed(entry: dict, repo: Path) -> str:
    """The date of the pinned commit, as YYYY-MM-DD: from the clone, or from GitHub for an archive. "" when unknown."""
    if (repo / ".git").exists():
        return subprocess.run(["git", "log", "-1", "--format=%cs"], cwd=repo, capture_output=True, text=True).stdout.strip()
    where, dataset = _github(entry["github"]), _huggingface(entry["github"])
    url = (f"https://api.github.com/repos/{where[0]}/{where[1]}/commits/{entry['commit']}" if where else
           f"https://huggingface.co/api/datasets/{dataset}/revision/{entry['commit']}" if dataset else "")
    if not url:
        return ""
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.load(r)
        return (data["commit"]["committer"]["date"] if where else data["lastModified"])[:10]
    except (OSError, KeyError, ValueError):
        return ""


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


NOT_TASKS = {"ci", "ci_checks", "test", "tests", "test-tasks", "fixtures", "archive", "archived", "example", "examples",
             "template", "templates", "docs", ".github"}       # folders that hold task folders which are not the benchmark


def tasks_root(repo: Path) -> tuple[str, int] | None:
    """Where a repository keeps its Harbor tasks, and how many: the folder with a Harbor dataset manifest (dataset.toml)
    over the most tasks, or else the common folder of every task outside test fixtures, archives and examples. Tasks
    may sit at any depth under it, such as tasks/DOMAIN/FIELD/TASK. None when the repository has no task folders."""
    tasks = [p.parent for p in repo.rglob("task.toml") if ".git" not in p.parts]
    if not tasks:
        return None
    manifests = [m.parent for m in repo.rglob("dataset.toml") if ".git" not in m.parts]
    counted = [(sum(1 for t in tasks if m in t.parents), m) for m in manifests]
    counted = [(n, m) for n, m in counted if n]
    if counted:
        n, folder = max(counted, key=lambda c: c[0])
        return str(folder.relative_to(repo)) or ".", n
    kept = [t for t in tasks if not NOT_TASKS & {part.lower() for part in t.relative_to(repo).parts}] or tasks
    if len(kept) == 1:
        return str(kept[0].parent.relative_to(repo)) or ".", 1
    common = Path(os.path.commonpath([str(t.parent) for t in kept]))
    return str(common.relative_to(repo)) or ".", len(kept)


CARD_LICENSES = {"cc0-1.0": "CC0-1.0", "cc-by-4.0": "CC-BY-4.0", "cc-by-sa-4.0": "CC-BY-SA-4.0", "cc-by-nc-4.0": "CC-BY-NC-4.0",
                 "mit": "MIT", "apache-2.0": "Apache-2.0", "bsd-3-clause": "BSD-3-Clause", "odc-by": "ODC-By"}


def describe(url: str, ref: str, commit: str, sources: Path, id_: str = "") -> dict:
    """The entry for a benchmark, read from its repository at the pinned commit: where its tasks are, and in what format."""
    name = url.rstrip("/").removesuffix(".git").split("/")[-1]
    entry = {"id": id_ or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"), "name": name,
             "version": ref.lstrip("v") if re.search(r"\d", ref) else commit[:7], "released": "",
             "github": url.rstrip("/").removesuffix(".git"), "ref": ref, "commit": commit, "tasks": ".", "format": "harbor"}
    repo = fetch(entry, sources)
    entry["released"] = committed(entry, repo)
    found = tasks_root(repo)
    if found:
        entry["tasks"], entry["task_count"] = found
    else:
        entry["format"] = "other"       # not Harbor task folders: the first run builds an adapter for it
    for file in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
        if (repo / file).is_file():
            head = (repo / file).read_text(errors="replace")[:600]
            entry["license"] = next((spdx for words, spdx in LICENSES if words.lower() in head.lower()), "")
            break
    else:                   # a Hugging Face dataset card names its licence in its front matter
        card = (repo / "README.md").read_text(errors="replace")[:4000] if (repo / "README.md").is_file() else ""
        named = re.search(r"\A---\s*\n.*?^license:\s*['\"]?([\w.+-]+)", card, flags=re.S | re.M)
        if named:
            entry["license"] = CARD_LICENSES.get(named.group(1).lower(), named.group(1))
    return {k: v for k, v in entry.items() if v != ""}
