"""Skills: what each agent is told, as files (see README.md in this folder).

A skill's version is a short hash of its file, recorded on every ledger event made with it.

Two kinds. The curation skills (adapt, triage, rank, probe, audit) decide what gets into an index,
so each index has its own copies under `skills/` in its repository, locked with its protocol.
The files here are the templates those copies start from. The session skills read what is
typed on the run screen. They change no result, so they stay in the tool, the same for every
index, and are not part of any protocol.
"""
from __future__ import annotations

import difflib
import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from string import Template

CURATION = ("adapt", "audit", "probe", "rank", "triage")      # the folders an index keeps its own copies of


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


_own: Path | None = None       # the skills folder of the index in use


def use(folder: Path | None) -> None:
    """Read the curation skills from this folder. One it does not have comes from the templates."""
    global _own
    folder = folder if folder is not None and folder.is_dir() else None
    if folder != _own:
        _own = folder
        load.cache_clear()


def template(name: str):
    """The tool's own file for `triage/gates.toml`."""
    return resources.files(__package__).joinpath(name)


def names() -> list[str]:
    """Every curation skill, as `folder/file`."""
    return sorted(f"{sub}/{f.name}" for sub in CURATION for f in template(sub).iterdir() if f.name.endswith((".md", ".toml")))


def _file(name: str):
    """The file in use for `triage/gates.toml`: the index's copy, or the template where it has none.
    A session skill is always the tool's."""
    own = _own / name if _own is not None and name.split("/")[0] in CURATION else None
    return own if own is not None and own.is_file() else template(name)


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
    """One hash over every curation skill in use. Part of the protocol: changing what an agent is told
    changes results. The session skills are left out: they change none."""
    h = hashlib.sha256()
    for name in names():
        h.update(name.encode())
        h.update(_file(name).read_bytes())
    return h.hexdigest()[:16]


# ----- an index's own copies -----

def missing(folder: Path) -> list[str]:
    """The curation skills this index has no copy of."""
    return [n for n in names() if not (folder / n).is_file()]


def _shape(value: object, at: str = "") -> dict[str, str]:
    """Every key of a TOML skill and the kind of thing it holds. The wording is not part of it."""
    if isinstance(value, dict):
        out = {at: "table"} if at else {}
        for k, v in value.items():
            out.update(_shape(v, f"{at}.{k}" if at else k))
        return out
    return {at: "list" if isinstance(value, list) else "number" if isinstance(value, (int, float)) else "text"}


def check(name: str, text: str) -> list[str]:
    """What the code could not use in an index's copy of a skill. Any wording is allowed. What must
    stay is what the code reads: in a TOML skill the same keys, in a prompt the same $placeholders
    and the two sections."""
    reference = template(name).read_text()
    if name.endswith(".toml"):
        try:
            mine = _shape(tomllib.loads(text))
        except tomllib.TOMLDecodeError as err:
            return [f"not valid TOML ({err})"]
        want = _shape(tomllib.loads(reference))
        found = [f"{k} is missing" for k in want if k not in mine]
        found += [f"{k} is not something the code reads" for k in mine if k not in want]
        found += [f"{k} must be a {want[k]}" for k in want if k in mine and mine[k] != want[k]]
        return found
    found = [f"the {part} section is missing" for part in ("## System", "## Prompt") if part in reference and part not in text]
    return found + [f"{var} is missing, so the agent would not be given it"
                    for var in sorted(set(re.findall(r"\$[a-z_]+", reference)) - set(re.findall(r"\$[a-z_]+", text)))]


def problems(folder: Path) -> list[str]:
    """What is wrong with the skills an index has, as lines."""
    return [f"skills/{n}: {p}" for n in names() if (folder / n).is_file() for p in check(n, (folder / n).read_text())]


def status(folder: Path) -> list[tuple[str, str]]:
    """Each curation skill and where this index stands with it: missing, same, or own."""
    out = []
    for n in names():
        own = folder / n
        out.append((n, "missing" if not own.is_file() else "same" if own.read_bytes() == template(n).read_bytes() else "own"))
    return out


def diff(folder: Path, name: str) -> str:
    """How an index's copy differs from the template, as a unified diff. Empty when it does not."""
    own = (folder / name).read_text().splitlines() if (folder / name).is_file() else []
    return "\n".join(difflib.unified_diff(template(name).read_text().splitlines(), own, f"template/{name}", f"skills/{name}", lineterm=""))


def adopt(folder: Path, which: list[str]) -> list[str]:
    """Copy templates into an index, over whatever it has. Returns the ones that changed."""
    changed = []
    for n in which:
        own = folder / n
        if not own.is_file() or own.read_bytes() != template(n).read_bytes():
            own.parent.mkdir(parents=True, exist_ok=True)
            own.write_bytes(template(n).read_bytes())
            changed.append(n)
    load.cache_clear()
    return changed
