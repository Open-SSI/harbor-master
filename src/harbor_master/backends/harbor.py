"""Running a task through Harbor: the reference solution, a no-op, a mutant, or an agent.

Each run is one `harbor run` job in Docker. The result comes from the trial's own
result.json, including the agent version that actually ran and Harbor's task checksum.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import tarfile
import tomllib
import uuid
from datetime import datetime
from pathlib import Path

from ..core import keys
from .base import TrialResult

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


def agent_limit(task_dir: Path) -> float:
    """The agent time limit the task itself sets, in seconds. 0 when it sets none."""
    try:
        return float(tomllib.loads((task_dir / "task.toml").read_text()).get("agent", {}).get("timeout_sec") or 0)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return 0.0


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


async def run(task_dir: Path, jobs_dir: Path, agent: str, model: str = "", agent_kwargs: dict | None = None,
              with_keys: bool = False, timeout: float = 3 * 3600, max_agent_seconds: float = 0,
              agent_env: dict[str, str] | None = None, key: str = "") -> TrialResult:
    """One Harbor job with one trial. Infrastructure failures come back as a failed result with `error` set."""
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
    if max_agent_seconds:
        own = agent_limit(task_dir)
        if own > max_agent_seconds:        # Harbor scales the task's own limit, so cap it by a factor
            cmd += ["--agent-timeout-multiplier", f"{max_agent_seconds / own:.6f}"]
        timeout = min(own or max_agent_seconds, max_agent_seconds) + 3600      # room for setup and the verifier
    env = dict(os.environ)
    if with_keys:      # only an agent trial gets keys, and only through the process environment
        env.update(keys.agent_env(agent, model, key, agent_env))
    proc = await asyncio.create_subprocess_exec(*cmd, env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        await _stop(proc)
        raise
    job = jobs_dir / name
    results = [p for p in job.glob("*/result.json")]
    if not results:
        tail = out.decode(errors="replace")[-400:]
        return TrialResult(False, 0.0, 0, 0, 0.0, 0.0, error=f"Harbor produced no trial result. {tail}", artifact=str(job),
                           job=str(job))
    r = json.loads(results[0].read_text())
    reward = float(((r.get("verifier_result") or {}).get("rewards") or {}).get("reward") or 0.0)
    usage = r.get("agent_result") or {}
    err = r.get("exception_info")
    return TrialResult(
        passed=reward > 0, reward=reward,
        tokens_in=int(usage.get("n_input_tokens") or 0), tokens_out=int(usage.get("n_output_tokens") or 0),
        cost=float(usage.get("cost_usd") or 0.0), seconds=_seconds(r.get("agent_execution")),
        agent_version=str((r.get("agent_info") or {}).get("version") or ""),
        verifier_seconds=_seconds(r.get("verifier")), checksum=str(r.get("task_checksum") or ""),
        artifact=str(results[0].parent), error=json.dumps(err)[:400] if err else "", job=str(job))


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
SKIP = {"lock.json", "config.json"}   # Harbor's bookkeeping, repeated in result.json


def settle(result: TrialResult, keep: bool, trials_dir: Path, label: str, relative_to: Path) -> TrialResult:
    """Keep a run's files only when they are evidence, as one archive. Remove Harbor's working folder either way.

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
                if f.name in SKIP:
                    continue
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
