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
PREVIEW = ".hm-preview"     # in a source folder that holds only the small files, for a draft: a run downloads it all
SMALL = 512 * 1024          # the largest file a draft's light copy holds


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


HUB = "https://hub.harborframework.com/datasets/"


def hub_address(given: str) -> str:
    """The Harbor Hub address of a dataset, from its page's link or from a command that names it, such as
    `harbor dataset download ORG/NAME` or `harbor run -d ORG/NAME`. "" when `given` names none. A bare ORG/NAME stays a
    GitHub repository, as it always was."""
    s = given.strip()
    m = (re.match(r"(?:https?://)?hub\.harborframework\.com/datasets/([\w.-]+)/([\w.-]+)", s) or
         re.match(r"harbor\s+datasets?\s+download\s+(?:\S+\s+)*?([\w.-]+)/([\w.-]+?)(?:@\S*)?(?:\s|$)", s) or
         re.match(r"harbor\s+run\s+(?:.*\s)?(?:-d|--dataset)[\s=]+([\w.-]+)/([\w.-]+?)(?:@\S*)?(?:\s|$)", s))
    return f"{HUB}{m.group(1)}/{m.group(2)}" if m else ""


def _hub(url: str) -> tuple[str, str] | None:
    """(ORG, NAME) for a Harbor Hub dataset's address, else None."""
    m = re.fullmatch(re.escape(HUB) + r"([\w.-]+)/([\w.-]+)/?", url.strip())
    return (m.group(1), m.group(2)) if m else None


def _hub_version(org: str, name: str, ref: str) -> dict:
    """A Hub dataset's version row: `ref` is a revision number, a tag (latest, the default) or sha256:HASH."""
    import asyncio

    from harbor.db.client import RegistryDB
    try:
        return asyncio.run(RegistryDB().resolve_dataset_version(org, name, ref or "latest"))[1]
    except Exception as err:            # the registry's client raises its own kinds, for a missing dataset or no network
        raise ValueError(f"Could not read {HUB}{org}/{name} at {ref or 'latest'}: {str(err)[:200]}") from None


def _hub_download(org: str, name: str, commit: str, repo: Path) -> bool:
    """A Hub dataset's tasks at one version, its content hash, as Harbor itself downloads them."""
    from ..backends.harbor import binary
    try:
        with tempfile.TemporaryDirectory(dir=repo.parent) as tmp:
            subprocess.run([binary(), "datasets", "download", f"{org}/{name}@sha256:{commit}", "--export", "-o", tmp],
                           check=True, capture_output=True, timeout=3600)
            shutil.move(str(Path(tmp) / name), str(repo))
        (repo / PINNED).write_text(commit + "\n")
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        shutil.rmtree(repo, ignore_errors=True)
        return False


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


received = {"bytes": 0}     # how much the download going on has received, for the screen to say that it moves


def _size(folder: Path) -> int:
    total = 0
    for root, _, files in os.walk(folder):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _dies_with_parent() -> None:
    """In a child about to become git: on Linux, be stopped when hm is, however hm ends, so that a fetch in a process
    group of its own is never left running."""
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)       # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def _fetch_watched(repo: Path, args: list[str], window: float = 10, fast: int = 100_000, tries: int = 3) -> None:
    """A git fetch that is started again on a fresh connection when it crawls. One connection to GitHub can crawl at
    tens of KB a second while a fresh one gets megabytes, and waiting on it can take hours. On every try but the last,
    under `fast` bytes a second for `window` seconds is a crawl; the last try goes on however slowly, unless nothing
    arrives for a minute, so that a slow network still gets there. Raises CalledProcessError when it could not be had.
    Each try runs in a process group of its own, so that stopping it also stops git's helper that holds the connection."""
    import signal
    import time
    for attempt in range(tries):
        last = attempt == tries - 1
        need, limit = (64 * 1024, 60.0) if last else (fast * window, window)
        with tempfile.TemporaryFile() as said:
            proc = subprocess.Popen(["git", *args], cwd=repo, stdout=subprocess.DEVNULL, stderr=said, start_new_session=True,
                                    preexec_fn=_dies_with_parent)
            seen, moved, stalled = _size(repo / ".git"), time.time(), False
            while proc.poll() is None:
                time.sleep(1)
                now = _size(repo / ".git")
                received["bytes"] = now
                if now - seen >= need:
                    seen, moved = now, time.time()
                elif time.time() - moved > limit:
                    stalled = True
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
            if proc.returncode == 0 and not stalled:
                return
            said.seek(0)
            err = said.read().decode(errors="replace").strip()
        if last:
            raise subprocess.CalledProcessError(proc.returncode or 1, ["git", *args],
                                                stderr="nothing arrived for a minute" if stalled else err)


def _git_in(repo: Path, *args: str, timeout: float = 3600, data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, timeout=timeout, input=data)


def _shallow(url: str, commit: str, repo: Path) -> bool:
    """The repository's files at one commit, by a shallow git fetch of that commit alone: for a large repository far
    faster than GitHub's archive of it, which it builds slowly. False when it could not be had."""
    try:
        repo.mkdir(parents=True)
        _git_in(repo, "init", "--quiet")
        _fetch_watched(repo, ["fetch", "--quiet", "--depth", "1", url.removesuffix(".git") + ".git", commit], window=20)
        _git_in(repo, "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", "FETCH_HEAD")
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        shutil.rmtree(repo, ignore_errors=True)
        return False


def preview(entry: dict, sources: Path) -> Path:
    """The benchmark at its pinned commit with only its small files (up to SMALL each): enough to find its tasks, read
    its README and licence, and run the cheap checks, in a minute where the whole repository can take hours. A run
    downloads the rest (fetch). A folder on this machine, or a Hugging Face dataset, is fetched in full. A fetch whose
    connection stalls is started again. Returns the tasks folder; raises ValueError when it could not be read."""
    repo, url, commit = sources / entry["id"], entry["github"], entry["commit"]
    if Path(url).exists() or _huggingface(url) or _hub(url):
        return fetch(entry, sources)
    if repo.exists() and _at(repo) == commit:
        return repo / entry["tasks"]
    shutil.rmtree(repo, ignore_errors=True)
    try:
        repo.mkdir(parents=True)
        _git_in(repo, "init", "--quiet")
        received["bytes"] = 0
        _fetch_watched(repo, ["fetch", "--quiet", "--depth", "1", f"--filter=blob:limit={SMALL}", url.removesuffix(".git") + ".git",
                              commit], window=10)
        _git_in(repo, "update-ref", "--no-deref", "HEAD", "FETCH_HEAD")      # its date is read from it
        # Which files are there: the large ones were left out, and asking for them would fetch them.
        absent = {line[1:] for line in _git_in(repo, "rev-list", "--objects", "--missing=print", "FETCH_HEAD").stdout.decode().split()
                  if line.startswith("?")}
        files = []
        for item in _git_in(repo, "ls-tree", "-r", "-z", "FETCH_HEAD").stdout.decode(errors="surrogateescape").split("\0"):
            if item:
                meta, path = item.split("\t", 1)
                mode, kind, obj = meta.split()
                if kind == "blob" and mode != "120000" and obj not in absent:
                    files.append((obj, path, mode))
        out = _git_in(repo, "cat-file", "--batch", data="".join(f"{o}\n" for o, _, _ in files).encode()).stdout
        at = 0
        for obj, path, mode in files:
            header_end = out.index(b"\n", at)
            size = int(out[at:header_end].split()[2])
            body, at = out[header_end + 1:header_end + 1 + size], header_end + 1 + size + 1
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            if mode == "100755":
                target.chmod(0o755)
        (repo / PREVIEW).write_text(f"files up to {SMALL // 1024} KB only; {len(absent)} larger ones are left out\n")
        (repo / PINNED).write_text(commit + "\n")
        return repo / entry["tasks"]
    except (OSError, ValueError, IndexError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
        shutil.rmtree(repo, ignore_errors=True)          # not the whole repository instead: that is what can take hours
        why = str(getattr(err, "stderr", "") or err).strip().splitlines()
        raise ValueError(f"Could not read {url} at {commit[:10]}: {why[-1][:200] if why else 'the fetch failed'}. "
                         "Paste the link again to try once more.") from None


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
    A GitHub repository is fetched as an archive of the commit, a Hugging Face dataset file by file at its revision, and
    a Harbor Hub dataset by Harbor at its version's content hash, which stands for the commit.
    Anything else, or a download that cannot be had, is cloned."""
    repo = sources / entry["id"]
    git = lambda *a, cwd=repo: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)  # noqa: E731
    if repo.exists() and (not _at(repo) or (repo / PREVIEW).exists()):     # cut off, or only a draft's light copy
        shutil.rmtree(repo)
    if not repo.exists():
        sources.mkdir(parents=True, exist_ok=True)
        where, dataset, hub = _github(entry["github"]), _huggingface(entry["github"]), _hub(entry["github"])
        got = ((_shallow(entry["github"], entry["commit"], repo) or _archive(*where, entry["commit"], repo)) if where else
               _dataset(dataset, entry["commit"], repo) if dataset else
               _hub_download(*hub, entry["commit"], repo) if hub else False)
        if not got and dataset:
            raise ValueError(f"Could not download the dataset {dataset} at {entry['commit'][:10]} from Hugging Face.")
        if not got and hub:
            raise ValueError(f"Could not download {entry['github']} at {entry['commit'][:10]} from the Harbor Hub.")
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
    if _hub(entry["github"]):
        try:
            return str(_hub_version(*_hub(entry["github"]), f"sha256:{entry['commit']}").get("published_at") or "")[:10]
        except ValueError:
            return ""
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
    """The release to pin, and its commit: the tag or branch that was named, else the newest tag, else the main branch.
    For a Harbor Hub dataset: the revision named (or its latest), and that version's content hash."""
    if _hub(url):
        version = _hub_version(*_hub(url), ref)
        return str(version["revision"]), version["content_hash"]
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
    repo = preview(entry, sources)          # a light copy: the whole of it is downloaded once it is added and run
    repo = sources / entry["id"]
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
