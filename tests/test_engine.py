import json

import pytest

from harbor_master import interact, ledger, tasks
from harbor_master.backends import NotImplementedBackend
from harbor_master.core import config as config_mod
from harbor_master.engine import Session


def test_a_tier_with_an_unknown_provider_fails_clearly(workspace, ident):
    cfg = config_mod.load(workspace)
    cfg.tiers.worker.provider = "something-else"
    with pytest.raises(NotImplementedBackend, match="worker tier"):
        Session(workspace, cfg, ident, tasks.read_registry(workspace), simulate=False)


async def test_a_simulated_run_is_sound_and_a_rerun_is_rejected(workspace, ident, session):
    await session.run()
    bench = session.benches["demo-bench"]
    assert bench.selected == 20 and bench.kept + bench.dropped == 20
    assert ledger.verify(workspace) == []
    states = ledger.fold(session.ledger.events)
    assert sum(s.status == "report_ready" for s in states.values()) == bench.kept
    assert sum(s.status == "not_selected" for s in states.values()) == 4
    assert not any(s.status == "in_progress" for s in states.values())
    trials = sum(e["type"] == "trial" for e in session.ledger.events)

    again = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace),
                    simulate=True, as_real=True, speed=0, no_input=True, run_id="second")
    await again.run()
    assert sum(e["type"] == "trial" for e in again.ledger.events) == trials     # nothing ran a second time
    assert again.trial_cost == 0 and again.benches["demo-bench"].selected == 0
    assert any(kind == "rejected" and "already completed" in text for _, kind, _, text in again.recent)
    assert ledger.verify(workspace) == []


async def test_a_missing_brief_blocks_only_that_benchmark(workspace, ident):
    cfg = config_mod.load(workspace)
    cfg.select.require_brief = True
    s = Session(workspace, cfg, ident, tasks.read_registry(workspace), simulate=True, as_real=True, speed=0, no_input=True)
    await s.run()
    assert s.benches["demo-bench"].phase == "skipped"
    assert [r.kind for r in s.requests] == ["brief"]
    assert json.loads((s.run_dir / "requests.json").read_text())[0]["kind"] == "brief"


async def test_a_benchmark_trial_leaves_the_same_record_as_a_difficulty_trial(workspace, ident, session):
    from harbor_master.core.config import SuiteEntry
    from harbor_master.engine import evaluate
    await session.run()                                         # curation, with its difficulty trials
    bench = session.benches["demo-bench"]
    some = bench.tasks[:5]
    entry = SuiteEntry(agent="some-agent", model="some-model", reasoning_effort="high", version="1.2.3")
    later = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, as_real=True,
                    speed=0, no_input=True, run_id="eval-1")
    rows = await evaluate(later, entry, some, trials=3)
    assert len(rows) == 5 and all(r["trials"] == 3 and r["new"] == 3 for r in rows)

    trials = [e for e in later.ledger.events if e["type"] == "trial"]
    difficulty = next(e for e in trials if e["purpose"] == "difficulty")
    benchmark = next(e for e in trials if e["purpose"] == "benchmark")
    assert set(difficulty) == set(benchmark)                    # the very same fields
    assert (benchmark["agent"], benchmark["agent_version"], benchmark["agent_version_ran"], benchmark["model"],
            benchmark["reasoning_effort"]) == ("some-agent", "1.2.3", "1.2.3", "some-model", "high")
    assert sum(e["purpose"] == "benchmark" for e in trials) == 15

    again = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, as_real=True,
                    speed=0, no_input=True, run_id="eval-2")
    rows = await evaluate(again, entry, some, trials=3)         # nothing is run twice
    assert all(r["new"] == 0 and "not run again" in r["note"] for r in rows)
    assert sum(e["type"] == "trial" and e["purpose"] == "benchmark" for e in again.ledger.events) == 15
    rows, n = ledger.leaderboard(again.ledger.events)
    assert n == 0 and not any(r["agent"] == "some-agent" for r in rows)       # nothing is accepted yet, so nothing ranks
    assert ledger.verify(workspace) == []


async def test_a_run_leaves_a_small_readable_folder(workspace, session):
    await session.run()
    kept = session.benches["demo-bench"].kept
    files = sorted(p.relative_to(session.run_dir).as_posix() for p in session.run_dir.rglob("*") if p.is_file())
    # no copy of the ledger, no folder per task, no reports. requests.json is there because reports wait for a verdict
    assert files == ["requests.json", "run.json", "tasks.jsonl"]
    reports = sorted((workspace / "reports" / "demo-bench").glob("*.md"))    # a report is beside the ledger, where a reviewer
    assert len(reports) == kept                                              # on another machine gets it. One per task that reached one
    written = {e["location"]: e["report_digest"] for e in session.ledger.events if e["type"] == "report"}
    assert set(written) == {str(p.relative_to(workspace)) for p in reports} and ledger.verify(workspace) == []
    reports[0].write_text(reports[0].read_text().replace("accept", "reject"))
    assert any("its report was changed after it was recorded" in p for p in ledger.verify(workspace))     # and cannot be edited unseen
    lines = [json.loads(line) for line in (session.run_dir / "tasks.jsonl").read_text().splitlines()]
    assert len(lines) == 20 and all({"task_id", "status", "stages", "digest"} <= set(line) for line in lines)

    triage = [e for e in session.ledger.events if e["type"] == "decision" and e.get("stage") == "triage"]
    summaries = [e for e in triage if e["gate"] == "gates"]
    assert len(summaries) == 24                                        # one admission record per task, all gates in it
    assert {"format", "verifier", "license", "reference_solution"} <= set(summaries[0]["value"])
    decision = next(e for e in session.ledger.events if e["type"] == "decision" and e.get("stage") == "difficulty")
    assert "rationale" not in decision and "error" not in decision    # nothing empty is written


def test_harbor_output_is_kept_only_when_it_is_evidence(tmp_path):
    import tarfile
    from harbor_master.backends import harbor
    from harbor_master.backends.base import TrialResult

    def fake_job(name: str) -> TrialResult:
        trial = tmp_path / "harbor" / name / "task__abc"
        (trial / "verifier").mkdir(parents=True)
        (trial / "artifacts").mkdir()
        (trial / "result.json").write_text("{}")
        (trial / "lock.json").write_text("{}")
        (trial / "verifier" / "reward.txt").write_text("1")
        (trial / "artifacts" / "huge.mtx").write_bytes(b"0" * (harbor.MAX_KEPT_FILE + 1))
        (trial / "agent").mkdir()
        (trial / "agent" / "trajectory.json").write_bytes(b"0" * (harbor.MAX_KEPT_FILE + 1))     # a long trajectory
        return TrialResult(True, 1.0, 0, 0, 0.0, 1.0, artifact=str(trial), job=str(trial.parent))

    r = harbor.settle(fake_job("oracle-1"), keep=False, trials_dir=tmp_path / "trials", label="x", relative_to=tmp_path)
    assert r.artifact == "" and not (tmp_path / "harbor" / "oracle-1").exists() and not (tmp_path / "trials").exists()

    r = harbor.settle(fake_job("agent-1"), keep=True, trials_dir=tmp_path / "trials", label="t.agent.m.0", relative_to=tmp_path)
    assert r.artifact == "trials/t.agent.m.0.tar.gz" and not (tmp_path / "harbor" / "agent-1").exists()
    with tarfile.open(tmp_path / r.artifact) as tar:
        names = set(tar.getnames())
    # one file: the trajectory whole, however long, without the task's bulky output or Harbor's bookkeeping
    assert names == {"result.json", "verifier/reward.txt", "agent/trajectory.json", "OMITTED.txt"}


async def test_a_finished_run_takes_one_more_benchmark(workspace, ident, tmp_path):
    from conftest import make_benchmark
    s = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, speed=0, no_input=True, run_id="r1")
    await s.run()
    assert s.finished and s.benches["demo-bench"].phase == "ready"
    tasks.register(workspace, "second", make_benchmark(tmp_path / "more", "second", 12), "chemistry", dev=True)
    assert "not on this index's list" in interact.execute(s, interact.Command("add", ["nothing"]))
    assert "second is added to this run" in interact.execute(s, interact.Command("add", ["second"]))
    assert not s.finished and "already in this run" in interact.execute(s, interact.Command("add", ["second"]))
    await s._more                                                            # it is a run again, and finishes again
    assert s.finished and s.benches["second"].phase == "ready" and len(s.benches["second"].tasks) == 12
    assert s.benches["second"].kept + s.benches["second"].dropped == s.benches["second"].selected > 0
    assert "+ second" in s.command and (s.run_dir / "run.json").exists()
