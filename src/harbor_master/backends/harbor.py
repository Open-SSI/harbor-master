"""Running a task through Harbor: the reference solution, a no-op, a mutant, or an agent.

Each run is one `harbor run` job in Docker. The result comes from the trial's own
result.json, including the agent version that actually ran and Harbor's task checksum.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tomllib
import uuid
from datetime import datetime
from pathlib import Path

from dataclasses import dataclass
from typing import Awaitable, Callable

from ..core import keys, prices
from .base import TaskDropped, TrialResult

WHY = re.compile(r"error|denied|failed to solve|not found|no such|cannot|could not|unable|missing|undefined|no module|timed? ?out|killed|exit code|return code", re.I)


def explain(err: dict | None, limit: int = 1200) -> str:
    """What went wrong in a Harbor trial, said so that a person or a model can act on it: the exception's type, its first
    line, and the lines further down that name the cause, such as Docker's "pull access denied". Harbor's own message
    puts the whole command line first, and a plain cut at a few hundred characters keeps only that."""
    if not err:
        return ""
    message = str(err.get("exception_message") or "")
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    first = lines[0].split(" Command: ")[0] if lines else ""        # the command line itself says nothing about the cause
    head = first[:160] + ("..." if len(first) > 160 else "")
    causes = list(dict.fromkeys(line[:240] for line in lines[1:] if WHY.search(line) and not line.startswith("Command:")))
    shown = causes if len(causes) <= 5 else [*causes[:3], "...", *causes[-2:]]     # the first error says why; the last says where
    text = f"{err.get('exception_type', 'Error')}: {head}" + (" | " + " | ".join(shown) if shown else "")
    return text[:limit]


def binary() -> str:
    """The `harbor` command that was installed with Harbor-Master, as its dependency. An older one that
    happens to be on the PATH is used only when that one is missing."""
    beside = Path(sys.executable).parent / "harbor"
    return str(beside) if beside.is_file() else "harbor"


def _seconds(span: dict | None) -> float:
    if not span or not span.get("started_at") or not span.get("finished_at"):
        return 0.0
    f = lambda t: datetime.fromisoformat(t.replace("Z", "+00:00"))  # noqa: E731
    return (f(span["finished_at"]) - f(span["started_at"])).total_seconds()


def own_limit(task_dir: Path, section: str, key: str = "timeout_sec", default: float = 0.0) -> float:
    """A time limit the task itself sets, in seconds, such as [agent] timeout_sec. `default` when it sets none."""
    try:
        return float(tomllib.loads((task_dir / "task.toml").read_text()).get(section, {}).get(key) or default)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return default


def agent_limit(task_dir: Path) -> float:
    """The agent time limit the task itself sets, in seconds. 0 when it sets none."""
    return own_limit(task_dir, "agent")


HARBOR_BUILD_DEFAULT = 600.0        # what Harbor allows for building an environment when the task names nothing


async def _stop(proc: asyncio.subprocess.Process, grace: float = 30.0) -> None:
    """End a Harbor job that is no longer wanted. It is interrupted first, as ctrl+c would, so that Harbor
    can stop the containers it started. Only if it does not go in time is it killed."""
    import contextlib
    import signal
    with contextlib.suppress(ProcessLookupError):
        proc.send_signal(signal.SIGINT)
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


@dataclass
class Watch:
    """How a running trial is watched. Every `every` seconds its logs are looked at; once they have been still for `quiet`
    seconds, `judge` is called with their last lines and how long they have been still. It answers "wait" (it is working:
    look again later), "retry" (it is hung: stop it and run it again) or "drop" (stop it, and drop the task)."""
    every: float
    quiet: float
    judge: Callable[[str, float], Awaitable[str]]
    say: Callable[[str, float], None] | None = None     # what the trial is doing, in a line, and how long it has run
    say_every: float = 300.0
    spend: Callable[[float], None] | None = None        # an agent trial's cost so far, each time its logs are looked at


def _container_of(job: Path) -> str:
    """The running container of a job's trial: Harbor names each trial's Docker Compose project after the trial."""
    import subprocess
    try:
        from harbor.environments.docker.docker import _sanitize_docker_compose_project_name as project
    except ImportError:
        project = str.lower
    trials = [project(d.name) for d in job.iterdir() if d.is_dir()] if job.is_dir() else []
    if not trials:
        return ""
    try:
        listed = subprocess.run(["docker", "ps", "--format", '{{.ID}} {{.Label "com.docker.compose.project"}} {{.Label "com.docker.compose.service"}}'],
                                capture_output=True, text=True, timeout=20).stdout.split("\n")
    except (OSError, subprocess.TimeoutExpired):
        return ""
    found = [p for p in (line.split() for line in listed) if len(p) >= 2 and p[1].startswith(tuple(trials))]
    return next((p[0] for p in found if p[2:] == ["main"]), found[0][0] if found else "")


def inside(job: Path, lines: int = 60) -> tuple[int, str]:
    """What the solution or the agent has written so far inside the trial's container, which reaches this machine only
    when the trial ends: its size in bytes, and its last lines. (0, "") when there is no container to read."""
    import subprocess
    cid = _container_of(job)
    if not cid:
        return 0, ""
    script = ('for f in /logs/agent/*.txt /logs/agent/*.log; do [ -f "$f" ] || continue; '
              f'echo "=== $f $(wc -c < "$f")"; tail -n {lines} "$f"; done')
    try:
        out = subprocess.run(["docker", "exec", cid, "sh", "-c", script], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0, ""
    size = sum(int(line.split()[-1]) for line in out.splitlines() if line.startswith("=== ") and line.split()[-1].isdigit())
    return size, out


ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
USAGE = {   # agent -> a script that prints its token counts so far, from what it is writing in its container
    "claude-code": "grep -ohE '\"id\":\"msg_[A-Za-z0-9]+\"|\"type\":\"result\"|\"(input_tokens|cache_creation_input_tokens|"
                   "cache_read_input_tokens|output_tokens)\":[0-9]+' /logs/agent/claude-code.txt 2>/dev/null",
    "codex": "grep -rhoE '\"total_token_usage\":\\{[^}]*\\}' /logs/agent/sessions 2>/dev/null | tail -n 1",
}


def tokens_so_far(agent: str, out: str) -> tuple[int, int, int, int]:
    """(fresh input, cached input, cache writes, output) from what USAGE printed. Claude Code repeats a message's usage
    on each of its parts, so each message counts once; Codex keeps a running total, so its last one is all of it."""
    if agent == "codex":
        n = dict((k, int(v)) for k, v in re.findall(r'"(\w+)":(\d+)', out))
        cached = n.get("cached_input_tokens", 0)
        return n.get("input_tokens", 0) - cached, cached, n.get("cache_write_input_tokens", 0), n.get("output_tokens", 0)
    by_message: dict[str, dict[str, int]] = {}
    current = ""
    for token in out.split():
        if token == '"type":"result"':          # the final summary repeats the totals: the messages are already counted
            break
        if token.startswith('"id":'):
            current = token.split('"')[3]
            continue
        key, _, value = token.strip('"').partition('":')
        if current and value.isdigit():
            slot = by_message.setdefault(current, {})
            slot[key] = max(slot.get(key, 0), int(value))
    total = lambda k: sum(m.get(k, 0) for m in by_message.values())      # noqa: E731
    return (total("input_tokens"), total("cache_read_input_tokens"), total("cache_creation_input_tokens"),
            total("output_tokens"))


def spent_so_far(job: Path, agent: str, model: str) -> float | None:
    """What an agent trial that is still running has cost so far, at list price; None when it cannot be read."""
    import subprocess
    script = USAGE.get(agent)
    cid = _container_of(job) if script else ""
    if not cid:
        return None
    try:
        out = subprocess.run(["docker", "exec", cid, "sh", "-c", script], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return prices.estimate(model, *tokens_so_far(agent, out))


def _name(path: str) -> str:
    return path.rstrip("/").split("/")[-1] or path


def plain_command(command: str) -> str:
    """A shell command an agent ran, said in a few words: what it does, not its text."""
    cmd = " ".join(str(command).split())
    if cmd.startswith(("bash -lc ", "/bin/bash -lc ", "sh -c ")):          # Codex wraps each command in a shell
        cmd = cmd.split(" ", 2)[-1].strip("'\"")
    parts = [x.strip() for x in re.split(r"&&|;|\|\|", cmd) if x.strip()]
    parts = [x for x in parts if x.split()[0] not in ("cd", "export", "source", "set", "ulimit", "true")] or parts
    if parts and not re.search(r"<<", cmd):             # a heredoc's text is no command: read the whole thing
        cmd = parts[0] if len(parts) == 1 else next((x for x in parts if x.split()[0] not in ("echo", "sleep")), parts[-1])
    if cmd.split()[:1] == ["sleep"] or re.match(r"^(timeout \d+ )?(sleep|wait)\b", cmd):
        return "waiting"
    if m := re.search(r"(?:cat|tee)\s*>{1,2}\s*([^\s<;&|]+)|^>\s*([^\s<;&|]+)", cmd):
        return f"writing {_name(m.group(1) or m.group(2))}"
    if re.search(r"<<-?\s*['\"]?\w+", cmd) and re.search(r"\bpython3?\b", cmd):
        return "running a Python snippet"
    if m := re.search(r"\b(?:python3?|Rscript|julia|node|bash|sh)\s+(?:-u\s+)?([^\s;&|-][^\s;&|]*\.(?:py|R|jl|js|sh))", cmd):
        return f"running {_name(m.group(1))}"
    if re.search(r"\bpython3?\s+-c\b", cmd):
        return "running a Python snippet"
    if re.search(r"\b(pip3?|uv|conda|apt-get|apt|npm)\s+(install|add|sync)\b", cmd):
        return "installing packages"
    if re.search(r"\b(pytest|make|cargo|go test|lake build)\b", cmd):
        return "running tests or a build"
    cmd = re.sub(r"^(?:(?:cd|export|source)\s+[^;&]*(?:;|&&)\s*)+", "", cmd)
    first = cmd.split()[0] if cmd else ""
    if first in ("ls", "cat", "head", "tail", "find", "grep", "rg", "wc", "file", "tree", "sed", "awk", "du", "stat"):
        return "looking at files"
    return f"running {first}" if first else "running a command"


def _step(name: str, given: dict) -> str:
    """One tool call of Claude Code, in a few words."""
    path = given.get("file_path") or given.get("path") or given.get("notebook_path") or ""
    if name == "Bash":
        return plain_command(given.get("command", ""))
    if name in ("Write", "NotebookEdit"):
        return f"writing {_name(path)}"
    if name in ("Edit", "MultiEdit"):
        return f"editing {_name(path)}"
    if name == "Read":
        return f"reading {_name(path)}"
    if name in ("Grep", "Glob", "LS"):
        return "looking at files"
    if name in ("TodoWrite", "Task", "Agent"):
        return "planning"
    if name in ("WebSearch", "WebFetch"):
        return "reading the web"
    return f"using {name}"


def _said(text: str) -> str:
    first = re.split(r"(?<=[.!?])\s", " ".join(text.split()), maxsplit=1)[0]
    return f"says: {first[:120]}"


def now_doing(text: str) -> str:
    """The trial's latest step, in a few plain words: from an agent's event stream (Claude Code's or Codex's), what its
    last tool call does ("writing overdisp.py", "running fit.py") or the first sentence it last said; from a solution's
    output, its last line, a progress bar as it stands now."""
    rows = [r for r in text.splitlines() if r.strip() and not r.startswith("=== ")]
    for row in reversed(rows[-400:]):
        if not row.lstrip().startswith("{"):
            continue
        try:
            event = json.loads(row)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        if item is not None and event.get("type", "").startswith("item."):         # Codex
            kind = item.get("type", "")
            if kind == "command_execution":
                return plain_command(item.get("command", ""))
            if kind == "file_change":
                changed = [_name(c.get("path", "")) for c in item.get("changes") or [] if isinstance(c, dict)]
                return f"editing {', '.join(changed[:3])}" if changed else "editing files"
            if kind == "agent_message" and item.get("text", "").strip():
                return _said(item["text"])
            if kind == "reasoning":
                return "thinking"
            continue
        for part in reversed((event.get("message") or {}).get("content") or [] if event.get("type") == "assistant" else []):
            if part.get("type") == "tool_use":
                return _step(part.get("name", ""), part.get("input") or {})
            if part.get("type") == "text" and part.get("text", "").strip():
                return _said(part["text"])
    plain = [r for r in rows if not r.lstrip().startswith("{")]
    if not plain:
        return "thinking" if rows else ""
    last = ANSI.sub("", plain[-1]).split("\r")
    return " ".join(next((p for p in reversed(last) if p.strip()), "").split())[:160]


def _newest(job: Path) -> float:
    """When anything in the job's folder was last written: Harbor's own log, the agent's log, the verifier's output."""
    try:
        return max((p.stat().st_mtime for p in job.rglob("*") if p.is_file()), default=0.0)
    except OSError:
        return 0.0


def log_tail(job: Path, printed: bytes, lines: int = 60) -> str:
    """The last lines of what a trial wrote, newest file first, for someone to judge whether it is hung."""
    files = sorted((p for p in job.rglob("*") if p.is_file() and p.suffix in (".log", ".txt", ".jsonl", ".out", "")),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    parts = []
    for p in files:
        try:
            text = p.read_text(errors="replace").splitlines()[-lines:]
        except OSError:
            continue
        parts.append(f"== {p.relative_to(job)} ==\n" + "\n".join(text))
    said = printed.decode(errors="replace").splitlines()[-20:]
    if said:
        parts.append("== harbor run output ==\n" + "\n".join(said))
    return "\n\n".join(parts)[-8000:] or "(nothing was written)"


def remove_containers(job: Path) -> None:
    """Remove every container a job started. Harbor names each trial's Docker Compose project after the trial, so a
    hung Harbor that was killed leaves nothing running behind it."""
    import subprocess
    try:
        from harbor.environments.docker.docker import _sanitize_docker_compose_project_name as project
    except ImportError:
        project = str.lower
    trials = [project(d.name) for d in job.iterdir() if d.is_dir()] if job.is_dir() else []
    if not trials:
        return
    try:
        listed = subprocess.run(["docker", "ps", "-a", "--format", '{{.ID}} {{.Label "com.docker.compose.project"}}'],
                                capture_output=True, text=True, timeout=30).stdout.split("\n")
        ids = [line.split()[0] for line in listed if len(line.split()) == 2 and line.split()[1].startswith(tuple(trials))]
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        pass


def remove_leftovers(hm_dir: Path) -> int:
    """Remove the containers that earlier runs in this index left behind: one that was quit or died while a trial
    was being stopped, or a Harbor that failed before it took its containers down. Only one run goes in an index at
    a time, so a container whose Compose files lie under its .hm/ belongs to none that is going. Returns how many."""
    import subprocess
    try:
        listed = subprocess.run(["docker", "ps", "-a", "--format",
                                 '{{.ID}} {{.Label "com.docker.compose.project.working_dir"}}'],
                                capture_output=True, text=True, timeout=30).stdout.split("\n")
        ids = [p[0] for p in (line.split(None, 1) for line in listed)
               if len(p) == 2 and p[1].startswith(f"{hm_dir.resolve()}/")]
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, timeout=120)
        return len(ids)
    except (OSError, subprocess.TimeoutExpired):
        return 0


async def run(task_dir: Path, jobs_dir: Path, agent: str, model: str = "", agent_kwargs: dict | None = None,
              with_keys: bool = False, timeout: float = 3 * 3600, max_agent_seconds: float = 0,
              agent_env: dict[str, str] | None = None, max_build_seconds: float = 0,
              max_verifier_seconds: float = 0, watch: Watch | None = None, agent_setup_seconds: float = 0,
              mounts: list[dict] | None = None) -> TrialResult:
    """One Harbor job with one trial. Infrastructure failures come back as a failed result with `error` set.
    `max_build_seconds` and `max_verifier_seconds` cap the task's own limits: a build or a verifier that takes longer
    fails, with Harbor's timeout as the error. With `watch`, a trial whose logs stop moving is judged, and a hung one is
    stopped, its containers removed, and run again or its task dropped, as `watch.judge` says."""
    while True:
        result = await _run_once(task_dir, jobs_dir, agent, model, agent_kwargs, with_keys, timeout, max_agent_seconds,
                                 agent_env, max_build_seconds, max_verifier_seconds, watch, agent_setup_seconds, mounts)
        if result is not None:
            return result


HARBOR_AGENT_SETUP = 360.0          # what Harbor allows for installing an agent in the container, unless told otherwise


async def _run_once(task_dir, jobs_dir, agent, model, agent_kwargs, with_keys, timeout, max_agent_seconds, agent_env,
                    max_build_seconds, max_verifier_seconds, watch, agent_setup_seconds=0, mounts=None) -> TrialResult | None:
    """One try. None when it hung and is to be run again."""
    name = f"{agent}-{uuid.uuid4().hex[:8]}"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    # --yes: nobody can answer Harbor here (it would wait unseen, as it does for a task that reads host variables)
    cmd = [binary(), "run", "-p", str(task_dir), "-a", agent, "-o", str(jobs_dir), "--job-name", name, "-q", "--yes"]
    if model:
        cmd += ["-m", model]
    for k, v in (agent_kwargs or {}).items():
        if v:
            cmd += ["--ak", f"{k}={v}"]
    for k, v in (agent_env or {}).items():      # settings for the agent, such as an output limit. Never a key
        cmd += ["--ae", f"{k}={v}"]
    for flag, cap, own in (("--environment-build-timeout-multiplier", max_build_seconds,
                            own_limit(task_dir, "environment", "build_timeout_sec", HARBOR_BUILD_DEFAULT)),
                           ("--verifier-timeout-multiplier", max_verifier_seconds, own_limit(task_dir, "verifier"))):
        if cap and own > cap:          # Harbor scales the task's own limit: a factor brings it down to the cap
            cmd += [flag, f"{cap / own:.6f}"]
    if mounts:                  # the agent's own program, from this machine's cache: Harbor then installs nothing
        cmd += ["--mounts", json.dumps(mounts)]
    if agent_setup_seconds > HARBOR_AGENT_SETUP:       # installing Node.js and the agent in a bare image takes minutes
        cmd += ["--agent-setup-timeout-multiplier", f"{agent_setup_seconds / HARBOR_AGENT_SETUP:.4f}"]
        timeout += agent_setup_seconds
    if max_agent_seconds:
        own = agent_limit(task_dir)
        if own > max_agent_seconds:        # Harbor scales the task's own limit, so cap it by a factor
            cmd += ["--agent-timeout-multiplier", f"{max_agent_seconds / own:.6f}"]
        timeout = min(own or max_agent_seconds, max_agent_seconds) + 3600 + agent_setup_seconds     # room for setup and the verifier
    env = dict(os.environ)
    if with_keys:      # only an agent trial gets keys, and only through the process environment
        env.update(keys.agent_env(agent))
    proc = await asyncio.create_subprocess_exec(*cmd, env=env, stdin=asyncio.subprocess.DEVNULL,   # a question ends, never waits
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    job = jobs_dir / name
    printed = bytearray()

    async def pump() -> None:
        while chunk := await proc.stdout.read(4096):
            printed.extend(chunk)

    reader = asyncio.create_task(pump())
    loop = asyncio.get_running_loop()
    deadline, moved, seen = loop.time() + timeout, loop.time(), None
    started = said = loop.time()
    try:
        while True:
            left = deadline - loop.time()
            if left <= 0:
                raise asyncio.TimeoutError
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), min(left, watch.every, 60) if watch else left)
                break
            except asyncio.TimeoutError:
                if watch is None or loop.time() >= deadline:
                    continue
            now = loop.time()
            size, text = await asyncio.to_thread(inside, job)       # what it has written in its container so far
            if watch.spend and agent in USAGE:
                so_far = await asyncio.to_thread(spent_so_far, job, agent, model)
                if so_far is not None:
                    watch.spend(so_far)
            state = (len(printed), _newest(job), size)
            if watch.say and now - said >= watch.say_every:
                said = now
                doing = now_doing(text)
                watch.say(doing or "running, with nothing printed yet", now - started)
            if state != seen:
                seen, moved = state, now
                continue
            if now - moved < watch.quiet:
                continue
            verdict = await watch.judge((text[-6000:] + "\n\n" if text else "") + log_tail(job, bytes(printed)), now - moved)
            if verdict == "wait":           # it is working, only quietly: look again after another quiet spell
                moved = loop.time()
                continue
            await _stop(proc)
            remove_containers(job)
            if verdict == "drop":
                raise TaskDropped("hung", f"a trial was stuck for {(now - moved) / 60:.0f} minutes, and was dropped")
            return None
        await reader
    except (asyncio.TimeoutError, asyncio.CancelledError):
        await _stop(proc)
        remove_containers(job)
        raise
    finally:
        reader.cancel()
    await asyncio.to_thread(remove_containers, job)     # Harbor that failed may not have taken its own down
    out = bytes(printed)
    results = [p for p in job.glob("*/result.json")]
    if not results:
        tail = out.decode(errors="replace")[-400:]
        return TrialResult(False, 0.0, 0, 0, 0.0, 0.0, error=f"Harbor produced no trial result. {tail}", artifact=str(job),
                           job=str(job))
    r = json.loads(results[0].read_text())
    said = results[0].parent / "agent" / "exit-code.txt"
    try:
        exit_code = int(said.read_text().strip()) if said.is_file() else None
    except ValueError:
        exit_code = None
    reward = float(((r.get("verifier_result") or {}).get("rewards") or {}).get("reward") or 0.0)
    usage = r.get("agent_result") or {}
    err = r.get("exception_info")
    tokens_in, cached = int(usage.get("n_input_tokens") or 0), int(usage.get("n_cache_tokens") or 0)
    cost = float(usage.get("cost_usd") or 0.0)
    if not cost:        # an agent on a subscription, or an API that names no price: its tokens at list price
        cost = prices.estimate(model, tokens_in - min(cached, tokens_in), min(cached, tokens_in), 0,
                               int(usage.get("n_output_tokens") or 0)) or 0.0
    return TrialResult(
        passed=reward > 0, reward=reward,
        tokens_in=tokens_in, tokens_out=int(usage.get("n_output_tokens") or 0),
        cost=cost, seconds=_seconds(r.get("agent_execution")),
        agent_version=str((r.get("agent_info") or {}).get("version") or ""),
        verifier_seconds=_seconds(r.get("verifier")), checksum=str(r.get("task_checksum") or ""),
        artifact=str(results[0].parent), error=explain(err), job=str(job), exit_code=exit_code)


def with_solution(task_dir: Path, files: dict[str, str], scratch: Path) -> Path:
    """A copy of the task whose solution/ has the given files written over it, for a mutant run."""
    copy = scratch / f"{task_dir.name}-{uuid.uuid4().hex[:6]}" / task_dir.name
    shutil.copytree(task_dir, copy, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    for rel, content in files.items():
        target = (copy / "solution" / rel).resolve()
        if copy.resolve() not in target.parents:
            raise ValueError(f"mutant file {rel} is outside the task")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return copy


MAX_KEPT_FILE = 5 * 1024 * 1024       # for the task's own output files under artifacts/, which can be huge.
                                      # The trajectory and the verifier's output are always kept whole


def settle(result: TrialResult, keep: bool, trials_dir: Path, label: str, relative_to: Path) -> TrialResult:
    """Keep a run's files only when they are evidence, as one archive that unpacks to a whole Harbor trial folder:
    agent/, verifier/, artifacts/, config.json, lock.json and the result. Remove Harbor's working folder either way.

    An agent trial is evidence, and so is any run that went wrong. A reference solution that passed or a
    no-op that failed is not: the ledger record says all there is to say.
    """
    source, job = Path(result.artifact) if result.artifact else None, Path(result.job) if result.job else None
    result.artifact = ""
    if keep and source is not None and source.exists():
        trials_dir.mkdir(parents=True, exist_ok=True)
        archive = trials_dir / f"{label}.tar.gz"
        omitted = []
        with tarfile.open(archive, "w:gz") as tar:
            for f in sorted(p for p in source.rglob("*") if p.is_file()):
                rel = f.relative_to(source)
                if rel.parts[0] == "artifacts" and f.stat().st_size > MAX_KEPT_FILE:      # never the trajectory
                    omitted.append(f"{rel}  {f.stat().st_size} bytes")
                    continue
                tar.add(f, arcname=str(rel))
            if omitted:
                note = ("Left out for size:\n" + "\n".join(omitted) + "\n").encode()
                info = tarfile.TarInfo("OMITTED.txt")
                info.size = len(note)
                tar.addfile(info, io.BytesIO(note))
        result.artifact = str(archive.relative_to(relative_to))
    if job is not None:
        shutil.rmtree(job, ignore_errors=True)
    result.job = ""
    return result
