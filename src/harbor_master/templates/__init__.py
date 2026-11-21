"""Files that `hm` copies out.

Two kinds. The definition of an index (about.toml, protocol.toml, contributors.toml) starts from
the blank files here, and from then on lives in the index's own repository, which only its owner
changes. The rest (hm.toml, secrets.env.example) are examples: an index commits them as hm.example.toml
and secrets.env.example, and each clone gets its own hm.toml and secrets.env made from them.
"""
from __future__ import annotations

from importlib import resources

DEFINITION = ("about.toml", "protocol.toml", "contributors.toml")


def read(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text()

