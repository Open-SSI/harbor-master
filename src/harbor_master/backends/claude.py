"""Calling Claude through the Claude Agent SDK, with a typed result.

Authenticated by the subscription token (CLAUDE_CODE_OAUTH_TOKEN) or an API key, whichever
the keys file has. A call either answers from the prompt alone or works in a directory with
read-only tools, and it always ends in one object that matches the given JSON schema.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..core import keys
from .base import NotImplementedBackend

READ_ONLY = ["Read", "Glob", "Grep"]


@dataclass
class Reply:
    data: dict
    cost: float                 # what the provider reported. 0 when it reported none: sessions.ask then estimates it
    tokens: int
    fresh_in: int = 0           # input tokens that were not read from a cache
    cached: int = 0             # input tokens read from a cache
    cache_write: int = 0        # input tokens written to a cache
    out: int = 0


def _env() -> dict[str, str]:
    token, key = keys.get("CLAUDE_CODE_OAUTH_TOKEN"), keys.get("ANTHROPIC_API_KEY")
    if token:
        return {"CLAUDE_CODE_OAUTH_TOKEN": token, "ANTHROPIC_API_KEY": ""}     # the subscription, not the API
    if key:
        return {"ANTHROPIC_API_KEY": key}
    raise NotImplementedBackend("No Claude credential found. Set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY in your keys file")


async def ask(model: str, system: str, prompt: str, schema: dict, cwd: Path | None = None,
              max_turns: int = 6, effort: str | None = None, attempts: int = 2) -> Reply:
    """One session. With `cwd`, the model may read files there. Raises RuntimeError when no typed result comes back."""
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query     # here, so that hm starts without loading it
    options = ClaudeAgentOptions(
        model=model, system_prompt=system, max_turns=max_turns, setting_sources=[],
        tools=READ_ONLY if cwd else [],      # with no directory, the model has no tools at all
        allowed_tools=READ_ONLY if cwd else [], cwd=str(cwd) if cwd else None,
        permission_mode="dontAsk" if cwd else None, effort=effort,
        output_format={"type": "json_schema", "schema": schema}, env=_env())
    problem = "no result"
    for _ in range(attempts):
        result = None
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    result = message
        except Exception as err:        # the SDK raises on an error result, such as running out of turns
            problem = f"{type(err).__name__}: {str(err)[:200]}"
            continue
        if result is not None and not result.is_error and isinstance(result.structured_output, dict):
            usage = result.usage or {}
            tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                          "cache_creation_input_tokens"))
            n = lambda k: int(usage.get(k) or 0)  # noqa: E731
            return Reply(result.structured_output, float(result.total_cost_usd or 0.0), tokens, n("input_tokens"),
                         n("cache_read_input_tokens"), n("cache_creation_input_tokens"), n("output_tokens"))
        problem = (result.result or result.subtype or "no typed result")[:200] if result else "no result"
    raise RuntimeError(f"Claude ({model}) did not return a typed result: {problem}")
