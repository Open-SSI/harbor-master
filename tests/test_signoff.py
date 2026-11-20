"""The two points where a person decides: the selection, and the final verdict."""
import asyncio
import time
import tomllib

from harbor_master import interact, ledger, skills, tasks
from harbor_master.core import config as config_mod
from harbor_master.core import identity
from harbor_master.curation import gates
from harbor_master.engine import Session, shared
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


async def until(condition, seconds=5.0):
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def test_no_trial_is_spent_before_the_lead_approves_the_selection_and_a_new_brief_asks_again(workspace, ident):
    s = interactive(workspace, ident, "r1")
    run = asyncio.create_task(s.run())
    await until(lambda: any(r.kind == "selection" for r in s.requests))
    await asyncio.sleep(0.1)
    first = s.requests[0]
    assert s.benches[BENCH].phase == "signoff" and "24 tasks are ranked" in first.text and "from 1 to 20. 20 is proposed" in first.text
    assert not any(e["type"] == "trial" for e in s.ledger.events)       # nothing costly has started
    assert "selected" in do(s, "selection", BENCH)

    do(s, "brief", BENCH, "Spectra", "tasks", "matter", "most.")        # a new brief ranks again, and asks again
    await until(lambda: any(r.kind == "selection" and r is not first for r in s.requests))
    assert s.ledger.brief(BENCH)["text"] == "Spectra tasks matter most."
    assert not any(e["type"] == "trial" for e in s.ledger.events)

    assert "from 1 to 20" in do(s, "approve", BENCH, "21") and "from 1 to 20" in do(s, "approve", BENCH, "none")     # not more than may go on
    assert "the top 7 of 24" in do(s, "approve", BENCH, "7")            # the user says how many of the top ones go on
    await run
    signed = [e for e in s.ledger.events if e.get("gate") == "signoff" and e["tier"] == "human"]
    assert len(signed) == 1 and signed[0]["selected"] == 7
    assert s.benches[BENCH].kept + s.benches[BENCH].dropped == 7 and s.benches[BENCH].not_selected == 17 and ledger.verify(workspace) == []

    again = interactive(workspace, ident, "r2")                          # an approved selection is not asked twice
    await again.run()
    assert not any(r.kind == "selection" for r in again.requests)


async def test_reports_are_signed_in_the_session_and_the_leaderboard_covers_accepted_tasks_only(workspace, ident):
    s = interactive(workspace, ident, "r1", signoff=False)
    await s.run()
    kept = s.benches[BENCH].kept
    assert kept and len(s.ready) == kept
    assert [r.kind for r in s.requests] == ["verdict"]                   # it waits, and blocks nothing
    briefing = do(s, "review")
    assert f"{kept} report" in briefing and "/review opens them" in briefing and "by frontier agents" in briefing

    a, b, second = s.ready[:3]
    assert "Recommendation" in interact.answers.report_text(s, a) and interact.waiting_reports(s, a.name) == [a]
    assert "Give a reason" in do(s, "reject", second.name)               # a rejection needs a reason
    for gone in ("accept", "reject", "sendback", "replace", "report"):   # a verdict is given under the report, nowhere else
        assert gone not in interact.COMMANDS
    assert "Type accept" in interact.decide(s, a, "looks fine")          # anything else is not taken for a verdict
    assert "Accepted" in do(s, "accept", a.name, "clean", "and", "hard")
    assert "Rejected" in do(s, "reject", second.name, "too", "narrow")
    assert "no report waiting" in do(s, "accept", a.name)                # not twice
    states = ledger.fold(s.ledger.events)
    assert states[a.task_id].status == "accepted" and states[second.task_id].status == "rejected"
    assert len(s.ready) == kept - 2 and ledger.verify(workspace) == []

    do(s, "accept", b.name)
    index_file = workspace / ledger.INDEX_FILE          # the accepted list is a file, kept by the tool
    listed = tomllib.loads(index_file.read_text())["task"]
    assert [t["task"] for t in listed] == sorted([a.task_id, b.task_id]) and listed[0]["accepted_by"] == ["test-runner"]

    dropped = next(t for t in s.benches[BENCH].tasks if s.blackboards.get(t.task_id, {}).get("status") == "dropped")
    suite = [(e.agent, e.model) for e in s.cfg.difficulty.suite]
    rows, n = ledger.leaderboard(s.ledger.events, roster=[{"name": "listed", "agent": "x", "model": "y"}], suite=suite)
    by = {r["agent"]: r for r in rows}
    assert n == 2 and by["claude-code"]["selected_against"] and by["claude-code"]["selection_trials"] == by["claude-code"]["trials"] > 0
    assert by["claude-code"]["score"] <= 0.4                                           # the suite mostly failed them, by construction
    assert by["x"]["tasks"] == 0 and by["x"]["score"] is None                          # on the roster, not run yet

    index_file.write_text(index_file.read_text() + f'\n[[task]]\ntask = "{dropped.task_id}"\ndigest = "{dropped.digest}"\n')
    assert any("edited by hand" in p for p in ledger.verify(workspace))     # nobody adds a task by hand
    ledger.write_index(workspace)
    assert ledger.verify(workspace) == []


async def test_someone_off_the_contributor_list_cannot_sign(workspace, ident):
    (workspace / ledger.CONTRIBUTORS).write_text("")
    s = interactive(workspace, ident, "r1", signoff=False)
    await s.run()
    assert not s.is_reviewer and not s.ready and not s.requests and "cannot sign" in do(s, "review")


async def test_a_benchmark_may_have_any_number_of_tasks_in_the_index(workspace, ident):
    s = interactive(workspace, ident, "r1", signoff=False)
    await s.run()
    waiting = list(s.ready)
    assert len(waiting) > 2 and all("Accepted" in do(s, "accept", t.name) for t in waiting)       # every one goes in: there is no cap
    assert len(ledger.accepted(s.ledger.events, BENCH)) == len(waiting) and not s.requests
    assert f"{len(waiting)} tasks in the index" in do(s, "included", BENCH) and ledger.verify(workspace) == []
    assert "Type accept" in interact.decide(s, waiting[0], f"replace {waiting[1].name}") and "replace" not in interact.COMMANDS
async def test_a_second_person_leaves_claimed_tasks_alone(workspace, ident, monkeypatch, tmp_path):
    first = interactive(workspace, ident, "r1", signoff=False)
    await shared.run_shared(first, first.benches[BENCH])
    assert len(first.claimed[BENCH]) == 20

    monkeypatch.setenv("HM_CONFIG_DIR", str(tmp_path / "second"))      # a second contributor, with their own key
    other = identity.create("Second Person", "Other University", "s@example.org", "second-person")
    path = workspace / ledger.CONTRIBUTORS
    path.write_text(path.read_text() + f'\n[[contributor]]\nname = "Second Person"\ngithub = "second-person"\nkey = "{other.public_key}"\n')
    second = interactive(workspace, other, "r2", signoff=False, no_input=True)
    await second.run()
    assert second.benches[BENCH].selected == 0               # every selected task is held by the first person
    assert not any(e["type"] == "trial" and e["runner"] == "second-person" for e in second.ledger.events)
    assert any(kind == "rejected" and "being run by @test-runner" in text for _, kind, _, text in second.recent)
    assert ledger.Ledger(workspace).claims(BENCH, time.time(), me="second-person")


def test_anyone_listed_with_a_handle_and_a_key_can_contribute():
    owner, listed = {"name": "O", "github": "o", "key": "k1", "role": "owner"}, {"name": "U", "github": "u", "key": "k3"}
    waiting = {"name": "W", "github": "w", "key": ""}
    assert ledger.can_contribute(owner) and ledger.can_contribute(listed)
    assert not ledger.can_contribute(waiting) and not ledger.can_contribute(None)
    assert (ledger.status(owner), ledger.status(listed), ledger.status(waiting)) == ("owner", "can contribute", "listed, waiting for signing key")


def test_every_skill_loads_and_has_a_version():
    for name in ("triage/gates", "triage/confirm", "rank/compare", "probe/write-mutants", "audit/rubric", "session/interpret", "session/answer"):
        s = skills.load(name)
        assert len(s.version) == 10 and s.tag == f"{name}@{s.version}"
    questions, criteria, tag = gates.wording()
    assert set(questions) == set(gates.D_GATES) and tag.startswith("triage/gates@")
    for gate, (choices, passing) in gates.D_GATES.items():
        assert set(criteria[gate]) == set(choices) and passing <= set(choices)      # every choice has a meaning
    assert "$question" not in skills.load("session/answer").fill(question="q", records="r")
