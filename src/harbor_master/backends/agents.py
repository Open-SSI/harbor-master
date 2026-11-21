"""The agents' own programs, downloaded once on this machine and shared by every trial's container.

An agent runs inside the task's container, where the task's files and tools are, at the version the protocol pins. Left
to itself, Harbor installs it there for every trial: Node.js and npm from the image's package manager, then the agent,
a few hundred MB and several minutes each time. But Harbor skips that when the container already answers
`claude --version` or `codex --version` with the pinned version. So the pinned version's single-file program is kept in
a cache here, and mounted read-only into each container at /usr/local/bin. Where it cannot be had, or does not run in
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
CODEX = "https://github.com/openai/codex/releases/download/rust-v{version}/codex-{arch}-unknown-linux-musl.tar.gz"
NAMES = {"claude-code": "claude", "codex": "codex"}         # agent -> the command its container runs
_locks: dict[tuple[str, str], threading.Lock] = {}
_guard = threading.Lock()


def cache_dir() -> Path:
    return Path(os.environ.get("HM_AGENT_CACHE") or Path.home() / ".cache" / "harbor-master" / "agents")


def _get(url: str, timeout: float = 60) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:        # a read that stalls for a minute gives up
        return r.read()


def _fetch(agent: str, version: str) -> bytes:
    """The agent's Linux program at one version, as bytes. Raises when it cannot be had or is not what was published."""
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if agent == "claude-code":
        base, plat = CLAUDE.format(version=version), "linux-arm64" if arm else "linux-x64"
        listed = json.loads(_get(f"{base}/manifest.json"))["platforms"][plat]
        data = _get(f"{base}/{plat}/{listed.get('binary', 'claude')}", timeout=120)
        if hashlib.sha256(data).hexdigest() != listed["checksum"]:
            raise ValueError("its checksum is not the published one")
        return data
    arch = "aarch64" if arm else "x86_64"
    packed = _get(CODEX.format(version=version, arch=arch), timeout=120)
    with tarfile.open(fileobj=io.BytesIO(packed)) as tar:
        member = next(m for m in tar.getmembers() if m.isfile() and m.name.split("/")[-1].startswith("codex"))
        return tar.extractfile(member).read()


def program(agent: str, version: str) -> Path | None:
    """The cached program of an agent at a pinned version, downloaded the first time it is asked for. None when the
    agent has no single-file program, no version is pinned, or the download failed: Harbor then installs it itself."""
    if agent not in NAMES or not version:
        return None
    path = cache_dir() / agent / version / NAMES[agent]
    if path.is_file():
        return path
    with _guard:
        lock = _locks.setdefault((agent, version), threading.Lock())
    with lock:                                  # trials start side by side: one of them downloads, the rest wait for it
        if path.is_file():
            return path
        try:
            data = _fetch(agent, version)
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_suffix(".part")
            part.write_bytes(data)
            part.chmod(0o755)
            part.replace(path)
            return path
        except Exception:                       # no network, no such release, a bad archive: nothing is mounted
            return None


def mounts(agent: str, version: str) -> list[dict]:
    """What to mount into a trial's container so that the agent is already there: Harbor's own mount records."""
    path = program(agent, version)
    return [] if path is None else [{"type": "bind", "source": str(path), "target": f"/usr/local/bin/{NAMES[agent]}",
                                     "read_only": True}]
