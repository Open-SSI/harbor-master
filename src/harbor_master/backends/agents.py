"""The agents' own programs, downloaded once on this machine and shared by every trial's container.

An agent runs inside the task's container, where the task's files and tools are, at the version the protocol pins. Left
to itself, Harbor installs it there for every trial: Node.js and npm from the image's package manager, then the agent,
a few hundred MB and several minutes each time. But Harbor skips that when the container already answers
`claude --version` or `codex --version` with the pinned version. So the pinned version's programs are kept in a cache
here, and mounted read-only into each container at /usr/local/bin: claude is one file; codex is two, itself and the
code-mode host it starts to run every command (without it, codex cannot run anything at all). Where it cannot be had, or does not run in
an image (a musl image and a glibc program), nothing is mounted and Harbor installs the agent as before.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import tarfile
import threading
import urllib.request
from pathlib import Path

CLAUDE = "https://downloads.claude.ai/claude-code-releases/{version}"
CODEX = "https://github.com/openai/codex/releases/download/rust-v{version}/{name}-{arch}-unknown-linux-musl.tar.gz"
NAMES = {"claude-code": "claude", "codex": "codex"}         # agent -> the command its container runs
HELPERS = {"codex": ["codex-code-mode-host"]}               # agent -> the other programs its command starts
_locks: dict[tuple[str, str], threading.Lock] = {}
_guard = threading.Lock()


def cache_dir() -> Path:
    return Path(os.environ.get("HM_AGENT_CACHE") or Path.home() / ".cache" / "harbor-master" / "agents")


def _get(url: str, timeout: float = 60) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:        # a read that stalls for a minute gives up
        return r.read()


def _fetch(agent: str, version: str, name: str = "") -> bytes:
    """One of the agent's Linux programs at one version, as bytes: the agent's own command, or `name`, one of its
    helpers. Raises when it cannot be had or is not what was published."""
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if agent == "claude-code":
        base, plat = CLAUDE.format(version=version), "linux-arm64" if arm else "linux-x64"
        listed = json.loads(_get(f"{base}/manifest.json"))["platforms"][plat]
        data = _get(f"{base}/{plat}/{listed.get('binary', 'claude')}", timeout=120)
        if hashlib.sha256(data).hexdigest() != listed["checksum"]:
            raise ValueError("its checksum is not the published one")
        return data
    arch, name = "aarch64" if arm else "x86_64", name or "codex"
    packed = _get(CODEX.format(version=version, arch=arch, name=name), timeout=120)
    with tarfile.open(fileobj=io.BytesIO(packed)) as tar:
        member = next(m for m in tar.getmembers() if m.isfile() and m.name.split("/")[-1].startswith(name))
        return tar.extractfile(member).read()


def programs(agent: str, version: str) -> list[Path]:
    """The cached programs of an agent at a pinned version, its command first, downloaded the first time they are asked
    for. Empty when the agent has none to mount, no version is pinned, or any download failed: Harbor then installs the
    agent itself. Never only some of them: an agent mounted without a helper would fail every command it runs."""
    if agent not in NAMES or not version:
        return []
    folder = cache_dir() / agent / version
    names = [NAMES[agent], *HELPERS.get(agent, [])]
    paths = [folder / n for n in names]
    if all(p.is_file() for p in paths):
        return paths
    with _guard:
        lock = _locks.setdefault((agent, version), threading.Lock())
    with lock:                                  # trials start side by side: one of them downloads, the rest wait for it
        try:
            for n, path in zip(names, paths):
                if path.is_file():
                    continue
                data = _fetch(agent, version, "" if n == NAMES[agent] else n)
                path.parent.mkdir(parents=True, exist_ok=True)
                part = path.with_suffix(".part")
                part.write_bytes(data)
                part.chmod(0o755)
                part.replace(path)
            return paths
        except Exception:                       # no network, no such release, a bad archive: nothing is mounted
            return []


def program(agent: str, version: str) -> Path | None:
    """The agent's own command, cached, or None: see programs."""
    got = programs(agent, version)
    return got[0] if got else None


def mounts(agent: str, version: str) -> list[dict]:
    """What to mount into a trial's container so that the agent is already there: Harbor's own mount records."""
    return [{"type": "bind", "source": str(p), "target": f"/usr/local/bin/{p.name}", "read_only": True}
            for p in programs(agent, version)]
