import json
import tarfile

import pytest

from harbor_master import interact, ledger, tasks
from harbor_master.backends import NotImplementedBackend, harbor
from harbor_master.backends.base import TrialResult
from harbor_master.core import config as config_mod
from harbor_master.engine import Session

from conftest import make_benchmark


def another(workspace, ident, run_id, as_real=True):
    """One more session on the same index, as the `session` fixture makes them."""
    return Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, as_real=as_real, speed=0,
                   no_input=True, run_id=run_id)


def test_a_tier_with_an_unknown_provider_fails_clearly(workspace, ident):
    cfg = config_mod.load(workspace)
    cfg.tiers.worker.provider = "something-else"
    with pytest.raises(NotImplementedBackend, match="worker tier"):
        Session(workspace, cfg, ident, tasks.read_registry(workspace), simulate=False)


async def test_a_simulated_run_is_sound_leaves_a_small_readable_folder_and_a_rerun_is_rejected(workspace, ident, session):
    await session.run()
    bench, events = session.benches["demo-bench"], session.ledger.events
    assert bench.selected == 20 and bench.kept + bench.dropped == 20
    assert ledger.verify(workspace) == []
    states = ledger.fold(events)
    assert sum(s.status == "report_ready" for s in states.values()) == bench.kept
    assert sum(s.status == "not_selected" for s in states.values()) == 4
    assert not any(s.status == "in_progress" for s in states.values())
    trials = sum(e["type"] == "trial" for e in events)

    files = sorted(p.relative_to(session.run_dir).as_posix() for p in session.run_dir.rglob("*") if p.is_file())
    # no copy of the ledger, no folder per task, no reports. requests.json is there because reports wait for a verdict
    assert files == ["requests.json", "run.json", "tasks.jsonl"]
    reports = sorted((workspace / "reports" / "demo-bench").glob("*.md"))    # a report is beside the ledger, where a reviewer
    assert len(reports) == bench.kept                                        # on another machine gets it. One per task that reached one
    written = {e["location"]: e["report_digest"] for e in events if e["type"] == "report"}
    assert set(written) == {str(p.relative_to(workspace)) for p in reports}
    lines = [json.loads(line) for line in (session.run_dir / "tasks.jsonl").read_text().splitlines()]
    assert len(lines) == 20 and all({"task_id", "status", "stages", "digest"} <= set(line) for line in lines)
    summaries = [e for e in events if e["type"] == "decision" and e.get("stage") == "triage" and e["gate"] == "gates"]
    assert len(summaries) == 24                                        # one admission record per task, all gates in it
    assert {"format", "verifier", "license", "reference_solution"} <= set(summaries[0]["value"])
    decision = next(e for e in events if e["type"] == "decision" and e.get("stage") == "difficulty")
    assert "rationale" not in decision and "error" not in decision    # nothing empty is written

    again = another(workspace, ident, "second")
    await again.run()
    assert sum(e["type"] == "trial" for e in again.ledger.events) == trials     # nothing ran a second time
    assert again.trial_cost == 0 and again.benches["demo-bench"].selected == 0
    assert any(kind == "rejected" and "already completed" in text for _, kind, _, text in again.recent)
    assert ledger.verify(workspace) == []

    reports[0].write_text(reports[0].read_text().replace("accept", "reject"))
    assert any("its report was changed after it was recorded" in p for p in ledger.verify(workspace))     # and cannot be edited unseen


async def test_a_missing_brief_blocks_only_that_benchmark(workspace, ident):
    cfg = config_mod.load(workspace)
    cfg.select.require_brief = True
    s = Session(workspace, cfg, ident, tasks.read_registry(workspace), simulate=True, as_real=True, speed=0, no_input=True)
    await s.run()
    assert s.benches["demo-bench"].phase == "skipped" and [r.kind for r in s.requests] == ["brief"]
    assert json.loads((s.run_dir / "requests.json").read_text())[0]["kind"] == "brief"


def test_harbor_output_is_kept_only_when_it_is_evidence(tmp_path):
    def fake_job(name: str) -> TrialResult:
        trial = tmp_path / "harbor" / name / "task__abc"
        for sub in ("verifier", "artifacts", "agent"):
            (trial / sub).mkdir(parents=True)
        for file, text in (("result.json", "{}"), ("lock.json", "{}"), ("verifier/reward.txt", "1")):
            (trial / file).write_text(text)
        for file in ("artifacts/huge.mtx", "agent/trajectory.json"):                 # bulky output, and a long trajectory
            (trial / file).write_bytes(b"0" * (harbor.MAX_KEPT_FILE + 1))
        return TrialResult(True, 1.0, 0, 0, 0.0, 1.0, artifact=str(trial), job=str(trial.parent))

    r = harbor.settle(fake_job("oracle-1"), keep=False, trials_dir=tmp_path / "trials", label="x", relative_to=tmp_path)
    assert r.artifact == "" and not (tmp_path / "harbor" / "oracle-1").exists() and not (tmp_path / "trials").exists()

    r = harbor.settle(fake_job("agent-1"), keep=True, trials_dir=tmp_path / "trials", label="t.agent.m.0", relative_to=tmp_path)
    assert r.artifact == "trials/t.agent.m.0.tar.gz" and not (tmp_path / "harbor" / "agent-1").exists()
    with tarfile.open(tmp_path / r.artifact) as tar:
        # one file: the trajectory whole, however long, without the task's bulky output or Harbor's bookkeeping
        assert {"result.json", "verifier/reward.txt", "agent/trajectory.json", "OMITTED.txt", "lock.json"} == set(tar.getnames())     # the whole Harbor folder


async def test_a_finished_run_takes_one_more_benchmark(workspace, ident, tmp_path):
    s = another(workspace, ident, "r1", as_real=False)
    await s.run()
    assert s.finished and s.benches["demo-bench"].phase == "ready"
    tasks.register(workspace, "second", make_benchmark(tmp_path / "more", "second", 12), dev=True)
    assert "not on this index's list" in interact.execute(s, interact.Command("add", ["nothing"]))
    assert "second is added to this run" in interact.execute(s, interact.Command("add", ["second"]))
    assert not s.finished and "already in this run" in interact.execute(s, interact.Command("add", ["second"]))
    await s._more                                                            # it is a run again, and finishes again
    assert s.finished and s.benches["second"].phase == "ready" and len(s.benches["second"].tasks) == 12
    assert s.benches["second"].kept + s.benches["second"].dropped == s.benches["second"].selected > 0
    assert "+ second" in s.command and (s.run_dir / "run.json").exists()
