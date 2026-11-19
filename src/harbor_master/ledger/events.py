"""Ledger events: their identity and the keys that say "this has been done already"."""
from __future__ import annotations

import hashlib
import json
from typing import Any

EVENT_TYPES = {"submission", "decision", "trial", "repair", "report", "verdict", "claim", "brief", "roster", "protocol",
               "usage", "skip"}       # usage: what the model tiers were called for. skip: a benchmark a run left out, and why
TRIAL_PURPOSES = {"oracle", "noop", "probe", "difficulty", "parity", "benchmark"}
TIER_RANK = {"code": 0, "decision": 1, "worker": 2, "frontier": 3, "human": 4}
CONTRIBUTORS = "contributors.toml"


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_id(event: dict) -> str:
    body = {k: v for k, v in event.items() if k not in ("id", "sig")}
    return hashlib.sha256(canonical(body).encode()).hexdigest()


def slug(benchmark: str) -> str:
    return benchmark.replace("/", "__").replace("@", "_at_") or "_session"


def trial_slot(e: dict) -> tuple:
    """(task digest, agent and version, model, reasoning effort, protocol, trial index). Purpose is not part of it:
    a difficulty trial and a later benchmark trial of the same agent are the same evidence."""
    return (e["digest"], e.get("agent", ""), e.get("agent_version", ""), e.get("model", ""),
            e.get("reasoning_effort", ""), e["protocol"], e.get("trial_index", 0))


def decision_key(e: dict) -> tuple:
    return (e["digest"], e.get("stage", ""), e.get("gate", ""), e["protocol"], e.get("tier", ""))


def duplicate_key(e: dict) -> tuple | None:
    t = e["type"]
    kind = t + ":assumed" if e.get("assumed") else t        # an assumed record never fills a real slot
    if t == "trial":
        return (kind, *trial_slot(e))
    if t == "decision":
        return (kind, *decision_key(e))
    if t == "verdict":
        return ("verdict", e["digest"], e["runner"], e.get("value", ""))
    if t == "report":
        return (kind, e["digest"], e["protocol"])
    return None
