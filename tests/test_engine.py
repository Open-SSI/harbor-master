import json
from pathlib import Path


from harbor_master import ledger, tasks
from harbor_master.core import config as config_mod
from harbor_master.engine import Session


def another(workspace, ident, run_id, as_real=True):
    """One more session on the same index, as the `session` fixture makes them."""
    return Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, as_real=as_real, speed=0,
                   no_input=True, run_id=run_id)


async def test_a_simulated_run_is_sound_leaves_a_small_readable_folder_and_a_rerun_is_rejected(workspace, ident, session):
    await session.run()
    bench, events = session.benches["demo-bench"], session.ledger.events
    assert bench.selected == 20 and bench.kept + bench.dropped == 20
    assert ledger.verify(workspace) == []
    states = ledger.fold(events)
    assert sum(s.status == "report_ready" for s in states.values()) == bench.kept         # a dropped task gets no report
    assert sum(s.status == "dropped" for s in states.values()) == bench.dropped
    assert sum(s.status == "not_selected" for s in states.values()) == 4
    assert not any(s.status == "in_progress" for s in states.values())
    trials = sum(e["type"] == "trial" for e in events)

    files = sorted(p.relative_to(session.run_dir).as_posix() for p in session.run_dir.rglob("*") if p.is_file())
    # no copy of the ledger, no folder per task, no reports. requests.json is there because reports wait for a verdict
    assert files == ["requests.json", "run.json", "tasks.jsonl"]
    reports = sorted((workspace / "reports" / "demo-bench").glob("*.md"))    # a report is beside the ledger, where a reviewer
    assert len(reports) == bench.kept                                        # on another machine gets it. One per kept task
    written = {e["location"]: e["report_digest"] for e in events if e["type"] == "report"}
    assert set(written) == {str(p.relative_to(workspace)) for p in reports}
    lines = [json.loads(line) for line in (session.run_dir / "tasks.jsonl").read_text().splitlines()]
    assert len(lines) == bench.selected and all({"task_id", "status", "stages", "digest"} <= set(line) for line in lines)
    summaries = [e for e in events if e["type"] == "decision" and e.get("stage") == "triage" and e["gate"] == "gates"]
    assert len(summaries) == 24                                        # one admission record per task, all gates in it
    assert {"format", "verifier", "reference_solution"} <= set(summaries[0]["value"])
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


async def test_a_selected_task_with_no_reference_solution_has_one_written(workspace, ident):
    """It passes the screen. In its work cell the decision model says what would make it valid, before any repair is
    paid for, and then the solution is written as a repair, and the task goes on."""
    import shutil
    task_dir = sorted((workspace.parent / "fake" / "demo-bench").iterdir())[0]
    shutil.rmtree(task_dir / "solution", ignore_errors=True)
    s = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, speed=0,
                no_input=True, run_id="r-solution", only=[task_dir.name])
    await s.run()
    events = [e for e in s.ledger.events if e.get("task_id", "").endswith("/" + task_dir.name)]
    assert next(e for e in events if e.get("gate") == "gates")["outcome"] == "pass"      # no solution: not dropped at the screen
    diagnosis = next(e for e in events if e.get("gate") == "diagnose")
    assert diagnosis["tier"] == "decision" and diagnosis["value"] in ("write_solution", "drop")
    if diagnosis["value"] == "write_solution":
        assert any(e["type"] == "repair" for e in events)                                  # written, as a repair


async def test_a_benchmark_of_which_no_task_is_accepted_is_dropped(workspace, ident):
    """Once a run is over, the verdict that leaves no task accepted drops the benchmark. Anyone may take it again."""
    from harbor_master import interact
    task_dir = sorted((workspace.parent / "fake" / "demo-bench").iterdir())[-1]
    s = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, speed=0,
                no_input=True, run_id="r-none", only=[task_dir.name])
    await s.run()
    status = lambda: ledger.benchmarks.statuses(s.ledger.events, ["demo-bench"])["demo-bench"]["status"]  # noqa: E731
    for task in list(s.ready):
        assert status() != "dropped"                            # a report still waits: nothing is decided yet
        assert "Rejected" in interact.decide(s, task, "reject the verifier is too loose")
    assert s.benches["demo-bench"].kept + s.benches["demo-bench"].dropped == 1
    assert status() == "dropped" and "no task was accepted" in s.benches["demo-bench"].detail


async def test_an_unstated_check_is_the_worker_agents_to_confirm_never_the_small_models_word(workspace, session, monkeypatch):
    from harbor_master.backends import Decision
    from harbor_master.engine import stages
    from harbor_master.engine.state import CellState
    task = tasks.load_benchmark(Path(tasks.read_registry(workspace)["demo-bench"]["path"]), "demo-bench")[0]
    asked = []

    async def covers(task, first=None):         # the small model is sure, and wrong; the worker agent reads the files
        asked.append(first)
        return (Decision("unstated", 0.95, "decision", "small") if first is None else
                Decision("covered", 0.9, "worker", "agent", rationale="everything is stated"))
    monkeypatch.setattr(session.backend, "covers", covers)
    assert await stages.coverage(session, CellState(1), task) == ("ok", "") and len(asked) == 2
    said = [e for e in session.ledger.events if e.get("gate") == "coverage"]
    assert [(e["tier"], e["outcome"]) for e in said] == [("decision", "escalate"), ("worker", "pass")]
