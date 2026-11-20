"""Files that `hm` copies out.

Two kinds. The definition of an index (about.toml, protocol.toml, roster.toml,
contributors.toml) starts from the blank files here, and from then on lives in the index's own
repository, which only its owner changes. The roster is not locked: its owner adds configurations
at any time, before the release and after it, as new models come out. The rest (hm.toml, secrets.env.example) are examples
that every user copies and edits.
"""
from __future__ import annotations

from importlib import resources

DEFINITION = ("about.toml", "protocol.toml", "roster.toml", "contributors.toml")


def read(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text()

