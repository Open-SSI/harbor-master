"""The two points where a person decides: the selection, and the final verdict."""
import asyncio

from harbor_master import interact, ledger, tasks
from harbor_master.core import config as config_mod
from harbor_master.engine import Session
from harbor_master.interact import Command

BENCH = "demo-bench"


def interactive(workspace, ident, run_id, signoff=True):
    cfg = config_mod.load(workspace)
    cfg.select.require_signoff = signoff
    return Session(workspace, cfg, ident, tasks.read_registry(workspace), simulate=True, as_real=True, speed=0, run_id=run_id)


async def until(condition, seconds=5.0):
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def test_no_trial_is_spent_before_the_lead_approves_the_selection(workspace, ident):
    s = interactive(workspace, ident, "r1")
    run = asyncio.create_task(s.run())
    await until(lambda: any(r.kind == "selection" for r in s.requests))
    await asyncio.sleep(0.1)
    assert s.benches[BENCH].phase == "signoff"
    assert not any(e["type"] == "trial" for e in s.ledger.events)       # nothing costly has started
    assert "20 of 24" in s.requests[0].text
    assert "selected" in interact.execute(s, Command("selection", ["demo-bench"]))

    assert "approved" in interact.execute(s, Command("approve", ["demo-bench"]))
    await run
    assert any(e.get("gate") == "signoff" and e["tier"] == "human" for e in s.ledger.events)
    assert s.benches[BENCH].kept + s.benches[BENCH].dropped == 20
    assert ledger.verify(workspace) == []

    again = interactive(workspace, ident, "r2")                          # an approved selection is not asked twice
    await again.run()
    assert not any(r.kind == "selection" for r in again.requests)


async def test_a_new_brief_ranks_again_and_asks_again(workspace, ident):
    s = interactive(workspace, ident, "r1")
    run = asyncio.create_task(s.run())
    await until(lambda: any(r.kind == "selection" for r in s.requests))
    first = s.requests[0]
    interact.execute(s, Command("brief", [BENCH, "Spectra", "tasks", "matter", "most."]))
    await until(lambda: any(r.kind == "selection" and r is not first for r in s.requests))
    assert s.ledger.brief(BENCH)["text"] == "Spectra tasks matter most."
    assert not any(e["type"] == "trial" for e in s.ledger.events)
    interact.execute(s, Command("approve", [BENCH]))
    await run


async def test_reports_are_briefed_and_signed_in_the_session(workspace, ident):
    s = interactive(workspace, ident, "r1", signoff=False)
    await s.run()
    kept = s.benches[BENCH].kept
    assert kept and len(s.ready) == kept
    assert [r.kind for r in s.requests] == ["verdict"]                   # it waits, and blocks nothing
    briefing = interact.execute(s, Command("review"))
    assert f"{kept} report" in briefing and "/report ID" in briefing and "by frontier agents" in briefing

    first, second = s.ready[0], s.ready[1]
    assert "Recommendation" in interact.execute(s, Command("report", [first.name]))
    assert "Give a reason" in interact.execute(s, Command("reject", [second.name]))   # a rejection needs a reason
    assert interact.needs_confirmation(interact.interpret(s, f"/accept {first.name}"))  # a verdict is always confirmed
    assert "Accepted" in interact.execute(s, Command("accept", [first.name, "clean", "and", "hard"]))
    assert "Rejected" in interact.execute(s, Command("reject", [second.name, "too", "narrow"]))
    assert "no report waiting" in interact.execute(s, Command("accept", [first.name]))  # not twice

    states = ledger.fold(s.ledger.events)
    assert states[first.task_id].status == "accepted" and states[second.task_id].status == "rejected"
    assert len(s.ready) == kept - 2
    assert ledger.verify(workspace) == []


async def test_someone_off_the_contributor_list_cannot_sign(workspace, ident):
    (workspace / ledger.CONTRIBUTORS).write_text("")
    s = interactive(workspace, ident, "r1", signoff=False)
    await s.run()
    assert not s.is_reviewer and not s.ready and not s.requests
    assert "cannot sign" in interact.execute(s, Command("review"))


async def test_a_full_benchmark_asks_which_task_to_replace(workspace, ident):
    cfg = config_mod.load(workspace)
    cfg.select.require_signoff = False
    cfg.index.max_per_benchmark = 2
    s = Session(workspace, cfg, ident, tasks.read_registry(workspace), simulate=True, as_real=True, speed=0, run_id="r1")
    await s.run()
    a, b, c = s.ready[:3]
    interact.execute(s, Command("accept", [a.name]))
    interact.execute(s, Command("accept", [b.name]))

    reply = interact.execute(s, Command("accept", [c.name]))            # the third does not go in
    assert "is full (2 of 2)" in reply and "/replace OLD" in reply
    assert c in s.ready
    ask = next(r for r in s.requests if r.kind == "replace")
    assert a.name in ask.text and b.name in ask.text and not ask.blocking
    assert "2 of 2 places" in interact.execute(s, Command("included", ["demo-bench"]))

    intent = interact.interpret(s, f"replace {a.name} with {c.name}")   # plain text maps to it, and is confirmed
    assert str(intent.commands[0]) == f"/replace {a.task_id} {c.task_id}" and interact.needs_confirmation(intent)
    assert "in place of" in interact.execute(s, intent.commands[0])
    states = ledger.fold(s.ledger.events)
    assert states[a.task_id].status == "replaced" and states[c.task_id].status == "accepted"
    assert len(ledger.accepted(s.ledger.events, BENCH)) == 2
    assert not any(r.kind == "replace" for r in s.requests)
    assert ledger.verify(workspace, max_per_benchmark=2) == []
    assert any("more than the 1" in p for p in ledger.verify(workspace, max_per_benchmark=1))


async def test_a_second_person_leaves_claimed_tasks_alone(workspace, ident, monkeypatch, tmp_path):
    from harbor_master.core import identity
    first = interactive(workspace, ident, "r1", signoff=False)
    from harbor_master.engine import shared
    await shared.run_shared(first, first.benches[BENCH])
    assert len(first.claimed[BENCH]) == 20

    monkeypatch.setenv("HM_CONFIG_DIR", str(tmp_path / "second"))      # a second contributor, with their own key
    other = identity.create("Second Person", "Other University", "s@example.org", "second-person")
    path = workspace / ledger.CONTRIBUTORS
    path.write_text(path.read_text() + f'\n[[contributor]]\nname = "Second Person"\ngithub = "second-person"\n'
                    f'key = "{other.public_key}"\n')
    second = Session(workspace, config_mod.load(workspace), other, tasks.read_registry(workspace), simulate=True, as_real=True,
                     speed=0, no_input=True, run_id="r2")
    second.cfg.select.require_signoff = False
    await second.run()
    assert second.benches[BENCH].selected == 0               # every selected task is held by the first person
    assert not any(e["type"] == "trial" and e["runner"] == "second-person" for e in second.ledger.events)
    assert any(kind == "rejected" and "being run by @test-runner" in text for _, kind, _, text in second.recent)
    assert ledger.Ledger(workspace).claims(BENCH, __import__("time").time(), me="second-person")


def test_anyone_listed_with_a_handle_and_a_key_can_contribute():
    owner = {"name": "O", "github": "o", "key": "k1", "role": "owner"}
    listed = {"name": "U", "github": "u", "key": "k3"}
    waiting = {"name": "W", "github": "w", "key": ""}
    assert ledger.can_contribute(owner) and ledger.can_contribute(listed)
    assert not ledger.can_contribute(waiting) and not ledger.can_contribute(None)
    assert (ledger.status(owner), ledger.status(listed), ledger.status(waiting)) == ("owner", "can contribute", "listed, waiting for signing key")


def test_every_skill_loads_and_has_a_version():
    from harbor_master import skills
    from harbor_master.curation import gates
    for name in ("triage/gates", "triage/confirm", "rank/compare", "probe/write-mutants",
                 "audit/rubric", "session/interpret", "session/answer"):
        s = skills.load(name)
        assert len(s.version) == 10 and s.tag == f"{name}@{s.version}"
    questions, criteria, tag = gates.wording()
    assert set(questions) == set(gates.D_GATES) and tag.startswith("triage/gates@")
    for gate, (choices, passing) in gates.D_GATES.items():
        assert set(criteria[gate]) == set(choices) and passing <= set(choices)      # every choice has a meaning
    assert "$question" not in skills.load("session/answer").fill(question="q", records="r")


async def test_the_leaderboard_and_the_accepted_list_cover_accepted_tasks_only(workspace, ident):
    import tomllib
    from harbor_master.core.config import SuiteEntry
    from harbor_master.engine import evaluate
    s = interactive(workspace, ident, "r1", signoff=False)
    await s.run()
    a, b = s.ready[0], s.ready[1]
    interact.execute(s, Command("accept", [a.name]))
    interact.execute(s, Command("accept", [b.name]))

    index_file = workspace / ledger.INDEX_FILE          # the accepted list is a file, kept by the tool
    listed = tomllib.loads(index_file.read_text())["task"]
    assert [t["task"] for t in listed] == sorted([a.task_id, b.task_id]) and listed[0]["accepted_by"] == ["test-runner"]

    entry = SuiteEntry(agent="new-agent", model="new-model", version="2.0")
    later = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, as_real=True,
                    speed=0, no_input=True, run_id="eval")
    dropped = next(t for t in s.benches[BENCH].tasks if s.blackboards.get(t.task_id, {}).get("status") == "dropped")
    await evaluate(later, entry, [a, dropped], trials=3)             # one accepted task, one that was dropped

    suite = [(e.agent, e.model) for e in later.cfg.difficulty.suite]
    rows, n = ledger.leaderboard(later.ledger.events, roster=[{"name": "listed", "agent": "x", "model": "y"}], suite=suite)
    by = {r["agent"]: r for r in rows}
    assert n == 2
    assert by["new-agent"]["tasks"] == 1 and by["new-agent"]["trials"] == 3 and not by["new-agent"]["complete"]   # the dropped task does not count
    assert by["claude-code"]["complete"] and by["claude-code"]["selected_against"]      # the suite ran both accepted tasks
    assert by["claude-code"]["score"] <= 0.4                                           # and mostly failed them, by construction
    assert by["x"]["tasks"] == 0 and by["x"]["score"] is None                          # on the roster, not run yet

    index_file.write_text(index_file.read_text() + f'\n[[task]]\ntask = "{dropped.task_id}"\ndigest = "{dropped.digest}"\n')
    assert any("edited by hand" in p for p in ledger.verify(workspace))     # nobody adds a task by hand
    ledger.write_index(workspace)
    assert ledger.verify(workspace) == []
