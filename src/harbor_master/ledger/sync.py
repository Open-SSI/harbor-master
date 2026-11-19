"""Sharing an index through git, and using a push as the lock (DESIGN.md Section 7).

A workspace is a clone of the index's repository. Every runner writes only their own shard
files under `ledger/`, so a pull never conflicts. A push is refused when someone else pushed
first, and that refusal is what stops two people from claiming the same tasks: the one who
is refused pulls, sees the other's claim, and gives way.

A run commits nothing but `ledger/`. The files that define the index are committed only by
the owner's own commands (`define`), so an edit anyone else makes to them stays on their machine.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


from .store import SHARDS

DEFINITION = ("about.toml", "protocol.toml", "benchmarks.toml", "roster.toml", "contributors.toml", "skills",
              ".gitignore", "README.md")


class SyncError(RuntimeError):
    pass


class Sync:
    def __init__(self, ledger_dir: Path, name: str = "harbor-master", handle: str = "harbor-master"):
        self.dir = ledger_dir
        self.who = ["-c", f"user.name={name}", "-c", f"user.email={handle}@users.noreply.github.com"]

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run(["git", *self.who, *args], cwd=self.dir, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise SyncError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:300]}")
        return r

    @property
    def enabled(self) -> bool:
        """True when the index is a git clone with a remote to share through."""
        return (self.dir / ".git").exists() and bool(self._git("remote", check=False).stdout.strip())

    def _remote_has_branch(self) -> bool:
        return self._git("rev-parse", "--verify", "--quiet", "origin/main", check=False).returncode == 0

    def pull(self) -> None:
        """Bring in what others pushed. Local shards are committed first, so nothing is lost."""
        self._commit("local records")
        self._drop_derived()
        self._git("fetch", "--quiet", "origin")
        if self._remote_has_branch():
            r = self._git("rebase", "--quiet", "--autostash", "origin/main", check=False)   # local edits are kept aside
            if r.returncode != 0:
                self._git("rebase", "--abort", check=False)
                raise SyncError("the ledger could not be rebased onto the shared one. Two runners wrote the same "
                                f"file, which should never happen: {(r.stderr or r.stdout).strip()[:200]}")
            if self._git("diff", "--quiet", "--diff-filter=U", check=False).returncode != 0:
                raise SyncError("a file of the index that you edited here was also changed by the owner. Put yours "
                                "back with: git checkout origin/main -- FILE")

    def _drop_derived(self) -> None:
        """index.toml is rebuilt from the records, so a local copy never stands in the way of a pull."""
        path = self.dir / "index.toml"
        if self._git("ls-files", "--error-unmatch", "index.toml", check=False).returncode == 0:
            self._git("checkout", "--", "index.toml", check=False)
        elif path.exists():
            path.unlink()

    def publish_index(self, message: str) -> None:
        """Commit and push index.toml itself. Done by `hm index build`, so that one person writes it."""
        self._git("add", "index.toml")
        if self._git("diff", "--cached", "--quiet", check=False).returncode != 0:
            self._git("commit", "--quiet", "-m", message)
        self.publish(message)

    def _commit(self, message: str) -> bool:
        """Commit this machine's new records, and nothing else."""
        if (self.dir / SHARDS).exists():
            self._git("add", "-A", "--", SHARDS)
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self._git("commit", "--quiet", "-m", message)
        return True

    def define(self, message: str, only: tuple[str, ...] = DEFINITION) -> bool:
        """Owner only: commit the files that define the index. Starts the repository if there is none."""
        if not (self.dir / ".git").exists():
            self._git("init", "--quiet", "-b", "main")
        found = [name for name in only if (self.dir / name).exists()]
        if found:
            self._git("add", "-A", "--", *found)
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self._git("commit", "--quiet", "-m", message)
        return True

    @classmethod
    def clone(cls, url: str, directory: Path, name: str, handle: str) -> "Sync":
        """Join an index: clone its repository into a new folder."""
        r = subprocess.run(["git", "clone", "--quiet", url, str(directory)], capture_output=True, text=True)
        if r.returncode != 0:
            raise SyncError(f"could not clone {url}: {(r.stderr or r.stdout).strip()[:300]}")
        return cls(directory, name, handle)

    def push(self, message: str) -> bool:
        """Commit and push. False means someone pushed first: this pulled their work in, and the caller
        must look at it before trying again."""
        self._commit(message)
        r = self._git("push", "--quiet", "origin", "HEAD:main", check=False)
        if r.returncode == 0:
            return True
        self.pull()
        return False

    def publish(self, message: str, tries: int = 8) -> None:
        """Push, pulling and retrying while others get in first. For records that cannot conflict."""
        for _ in range(tries):
            if self.push(message):
                return
        raise SyncError("could not push the ledger: others kept pushing first")

    def connect(self, url: str) -> str:
        """Share this index through a repository. Where the repository already has the index, its files
        that define the index win over the local copies."""
        if not (self.dir / ".git").exists():
            self._git("init", "--quiet", "-b", "main")
        if "origin" in self._git("remote", check=False).stdout.split():
            self._git("remote", "set-url", "origin", url)
        else:
            self._git("remote", "add", "origin", url)
        self._git("fetch", "--quiet", "origin")
        if self._remote_has_branch():
            self._git("reset", "--quiet", "origin/main")                    # keep local files, adopt the shared history
            for name in DEFINITION:
                if self._git("cat-file", "-e", f"origin/main:{name}", check=False).returncode == 0:
                    self._git("checkout", "origin/main", "--", name)
            state = "joined the shared index"
        else:
            state = "started the shared index"
        self.publish("connect this workspace")
        self._git("branch", "--quiet", "--set-upstream-to=origin/main", check=False)
        return state
