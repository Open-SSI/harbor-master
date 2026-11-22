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


def _scripted(session, monkeypatch, decided, verdicts=None):
    """The audits as a script: each agent's audit says which tier wrote it, with its verdict from `verdicts` (by tier;
    by default the decision's own first answer, so they agree), and the decision model answers `decided`, one (value,
    probability) for each call, in turn. Returns what each call was given."""
    from harbor_master.backends import Decision
    seen = {"audit": [], "decide": []}
    verdicts = verdicts or {"worker": decided[0][0], "frontier": decided[-1][0]}

    async def check_audit(task, check, tier, earlier=None):
        seen["audit"].append((check, tier, [e.rationale for e in earlier or []]))
        return Decision(verdicts[tier], 0.0, tier, f"{tier}-model", 0.1, 100, f"the {tier} agent's findings")

    async def check_decide(task, check, audits):
        seen["decide"].append([a.rationale for a in audits])
        value, prob = decided.pop(0)
        return Decision(value, prob, "decision", "jev", 0.001, 10)
    monkeypatch.setattr(session.backend, "check_audit", check_audit)
    monkeypatch.setattr(session.backend, "check_decide", check_decide)
    return seen


async def test_a_sure_decision_on_the_workers_audit_is_final(workspace, session, monkeypatch):
    from harbor_master.engine import stages
    from harbor_master.engine.state import CellState
    task = tasks.load_benchmark(Path(tasks.read_registry(workspace)["demo-bench"]["path"]), "demo-bench")[0]
    seen = _scripted(session, monkeypatch, [("valid", 0.97)])
    assert await stages.stage_verify(session, CellState(1), task, 0) == ("ok", "")
    assert [t for _, t, _ in seen["audit"]] == ["worker"] and seen["decide"] == [["the worker agent's findings"]]
    said = [e for e in session.ledger.events if e.get("gate") == "solution"]
    assert [(e["tier"], e["outcome"]) for e in said] == [("worker", "pass")]


async def test_an_unsure_decision_goes_to_the_frontier_agent_and_is_decided_on_both_audits(workspace, session, monkeypatch):
    from harbor_master.engine import stages
    from harbor_master.engine.state import CellState
    task = tasks.load_benchmark(Path(tasks.read_registry(workspace)["demo-bench"]["path"]), "demo-bench")[0]
    seen = _scripted(session, monkeypatch, [("unstated", 0.6), ("covered", 0.93)])         # the same logic for coverage
    assert await stages.stage_probe(session, CellState(1), task, 0) == ("ok", "")
    assert seen["audit"] == [("coverage", "worker", []), ("coverage", "frontier", ["the worker agent's findings"])]
    assert seen["decide"][1] == ["the worker agent's findings", "the frontier agent's findings"]
    said = [e for e in session.ledger.events if e.get("gate") == "coverage"]
    assert [(e["tier"], e["outcome"]) for e in said] == [("worker", "escalate"), ("frontier", "pass")]


async def test_a_sure_decision_against_the_workers_own_verdict_goes_to_the_frontier_agent(workspace, session, monkeypatch):
    """The worker found only minor points and said covered; the decision model, sure, said unstated. The frontier agent
    audits it, and the box, if any, opens with both verdicts."""
    from harbor_master.engine import stages
    from harbor_master.engine.state import CellState
    task = tasks.load_benchmark(Path(tasks.read_registry(workspace)["demo-bench"]["path"]), "demo-bench")[0]
    seen = _scripted(session, monkeypatch, [("unstated", 0.95), ("unstated", 0.9)], {"worker": "covered", "frontier": "unstated"})
    boxes = []

    async def person(kind, text, target="", choices=None):
        boxes.append(text)
        return "continue"
    monkeypatch.setattr(session, "ask", person)
    assert await stages.stage_probe(session, CellState(1), task, 0) == ("ok", "")
    assert [t for _, t, _ in seen["audit"]] == ["worker", "frontier"]
    said = [e for e in session.ledger.events if e.get("gate") == "coverage"]
    assert [(e["tier"], e["outcome"]) for e in said] == [("worker", "escalate"), ("frontier", "reject"), ("human", "pass")]
    assert said[1]["verdicts"] == {"worker": "covered", "frontier": "unstated"}
    first = boxes[0].split("\n\n")[1]
    assert first.startswith("The worker agent's verdict: covered. The frontier agent's verdict: unstated. The decision model decided unstated")


async def test_a_rejection_is_a_persons_to_confirm_and_continue_lets_the_task_go_on(workspace, session, monkeypatch):
    from harbor_master.engine import stages
    from harbor_master.engine.state import CellState
    found = tasks.load_benchmark(Path(tasks.read_registry(workspace)["demo-bench"]["path"]), "demo-bench")
    _scripted(session, monkeypatch, [("invalid", 0.95), ("invalid", 0.95), ("invalid", 0.95)])
    boxes = []

    async def person(kind, text, target="", choices=None):
        boxes.append((kind, target, choices))
        return "continue" if target == found[0].task_id else "drop"
    monkeypatch.setattr(session, "ask", person)
    assert await stages.stage_verify(session, CellState(1), found[0], 0) == ("ok", "")             # the person let it go on
    assert await stages.stage_verify(session, CellState(1), found[1], 0) == ("drop", "solution_rejected")
    assert [b[0] for b in boxes] == ["drop", "drop"] and boxes[0][2] == ["drop", "continue"]
    human = [(e["task_id"], e["outcome"]) for e in session.ledger.events if e.get("tier") == "human"]
    assert human == [(found[0].task_id, "pass"), (found[1].task_id, "drop")]
    assert await stages.stage_verify(session, CellState(1), found[0], 0) == ("ok", "") and len(boxes) == 2   # never asked twice

    async def nobody(kind, text, target="", choices=None):
        return None
    monkeypatch.setattr(session, "ask", nobody)                 # with nobody to ask it is dropped, and asked again next time
    assert await stages.stage_verify(session, CellState(1), found[2], 0) == ("drop", "solution_rejected")
    monkeypatch.setattr(session, "ask", person)
    assert await stages.stage_verify(session, CellState(1), found[2], 0) == ("drop", "solution_rejected") and len(boxes) == 3


async def test_a_costly_verifier_is_the_worker_agents_to_confirm_and_an_old_drop_is_screened_again(workspace, session, monkeypatch):
    from harbor_master.backends import Decision
    from harbor_master.engine import shared
    task = tasks.load_benchmark(Path(tasks.read_registry(workspace)["demo-bench"]["path"]), "demo-bench")[0]
    session.benches[task.benchmark].reported = {}
    sure = {"format": "agentic", "verifier": "programmatic", "duplicate": "distinct"}
    confirmed = []

    async def decide_gates(task, wanted):        # the small model calls the verifier costly, fairly sure
        return {g: Decision("costly", 0.7, "decision", "small") if g == "verifier_cost" else Decision(sure[g], 0.99, "decision", "small")
                for g in wanted}

    async def confirm(gate, choices, task, first):   # the worker agent reads the tests: it is cheap
        confirmed.append((gate, first.value))
        return Decision("cheap", 0.9, "worker", "agent", rationale="the tests run in seconds")
    monkeypatch.setattr(session.backend, "decide_gates", decide_gates)
    monkeypatch.setattr(session.backend, "confirm", confirm)
    assert await shared.triage(session, task) and confirmed == [("verifier_cost", "costly")]

    # A drop recorded before, on the small model's word alone, is screened again: the worker decides it.
    other = tasks.load_benchmark(Path(tasks.read_registry(workspace)["demo-bench"]["path"]), "demo-bench")[1]
    session.record(other.benchmark, "decision", other, stage="triage", gate="gates", tier="decision", outcome="drop",
                   reason="verifier_cost:costly", value={"format": {"value": "agentic", "p": 1.0, "tier": "decision"},
                                                         "verifier": {"value": "programmatic", "p": 1.0, "tier": "decision"},
                                                         "verifier_cost": {"value": "costly", "p": 0.64, "tier": "decision"}})
    assert await shared.triage(session, other) and confirmed[-1] == ("verifier_cost", "costly")
    standing = session.ledger.decision(other.digest, "triage", "gates", session.protocol)
    assert (standing["outcome"], standing["tier"]) == ("pass", "worker")
    assert await shared.triage(session, other)                  # and that answer stands: it is not asked a third time
    assert len(confirmed) == 2


def test_a_costly_verdict_must_cite_a_line_the_tasks_file_really_has(tmp_path):
    from harbor_master.backends.real import cited
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test.py").write_text("def test_fit():\n    run_mcmc(walkers=128,   steps=9000)\n")
    assert cited(tmp_path, "tests/test.py", "run_mcmc(walkers=128, steps=9000)") == ""      # spacing aside, as copied
    assert "not in tests/test.py" in cited(tmp_path, "tests/test.py", "steps=900000")
    assert "does not have" in cited(tmp_path, "tests/slow.py", "anything")
    assert "does not have" in cited(tmp_path, "../outside.py", "anything")
    assert "no file" in cited(tmp_path, "", "")


async def test_a_workers_costly_drop_that_cited_nothing_is_screened_again(workspace, session, monkeypatch):
    from harbor_master.backends import Decision
    from harbor_master.engine import shared
    from harbor_master.ledger import duplicate_key
    task = tasks.load_benchmark(Path(tasks.read_registry(workspace)["demo-bench"]["path"]), "demo-bench")[2]
    session.benches[task.benchmark].reported = {}
    session.record(task.benchmark, "decision", task, stage="triage", gate="verifier_cost", tier="worker", value="costly",
                   outcome="drop", rationale="I did not read the files.")
    session.record(task.benchmark, "decision", task, stage="triage", gate="gates", tier="worker", outcome="drop",
                   reason="verifier_cost:costly", value={"format": {"value": "agentic", "p": 1.0, "tier": "decision"},
                                                         "verifier": {"value": "programmatic", "p": 1.0, "tier": "decision"},
                                                         "verifier_cost": {"value": "costly", "p": 0.7, "tier": "worker"}})

    async def decide_gates(task, wanted):
        return {g: Decision("distinct", 0.99, "decision", "small") for g in wanted}

    async def confirm(gate, choices, task, first):      # this time it reads the tests, and cites them
        return Decision("costly", 0.9, "worker", "agent", rationale="a long simulation", evidence="tests/test.sh: run 10000 steps")
    monkeypatch.setattr(session.backend, "decide_gates", decide_gates)
    monkeypatch.setattr(session.backend, "confirm", confirm)
    assert not await shared.triage(session, task)
    standing = session.ledger.decision(task.digest, "triage", "gates", session.protocol)
    assert standing.get("redo") == 2 and standing["value"]["verifier_cost"]["evidence"] == "tests/test.sh: run 10000 steps"
    assert not shared.unconfirmed_drop(standing)            # cited: it stands
    keys = [duplicate_key(e) for e in session.ledger.events if duplicate_key(e)]
    assert len(keys) == len(set(keys))                      # no record clashes with the first screening's


async def test_ranking_asks_both_orders_at_once_and_a_restart_asks_nothing_again(workspace, tmp_path, monkeypatch):
    import asyncio
    import time
    from harbor_master.backends import jev, real
    from harbor_master.core import config as config_mod
    calls = []

    async def decisions(model, state, questions, **_):  # a decision model that takes a moment to answer
        calls.append(state)
        await asyncio.sleep(0.2)
        if "importance" in questions:
            return {"importance": {"type": "score", "score": float(len(state["task"]) % 7)}}, 0.0, 10
        return {"first_matters_more": {"noul": 0.7}}, 0.0, 10
    monkeypatch.setattr(jev, "decisions", decisions)
    cfg = config_mod.load(workspace)
    run_dir = tmp_path / ".hm" / "runs" / "r1"
    backend = real.RealBackend(cfg, lambda tier, cost, tokens: None, run_dir)
    found = tasks.load_benchmark(Path(tasks.read_registry(workspace)["demo-bench"]["path"]), "demo-bench")[:2]
    started = time.monotonic()
    assert abs(await backend.compare("brief", found[0], found[1]) - 0.5) < 1e-9       # 0.7 one way, 0.3 the other
    assert time.monotonic() - started < 0.35 and len(calls) == 2                     # the two orders side by side
    assert await backend.importance("brief", found[0]) is not None
    again = real.RealBackend(cfg, lambda tier, cost, tokens: None, tmp_path / ".hm" / "runs" / "r2")     # the next run
    await again.compare("brief", found[0], found[1])
    await again.importance("brief", found[0])
    assert len(calls) == 3                       # a restart asks neither comparison again, nor a score it has
    await again.compare("another brief", found[0], found[1])
    assert len(calls) == 5                       # a new brief is asked afresh


async def test_a_stage_that_errors_goes_to_the_frontier_model_and_runs_again_and_a_second_error_drops_it(workspace, session, monkeypatch):
    real, repair = session.backend.check_audit, session.backend.repair
    calls, repairs = {}, []
    names = sorted({t.name for t in (workspace.parent / "fake" / "demo-bench").iterdir()})
    broken = names[0]                       # this one errors every time; the rest error once

    async def flaky(task, check, *a, **k):  # the coverage audit fails on something in the task
        if check != "coverage":
            return await real(task, check, *a, **k)
        calls[task.name] = calls.get(task.name, 0) + 1
        if calls[task.name] == 1 or task.name == broken:
            raise RuntimeError("tests/test_outputs.py could not be read: UnicodeDecodeError")
        return await real(task, check, *a, **k)

    async def looked(task, defect, round_no, tier, why=""):
        repairs.append((task.name, defect, tier, "UnicodeDecodeError" in why))
        return await repair(task, defect, round_no, tier, why)
    monkeypatch.setattr(session.backend, "check_audit", flaky)
    monkeypatch.setattr(session.backend, "repair", looked)
    await session.run()
    bench = session.benches["demo-bench"]
    assert bench.kept + bench.dropped == bench.selected == 20 and bench.done == 20       # never put back in the queue
    errored = [r for r in repairs if r[1] == "error"]
    assert errored and all(tier == "frontier" and told for _, _, tier, told in errored)    # the frontier model, given the error
    assert calls.get(broken) == 2 and sum(r[0] == broken for r in errored) == 1           # one look, one retry, then dropped
    gone = [e for e in session.ledger.events if e.get("gate") == "error" and e["task_id"].endswith("/" + broken)]
    assert gone and gone[0]["outcome"] == "drop" and gone[0]["reason"] == "error:probe"


async def test_a_model_that_cannot_be_reached_neither_repairs_nor_drops_the_task_and_the_next_run_takes_it_again(workspace, ident, session, monkeypatch):
    from harbor_master.backends.base import Unreachable
    real, repair, repairs = session.backend.check_audit, session.backend.repair, []
    names = sorted({t.name for t in (workspace.parent / "fake" / "demo-bench").iterdir()})
    away = names[0]

    async def cut_off(task, check, *a, **k):    # Claude Code never starts for this task's coverage audit
        if check == "coverage" and task.name == away:
            raise Unreachable("Claude (worker) could not be reached, after 5 tries over 7.5 minutes: Control request timeout")
        return await real(task, check, *a, **k)

    async def looked(*a, **k):
        repairs.append(a)
        return await repair(*a, **k)
    monkeypatch.setattr(session.backend, "check_audit", cut_off)
    monkeypatch.setattr(session.backend, "repair", looked)
    await session.run()
    mine = [e for e in session.ledger.events if e.get("task_id", "").endswith("/" + away)]
    assert not any(e.get("gate") == "error" or e.get("outcome") == "drop" for e in mine)      # not dropped
    assert not any(a[0].name == away and a[1] == "error" for a in repairs)                       # not repaired
    bench = session.benches["demo-bench"]
    assert bench.done == 20 and bench.kept + bench.dropped == 19           # counted as finished in this run, with no outcome
    again = another(workspace, ident, "r2")
    await again.run()
    assert any(e.get("gate") == "coverage" and e.get("task_id", "").endswith("/" + away) for e in again.ledger.events)


async def test_trials_stop_once_the_result_cannot_change_and_no_agent_waits_for_another(workspace, session, monkeypatch):
    import asyncio
    import time

    from harbor_master.backends.base import TrialResult
    fable, astra = session.cfg.difficulty.suite                 # claude-code with Fable, codex with Astra
    worker = session.cfg.tiers.worker.model
    started, finished = [], []

    async def run_trial(task, entry, index, max_hours=0):
        started.append((task.name, entry.model, index))
        if entry.model == worker:           # the worker agent: its first try fails at once, its second would take long
            if index == 0:
                return TrialResult(False, 0.0, 0, 0, 0.0, 1.0)
            await asyncio.sleep(60)
        if entry.model == fable.model:      # Fable's tries take long; Astra's fail at once, three of them settle it
            await asyncio.sleep(60)
        finished.append((task.name, entry.model, index))
        return TrialResult(False, 0.0, 0, 0, 0.0, 1.0)
    monkeypatch.setattr(session.backend, "run_trial", run_trial)
    t0 = time.monotonic()
    await session.run()
    assert time.monotonic() - t0 < 30                           # no slow try ran to its end
    assert not any(m == fable.model or (m == worker and i == 1) for _, m, i in finished)
    trials = [e for e in session.ledger.events if e["type"] == "trial"]
    assert trials and not any(e["model"] == fable.model or (e["purpose"] == "cheap_solve" and e["trial_index"] == 1)
                              for e in trials)                  # a stopped try is not recorded
    rates = [e for e in session.ledger.events if e.get("gate") == "pass_rate"]
    assert rates and all(e["trials"] == 3 and not e.get("passes") and e["stopped_early"] and e["outcome"] == "pass" for e in rates)
    for name in {n for n, _, _ in started}:                    # Astra ran all three while Fable's first was still going
        assert sum((n, m) == (name, astra.model) for n, m, _ in started) in (0, 3)
