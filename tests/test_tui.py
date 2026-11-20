from rich.console import Console
from textual.widgets import Static

from harbor_master.tui import SessionApp
from harbor_master import interact, tasks
from harbor_master.core import config as config_mod
from harbor_master.engine import Session


def text_of(app, selector: str) -> str:
    console = Console(width=120, record=True, file=open("/dev/null", "w"))
    console.print(app.query_one(selector, Static).content)
    return console.export_text()


async def box(pilot, kind):
    for _ in range(100):
        await pilot.pause(0.05)
        if isinstance(pilot.app.screen, kind):
            return pilot.app.screen
    raise AssertionError(f"no {kind.__name__} opened")


async def test_the_curate_screen_has_nothing_to_type_and_every_decision_is_a_box(workspace, ident, monkeypatch):
    import asyncio

    from textual.widgets import Button, Input

    from harbor_master.tui.parts import ChoiceBox
    session = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, as_real=True, speed=5)
    app = SessionApp(session)
    labels = lambda b: [x.label.plain.split(":")[0] for x in b.query(Button)]      # noqa: E731
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause(0.3)
        assert "demo-bench" in text_of(app, "#benchmarks") and not app.query(Input) and "commands" not in text_of(app, "#keys")   # no prompt, no commands
        await pilot.press("ctrl+p")                                 # a few keys do the rest
        assert session.paused
        await pilot.press("ctrl+p")
        assert not session.paused

        monkeypatch.setattr(Session, "spent", property(lambda self: 185.0))     # the budget is nearly gone: asked before it is
        session._budget_reached()
        asked = await box(pilot, ChoiceBox)
        assert "$185.00 of the $200 budget is spent" in text_of(asked, "#text") and labels(asked) == ["Raise it to $400, and go on", "Keep it at $200"]
        await pilot.press("escape")                                 # it has to be answered
        assert app.screen is asked
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert session.budget == 400 and not any(r.kind == "budget" for r in session.requests) and not session.paused
        session._budget_reached()                                   # asked once at each budget: under nine tenths now
        assert not session.requests
        monkeypatch.setattr(Session, "spent", property(lambda self: 12.0))

        trial = asyncio.create_task(session.failed("t0003", "difficulty trial 2 of claude-code with claude-fable-5-1", "the container did not start"))
        asked = await box(pilot, ChoiceBox)                         # a trial failed to run: what ran, why, and two things to do
        told = " ".join(text_of(asked, "#text").split())
        assert "t0003" in told and "difficulty trial 2 of claude-code" in told and "the container did not start" in told
        assert labels(asked) == ["Retry", "Discard"]
        await pilot.press("down", "enter")
        assert await trial is False and not session.requests
        trial = asyncio.create_task(session.failed("t0003", "difficulty trial 2", "again"))
        await box(pilot, ChoiceBox)
        await pilot.press("enter")
        assert await trial is True

        await pilot.press("ctrl+q")                                 # ending asks first, in a box too
        asked = await box(pilot, ChoiceBox)
        assert labels(asked) == ["Keep it going", "End the run"]
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, ChoiceBox) and not session.quitting
        await pilot.press("ctrl+q")
        await box(pilot, ChoiceBox)
        await pilot.press("down", "enter")
        await pilot.pause(0.5)
    assert session.quitting


async def test_a_short_terminal_keeps_the_cells_and_drops_the_rest(session):
    session.backend.speed = 5
    app = SessionApp(session)
    async with app.run_test(size=(92, 12)) as pilot:
        await pilot.pause(0.5)
        assert app.query_one("#cells").display and not app.query_one("#summary").display     # the cells are the last thing to go
        session.kill()


async def test_whatever_the_run_asks_is_asked_in_a_box_that_has_to_be_answered(workspace, ident):
    from harbor_master.tui.widgets import AskScreen
    cfg = config_mod.load(workspace)
    cfg.select.require_brief = cfg.select.require_signoff = True
    session = Session(workspace, cfg, ident, tasks.read_registry(workspace), simulate=True, speed=0)
    session.benches["demo-bench"].entry = {"name": "Demo Bench", "description": "Made-up chemistry tasks.",
                                           "categories": {"spectra": "Reading a structure out of a spectrum."}}
    app = SessionApp(session)
    async with app.run_test(size=(140, 50)) as pilot:
        for _ in range(100):
            await pilot.pause(0.05)
            if isinstance(app.screen, AskScreen):
                break
        await pilot.pause(0.2)
        box, asked = app.screen, session.requests[0].text.splitlines()
        # The benchmark, then its categories with how many tasks each has and what each is about, then the question.
        assert asked[:2] == ["Demo Bench", "Made-up chemistry tasks."] and asked[3] == "24 of its 24 tasks passed the gates, in 3 categories:"
        assert any(ln.split()[:2] == ["spectra", "11"] and ln.endswith("Reading a structure out of a spectrum.") for ln in asked[4:7])
        assert asked[-1].startswith("What counts as important in this benchmark?") and "Any contributor may give it" in asked[-1]
        assert isinstance(box, AskScreen) and "a brief is needed" in str(box.query_one("Vertical").border_title)
        assert "Demo Bench" in text_of(box, "#asked") and box.query_one("#reply").placeholder.startswith("what matters most")
        reply = box.query_one("#reply")

        async def answer(text):
            reply.value = text
            await pilot.press("enter")
            await pilot.pause(0.2)

        await pilot.press("escape")                             # no way round it, and nothing is typed in for the user
        assert app.screen is box and "The run waits" in text_of(box, "#said") and reply.value == ""
        await answer("ok")                                      # not a brief
        assert app.screen is box and session.ledger.brief("demo-bench") is None
        await answer("hard and realistic tasks")                # the answer itself: no command to remember
        for _ in range(100):
            await pilot.pause(0.05)
            if any(r.kind == "selection" for r in session.requests):
                break
        await pilot.pause(0.3)
        assert session.ledger.brief("demo-bench")["text"] == "hard and realistic tasks"

        # The ranking is put in front of the user: every task with a line on what it asks, and a choice of how many go on.
        from harbor_master.tui.widgets import SelectionScreen
        page = app.screen
        assert isinstance(page, SelectionScreen) and (page.most, page.k, len(page.rows)) == (20, 20, 24)
        listed = text_of(page, "#tasks")
        first = page.rows[0][0].split("/")[-1]
        assert listed.index(first) < listed.index(page.rows[1][0].split("/")[-1]) and "Task " in listed and listed.count("●") == 20
        line = page.query_one("#choice")

        async def say(text):
            line.value = text
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.2)

        await pilot.press("escape")                             # no way round it
        await say("21")                                         # more than may go on
        assert app.screen is page and "from 1 to 20" in text_of(page, "#said") and not any(e.get("gate") == "signoff" for e in session.ledger.events)
        await say("brief only the spectra tasks matter")        # a new brief ranks again, and the page comes back
        for _ in range(100):
            await pilot.pause(0.05)
            if isinstance(app.screen, SelectionScreen) and app.screen is not page:
                break
        page = app.screen
        assert isinstance(page, SelectionScreen) and session.ledger.brief("demo-bench")["text"] == "only the spectra tasks matter"
        line = page.query_one("#choice")
        line.value = "5"
        await pilot.pause(0.2)
        assert text_of(page, "#tasks").count("●") == 5          # the marks follow the number as it is typed
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.05)
            if session.benches["demo-bench"].phase == "ready":
                break
        signed = [e for e in session.ledger.events if e.get("gate") == "signoff"]
        assert len(signed) == 1 and signed[0]["selected"] == 5 and session.benches["demo-bench"].selected == 5
        assert [tid for tid, _, on in session.benches["demo-bench"].ranking if on] == [r[0] for r in page.rows[:5]]     # the top 5 as listed
        session.kill()


async def test_review_shows_each_report_in_turn_and_takes_the_verdict_under_it(workspace, ident):
    from harbor_master import ledger
    from harbor_master.tui.widgets import ReviewScreen
    session = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, as_real=True, speed=0)
    app = SessionApp(session)
    async with app.run_test(size=(140, 50)) as pilot:
        for _ in range(200):
            await pilot.pause(0.05)
            if session.finished:
                break
        await pilot.pause(0.3)
        first, second, third = session.ready[:3]
        page = app.screen                                       # it opened by itself: reports wait, and nothing else comes first
        assert isinstance(page, ReviewScreen) and first.task_id in str(page.query_one("Vertical").border_title)
        line = page.query_one("#verdict")
        area = page.query_one("VerticalScroll")
        page.query_one("#report").update("\n".join(f"line {i}" for i in range(200)))      # a report longer than the page
        await pilot.pause(0.1)
        await pilot.press("pagedown")
        await pilot.pause(0.1)
        assert area.max_scroll_y > 0 and area.scroll_y > 0          # it scrolls, while the prompt keeps the keyboard
        assert page.focused is line

        async def say(text):
            line.value = text
            await pilot.press("enter")
            await pilot.pause(0.1)

        await say("reject")                                     # no reason: nothing is signed, and the report stays
        assert "Give a reason" in text_of(page, "#said") and page.at == 0 and first in session.ready
        await say("accept clean and hard")
        assert page.at == 1 and first not in session.ready and second.task_id in str(page.query_one("Vertical").border_title)
        await say("")                                           # no skipping a report, and no way back to the run's prompt
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert page.at == 1 and second in session.ready and "needs your verdict" in text_of(page, "#said") and app.screen is page
        await say("sendback the tolerance looks arbitrary")     # there is no sending back: it is not taken for a verdict
        assert "Type accept" in text_of(page, "#said") and page.at == 1
        await say("reject the tolerance looks arbitrary")
        for _ in range(40):                                     # every report gets a verdict. Ones that became ready
            await pilot.pause(0.3)                              # meanwhile open the page again
            if not isinstance(app.screen, ReviewScreen):
                break
            app.screen.query_one("#verdict").value = "reject too narrow"
            await pilot.press("enter")
        assert not isinstance(app.screen, ReviewScreen) and not session.ready and "0 reports left to review" in text_of(app, "#answer")
        states = ledger.fold(session.ledger.events)
        assert states[first.task_id].status == "accepted" and states[second.task_id].status == "rejected"
        assert states[third.task_id].status == "rejected"
        session.kill()
