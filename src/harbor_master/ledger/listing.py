"""The list of indexes: where each index's repository is (DESIGN.md Section 10).

The list is `indexes.toml` at the top of the Harbor-Master repository, read from its main
branch, so a new entry is visible as soon as it is merged. It holds addresses and nothing
else: what an index is comes from the index's own repository.
"""
from __future__ import annotations

import os
import re
import tomllib
import urllib.request
from pathlib import Path

from ..core import config as config_mod
from . import protocol
from .fold import index_entries
from .store import Ledger
from .sync import Sync, SyncError
from .verify import active, load_contributors, owner_keys, verify

DEFAULT = "https://raw.githubusercontent.com/Open-SSI/harbor-master/main/indexes.toml"
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
REQUIRED = ("name", "url", "owner")


class ListError(RuntimeError):
    pass


def source(given: str | None = None) -> str:
    """Which list to read: the one named, then $HM_INDEXES, then the one in the Harbor-Master repository."""
    return given or os.environ.get("HM_INDEXES") or DEFAULT


def read(src: str) -> list[dict]:
    """The entries of a list, from an address or a file."""
    try:
        if "://" in src:
            with urllib.request.urlopen(src, timeout=20) as response:
                text = response.read().decode()
        else:
            text = Path(src).expanduser().read_text()
        return tomllib.loads(text).get("index", [])
    except (OSError, ValueError) as err:        # not reachable, not there, or not TOML
        raise ListError(f"Could not read the list of indexes at {src}: {err}") from err


def owners(entry: dict) -> list[str]:
    """An entry's owner, as the lines a workspace remembers: `handle ssh-ed25519 KEY`."""
    listed = entry.get("owner", [])
    return sorted(" ".join(o.split()) for o in ([listed] if isinstance(listed, str) else listed))


def _same_place(url: str) -> str:
    return url.strip().rstrip("/").removesuffix(".git").lower()


def problems(entries: list[dict]) -> list[str]:
    """What is wrong with the list on its own, before any index is looked at."""
    found, names, places = [], set(), set()
    for n, e in enumerate(entries, 1):
        who = e.get("name") or f"entry {n}"
        missing = [k for k in REQUIRED if not e.get(k)]
        if missing:
            found.append(f"{who}: missing {', '.join(missing)}")
            continue
        if not NAME.fullmatch(e["name"]):
            found.append(f"{who}: a name is lower case letters, digits and hyphens")
        if e["name"] in names:
            found.append(f"{who}: listed twice")
        if _same_place(e["url"]) in places:
            found.append(f"{who}: its address is already listed under another name")
        if any(len(o.split()) != 3 for o in owners(e)):
            found.append(f"{who}: owner is written as: handle ssh-ed25519 KEY")
        names.add(e["name"])
        places.add(_same_place(e["url"]))
    return found


def find(entries: list[dict], name: str) -> dict | None:
    return next((e for e in entries if e.get("name") == name.lower()), None)


def owner_matches(entry: dict, index_dir: Path) -> bool:
    """Whether a clone names the owner its entry was listed with. A repository that changed hands does not."""
    return owners(entry) == owner_keys(load_contributors(index_dir))


def inspect(entry: dict, directory: Path) -> tuple[dict, list[str]]:
    """Clone the index an entry names into `directory`, and check that it is a sound index and the one
    the entry says. Returns what a listing shows about it, and the problems."""
    from .. import skills, tasks
    try:
        Sync.clone(entry["url"], directory, "harbor-master", "harbor-master")
    except SyncError as err:
        return {}, [str(err)]
    if not (directory / config_mod.PROTOCOL_NAME).exists():
        return {}, [f"not an index: no {config_mod.PROTOCOL_NAME}"]
    found: list[str] = []
    try:
        cfg = config_mod.load(directory)
        benchmarks = tasks.supported.supported(directory)
        people = load_contributors(directory)
    except (ValueError, OSError) as err:        # a file that does not parse, or a benchmark that is not pinned
        return {}, [f"its files cannot be read: {err}"]
    finally:
        skills.use(None)                        # looking at an index must not leave its skills in use
    if cfg.about.name != entry["name"]:
        found.append(f"listed as {entry['name']}, but its about.toml says {cfg.about.name or '(no name)'}")
    if not owner_matches(entry, directory):
        found.append("the owner on its contributor list is not the owner on its entry")
    found += skills.problems(directory / "skills")
    found += [f"it has no copy of skills/{n}" for n in skills.missing(directory / "skills")]
    found += verify(directory)
    events = Ledger(directory).events
    lock = protocol.current(events)
    info = {"title": cfg.about.title or cfg.about.name, "description": cfg.about.description,
            "owner": ", ".join(f"{c.get('name', '?')} (@{c['github']})" for c in people if c.get("role") == "owner" and c.get("github")),
            "contributors": len(active(people)), "benchmarks": [b["id"] for b in benchmarks],
            "accepted": len(index_entries(events)), "locked": bool(lock and lock["protocol"] == cfg.protocol)}
    return info, found
