"""The task store: a later benchmark run gets exactly the version of a task that was accepted, or is refused."""
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harbor_master import interact, tasks
from harbor_master.cli import app
from harbor_master.core import config as config_mod
from harbor_master.engine import Session
from harbor_master.tasks import store

BENCH = "demo-bench"
runner = CliRunner()


def hm(ws, *args: object) -> str:
    result = runner.invoke(app, [*(str(a) for a in args), "-w", str(ws)])
    return f"{result.exit_code} {' '.join(result.output.split())}"


def name_store(workspace, where) -> None:
    (workspace / "about.toml").write_text(f'name = "demo"\ntasks = "{where}"\n')


async def accepted(workspace, ident, shared: Path | None = None):
    """A run with one task accepted. Returns the session and the task."""
    if shared is not None:
        name_store(workspace, shared)
    s = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, as_real=True, speed=0,
                no_input=True, run_id="r1")
    await s.run()
    task = next(t for t in s.ready if tasks.digest_dir(t.path) == t.digest)    # never repaired: its files are the version the report is about
    assert "Accepted" in interact.decide(s, task, "accept clean")
    await s.kept()
    return s, task


async def test_an_accepted_task_is_kept_at_the_version_that_was_accepted(workspace, ident, tmp_path):
    shared, other, third = tmp_path / "shared-store", tmp_path / "another-machine", tmp_path / "third"
    s, task = await accepted(workspace, ident, shared)
    k = store.key(BENCH, task.name, task.digest)
    assert (workspace / ".hm" / "store" / k).is_file() and (shared / k).is_file()                 # here, and in the index's store
    assert any(kind == "checked" and f"kept in {shared}" in text for _, kind, _, text in s.recent)

    other.mkdir()                                                             # a machine that never fetched the benchmark
    got = store.fetch(other, s.cfg, BENCH, task.name, task.digest)
    assert got.name == task.name and tasks.digest_dir(got) == task.digest and tasks.load_task(got, BENCH).digest == task.digest
    assert store.fetch(other, s.cfg, BENCH, task.name, "0" * 64) is None      # no other version is there

    (shared / k).write_bytes((shared / k).read_bytes()[:-40] + b"x" * 40)     # the copy in the store is damaged or swapped
    with pytest.raises(store.StoreError, match="changed or damaged"):
        store.fetch(third, s.cfg, BENCH, task.name, task.digest)
    assert not (third / ".hm" / "store" / "unpacked" / BENCH / task.digest[:16] / task.name).exists()

    repaired = next((t for t in s.ready if tasks.digest_dir(t.path) != t.digest), None)        # a version this machine does not have
    if repaired is not None:
        interact.decide(s, repaired, "accept fine")
        await s.kept()
        assert any("not the version that was accepted" in text for _, _, tid, text in s.recent if tid == repaired.task_id)
