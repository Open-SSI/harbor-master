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
