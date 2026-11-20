import asyncio

from harbor_master import interact, tasks
from harbor_master.core import config as config_mod
from harbor_master.engine import Session
from harbor_master.engine.state import Request
from harbor_master.interact import Command


def test_commands_change_only_what_they_say(session):
    assert "Paused" in interact.execute(session, Command("pause")) and session.paused
    interact.execute(session, Command("undo"))
    assert not session.paused
    assert "Give an amount" in interact.execute(session, Command("budget", ["lots"])) and session.budget == 200
    interact.execute(session, Command("budget", ["350"]))
    assert session.budget == 350
    assert "not built yet" in interact.execute(session, Command("retry", ["t0001"]))


async def test_a_simulated_run_answers_different_questions_differently(workspace, ident):
    """There is no model in a simulated run, so the records are picked by the words of the question."""
    cfg = config_mod.load(workspace)
    cfg.select.require_brief = True
    s = Session(workspace, cfg, ident, tasks.read_registry(workspace), simulate=True, as_real=True, speed=0, run_id="q1")
    run = asyncio.create_task(s.run())
    for _ in range(500):
        if any(r.kind == "brief" for r in s.requests):
            break
        await asyncio.sleep(0.01)
    ask = lambda q: interact.context_for(s, q, focused=True)  # noqa: E731
    nxt, going, brief, cost = ask("what should i do next"), ask("how is it going"), ask("what is a brief?"), ask("how much has been spent")
    assert len({nxt, going, brief, cost}) == 4 and brief.startswith("A brief is") and "Curation:" in cost
    assert "Waiting on you" in nxt and "/brief demo-bench" in nxt
    assert "Waiting for a brief" in going and "passed the admission gates" in going
    assert "t0003" in ask("what is t0003 about") and "The task:" in ask("what is t0003 about")
    assert "cannot answer a free question" in ask("tell me a joke") and "Waiting on you" in ask("tell me a joke")
    assert "Waiting on you" in interact.context_for(s, "anything")          # a model gets every record, and picks
    assert interact.execute(s, Command("status")).startswith("demo-bench: ")
    s.answer_request(next(r for r in s.requests if r.kind == "brief"), "Tasks that need real computation.")
    await run
    after = ask("what now?")
    assert after.startswith("The run is finished. Waiting on you:") and "/review" in after      # reports to sign


