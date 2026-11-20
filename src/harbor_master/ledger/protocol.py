"""The protocol lock: a curation runs under one fixed set of settings (DESIGN.md Section 10).

The protocol is every setting that can change a result, hashed. The owner locks it by
writing a signed `protocol` event that holds the hash and the settings themselves. A run
whose own settings hash to anything else is refused, and every record carries the hash it
was made under, so a change of settings can never slip into a curation unnoticed.
"""
from __future__ import annotations

from typing import Iterable

BENCH = "_protocol"       # the shard folder that holds lock events


def locks(events: Iterable[dict]) -> list[dict]:
    return sorted((e for e in events if e["type"] == "protocol"), key=lambda e: e["time"])


def current(events: Iterable[dict]) -> dict | None:
    found = locks(events)
    return found[-1] if found else None


def flatten(settings: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in settings.items():
        if isinstance(v, dict):
            out.update(flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def differences(locked: dict, mine: dict) -> list[str]:
    """The settings that differ, as lines: `difficulty.max_agent_hours: locked 4.0, here 8.0`."""
    a, b = flatten(locked), flatten(mine)
    return [f"{k}: locked {a.get(k, '(not set)')!r}, here {b.get(k, '(not set)')!r}"
            for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


START = "0.0.0"


def version(lock: dict | None) -> str:
    """A lock's version, such as 0.1.2. Before anything is locked, and for a lock that says none, it is 0.0.0."""
    return (lock or {}).get("version") or START


def bumped(current: str, how: str) -> str:
    """The next version. A lock keeps the version unless its owner asks for a new one: `patch` for a small
    change, such as a skill's wording (0.0.1 to 0.0.2), `minor` when settings changed (0.1.0 to 0.2.0).
    1.0.0 is the index's release, which `hm release` sets."""
    major, minor, patch = (int(n) for n in (current.split(".") + ["0", "0"])[:3])
    if how == "":
        return f"{major}.{minor}.{patch}"
    if how == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if how == "minor":
        return f"{major}.{minor + 1}.0"
    if how == "release" and major == 0:
        return "1.0.0"
    raise ValueError("A version goes up by patch or by minor. 1.0.0 is the index's release: hm release sets it, once.")


def write(ledger_dir, ident, cfg, note: str = "", bump: str = "") -> dict:
    """Append the owner's lock: the settings. The roster is not in it: it may change at any time."""
    from .store import MANUAL, Ledger, Shard
    lock = current(Ledger(ledger_dir).events)
    shard = Shard(ledger_dir, BENCH, ident, MANUAL, cfg.protocol)
    return shard.append("protocol", version=bumped(version(lock), bump), settings=cfg.protocol_settings(),
                        requires=cfg.requires.harbor_master, note=note,
                        replaces=lock["protocol"] if lock and lock["protocol"] != cfg.protocol else "")
