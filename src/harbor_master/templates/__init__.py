"""Files that `hm` copies out.

Two kinds. The definition of an index (about.toml, protocol.toml, roster.toml,
contributors.toml) starts from the blank files here, and from then on lives in the index's own
repository, which only its owner changes. The rest (hm.toml, secrets.env.example) are examples
that every user copies and edits.
"""
from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path

DEFINITION = ("about.toml", "protocol.toml", "roster.toml", "contributors.toml")
LOCKED = ("roster.toml",)    # locked in the ledger beside the protocol: it decides what the leaderboard compares.
# The benchmark list, the adapters and the contributor list are not: each is vouched for by a signed record of its own


def read(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text()


def digests(index_dir: Path) -> dict[str, str]:
    """Checksums of what is locked with the protocol: the roster. A missing file counts as empty."""
    return {name: hashlib.sha256((index_dir / name).read_bytes() if (index_dir / name).exists() else b"").hexdigest()[:16]
            for name in LOCKED}


def changed(lock: dict, index_dir: Path) -> list[str]:
    """The locked files that are not the copy the owner locked. A lock made before a file was locked says nothing of it."""
    here, locked = digests(index_dir), lock.get("registry") or {}
    return [name for name in LOCKED if name in locked and locked[name] != here[name]]
