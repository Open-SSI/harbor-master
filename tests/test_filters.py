"""The cheap filters, the once-only difficulty run, the final cut, presets, providers and costs (DESIGN.md Sections 2, 4)."""


from harbor_master import tasks
from harbor_master.core import config as config_mod
from harbor_master.engine import Session


async def test_triage_drops_tasks_a_model_already_solves(workspace, ident, session):
    calls = []

    async def reported(benchmark, repo, names):       # the benchmark's own results cover three of its tasks
        calls.append(len(names))
        return {"tasks": {"t0001": {"pass_rate": 0.9, "model": "m"}, "T0002": {"pass_rate": 0.4, "model": "m"},
                          "not-a-task": {"pass_rate": 1.0}}, "source": "results.csv", "cost": 0.4}

    session.backend.reported = reported
    await session.run()
    gates = {e["task_id"].split("/")[-1]: e for e in session.ledger.events if e["type"] == "decision" and e.get("gate") == "gates"}
    assert gates["t0001"]["reason"] == "reported:0.9"
    assert gates["t0002"]["outcome"] == "pass" and "reported" not in gates["t0000"]["value"]     # at the limit, or no result: it passes
    assert session.benches["demo-bench"].triaged_out == 1 and calls == [24]

    again = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, as_real=True,
                    speed=0, no_input=True, run_id="second")
    again.backend.reported = reported
    await again.run()
    assert calls == [24]                                # the record is reused: no agent reads the results twice
