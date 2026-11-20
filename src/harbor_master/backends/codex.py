"""One Codex session that ends in a typed result: the worker and frontier tiers on OpenAI models.

The same job as claude.py, through the Codex SDK, which starts the Codex it ships with. It uses the
sign-in to a ChatGPT subscription that `codex login` leaves in ~/.codex/auth.json, or in the file
CODEX_AUTH_JSON_PATH names. The session may read files and nothing else: the sandbox is read-only,
and every request to go beyond it is refused.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ..core import keys
from .base import NotImplementedBackend
from .claude import Reply

PROVIDERS = ("codex", "codex-sdk")
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def sign_in() -> Path | None:
    """The file that holds the Codex sign-in: the one CODEX_AUTH_JSON_PATH names, else Codex's own."""
    named = keys.get("CODEX_AUTH_JSON_PATH")
    if named:
        return Path(named)
    usual = Path.home() / ".codex" / "auth.json"
    return usual if usual.is_file() else None


def _env() -> dict[str, str]:
    found = sign_in()
    if found is None:
        raise NotImplementedBackend("No Codex sign-in found. Sign in with `codex login`, and if the file it writes is not "
                                    "~/.codex/auth.json, give its path as CODEX_AUTH_JSON_PATH in your keys file")
    if found.name != "auth.json":
        raise NotImplementedBackend(f"CODEX_AUTH_JSON_PATH names {found.name}. Codex reads its sign-in from a file called auth.json.")
    return {"CODEX_HOME": str(found.parent)}        # the folder Codex reads auth.json from


async def ask(model: str, system: str, prompt: str, schema: dict, cwd: Path | None = None,
              max_turns: int = 6, effort: str | None = None, attempts: int = 2) -> Reply:
    """One session. With `cwd`, the model may read files there. Raises RuntimeError when no typed result comes back.

    `max_turns` is not used: a Codex turn runs until the model gives its final answer. The SDK reports
    tokens and no price: sessions.ask costs them at list price.
    """
    from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, CodexError, Sandbox

    env, problem = _env(), "no result"
    with tempfile.TemporaryDirectory() as empty:        # with no directory, the model is given an empty one
        where = str(cwd) if cwd else empty
        for _ in range(attempts):
            try:
                async with AsyncCodex(CodexConfig(env=env, cwd=where, client_name="harbor_master", client_title="Harbor-Master")) as codex:
                    thread = await codex.thread_start(model=model, cwd=where, developer_instructions=system, ephemeral=True,
                                                      sandbox=Sandbox.read_only, approval_mode=ApprovalMode.deny_all)
                    result = await thread.run(prompt, output_schema=schema, effort=effort if effort in EFFORTS else None)
            except CodexError as err:
                problem = f"{type(err).__name__}: {str(err)[:200]}"
                continue
            status = getattr(result.status, "value", str(result.status))
            if status != "completed" or not result.final_response:
                problem = (str(getattr(result.error, "message", "") or result.error or status))[:200]
                continue
            try:
                data = json.loads(result.final_response)
            except json.JSONDecodeError:
                problem = "the final answer was not JSON"
                continue
            if isinstance(data, dict):
                total = result.usage.total if result.usage else None
                if total is None:
                    return Reply(data, 0.0, 0)
                cached = int(total.cached_input_tokens or 0)        # counted inside input_tokens, as OpenAI reports it
                return Reply(data, 0.0, int(total.total_tokens), int(total.input_tokens) - cached, cached, 0,
                             int(total.output_tokens))
            problem = "the final answer was not an object"
    raise RuntimeError(f"Codex ({model}) did not return a typed result: {problem}")
