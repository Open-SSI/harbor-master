"""One Codex session that ends in a typed result: the worker and frontier tiers on OpenAI models.

The same job as claude.py, through the Codex SDK, which starts the Codex it ships with. It takes either credential:
the sign-in to a ChatGPT subscription that `codex login` leaves in ~/.codex/auth.json (or the file CODEX_AUTH_JSON_PATH
names), or an OpenAI API key, OPENAI_API_KEY. Where both are set, the sign-in is used, as for Claude. A key is logged in
inside a throwaway Codex home, so the person's own ~/.codex is never touched. The session may read files and nothing
else: the sandbox is read-only, and every request to go beyond it is refused.
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
    """The Codex sign-in file, the same one an agent trial is given (keys.codex_sign_in)."""
    return keys.codex_sign_in()


def credential() -> tuple[dict[str, str], str]:
    """How a session signs in: (environment, API key to log in with). The sign-in when there is one, else the key."""
    found = sign_in()
    if found is not None:
        if found.name != "auth.json":
            raise NotImplementedBackend(f"CODEX_AUTH_JSON_PATH names {found.name}. Codex reads its sign-in from a file called auth.json.")
        return {"CODEX_HOME": str(found.parent)}, ""        # the folder Codex reads auth.json from
    key = keys.get("OPENAI_API_KEY")
    if key:
        return {}, key
    raise NotImplementedBackend("No OpenAI credential for Codex. Either sign in to a ChatGPT subscription with `codex login`, "
                                "or put OPENAI_API_KEY in your keys file. One is enough")


async def ask(model: str, system: str, prompt: str, schema: dict, cwd: Path | None = None,
              max_turns: int = 6, effort: str | None = None, attempts: int = 2, write: bool = False, web: bool = False) -> Reply:
    """One session. With `cwd`, the model may read files there, and with `write` also change them. With `web`, it may
    search the web (Codex's live web search). Raises RuntimeError when no typed result comes back.

    `max_turns` is not used: a Codex turn runs until the model gives its final answer. The SDK reports
    tokens and no price: sessions.ask costs them at list price.
    """
    from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, CodexError, Sandbox

    (env, api_key), problem = credential(), "no result"
    with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryDirectory() as home:    # an empty folder, and a throwaway Codex home
        where = str(cwd) if cwd else empty
        if api_key:
            env = {**env, "CODEX_HOME": home}           # the key is logged in here, never in the person's own ~/.codex
        for _ in range(attempts):
            try:
                async with AsyncCodex(CodexConfig(env=env, cwd=where, client_name="harbor_master", client_title="Harbor-Master")) as codex:
                    if api_key:
                        await codex.login_api_key(api_key)
                    thread = await codex.thread_start(model=model, cwd=where, developer_instructions=system, ephemeral=True,
                                                      sandbox=Sandbox.workspace_write if write and cwd else Sandbox.read_only, approval_mode=ApprovalMode.deny_all,
                                                      config={"web_search": "live"} if web else None)
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
