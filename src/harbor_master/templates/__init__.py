"""Files that `hm` copies out.

Two kinds. The definition of an index (about.toml, protocol.toml, benchmarks.toml, roster.toml,
contributors.toml) starts from the blank files here, and from then on lives in the index's own
repository, which only its owner changes. The rest (hm.toml, secrets.env.example) are examples
that every user copies and edits.
"""
from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path

DEFINITION = ("about.toml", "protocol.toml", "benchmarks.toml", "roster.toml", "contributors.toml")
LOCKED = ("benchmarks.toml", "roster.toml", "contributors.toml")    # locked in the ledger beside the protocol


def read(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text()


def digests(index_dir: Path) -> dict[str, str]:
    """Checksums of what is locked with the protocol: the three lists, and every adapter. A missing file counts as empty."""
    from ..tasks import adapter
    out = {name: hashlib.sha256((index_dir / name).read_bytes() if (index_dir / name).exists() else b"").hexdigest()[:16]
           for name in LOCKED}
    folder = index_dir / adapter.FOLDER         # an adapter decides what the tasks are, so each one is locked too
    for d in sorted(p for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []:
        out[f"{adapter.FOLDER}/{d.name}"] = adapter.digest(d)
    return out
