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


def write(ledger_dir, ident, cfg, note: str = "", registry: dict | None = None) -> dict:
    """Append the owner's lock: the settings, and the checksums of what is locked beside them.
    `registry` gives those checksums when not everything on disk is meant, as when a run locks one new adapter."""
    from .. import templates
    from .store import MANUAL, Ledger, Shard
    lock = current(Ledger(ledger_dir).events)
    shard = Shard(ledger_dir, BENCH, ident, MANUAL, cfg.protocol)
    return shard.append("protocol", settings=cfg.protocol_settings(), registry=registry if registry is not None else templates.digests(ledger_dir),
                        requires=cfg.requires.harbor_master, note=note,
                        replaces=lock["protocol"] if lock and lock["protocol"] != cfg.protocol else "")
