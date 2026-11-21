"""Before a real curation spends anything: does this machine have everything it needs, and does each part answer?

A key that is present can still be wrong or expired, a token that sees the task store may not write to it, a clone may
not be allowed to push, and Docker may not be running. Each would fail hours into a run. So each is tried once, with
the smallest real request there is, and every problem is listed together. Nothing here writes or spends anything that
matters: key checks read an account or list models (one a few-token model call, for a Claude sign-in, which has no free
check), the push is a dry run, and the store is only asked whether this token may write.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import keys

TIMEOUT = 60


def _get(url: str, headers: dict[str, str]) -> str:
    """"" when the request is accepted, else why not. The key is in the headers only, and never in what is returned."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT):
            return ""
    except urllib.error.HTTPError as err:
        return "it was refused (HTTP 401: wrong or expired)" if err.code in (401, 403) else f"HTTP {err.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        return f"the service did not answer ({type(err).__name__})"


def openrouter(key: str) -> str:
    return _get("https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {key}"})


def anthropic(key: str) -> str:
    return _get("https://api.anthropic.com/v1/models", {"x-api-key": key, "anthropic-version": "2023-06-01"})


def openai(key: str) -> str:
    return _get("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"})


def claude_signin(model: str) -> str:
    """A Claude subscription sign-in has no free check: one session of a few tokens, on the given model."""
    import asyncio

    from ..backends import claude
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    try:
        asyncio.run(claude.ask(model, "Answer with ok = true.", "Check.", schema, attempts=1))
        return ""
    except Exception as err:        # the SDK raises many kinds: a refused token, no network, a model it does not know
        return f"a test call to {model} failed: {keys.scrub(str(err))[:160]}"


KEY_CHECKS: dict[str, Callable[[str], str]] = {"OPENROUTER_API_KEY": openrouter, "ANTHROPIC_API_KEY": anthropic, "OPENAI_API_KEY": openai}


def used(names: list[str]) -> str:
    """Which of a configuration's credentials will be used: a subscription's sign-in where there is one, else the first
    key that is set, the same for curation agents and agent trials. "" when none is."""
    if "CLAUDE_CODE_OAUTH_TOKEN" in names and keys.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "CLAUDE_CODE_OAUTH_TOKEN"
    if "CODEX_AUTH_JSON_PATH" in names and keys.codex_sign_in() is not None:
        return "CODEX_AUTH_JSON_PATH"
    return next((n for n in names if keys.get(n)), "")


def wanted(cfg) -> set[str]:
    """The credentials this protocol will use: for every model tier, the difficulty suite and the worker's own tries,
    the one each will sign in with. A key that is set but not used, beside a sign-in, is not tried."""
    return {u for what, var, _, _ in keys.doctor(cfg) if what != "task store"
            if (u := used([n.strip() for n in var.split(" or ")]))}


def codex_signin() -> str:
    """A Codex sign-in is a file that `codex login` wrote: it must be there, and called auth.json."""
    found = keys.codex_sign_in()
    if found is None or not found.is_file():
        return f"the Codex sign-in file {found or '~/.codex/auth.json'} is not there. Sign in again with `codex login`"
    return "" if found.name == "auth.json" else f"{found} is not a Codex sign-in file (auth.json)"


def store(cfg) -> str:
    """Why this person cannot put an accepted task in the index's store, or "": the store must take a write from them."""
    from ..tasks import store as store_mod
    try:
        there = store_mod.remote(cfg)
        if there is None:
            return ""
        why = there.writable()
        return f"The task store: {why}" if why else ""
    except store_mod.StoreError as err:
        return f"The task store: {err}"


def repository(ws: Path) -> str:
    """Why this clone cannot share its records, or "": its repository must take a push from this machine's git.
    A dry run to a branch nobody uses, so that nothing is sent and nothing is changed."""
    from ..ledger import Sync
    sync = Sync(ws)
    if not sync.enabled:
        return ""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        r = subprocess.run(["git", "push", "--dry-run", "--quiet", "origin", "HEAD:refs/heads/hm-access-check"], cwd=ws,
                           capture_output=True, text=True, timeout=TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return "The index's repository did not answer a push."
    if r.returncode == 0:
        return ""
    lines = [line.strip() for line in r.stderr.splitlines() if line.strip()]
    said = next((line for line in lines if line.startswith(("fatal:", "ERROR:", "error:", "remote:"))), lines[-1] if lines else "")
    return (f"The index's repository does not take a push from you ({said}). Your claims and records could not be "
            "shared: ask its owner for write access on GitHub.")


def docker() -> str:
    """Why Harbor cannot run a task here, or "": Docker must answer, and the harbor command must be there."""
    from ..backends import harbor
    if shutil.which("docker") is None:
        return "Docker is not installed. Harbor runs every task in a container."
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=TIMEOUT).returncode:
            return "Docker is not running, or this user may not use it. Start it, then try again."
    except subprocess.TimeoutExpired:
        return "Docker did not answer."
    found = harbor.binary()
    if shutil.which(found) is None and not Path(found).is_file():
        return "The harbor command is not installed. It comes with Harbor-Master: reinstall it."
    return ""


def problems(cfg, ws: Path) -> list[str]:
    """Everything that keeps a real curation from working here, tried side by side. Empty when it can start."""
    jobs: dict[str, Callable[[], str]] = {"store": lambda: store(cfg), "repository": lambda: repository(ws), "docker": docker}
    for name in sorted(wanted(cfg)):
        if name in KEY_CHECKS:
            jobs[name] = lambda name=name: KEY_CHECKS[name](keys.get(name) or "")
        elif name == "CODEX_AUTH_JSON_PATH":
            jobs[name] = codex_signin
        elif name == "CLAUDE_CODE_OAUTH_TOKEN":
            model = next((cfg.tier(t).model for t in ("worker", "frontier") if cfg.tier(t).provider in ("claude-agent-sdk", "claude")),
                         "claude-haiku-5-5")
            jobs[name] = lambda model=model: claude_signin(model)
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        said = dict(zip(jobs, pool.map(lambda f: f(), jobs.values())))
    out = []
    for name, why in said.items():
        if why:
            out.append(why if name in ("store", "repository", "docker") else f"{name} in secrets.env: {why}")
    return out


__all__ = ["problems", "wanted", "store", "repository", "docker"]
