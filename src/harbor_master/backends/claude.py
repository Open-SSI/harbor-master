"""Calling Claude through the Claude Agent SDK, with a typed result.

Authenticated by the subscription token (CLAUDE_CODE_OAUTH_TOKEN) or an API key, whichever
the keys file has. A call either answers from the prompt alone or works in a directory with
read-only tools, and it always ends in one object that matches the given JSON schema.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from ..core import keys
from .base import NotImplementedBackend, Unreachable

READ_ONLY = ["Read", "Glob", "Grep"]
WEB = ["WebSearch", "WebFetch"]           # to find something on the web: search, and read the pages found
EDIT = [*READ_ONLY, "Write", "Edit"]      # a repair: it changes files in its own copy of a task, and runs nothing

STARTUP_SECONDS = 180       # how long Claude Code may take to start before the SDK gives up on it (the SDK's own: 60)
WAITS = (30, 60, 120, 240)  # seconds between tries when the session could not be had, not when the model failed
# What a session that could not be had looks like, as against a model that answered badly: Claude Code that did not
# start or that ended with no result, a broken connection, an overloaded or rate-limited API.
UNREACHABLE = ("control request timeout", "no result", "cliconnectionerror", "processerror", "connection error",
               "connection refused", "connection reset", "timed out", "overloaded", "rate limit", "rate_limit",
               "api error: 5", "api error: 429")


def unreachable(problem: str) -> bool:
    """Whether a failed session failed for want of a connection, so that waiting may help."""
    low = problem.lower()
    return any(mark in low for mark in UNREACHABLE)


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
              max_turns: int = 6, effort: str | None = None, attempts: int = 2, write: bool = False, web: bool = False,
              waits: tuple[int, ...] | None = None) -> Reply:
    """One session. With `cwd`, the model may read files there, and with `write` also change them. With `web`, it may
    search the web and read pages. Raises RuntimeError when the model gave no typed result in `attempts` tries, and
    Unreachable when the session could not be had even after waiting (`waits`, else WAITS)."""
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query     # here, so that hm starts without loading it
    tools = (EDIT if write else READ_ONLY) if cwd else []      # with no directory and no web, the model has no tools at all
    tools = [*tools, *WEB] if web else tools
    options = ClaudeAgentOptions(
        model=model, system_prompt=system, max_turns=max_turns, setting_sources=[],
        tools=tools, allowed_tools=tools, cwd=str(cwd) if cwd else None,
        permission_mode="dontAsk" if tools else None, effort=effort,
        output_format={"type": "json_schema", "schema": schema}, env=_env())
    os.environ.setdefault("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", str(STARTUP_SECONDS * 1000))   # read by the SDK, in this process
    waits = WAITS if waits is None else waits
    problem, failed, waited = "no result", 0, 0
    while True:
        result = None
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    result = message
        except Exception as err:        # the SDK raises on an error result, such as running out of turns
            problem = f"{type(err).__name__}: {str(err)[:200]}"
        else:
            if result is not None and not result.is_error and isinstance(result.structured_output, dict):
                usage = result.usage or {}
                tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                              "cache_creation_input_tokens"))
                n = lambda k: int(usage.get(k) or 0)  # noqa: E731
                return Reply(result.structured_output, float(result.total_cost_usd or 0.0), tokens, n("input_tokens"),
                             n("cache_read_input_tokens"), n("cache_creation_input_tokens"), n("output_tokens"))
            problem = (result.result or result.subtype or "no typed result")[:200] if result else "no result"
            status = getattr(result, "api_error_status", None) if result else None
            if status:                  # the API call failed: 429 and 5xx are waited out, 401 and the like are not
                problem = f"API error: {status}: {problem}"
        if unreachable(problem):        # not the model's doing: wait, longer each time, and try again
            if waited == len(waits):
                raise Unreachable(f"Claude ({model}) could not be reached, after {len(waits) + 1} tries "
                                  f"over {sum(waits) / 60:.1f} minutes: {problem}")
            await asyncio.sleep(waits[waited])
            waited += 1
            continue
        failed += 1
        if failed >= attempts:
            raise RuntimeError(f"Claude ({model}) did not return a typed result: {problem}")
