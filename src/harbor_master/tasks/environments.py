"""Whether a benchmark's tasks can run here at all: the container images they name must be public.

Checked right after a benchmark is fetched, before any model is asked to adapt it and before anything is screened.
A benchmark whose tasks run in images nobody can pull (published "on request", or private) cannot be curated, however
good its tasks are. The images are found where benchmarks name them: Dockerfiles (FROM), compose files (image:), and
task records ("docker_image": ...). A few, spread over the distinct repositories, are looked up in their registry
without being pulled.
"""
from __future__ import annotations

import re
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.I | re.M)
NAMED = re.compile(r'"(?:docker_image|image|base_image|container_image)"\s*:\s*"([^"]+)"')
COMPOSE = re.compile(r"^\s*image:\s*[\"']?([^\s\"']+)", re.M)
PLACEHOLDER = re.compile(r"[{}$<>]|__[A-Z]|@(?!sha256:)")     # a template's {{IMAGE}} or @IMAGE@; an image pinned by digest is real
SCANNED = (".json", ".jsonl", ".yaml", ".yml", ".toml")
BUILT = re.compile(r'"needs_build"\s*:\s*true', re.I)    # a task record whose image its harness builds, not pulls
MAX_BYTES = 2 << 30          # read at most this much of a benchmark's files


def _image(ref: str) -> str:
    """The image a reference names, or "" for none: `scratch`, or a template's placeholder such as {{IMAGE}}."""
    ref = ref.strip()
    return "" if not ref or ref.lower() == "scratch" or PLACEHOLDER.search(ref) else ref


def references(root: Path, built: set | None = None) -> Counter:
    """Every image the benchmark's files name, and how often. A build stage named in the same Dockerfile is not an image.
    The images of task records that say their harness builds them ("needs_build": true) are added to `built`."""
    found: Counter = Counter()
    left = MAX_BYTES
    files = sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts
                   and (p.name.startswith("Dockerfile") or p.name.startswith(("docker-compose", "compose")) or p.suffix in SCANNED))
    for path in files:
        if left <= 0:
            break
        left -= path.stat().st_size
        try:
            with path.open(errors="replace") as fh:
                if path.name.startswith("Dockerfile"):
                    text = fh.read()
                    stages = {m.group(2).lower() for m in FROM.finditer(text) if m.group(2)}
                    found.update(i for m in FROM.finditer(text) if (i := _image(m.group(1))) and i.lower() not in stages)
                    continue
                for line in fh:                 # line by line: a task file can be hundreds of megabytes
                    for pattern in (NAMED, COMPOSE):
                        named = [i for m in pattern.finditer(line) if (i := _image(m.group(1)))]
                        found.update(named)
                        if built is not None and named and BUILT.search(line):
                            built.update(named)
        except OSError:
            continue
    return found


def public(image: str, timeout: float = 45) -> bool | None:
    """Whether the registry serves this image to anyone: looked up, not pulled. None when it could not be asked."""
    try:
        r = subprocess.run(["docker", "manifest", "inspect", image], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode == 0:
        return True
    said = (r.stderr + r.stdout).lower()
    if any(w in said for w in ("denied", "unauthorized", "not found", "no such manifest", "manifest unknown", "does not exist")):
        return False
    return None


def repository(image: str) -> str:
    """mswebench/pyscf_m_pyscf for mswebench/pyscf_m_pyscf:pr-1426: one image per task often shares one repository.
    A registry's port, as in localhost:5000/name, is not a tag."""
    name = image.split("@")[0]
    slash = name.rfind("/")
    colon = name.rfind(":")
    return name[:colon] if colon > slash else name


def sample(found: Counter, n: int = 8) -> list[str]:
    """Up to `n` images, one per repository, the most used repositories first. A repository's images are taken to share
    whether they are public, as the tags of one repository do."""
    uses: Counter = Counter()
    first: dict[str, str] = {}
    for image, count in found.items():
        uses[repository(image)] += count
        first.setdefault(repository(image), image)
    return [first[repo] for repo, _ in uses.most_common(n)]


def check(root: Path, n: int = 8) -> dict:
    """What the benchmark's files name, and which of it can be pulled: {"named", "uses", "public", "private", "unknown",
    "private_uses", "public_uses"}. The uses are counted over whole repositories, so they stand for how many tasks."""
    built: set = set()
    found = references(root, built)
    picked = sample(found, n)
    with ThreadPoolExecutor(max_workers=8) as pool:
        said = dict(zip(picked, pool.map(public, picked)))
    # An image refused by the registry is not refused for good when it is built here: its harness says so, or its name has
    # no registry and no owner (astabench-sandbox, a tag given to a local build: an official image would have been found).
    local = {i for i, v in said.items() if v is False and "/" not in repository(i)}
    built |= local
    built_repos = {repository(i) for i in built}
    said = {i: (None if v is False and repository(i) in built_repos else v) for i, v in said.items()}
    uses: Counter = Counter()
    for image, count in found.items():
        uses[repository(image)] += count
    count = lambda flag: sum(uses[repository(i)] for i, v in said.items() if v is flag)  # noqa: E731
    return {"named": len(found), "uses": sum(found.values()), "built": len(built), "public": [i for i, v in said.items() if v is True],
            "private": [i for i, v in said.items() if v is False], "unknown": [i for i, v in said.items() if v is None],
            "private_uses": count(False), "public_uses": count(True)}


def verdict(result: dict) -> str:
    """Why the benchmark cannot be curated here, or "": every image looked up is refused by its registry."""
    if result["private"] and not result["public"] and not result["unknown"]:
        return (f"Its tasks run in container images that are not public: every one looked up was refused by its registry "
                f"({', '.join(result['private'][:3])}). Its files name {result['named']} distinct images, {result['uses']} times. "
                "Without them the tasks cannot run here.")
    return ""


def partly(result: dict) -> str:
    """When some of the tasks' images are refused and others are public: which, said for whoever adapts or screens it."""
    if not (result["private"] and result["public"]):
        return ""
    return (f"Some of its tasks run in container images that are not public: {', '.join(result['private'][:6])} were refused by "
            f"their registry, standing for about {result['private_uses']} task records; {', '.join(result['public'][:3])} "
            f"can be pulled, for about {result['public_uses']}. A task whose image cannot be pulled cannot run here: leave such "
            "tasks out.")
