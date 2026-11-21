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


def now_doing(text: str) -> str:
    """The trial's latest step, in one line: from an agent's event stream, its last tool call or message; from a
    solution's output, its last line, a progress bar as it stands now."""
    rows = [r for r in text.splitlines() if r.strip() and not r.startswith("=== ")]
    for row in reversed(rows[-400:]):
        if not row.lstrip().startswith("{"):
            continue
        try:
            event = json.loads(row)
        except ValueError:
            continue
        for item in reversed((event.get("message") or {}).get("content") or [] if event.get("type") == "assistant" else []):
            if item.get("type") == "tool_use":
                given = item.get("input") or {}
                what = given.get("command") or given.get("file_path") or given.get("pattern") or given.get("description") or ""
                return f"{item.get('name', 'tool')}: {' '.join(str(what).split())}"[:160]
            if item.get("type") == "text" and item.get("text", "").strip():
                return " ".join(item["text"].split())[:160]
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
    cmd = [binary(), "run", "-p", str(task_dir), "-a", agent, "-o", str(jobs_dir), "--job-name", name, "-q"]
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
    proc = await asyncio.create_subprocess_exec(*cmd, env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
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
