"""The task store: a later benchmark run gets exactly the version of a task that was accepted, or is refused."""
import asyncio
import subprocess
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import call
from harbor_master import engine, interact, tasks
from harbor_master.cli import app, views
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


def test_an_accepted_task_that_missed_the_store_reaches_it_at_the_next_sync(workspace, ident, tmp_path):
    s, task = asyncio.run(accepted(workspace, ident))                          # no store is named yet: kept on this machine only
    shown, shared = call(views.tasks, workspace), tmp_path / "shared-store"
    assert shown.startswith("0 ") and task.task_id in shown and "none named" in shown
    name_store(workspace, shared)
    k = store.key(BENCH, task.name, task.digest)
    assert not (shared / k).exists() and "1 accepted" in call(views.tasks, workspace)
    store.remember(workspace, BENCH, task.name, task.digest)                   # what the run notes when the store could not take it
    store.remember(workspace, BENCH, "gone", "0" * 64)                         # and one this machine has no copy of
    said = store.retry(workspace, config_mod.load(workspace))                  # what hm sync does
    assert said[0] == f"{task.task_id}: accepted version kept in {shared}" and (shared / k).is_file()
    assert "not kept" in said[1] and len(store.retry(workspace, config_mod.load(workspace))) == 1     # that one stays noted
    (workspace / ".hm" / store.UNKEPT).write_text("")
    assert store.retry(workspace, config_mod.load(workspace)) == [] and not (workspace / ".hm" / store.UNKEPT).exists()

    name_store(workspace, "some-org/some-tasks")                                                   # a Hugging Face repository
    told = call(views.tasks, workspace)
    assert "huggingface_hub" in told or "HF_TOKEN" in told                                         # says what it needs, plainly


def test_hm_eval_runs_the_accepted_version_or_says_it_has_none(workspace, ident, tmp_path, monkeypatch):
    from harbor_master.ledger import evals
    shared, ran = tmp_path / "shared-store", []
    s, task = asyncio.run(accepted(workspace, ident, shared))
    tasks.register(workspace, BENCH, Path(tasks.read_registry(workspace)[BENCH]["path"]), supported=BENCH)    # as a listed one is
    (workspace / "roster.toml").write_text('[[config]]\nname = "new"\nagent = "some-agent"\nmodel = "some-model"\n')

    async def stand_in(session, index, roster, k, fetch):
        for entry in index:
            try:
                t = fetch(entry)
                ran.append((t.task_id, t.digest, str(t.path)))
            except store.StoreError as err:
                ran.append(str(err))
        return []

    def go() -> str:
        ran.clear()
        return hm(workspace, "eval")

    monkeypatch.setattr(engine, "evaluate", stand_in)
    monkeypatch.setattr("harbor_master.cli.evals.check_lock", lambda *a: None)
    monkeypatch.setattr("harbor_master.core.keys.needs", lambda *a: [])
    assert "not released yet" in go()
    evals.append(workspace, ident, "release", version="1.0.0")
    first = go()
    assert ran == [(task.task_id, task.digest, str(task.path))], first                # the files here are the accepted version: used as they are

    (task.path / "instruction.md").write_text("Something else entirely.\n")   # the task on this machine is no longer that version
    go()
    assert len(ran) == 1 and ran[0][1] == task.digest and ".hm/store/unpacked" in ran[0][2]        # so the stored one is run

    subprocess.run(["rm", "-rf", str(workspace / ".hm" / "store"), str(shared)], check=True)       # and with no copy anywhere: not run
    go()
    assert ran == [f"the accepted version of {task.task_id} is not on this machine and not in the task store"]


def test_every_index_has_a_store_which_its_owner_names_and_a_real_run_needs(workspace, ident, tmp_path, monkeypatch):
    assert "names no store" in store.not_ready(config_mod.load(workspace))                # an index from before stores were required
    NotFound = type("RepositoryNotFoundError", (Exception,), {})
    repos: dict[str, dict] = {}
    seen: list = []

    class Api:                                                                            # a stand-in for Hugging Face
        def __init__(self, token=None):
            self.token = token
            seen.append(token)

        def repo_info(self, repo, repo_type):
            if repo not in repos:
                raise NotFound(repo)

        def create_repo(self, repo, repo_type, private, exist_ok):
            assert self.token and private
            repos.setdefault(repo, {})

        def file_exists(self, repo, k, repo_type):
            return k in repos[repo]

        def upload_file(self, path_or_fileobj, path_in_repo, repo_id, repo_type, commit_message):
            assert self.token
            repos[repo_id][path_in_repo] = open(path_or_fileobj, "rb").read()

    def download(repo_id, filename, repo_type, token, local_dir):
        (tmp_path / "downloaded").write_bytes(repos[repo_id][filename])
        return str(tmp_path / "downloaded")

    token = lambda value: monkeypatch.setattr(store.keys, "get", lambda name: value if name == "HF_TOKEN" else "")    # noqa: E731
    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(HfApi=Api, hf_hub_download=download))
    token("")
    name_store(workspace, "some-org/some-tasks")
    cfg = config_mod.load(workspace)
    assert "needs your own Hugging Face token" in store.not_ready(cfg)                    # no token: said plainly, before anything is spent
    token("hf_test")
    assert "does not exist" in store.not_ready(cfg)
    there = store.remote(cfg)
    there.create()
    assert store.not_ready(cfg) == "" and there.exists()
    (tmp_path / "a.tar.gz").write_bytes(b"archive")
    there.put("tasks/b/t.0123.tar.gz", tmp_path / "a.tar.gz")
    token("")                                                                             # reading needs no token
    reader = store.remote(cfg, write=False)
    assert seen[-1] is None and reader.get("tasks/b/t.0123.tar.gz", tmp_path / "back.tar.gz") and (tmp_path / "back.tar.gz").read_bytes() == b"archive"
