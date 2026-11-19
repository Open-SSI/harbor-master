"""The task store: every accepted task, at the version that was accepted (DESIGN.md Section 8).

A later benchmark run must run exactly the task that was accepted. Its files may be too large
for a git repository, a repair may have changed them from what the benchmark's source gives, and
the source may be gone one day. So each accepted task is kept as one archive, named by its content
digest, in the store the index names in about.toml: a Hugging Face dataset repository, or a folder.
Every index has one, named when the index is created.
A copy is also kept on the machine, under .hm/store.
"""
from __future__ import annotations

import shutil
import tarfile
import tempfile
from pathlib import Path

from ..core import config as config_mod
from ..core import keys
from .model import digest_dir

FOLDER = "store"


class StoreError(RuntimeError):
    pass


def key(benchmark: str, name: str, digest: str) -> str:
    """Where a task's archive is in a store: by benchmark, task and the start of its digest."""
    return f"tasks/{benchmark.replace('/', '__')}/{name}.{digest[:16]}.tar.gz"


class Folder:
    """A store that is a folder: the copy on this machine, or a shared drive."""

    def __init__(self, root: Path):
        self.root, self.where = root, str(root)

    def exists(self) -> bool:
        return self.root.is_dir()

    def create(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def has(self, k: str) -> bool:
        return (self.root / k).is_file()

    def put(self, k: str, file: Path) -> None:
        (self.root / k).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, self.root / k)

    def get(self, k: str, dest: Path) -> bool:
        if not self.has(k):
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.root / k, dest)
        return True

    def remove(self, k: str) -> None:
        (self.root / k).unlink(missing_ok=True)


class HuggingFace:
    """A store that is a Hugging Face dataset repository. Writing to it needs HF_TOKEN, each person's own.
    Reading a public one needs no token."""

    def __init__(self, repo: str, write: bool = True):
        self.repo, self.where = repo, f"huggingface.co/datasets/{repo}"
        try:
            import huggingface_hub
        except ImportError as err:
            raise StoreError("The task store needs the huggingface_hub package. Install Harbor-Master again: "
                             "uv tool install --editable .") from err
        token = keys.get("HF_TOKEN") or None
        if write and not token:
            raise StoreError(f"The task store is {self.where}, and putting a task there needs your own Hugging Face token, "
                             "with write access to it. Add HF_TOKEN to your keys file: /keys")
        self.hub, self.api, self.token = huggingface_hub, huggingface_hub.HfApi(token=token), token

    def _guard(self, what: str, fn):
        try:
            return fn()
        except Exception as err:        # the library raises many kinds: no access, no network, no such repository
            raise StoreError(f"Could not {what} {self.where}: {type(err).__name__}: {str(err)[:200]}") from err

    def exists(self) -> bool:
        try:
            self.api.repo_info(self.repo, repo_type="dataset")
            return True
        except Exception as err:
            if type(err).__name__ == "RepositoryNotFoundError":     # also what a private one says to someone without access
                return False
            raise StoreError(f"Could not reach {self.where}: {type(err).__name__}: {str(err)[:200]}") from err

    def create(self) -> None:
        """Make the repository, private. Its owner opens it to others, or to everyone, on Hugging Face."""
        self._guard("create", lambda: self.api.create_repo(self.repo, repo_type="dataset", private=True, exist_ok=True))

    def has(self, k: str) -> bool:
        return bool(self._guard("look in", lambda: self.api.file_exists(self.repo, k, repo_type="dataset")))

    def put(self, k: str, file: Path) -> None:
        self._guard("upload to", lambda: self.api.upload_file(path_or_fileobj=str(file), path_in_repo=k, repo_id=self.repo,
                                                              repo_type="dataset", commit_message=f"add {k}"))

    def remove(self, k: str) -> None:
        self._guard("delete from", lambda: self.api.delete_file(k, repo_id=self.repo, repo_type="dataset",
                                                                commit_message=f"remove {k}"))

    def get(self, k: str, dest: Path) -> bool:
        if not self.has(k):
            return False
        with tempfile.TemporaryDirectory() as tmp:
            got = self._guard("download from", lambda: self.hub.hf_hub_download(
                repo_id=self.repo, filename=k, repo_type="dataset", token=self.token, local_dir=tmp))
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(got, dest)
        return True


def local(ws: Path) -> Folder:
    return Folder(config_mod.local(ws) / FOLDER)


def is_name(where: str) -> bool:
    """Whether this can name a store: `org/name` on Hugging Face, or a folder's path."""
    import re
    return where.startswith(("/", "~", ".")) or bool(re.fullmatch(r"[\w.-]+/[\w.-]+", where))


def remote(cfg, write: bool = True) -> Folder | HuggingFace | None:
    """The store the index names: `org/name` is a Hugging Face dataset repository, a path is a folder."""
    where = (cfg.about.tasks or "").strip()
    if not where:
        return None
    if where.startswith(("/", "~", ".")):
        return Folder(Path(where).expanduser())
    return HuggingFace(where, write)


def not_ready(cfg) -> str:
    """Why this machine cannot put an accepted task in the index's store, or "" when it can. Every index has a
    store, so a real run asks this before it starts."""
    if not (cfg.about.tasks or "").strip():
        return ("this index names no store for its accepted tasks. Its owner sets one, which also creates it: "
                "/tasks store ORG/NAME")
    try:
        there = remote(cfg)
        if not there.exists():
            return f"the task store {there.where} does not exist, or your token cannot see it. Its owner creates it with /tasks store"
    except StoreError as err:
        return str(err)
    return ""


def pack(task_dir: Path, out: Path) -> None:
    """One archive of a task's folder, with the files the digest covers."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        for f in sorted(p for p in task_dir.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            tar.add(f, arcname=str(Path(task_dir.name) / f.relative_to(task_dir)))


def put(ws: Path, cfg, benchmark: str, name: str, digest: str, task_dir: Path) -> tuple[bool, str]:
    """Keep the accepted version of a task: on this machine, and in the index's store when it names one.
    Returns whether a copy exists now, and one line on where."""
    k, here = key(benchmark, name, digest), local(ws)
    if not here.has(k):
        if not task_dir.is_dir() or digest_dir(task_dir) != digest:
            return False, "not kept: its files on this machine are not the version that was accepted"
        with tempfile.TemporaryDirectory() as tmp:
            pack(task_dir, Path(tmp) / "task.tar.gz")
            here.put(k, Path(tmp) / "task.tar.gz")
    there = remote(cfg)
    if there is None:
        return True, "kept on this machine only: about.toml names no task store"
    if not there.has(k):
        there.put(k, here.root / k)
    return True, f"kept in {there.where}"


def drop(ws: Path, cfg, benchmark: str, name: str, digest: str) -> str:
    """Take a task that was cut from the index out of the index's store. The archive on this machine stays,
    so that restoring the task can put it back. Returns one line on what was done."""
    k, there = key(benchmark, name, digest), remote(cfg)
    if there is None:
        return "this index names no task store"
    if not there.has(k):
        return f"it was not in {there.where}"
    if not local(ws).has(k):            # keep the only copy that will be left
        there.get(k, local(ws).root / k)
    there.remove(k)
    return f"removed from {there.where}"


def fetch(ws: Path, cfg, benchmark: str, name: str, digest: str) -> Path | None:
    """The accepted version of a task as a folder, checked against its digest. None when no copy is found."""
    k, here = key(benchmark, name, digest), local(ws)
    out = here.root / "unpacked" / benchmark.replace("/", "__") / digest[:16] / name
    if out.is_dir() and digest_dir(out) == digest:
        return out
    if not here.has(k):
        there = remote(cfg, write=False)
        if there is None or not there.get(k, here.root / k):
            return None
    shutil.rmtree(out.parent, ignore_errors=True)
    out.parent.mkdir(parents=True)
    try:
        with tarfile.open(here.root / k) as tar:
            tar.extractall(out.parent, filter="data")
        sound = out.is_dir() and digest_dir(out) == digest
    except (tarfile.TarError, OSError, EOFError, ValueError):       # not an archive any more
        sound = False
    if not sound:               # keep nothing of it: neither the files nor the archive they came from
        shutil.rmtree(out.parent, ignore_errors=True)
        (here.root / k).unlink(missing_ok=True)
        raise StoreError(f"The copy of {benchmark}/{name} in the store is not the version that was accepted "
                         f"({digest[:12]}): it was changed or damaged.")
    return out
