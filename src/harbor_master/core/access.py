"""Before a real curation spends anything: does this machine have everything it needs, and does each part answer?

A key that is present can still be wrong or expired, a token that sees the task store may not write to it, a clone may
not be allowed to push, and Docker may not be running. Each would fail hours into a run. So each is tried once, with
the smallest real request there is, and every problem is listed together. Nothing here writes or spends anything that
matters: key checks read an account or list models (one a few-token model call, for a Claude sign-in, which has no free
check), the push is a dry run, and the store is only asked whether this token may write. Each agent of the difficulty
suite is also started once in a small container, as a trial would start it (the same program, mounted the same way,
with the same credentials), and asked to run one shell command on its model: a few cents, and a broken agent is found
before a single trial is counted as failed because of it.
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        asyncio.run(claude.ask(model, "Answer with ok = true.", "Check.", schema, attempts=1, waits=()))   # a check: no waiting
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


IMAGE = "python:3.12-slim"          # small, with certificates, and the C library both agents' programs are built for


def agent_runs(agent: str, model: str, version: str) -> str:
    """Why an agent of the suite cannot run a command in a container here, or "". It is started as a trial starts it: its
    pinned program mounted at /usr/local/bin, and the credentials a trial gets. It is asked to write a word to a file,
    and the file is read after it ends: an agent that answers but cannot run a command (a missing helper program, a
    refused sign-in) fails here, not in a trial."""
    from ..backends import agents
    mounted = agents.mounts(agent, version)
    if not mounted:
        return ""           # no program of its own to mount: Harbor installs the agent in each container, and is trusted to
    word = f"hm-check-{secrets.token_hex(4)}"
    ask = f"Run this shell command with your shell tool: echo {word} > /root/hm-check . Then reply done."
    env = {k: v for k, v in keys.agent_env(agent).items() if k != "CODEX_AUTH_JSON_PATH"}
    cmd = ["docker", "run", "--rm", *(a for k in env for a in ("-e", k))]       # the values come from the environment, never the command line
    for m in mounted:
        cmd += ["-v", f"{m['source']}:{m['target']}:ro"]
    with tempfile.TemporaryDirectory() as home:
        signin = keys.codex_sign_in() if agent == "codex" else None
        if signin is not None and signin.is_file():         # a copy: Codex rewrites its sign-in when it refreshes it
            Path(home, "auth.json").write_bytes(signin.read_bytes())
            cmd += ["-v", f"{home}/auth.json:/root/.codex/auth.json"]       # only the file: what else it writes stays inside
        if agent == "claude-code":
            run = f"claude -p {_quote(ask)} --model {_quote(model)} --allowedTools Bash"
        else:
            run = (f"codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -m {_quote(model)} "
                   f"-c model_reasoning_effort=low {_quote(ask)}")
        cmd += [IMAGE, "bash", "-c", f"cd /root && timeout 170 {run} 2>&1 | tail -n 20; echo; echo \"HM_FILE=$(cat /root/hm-check 2>/dev/null)\""]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=400, env={**os.environ, **env})
        except subprocess.TimeoutExpired:
            return f"{agent} did not finish one shell command in a container within a few minutes"
    out = r.stdout + r.stderr
    if f"HM_FILE={word}" in out:
        return ""
    said = [line.strip() for line in out.splitlines() if line.strip() and not line.startswith("HM_FILE=")]
    return f"{agent} with {model} could not run a shell command in a container: {keys.scrub(' | '.join(said[-3:]))[:300]}"


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def jobs_of(cfg, ws: Path) -> dict[str, Callable[[], str]]:
    """Every check, by what it shows on the terminal."""
    jobs: dict[str, Callable[[], str]] = {"the task store": lambda: store(cfg), "the index's repository": lambda: repository(ws),
                                          "Docker": docker}
    for name in sorted(wanted(cfg)):
        if name in KEY_CHECKS:
            jobs[f"key {name}"] = lambda name=name: KEY_CHECKS[name](keys.get(name) or "")
        elif name == "CODEX_AUTH_JSON_PATH":
            jobs["the Codex sign-in"] = codex_signin
    for tier in ("worker", "frontier"):     # a Claude model a curation agent uses: one session of a few tokens on it
        t = cfg.tier(tier)
        if t.provider in ("claude-agent-sdk", "claude"):
            jobs[f"the {tier} model, {t.model}"] = lambda model=t.model: claude_signin(model)
    for e in cfg.difficulty.suite:
        jobs[f"{e.agent} with {e.model}, in a container"] = lambda e=e: agent_runs(e.agent, e.model, e.version)
    return jobs


def check(cfg, ws: Path, say: Callable[[str, str, float], None] | None = None) -> list[str]:
    """Everything that keeps a real curation from working here, tried side by side; `say(what, why, seconds)` is told of
    each as it ends, why being "" when it is fine. Returns the problems: empty when it can start."""
    jobs, start, out = jobs_of(cfg, ws), time.time(), []
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        running = {pool.submit(f): what for what, f in jobs.items()}
        for done in as_completed(running):
            what = running[done]
            try:
                why = done.result()
            except Exception as err:        # a check that breaks is a problem to show, not a crash
                why = f"the check failed: {keys.scrub(str(err))[:160]}"
            if say is not None:
                say(what, why, time.time() - start)
            if why:
                out.append(why if not what.startswith("key ") else f"{what[4:]} in secrets.env: {why}")
    return out


def problems(cfg, ws: Path) -> list[str]:
    return check(cfg, ws)


__all__ = ["problems", "check", "wanted", "store", "repository", "docker", "agent_runs"]
