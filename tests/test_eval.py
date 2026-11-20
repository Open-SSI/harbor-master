"""Stage 2, the evaluation of a released index: cells are taken from a to-do list that everyone computes alike,
held for a few hours, and run. A difficulty trial of stage 1 is not run again."""
import json
import time

import tomli_w
from typer.testing import CliRunner

from conftest import call
from harbor_master import interact, ledger, tasks
from harbor_master.backends.base import TrialResult
from harbor_master.cli import app, views
from harbor_master.core import config as config_mod
from harbor_master.core import identity
from harbor_master.core.config import RosterEntry
from harbor_master.engine import Session, evaluate
from harbor_master.engine.evaluate import take, todo
from harbor_master.ledger import evals

BENCH = "demo-bench"


def session(ws, who, run_id, sandbox=False):
    return Session(ws, config_mod.load(ws), who, tasks.read_registry(ws), simulate=True, as_real=not sandbox, speed=0, no_input=True, run_id=run_id)


async def curated(ws, who, sandbox=False):
    """Stage 1, with two tasks accepted at the version the agents attempted. Returns the session, the index and a way to get a task."""
    s = session(ws, who, "r1", sandbox)
    await s.run()
    for task in [t for t in s.ready if tasks.digest_dir(t.path) == t.digest][:2]:
        assert "Accepted" in interact.decide(s, task, "accept clean")
    index = ledger.index_entries(s.ledger.events)
    by_id = {t.task_id: t for t in s.benches[BENCH].tasks}
    return s, index, lambda entry: by_id[entry["task"]]


def roster_of(cfg):
    return [RosterEntry(name="suite", **cfg.difficulty.suite[0].model_dump()), RosterEntry(name="new", agent="some-agent", model="some-model", version="1.0")]


async def test_cells_are_taken_held_and_run_and_a_difficulty_trial_is_not_run_again(workspace, ident, tmp_path, monkeypatch):
    s, index, fetch = await curated(workspace, ident)
    roster = roster_of(s.cfg)
    cells = todo(s, index, roster)
    by = lambda cells, name: [c for c in cells if c.config.name == name]      # noqa: E731
    assert len(cells) == 4 and all(len(c.done) == 0 for c in by(cells, "new")) and [len(c.done) for c in cells] == sorted(len(c.done) for c in cells)
    reused = sum(len(c.done) for c in by(cells, "suite"))
    assert reused >= 2 and all(e["purpose"] == "difficulty" for c in by(cells, "suite") for e in c.done.values())   # stage 1 filled these

    rows = await evaluate(session(workspace, ident, "e1"), index, roster, 1, fetch)       # one cell: a task with a configuration
    assert len(rows) == 1 and rows[0]["config"] == "new" and (rows[0]["new"], rows[0]["trials"]) == (3, 3)
    mine = [json.loads(line) for line in (workspace / "eval" / f"{ident.github}.jsonl").read_text().splitlines()]
    claim, trial = mine[0], mine[1]
    assert [e["type"] for e in mine] == ["claim", "trial", "trial", "trial"] and claim["cells"] == [f"{rows[0]['task'] and trial['digest']}#new"]
    assert abs(claim["expires"] - claim["time"] - 4 * 3600) < 5                      # held for the protocol's four hours
    assert set(trial) == {"v", "type", "time", "runner", "prev", "id", "sig", "task_id", "digest", "agent", "agent_version", "model",
                          "reasoning_effort", "trial_index", "purpose", "passed", "reward", "tokens_in", "tokens_out", "cost",
                          "container_seconds", "error", "artifact"}                       # no protocol: a trial is not tied to its version
    stage1 = next(e for e in s.ledger.events if e["type"] == "trial" and e["purpose"] == "difficulty")
    assert set(trial) - {"v", "prev"} <= set(stage1) and trial["purpose"] == "benchmark"  # the fields of a difficulty trial

    monkeypatch.setenv("HM_CONFIG_DIR", str(tmp_path / "config-bob"))                     # a second person, who only evaluates
    bob = identity.create("Bob", "Brick Lab", "bob@example.org", "bob")
    people = ledger.load_contributors(workspace) + [{"name": "Bob", "github": "bob", "key": bob.public_key, "role": "evaluator"}]
    (workspace / ledger.CONTRIBUTORS).write_text(tomli_w.dumps({"contributor": people}))
    assert ledger.can_evaluate(people[-1]) and not ledger.can_contribute(people[-1]) and "evaluation only" in ledger.status(people[-1])
    theirs = await take(session(workspace, bob, "b1"), index, roster, 1)
    assert len(theirs) == 1 and theirs[0].mine
    monkeypatch.setenv("HM_CONFIG_DIR", str(tmp_path / "config"))                         # the first person again
    seen = {c.key: c for c in todo(session(workspace, ident, "e2"), index, roster)}       # the first person sees it held, and is not given it
    assert seen[theirs[0].key].holder == "bob" and not seen[theirs[0].key].mine
    rest = await evaluate(session(workspace, ident, "e3"), index, roster, 20, fetch)
    assert theirs[0].key not in {f"{next(e['digest'] for e in index if e['task'] == r['task'])}#{r['config']}" for r in rest}
    later = evals.cells(index, roster, s.cfg.eval, [], evals.read(workspace), time.time() + 5 * 3600)
    assert not any(c.holder for c in later)                                               # after four hours it is free again

    monkeypatch.setenv("HM_CONFIG_DIR", str(tmp_path / "config-bob"))
    done = await evaluate(session(workspace, bob, "b2"), index, roster, 20, fetch)        # bob runs what he holds
    monkeypatch.setenv("HM_CONFIG_DIR", str(tmp_path / "config"))
    assert len(done) == 1 and not [c for c in todo(s, index, roster) if c.open]
    assert await evaluate(session(workspace, ident, "e4"), index, roster, 20, fetch) == []    # nothing is run twice
    new_trials = [e for e in evals.read(workspace) if e["type"] == "trial"]
    assert len(new_trials) == 4 * 3 - reused and ledger.verify(workspace) == []           # the difficulty trials were not run again

    rows, n = ledger.leaderboard(ledger.Ledger(workspace).events, [c.model_dump() for c in roster], evals=evals.read(workspace))
    table = {r["name"]: r for r in rows}
    assert n == 2 and table["new"]["complete"] and table["new"]["trials"] == 6 and table["new"]["selection_trials"] == 0
    assert table["suite"]["complete"] and table["suite"]["trials"] == 6 and table["suite"]["selection_trials"] == reused

    path = workspace / "eval" / "bob.jsonl"                                               # an edited record does not pass
    path.write_text(path.read_text().replace('"trial_index":0', '"trial_index":9', 1))
    assert any("eval/bob.jsonl" in p and "edited after signing" in p for p in ledger.verify(workspace))


async def test_a_trial_that_fails_to_run_fills_no_slot_and_the_cell_is_given_up_after_a_few(workspace, ident):
    s, index, fetch = await curated(workspace, ident)
    roster = roster_of(s.cfg)[1:]

    async def broken(task, entry, i, max_hours=0):
        return TrialResult(False, 0.0, 0, 0, 0.0, 1.0, error="the container did not start")

    for n in range(3):                                          # the first try, and the protocol's two retries
        later = session(workspace, ident, f"e{n}")
        later.backend.run_trial = broken
        rows = await evaluate(later, index, roster, 1, fetch)
        assert rows[0]["new"] == 0 and "failed to run" in rows[0]["note"]
    cells = todo(s, index, roster)
    assert [(c.errors, c.failed, len(c.done)) for c in cells if c.errors] == [(3, True, 0)]
    rows = await evaluate(session(workspace, ident, "e9"), index, roster, 20, fetch)
    assert len(rows) == 1 and rows[0]["trials"] == 3            # the other cell runs, and the one given up is not taken again


def test_the_owner_releases_once_and_evaluation_opens_only_then(workspace, ident, monkeypatch):
    import asyncio
    hm = lambda *a: (lambda r: f"{r.exit_code} {' '.join(r.output.split())}")(CliRunner().invoke(app, [*(str(x) for x in a), "-w", str(workspace)]))  # noqa: E731
    people = ledger.load_contributors(workspace)
    people[0]["role"] = "owner"
    (workspace / ledger.CONTRIBUTORS).write_text(tomli_w.dumps({"contributor": people}))
    (workspace / "roster.toml").write_text(tomli_w.dumps({"config": [{"name": "new", "agent": "some-agent", "model": "some-model"}]}))
    assert hm("lock").startswith("0")                                              # copies the skills in, and locks
    assert "nothing to release" in hm("release") and "not released yet" in hm("eval")
    asyncio.run(curated(workspace, ident))
    out = hm("release")
    assert out.startswith("0") and "Released as v1.0.0" in out and "2 tasks, 1 configurations" in out
    made = evals.release(evals.read(workspace))
    assert (made["version"], made["tasks"], made["trials"]) == ("1.0.0", 2, 3) and made["index"].startswith("sha256:") and "roster" not in made
    assert ledger.protocol.version(ledger.protocol.current(ledger.Ledger(workspace).events)) == "1.0.0"
    assert "released already" in hm("release") and ledger.verify(workspace) == []
    out = hm("todo")
    assert "new" in out and "The index is released" in out
    (workspace / "roster.toml").write_text(tomli_w.dumps({"config": [{"name": "new", "agent": "some-agent", "model": "some-model"},
                                                                     {"name": "newer", "agent": "some-agent", "model": "next-model"}]}))
    assert "newer" in hm("todo") and ledger.verify(workspace) == []             # the roster grows after the release: a new model's cells
    assert "curation is closed" in hm("curate", "--no-input", "--plain")


def test_hm_eval_simulated_runs_the_tasks_accepted_in_simulated_runs(workspace, ident):
    import asyncio
    hm = lambda *a: (lambda r: f"{r.exit_code} {' '.join(r.output.split())}")(CliRunner().invoke(app, [*(str(x) for x in a), "-w", str(workspace)]))  # noqa: E731
    asyncio.run(curated(workspace, ident, sandbox=True))
    (workspace / "roster.toml").write_text(tomli_w.dumps({"config": [{"name": "new", "agent": "some-agent", "model": "some-model"}]}))
    out = hm("eval", "--simulate", "--take", 1)
    assert out.startswith("0") and "3 of 3" not in out.split("new trials")[0] or "3 new trials" in out
    assert "3 new trials" in out and not (workspace / "eval").exists()          # its records stay with the simulated ones
    assert len(evals.read(config_mod.simulated(workspace), simulated=True)) == 4 and "Simulated" in hm("todo")
    assert "3 new trials" in hm("eval", "--simulate") and "Nothing is left to evaluate" in hm("eval", "--simulate")
    assert "new" in call(views.leaderboard, workspace, True)
