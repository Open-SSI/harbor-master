"""One agent session on a model tier, whichever provider the tier names (DESIGN.md Section 2)."""
from __future__ import annotations

from pathlib import Path

from ..core import prices
from ..core.config import Tier
from . import claude, codex
from .base import NotImplementedBackend

CLAUDE = ("claude-agent-sdk", "claude")
PROVIDERS = (*CLAUDE, *codex.PROVIDERS)        # what the worker and frontier tiers can run on


async def ask(tier: Tier, system: str, prompt: str, schema: dict, cwd: Path | None = None, max_turns: int = 6,
              write: bool = False, web: bool = False) -> claude.Reply:
    """A session that ends in one object fitting `schema`. With `cwd`, the model may read files there, and with `write`
    change them. With `web`, it may search the web and read pages."""
    if tier.provider in CLAUDE:
        reply = await claude.ask(tier.model, system, prompt, schema, cwd=cwd, max_turns=max_turns, write=write, web=web)
    elif tier.provider in codex.PROVIDERS:
        reply = await codex.ask(tier.model, system, prompt, schema, cwd=cwd, max_turns=max_turns, write=write, web=web)
    else:
        raise NotImplementedBackend(f"Provider {tier.provider!r} is not built. A tier takes \"claude-agent-sdk\" or \"codex\".")
    if not reply.cost:      # no cost reported, as with Codex: its tokens at list price, so that it is not recorded as free
        reply.cost = prices.estimate(tier.model, reply.fresh_in, reply.cached, reply.cache_write, reply.out) or 0.0
    return reply
