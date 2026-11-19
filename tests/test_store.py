"""The task store: a later benchmark run gets exactly the version of a task that was accepted, or is refused."""
import asyncio
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harbor_master import interact, ledger, tasks
from harbor_master.cli import app
from harbor_master.core import config as config_mod
from harbor_master.engine import Session
from harbor_master.interact import Command
from harbor_master.tasks import store

BENCH = "demo-bench"
runner = CliRunner()


def hm(ws, *args: object) -> str:
    result = runner.invoke(app, [*(str(a) for a in args), "-w", str(ws)])
    return f"{result.exit_code} {' '.join(result.output.split())}"


def unrepaired(s: Session):
    """A report whose task was never repaired, so its files are the version the report is about."""
    return next(t for t in s.ready if tasks.digest_dir(t.path) == t.digest)


async def accepted(workspace, ident, shared: Path | None = None):
    """A run with one task accepted. Returns the session and the task."""
    if shared is not None:
        (workspace / "about.toml").write_text(f'name = "demo"\ntasks = "{shared}"\n')
    s = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, as_real=True, speed=0,
                no_input=True, run_id="r1")
    await s.run()
    task = unrepaired(s)
    assert "Accepted" in interact.execute(s, Command("accept", [task.name, "clean"]))
    await s.kept()
    return s, task


async def test_an_accepted_task_is_kept_at_the_version_that_was_accepted(workspace, ident, tmp_path):
    shared = tmp_path / "shared-store"
    s, task = await accepted(workspace, ident, shared)
    k = store.key(BENCH, task.name, task.digest)
    assert (workspace / ".hm" / "store" / k).is_file() and (shared / k).is_file()                 # here, and in the index's store
    assert any(kind == "checked" and f"kept in {shared}" in text for _, kind, _, text in s.recent)

    other = tmp_path / "another-machine"                                      # a machine that never fetched the benchmark
    other.mkdir()
    got = store.fetch(other, s.cfg, BENCH, task.name, task.digest)
    assert got.name == task.name and tasks.digest_dir(got) == task.digest and tasks.load_task(got, BENCH).digest == task.digest
    assert store.fetch(other, s.cfg, BENCH, task.name, "0" * 64) is None      # no other version is there

    (shared / k).write_bytes((shared / k).read_bytes()[:-40] + b"x" * 40)     # the copy in the store is damaged or swapped
    with pytest.raises(store.StoreError, match="changed or damaged"):
        store.fetch(tmp_path / "third", s.cfg, BENCH, task.name, task.digest)
    assert not (tmp_path / "third" / ".hm" / "store" / "unpacked" / BENCH / task.digest[:16] / task.name).exists()

    repaired = next((t for t in s.ready if tasks.digest_dir(t.path) != t.digest), None)        # a version this machine does not have
    if repaired is not None:
        interact.execute(s, Command("accept", [repaired.name, "fine"]))
        await s.kept()
        assert any("not the version that was accepted" in text for _, _, tid, text in s.recent if tid == repaired.task_id)


def test_hm_tasks_shows_pushes_and_pulls(workspace, ident, tmp_path):
    s, task = asyncio.run(accepted(workspace, ident))                          # no store is named yet
    shown = hm(workspace, "tasks")
    assert shown.startswith("0 ") and task.task_id in shown and "none named" in shown
    shared = tmp_path / "shared-store"
    (workspace / "about.toml").write_text(f'name = "demo"\ntasks = "{shared}"\n')
    assert "1 accepted task is not in the store" in hm(workspace, "tasks")
    pushed = hm(workspace, "tasks", "push")
    assert pushed.startswith("0 ") and f"kept in {shared}" in pushed and "1 accepted task kept" in pushed
    assert "not in the store" not in hm(workspace, "tasks")
    subprocess.run(["rm", "-rf", str(workspace / ".hm" / "store")], check=True)
    pulled = hm(workspace, "tasks", "pull")
    assert pulled.startswith("0 1 accepted task on this machine") and (workspace / ".hm" / "store" / store.key(BENCH, task.name, task.digest)).is_file()

    (workspace / "about.toml").write_text('name = "demo"\ntasks = "some-org/some-tasks"\n')      # a Hugging Face repository
    told = hm(workspace, "tasks")
    assert told.startswith("1 ") and ("huggingface_hub" in told or "HF_TOKEN" in told)             # says what it needs, plainly


def test_hm_eval_runs_the_accepted_version_or_refuses(workspace, ident, tmp_path, monkeypatch):
    from harbor_master import engine
    shared = tmp_path / "shared-store"
    s, task = asyncio.run(accepted(workspace, ident, shared))
    tasks.register(workspace, BENCH, Path(tasks.read_registry(workspace)[BENCH]["path"]), "chemistry", supported=BENCH)    # as a listed one is
    ran: list = []

    async def stand_in(session, entry, chosen, trials):
        ran.extend((t.task_id, t.digest, str(t.path)) for t in chosen)
        return []

    monkeypatch.setattr(engine, "evaluate", stand_in)
    monkeypatch.setattr("harbor_master.cli.curate.check_lock", lambda *a: None)
    monkeypatch.setattr(ledger.protocol, "current", lambda events: {"protocol": s.protocol, "registry": {}})
    go = lambda: hm(workspace, "eval", "--agent", "some-agent", "--model", "some-model")  # noqa: E731
    first = go()
    assert ran == [(task.task_id, task.digest, str(task.path))], first                # the files here are the accepted version: used as they are

    (task.path / "instruction.md").write_text("Something else entirely.\n")   # the task on this machine is no longer that version
    ran.clear()
    go()
    assert len(ran) == 1 and ran[0][1] == task.digest and ".hm/store/unpacked" in ran[0][2]        # so the stored one is run

    subprocess.run(["rm", "-rf", str(workspace / ".hm" / "store"), str(shared)], check=True)       # and with no copy anywhere: refused
    ran.clear()
    refused = go()
    assert refused.startswith("1 Refused: the accepted version of demo-bench/") and "different task" in refused and not ran
