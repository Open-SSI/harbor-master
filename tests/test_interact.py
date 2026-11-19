from harbor_master import interact, tasks
from harbor_master.interact import Command


def test_plain_text_maps_to_commands_questions_and_refusals(session):
    session.benches["demo-bench"].tasks = tasks.load_benchmark(
        session.benches["demo-bench"].path, "demo-bench")
    read = lambda text: interact.interpret(session, text)  # noqa: E731
    i = read("hold demo-bench for now and stop t0003")
    assert [str(c) for c in i.commands] == ["/pause demo-bench", "/stop demo-bench/t0003"]
    assert interact.needs_confirmation(i)
    assert read("how long until t0003 finishes?").kind == "question"
    assert read("change the pass threshold to 50%").kind == "unsupported"
    assert read("delete that record from the ledger").kind == "unsupported"
    assert read("purple monkey dishwasher").kind == "unclear"
    typed = read("/pause")
    assert typed.typed and not interact.needs_confirmation(typed)      # explicit control command
    assert interact.needs_confirmation(read("/budget 300"))            # spending is always confirmed
    assert not interact.needs_confirmation(read("show me the status")) # viewing runs at once
    assert read("/nonsense").kind == "unsupported"


def test_commands_change_only_what_they_say(session):
    assert "Paused" in interact.execute(session, Command("pause"))
    assert session.paused
    interact.execute(session, Command("undo"))
    assert not session.paused
    assert "Give an amount" in interact.execute(session, Command("budget", ["lots"]))
    assert session.budget == 200
    interact.execute(session, Command("budget", ["350"]))
    assert session.budget == 350
    assert "not built yet" in interact.execute(session, Command("retry", ["t0001"]))


async def test_a_simulated_run_answers_different_questions_differently(workspace, ident):
    """There is no model in a simulated run, so the records are picked by the words of the question."""
    import asyncio
    from harbor_master.core import config as config_mod
    from harbor_master.engine import Session
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
    assert len({nxt, going, brief, cost}) == 4
    assert "Waiting on you" in nxt and "/brief demo-bench" in nxt
    assert "Waiting for the lead's brief" in going and "passed the admission gates" in going
    assert brief.startswith("A brief is")
    assert "Curation:" in cost
    assert "t0003" in ask("what is t0003 about") and "The task:" in ask("what is t0003 about")
    assert "cannot answer a free question" in ask("tell me a joke") and "Waiting on you" in ask("tell me a joke")
    assert "Waiting on you" in interact.context_for(s, "anything")          # a model gets every record, and picks
    assert interact.execute(s, interact.Command("status")).startswith("demo-bench: ")
    s.answer_request(next(r for r in s.requests if r.kind == "brief"), "Tasks that need real computation.")
    await run
    after = ask("what now?")
    assert after.startswith("The run is finished. Waiting on you:") and "/review" in after      # reports to sign


def test_completion_puts_what_is_waiting_first(session):
    from harbor_master.engine.state import Request
    session.benches["other-bench"] = type(session.benches["demo-bench"])("other-bench", "physics", session.benches["demo-bench"].path)
    session.benches["demo-bench"].tasks = tasks.load_benchmark(session.benches["demo-bench"].path, "demo-bench")
    assert interact.complete(session, "/ap") == ["/approve "]
    assert interact.complete(session, "/approve ") == ["/approve demo-bench", "/approve other-bench"]
    session.benches["other-bench"].phase = "signoff"
    session.requests.append(Request(1, "selection", "approve?", "other-bench"))
    assert interact.complete(session, "/approve ")[0] == "/approve other-bench"       # the one waiting for approval
    assert interact.complete(session, "/")[:2] == ["/approve ", "/selection "]         # the commands that answer it
    assert interact.complete(session, "/accept ") == []                                # no report is ready
    session.ready.append(session.find_task("t0005"))
    assert interact.complete(session, "/accept ") == ["/accept t0005 "]
    assert interact.complete(session, "/skip 0007") == ["/skip t0007"]                 # matched inside the name
    assert interact.complete(session, "/brief demo-bench what") == []                  # free text is not completed
    assert interact.complete(session, "/logs") == [] and interact.complete(session, "pause") == []
