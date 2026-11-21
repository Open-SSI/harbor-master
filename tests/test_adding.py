"""Adding a benchmark from any link: the agent finds its repository, the code downloads it, a duplicate is refused."""
import subprocess

import pytest

from harbor_master import tasks
from harbor_master.backends import describe
from harbor_master.tasks import adding

from conftest import make_benchmark


def test_one_repository_is_one_benchmark_however_its_link_is_written():
    links = ["https://github.com/Example/Bench", "git@github.com:example/bench.git", "example/bench", "github.com/example/bench/tree/main"]
    assert {adding.repo_key(x) for x in links} == {"github.com/example/bench"}


async def test_the_agent_finds_the_repository_and_the_same_one_is_not_added_twice(workspace, ident, tmp_path, monkeypatch):
    repo = make_benchmark(tmp_path, "catalysis-bench", 3)
    for args in ("init -q -b main", "add -A", "-c user.name=a -c user.email=a@b.c commit -q -m first", "tag v1.0"):
        subprocess.run(["git", *args.split()], cwd=repo, check=True)

    async def agent(cfg, given, entry=None, repo_dir=None, note=""):
        return {"repository": str(repo), "release": "v1.0", "name": "Catalysis", "paper": "none", "sources": [given]}, 0.0
    monkeypatch.setattr(describe, "find", agent)
    entry = adding.from_found(workspace, ident, (await adding.find(workspace, ident, "https://arxiv.org/abs/1"))[0])
    assert (entry["github"], entry["ref"], entry["task_count"]) == (str(repo), "v1.0", 3)
    adding.confirm(workspace, ident, {**entry, "description": "Tasks."})
    assert tasks.supported.find(workspace, "catalysis-bench") is not None
    with pytest.raises(adding.AddError, match="on this index's list already"):
        adding.from_found(workspace, ident, (await adding.find(workspace, ident, "https://example.org"))[0])
