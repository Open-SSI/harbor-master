"""A simulated run never counts as real: its records are kept apart, marked, and refused by a real ledger."""
import json
import shutil
import subprocess

from typer.testing import CliRunner

from conftest import call
from harbor_master import interact, ledger, tasks
from harbor_master.cli import app, views
from harbor_master.core import config as config_mod
from harbor_master.engine import Session
from harbor_master.interact import Command, guide


def simulated(workspace, ident, run_id, **settings):
    return Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, speed=0,
                   no_input=True, run_id=run_id, **settings)


async def test_a_simulated_run_leaves_the_index_untouched_and_its_records_never_count_in_a_real_ledger(workspace, ident):
    s = simulated(workspace, ident, "s1")
    await s.run()
    apart = config_mod.simulated(workspace)
    assert s.ledger.events and all(e["simulated"] is True for e in s.ledger.events)       # every record says what it is
    assert ledger.shard_paths(apart) and not ledger.shard_paths(workspace)                 # and none is in the index's ledger
    assert ledger.Ledger(workspace).events == [] and ledger.verify(workspace) == []
    assert ledger.verify(apart, simulated=True, index_dir=workspace) == []                 # sound, as simulated records

    task = s.ready[0]
    assert "Accepted" in interact.decide(s, task, "accept")
    assert not (workspace / ledger.INDEX_FILE).exists()                                    # the accepted list is not touched
    listed = (apart / ledger.INDEX_FILE).read_text()
    assert listed.startswith("# SIMULATED") and task.task_id in listed

    trials = sum(e["type"] == "trial" for e in s.ledger.events)
    again = simulated(workspace, ident, "s2")                                              # a simulated run reuses simulated records
    await again.run()
    assert sum(e["type"] == "trial" for e in again.ledger.events) == trials
    assert simulated(workspace, ident, "r1", as_real=True).ledger.events == []             # and a real one sees none of them

    state = guide.look(workspace)                                                          # the guide says so, and labels it
    assert state.sim_ran and not state.ran and state.sim_accepted == 1 and state.accepted == 0
    assert "Simulated. These are the records" in call(views.status, workspace, True)
    assert "No task has been accepted" in call(views.leaderboard, workspace) and "Over the 1 accepted task" in call(views.leaderboard, workspace, True)

    shutil.copytree(apart / ledger.SHARDS, workspace / ledger.SHARDS)                      # someone copies them in
    assert ledger.Ledger(workspace).events == []                                           # they support nothing
    assert ledger.index_entries(ledger.Ledger(workspace).events) == []
    problems = ledger.verify(workspace)
    assert problems and all("a record of a simulated run, which never counts in an index" in p for p in problems)

    shard = ledger.shard_paths(workspace)[0]                                               # and the mark cannot be taken off
    first, *rest = shard.read_text().splitlines()
    first = json.loads(first)
    del first["simulated"]
    shard.write_text("\n".join([json.dumps(first), *rest]) + "\n")
    assert any("edited after signing" in p for p in ledger.verify(workspace))


def test_simulating_in_a_shared_index_pushes_nothing(workspace, ident, tmp_path):
    remote = tmp_path / "shared.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(remote)], check=True)
    sync = ledger.Sync(workspace, ident.name, ident.github)
    sync.define("start")
    sync.connect(str(remote))
    out = CliRunner().invoke(app, ["curate", "--simulate", "--plain", "--speed", "0", "-w", str(workspace)])
    assert out.exit_code == 0 and ledger.shard_paths(config_mod.simulated(workspace))
    sync.publish("after the run")
    pushed = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=remote, capture_output=True, text=True).stdout.split()
    assert pushed and not any(f.startswith(("ledger/", ".hm/")) for f in pushed)           # the definition, and no record
