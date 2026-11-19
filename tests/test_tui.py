from textual.widgets import Static

from harbor_master.tui import Prompt, SessionApp


def text_of(app, selector: str) -> str:
    from rich.console import Console
    console = Console(width=120, record=True, file=open("/dev/null", "w"))
    console.print(app.query_one(selector, Static).content)
    return console.export_text()


async def type_line(pilot, text: str) -> None:
    prompt = pilot.app.query_one(Prompt)
    prompt.value = text
    await pilot.press("enter")
    await pilot.pause()


async def test_the_session_screen_runs_and_takes_input(session):
    session.backend.speed = 0.3           # slow enough that the run is still going while we type
    app = SessionApp(session)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause(0.3)
        assert "demo-bench" in text_of(app, "#benchmarks")

        await type_line(pilot, "please pause everything")          # plain text: must ask first
        assert app.pending is not None and not session.paused
        assert "/pause" in text_of(app, "#confirm")
        await pilot.press("n")
        await pilot.pause()
        assert app.pending is None and not session.paused           # declined: nothing changed

        await type_line(pilot, "please pause everything")
        await pilot.press("y")
        await pilot.pause()
        assert session.paused

        await type_line(pilot, "/resume")                           # typed control command: no confirmation
        assert not session.paused and app.pending is None

        await type_line(pilot, "change the pass threshold to 60%")  # refused, with a reason
        assert "protocol" in text_of(app, "#answer")

        await type_line(pilot, "what is the status?")               # a question gets an answer panel
        await pilot.pause(0.6)
        assert app.query_one("#answer").display
        assert "Spent" in text_of(app, "#answer")

        await type_line(pilot, "/quit")                             # /quit is a control command
        await pilot.pause(0.3)
    assert session.quitting


async def test_a_short_terminal_keeps_the_cells_and_drops_the_rest(session):
    session.backend.speed = 0.3
    app = SessionApp(session)
    async with app.run_test(size=(92, 24)) as pilot:
        await pilot.pause(0.5)
        await type_line(pilot, "what is the status?")
        await pilot.pause(0.6)
        assert app.query_one("#answer").display
        assert app.query_one("#cells").display              # the cells are the last thing to go
        assert not app.query_one("#summary").display        # less needed panels made room
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert app.query_one("#summary").display            # and come back when there is room
        session.kill()


async def test_tab_completes_a_command_and_then_its_argument(session):
    session.backend.speed = 0.3
    app = SessionApp(session)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause(0.3)
        prompt = app.query_one(Prompt)
        await pilot.press("/", "b", "r")
        await pilot.pause()
        assert "brief" in text_of(app, "#keys") and "tab" in text_of(app, "#keys")      # the hint line
        assert prompt._suggestion == "/brief"                                           # shown dimmed after the cursor
        await pilot.press("tab")
        assert prompt.value == "/brief "
        await pilot.press("tab")
        assert prompt.value == "/brief demo-bench "                                     # ready for the text
        await pilot.pause()
        assert "/brief benchmark text" in text_of(app, "#keys")

        prompt.value = "/s"
        await pilot.press("tab")
        first = prompt.value
        await pilot.press("tab")
        assert prompt.value != first and prompt.value.startswith("/s")                  # tab again: the next one
        await pilot.press("shift+tab")
        assert prompt.value == first

        prompt.value = "/explain t00"
        await pilot.press("tab")
        assert prompt.value.startswith("/explain t00") and len(prompt.value) == len("/explain t0000")
        prompt.value = "pause t00"                                                      # plain text is left alone
        await pilot.press("tab")
        assert prompt.value == "pause t00"
        prompt.value = ""
        await pilot.pause()
        assert "open cell" in text_of(app, "#keys")                                     # the usual keys are back
        await type_line(pilot, "/quit")
        await pilot.pause(0.3)
