"""Skills: what each agent is told, as files (see README.md in this folder).

A skill's version is a short hash of its file, recorded on every ledger event made with it.

The files here are the defaults. An index may replace any of them with a file of the same
name under `skills/` in its own repository: `use()` points at that folder.
"""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from string import Template


@dataclass(frozen=True)
class Skill:
    name: str                       # "probe/write-mutants"
    version: str                    # short hash of the file
    system: str = ""
    prompt: str = ""
    data: dict = field(default_factory=dict, hash=False, compare=False)

    @property
    def tag(self) -> str:
        return f"{self.name}@{self.version}"

    def fill(self, **values: object) -> str:
        return Template(self.prompt).safe_substitute({k: str(v) for k, v in values.items()})


_own: Path | None = None       # the index's own skills folder, when it has one


def use(folder: Path | None) -> None:
    """Read skills from this folder first. A skill it does not have comes from the defaults."""
    global _own
    folder = folder if folder is not None and folder.is_dir() else None
    if folder != _own:
        _own = folder
        load.cache_clear()


def _file(name: str):
    """The file for `triage/gates.toml`: the index's own if it has one, otherwise the default."""
    own = _own / name if _own is not None else None
    return own if own is not None and own.is_file() else resources.files(__package__).joinpath(name)


@lru_cache(maxsize=None)
def load(name: str) -> Skill:
    for suffix in (".md", ".toml"):
        f = _file(name + suffix)
        if f.is_file():
            raw = f.read_bytes()
            version = hashlib.sha256(raw).hexdigest()[:10]
            text = raw.decode()
            if suffix == ".toml":
                return Skill(name, version, data=tomllib.loads(text))
            body = text.split("---", 2)[2] if text.startswith("---") else text
            system, _, prompt = body.partition("## Prompt")
            return Skill(name, version, system.replace("## System", "").strip(), prompt.strip())
    raise FileNotFoundError(f"no skill named {name}")


def digest() -> str:
    """One hash over every skill file in use. Part of the protocol: changing what an agent is told changes results."""
    h = hashlib.sha256()
    folder = resources.files(__package__)
    for sub in sorted((p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("_")), key=lambda p: p.name):
        for f in sorted(sub.iterdir(), key=lambda p: p.name):
            if f.name.endswith((".md", ".toml")):
                h.update(f"{sub.name}/{f.name}".encode())
                h.update(_file(f"{sub.name}/{f.name}").read_bytes())
    return h.hexdigest()[:16]


def replaced() -> list[str]:
    """The skills this index words its own way."""
    if _own is None:
        return []
    return sorted(str(f.relative_to(_own)) for f in _own.glob("*/*") if f.suffix in (".md", ".toml")
                  and resources.files(__package__).joinpath(str(f.relative_to(_own))).is_file())
