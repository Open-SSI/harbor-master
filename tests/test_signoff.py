"""The two points where a person decides: the selection, and the final verdict."""
import asyncio

from harbor_master import interact, ledger, tasks
from harbor_master.core import config as config_mod
from harbor_master.engine import Session
from harbor_master.interact import Command

BENCH = "demo-bench"


def interactive(workspace, ident, run_id, signoff=True, **more):
    cfg = config_mod.load(workspace)
    cfg.select.require_signoff = signoff
    return Session(workspace, cfg, ident, tasks.read_registry(workspace), simulate=True, as_real=True, speed=0, run_id=run_id, **more)


def do(s, name, *args) -> str:
    """A command of the run screen. A verdict is what a reviewer types under the report they have open."""
    if name in ("accept", "reject"):
        return interact.decide(s, s.find_task(args[0]), " ".join([name, *args[1:]]))
    return interact.execute(s, Command(name, list(args)))


async def drops(s, said="drop"):
    """The person at the box when a check rejects a task: drops it, each time."""
    while True:
        for r in list(s.requests):
            if r.kind == "drop" and r.answer is not None and not r.answer.done():
                r.answer.set_result(said)
        await asyncio.sleep(0.01)


async def until(condition, seconds=5.0):
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def test_no_trial_is_spent_before_the_lead_approves_the_selection_and_a_new_brief_asks_again(workspace, ident):
    s = interactive(workspace, ident, "r1")
    run = asyncio.create_task(s.run())
    person = asyncio.create_task(drops(s))
    await until(lambda: any(r.kind == "selection" for r in s.requests))
    await asyncio.sleep(0.1)
    first = s.requests[0]
    assert s.benches[BENCH].phase == "signoff" and "24 tasks are ranked" in first.text and "at most 20. The top 20 are proposed" in first.text
    assert not any(e["type"] == "trial" for e in s.ledger.events)       # nothing costly has started
    assert "selected" in do(s, "selection", BENCH)

    do(s, "brief", BENCH, "Spectra", "tasks", "matter", "most.")        # a new brief ranks again, and asks again
    await until(lambda: any(r.kind == "selection" and r is not first for r in s.requests))
    assert s.ledger.brief(BENCH)["text"] == "Spectra tasks matter most."
    assert not any(e["type"] == "trial" for e in s.ledger.events)

    assert "at most 20" in do(s, "approve", BENCH, "1-21") and "not on the list" in do(s, "approve", BENCH, "25")   # not more than may go on
    assert "not a position" in do(s, "approve", BENCH, "none")
    assert "7 of 24" in do(s, "approve", BENCH, "1-5,", "7", "9")      # the user names the positions that go on
    await run
    signed = [e for e in s.ledger.events if e.get("gate") == "signoff" and e["tier"] == "human"]
    assert len(signed) == 1 and signed[0]["selected"] == 7 and signed[0]["positions"] == [1, 2, 3, 4, 5, 7, 9]
    order = [t for t, _, _ in s.benches[BENCH].ranking]
    assert [t for t, _, on in s.benches[BENCH].ranking if on] == [order[i - 1] for i in (1, 2, 3, 4, 5, 7, 9)]   # 6 and 8 left out
    assert s.benches[BENCH].kept + s.benches[BENCH].dropped == 7 and s.benches[BENCH].not_selected == 17 and ledger.verify(workspace) == []

    again = interactive(workspace, ident, "r2")                          # an approved selection is not asked twice
    person.cancel()
    person = asyncio.create_task(drops(again))
    await again.run()
    person.cancel()
    assert not any(r.kind == "selection" for r in again.requests)


async def test_after_a_protocol_change_the_approved_selection_and_every_outcome_stand_and_only_the_rest_runs(workspace, ident):
    import tomllib

    import tomli_w
    s = interactive(workspace, ident, "r1")
    run = asyncio.create_task(s.run())
    person = asyncio.create_task(drops(s))
    await until(lambda: any(r.kind == "selection" for r in s.requests))
    assert "7 of 24" in do(s, "approve", BENCH, "1-7")
    await run
    person.cancel()
    first = s.protocol
    done = {t: st for t, st in ledger.fold(s.ledger.events).items() if st.status in ("dropped", "report_ready")}
    assert len(done) == 7

    # One of them is left with no outcome, as a task whose session could not be had is: its records are taken out
    shard = next((workspace / "ledger" / BENCH).glob("*-r1.jsonl"))
    lines = shard.read_text().splitlines()
    left = sorted(done)[0]
    kept = [line for line in lines if f'"task_id":"{left}"' not in line or '"gate":"gates"' in line]
    shard.write_text("\n".join(kept) + "\n")

    protocol = tomllib.loads((workspace / "protocol.toml").read_text())
    protocol["select"]["comparisons_per_task"], protocol["select"]["pool_factor"] = 8, 2   # how tasks are ranked
    (workspace / "protocol.toml").write_text(tomli_w.dumps(protocol))
    again = interactive(workspace, ident, "r2")
    assert again.protocol != first
    person = asyncio.create_task(drops(again))
    await again.run()
    person.cancel()
    assert not any(r.kind == "selection" for r in again.requests)       # the approval stands: nothing ranked or asked
    mine = [e for e in again.ledger.events if e["protocol"] == again.protocol and e.get("task_id")]
    assert mine and {e["task_id"] for e in mine} == {left}               # only the task with no outcome ran again
    assert not any(e.get("stage") == "triage" for e in mine)            # its gates, passed before, were not asked again
    states = ledger.fold(again.ledger.events)
    assert all(states[t].status in ("dropped", "report_ready") for t in done)
