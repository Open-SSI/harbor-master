"""The index's dashboard, for its owner and leads, and the two ways in for everyone: hm curate and hm eval."""
import asyncio

from rich.console import Console
from textual.widgets import Button, Input, Static
from typer.testing import CliRunner

from conftest import call
from harbor_master import interact, ledger
from harbor_master import tasks as tasks_mod
from harbor_master.cli import ANYWHERE, IN_AN_INDEX
from harbor_master.cli import app as cli
from harbor_master.cli.home import demo_eval
from harbor_master.cli.setup import index_new
from harbor_master.core import config as config_mod
from harbor_master.engine import Session
from harbor_master.interact import guide
from harbor_master.tui import IndexApp, SessionApp
from harbor_master.tui.home import ViewScreen, gather


def text_of(app, selector: str) -> str:
    console = Console(width=140, record=True, file=open("/dev/null", "w"))
    console.print(app.query_one(selector, Static).content)
    return " ".join(console.export_text().split())


async def wait(pause, done, tries: int = 400) -> None:
    for _ in range(tries):
        await pause(0.02)
        if done():
            break


def hm(*args) -> str:
    out = CliRunner().invoke(cli, [str(a) for a in args])
    return f"{out.exit_code} {' '.join(out.output.split())}"


async def test_the_dashboard_shows_where_the_index_stands_and_changes_nothing(tmp_path, monkeypatch, ident):
    monkeypatch.chdir(tmp_path)
    assert call(index_new, "my-index", None, "My Index", "", str(tmp_path / "store")).startswith("0")
    ws = tmp_path / "my-index"
    await asyncio.to_thread(demo_eval, ws)
    assert (await asyncio.to_thread(hm, "eval", "--demo", "--plain", "--take", 2, "-w", ws)).startswith("0 ")
    files = ("protocol.toml", "roster.toml", "contributors.toml", "about.toml")
    before = {name: (ws / name).read_text() for name in files}

    d = gather(ws)                                  # its figures, from the records. Only simulated runs: they are shown, and marked
    assert d["simulated"] and not d["released"] and not d["locked"] and d["accepted"] == 3 and list(d["benches"]) == ["demo-bench"]
    assert d["benches"]["demo-bench"]["accepted"] == 3 and sum(d["benches"]["demo-bench"].values()) == 10
    assert len(d["cells"]) == 9 and 0 < sum(len(c.done) for c in d["cells"]) < 27 and [c.name for c in d["roster"]][-1] == "demo-agent"

    app = IndexApp(ws)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause(0.3)
        head = text_of(app, "#header")
        assert "My Index" in head and "building" in head and "not locked" in head and "simulated runs only" in head and "owner" in head
        assert [text_of(app, f"#tile-{n}").split()[0] for n in ("benchmarks", "accepted", "review", "people")] == ["1", "3", "0", "1"]
        titles = {n: str(app.query_one(f"#{n}").border_title) for n in ("curation", "evaluation", "roster", "protocol", "people")}
        assert titles == {"curation": "stage 1   curation", "evaluation": "stage 2   evaluation", "roster": "roster", "protocol": "protocol",
                          "people": "contributors"}
        assert "demo-bench" in text_of(app, "#curation") and "demo-agent" in text_of(app, "#evaluation") and "of 9" in text_of(app, "#evaluation")
        assert "3 per task and configuration" in text_of(app, "#protocol") and ident.name in text_of(app, "#people")
        assert not app.query(Input)                                                 # read only: nothing to type, nothing to start
        assert [b.label.plain for b in app.query(Button)] == ["Leaderboard", "Tasks", "Status", "To do", "Credits", "Benchmarks", "Protocol", "Skills"]
        for button, label, shown in (("#view-leaderboard", "Leaderboard", "demo-agent"), ("#view-status", "Status", "Simulated"),
                                     ("#view-protocol", "Protocol", "protocol"), ("#view-skills", "Skills", "triage/gates.toml")):
            await pilot.click(button)                                               # a button opens the details, in a box to read
            await wait(pilot.pause, lambda: isinstance(app.screen, ViewScreen))
            assert app.last[0] == label and shown in app.last[1] and not app.screen.query(Input)
            await pilot.press("escape")
            await pilot.pause()
    assert before == {name: (ws / name).read_text() for name in files}


def test_the_dashboard_is_for_the_owner_and_leads_and_everyone_else_curates_or_evaluates(workspace, ident, monkeypatch):
    monkeypatch.chdir(workspace)                    # the test index lists its one person with no role
    out = hm("open")
    assert out.startswith("1 The dashboard is for the index's owner and its leads") and "hm curate" in out and "hm eval" in out
    people = ledger.load_contributors(workspace)
    people[0]["role"] = "lead"
    import tomli_w
    (workspace / ledger.CONTRIBUTORS).write_text(tomli_w.dumps({"contributor": people}))
    assert hm("open").startswith("1 The dashboard needs a terminal")                # a lead may: here there is no terminal to draw on
    hm("curate", "--demo", "--plain", "--no-input", "--speed", 0)                   # the demo of stage 1: one flag, simulated
    assert "demo-bench" in tasks_mod.read_registry(workspace) and not (workspace / "ledger").exists()
    assert ledger.Ledger(config_mod.simulated(workspace), simulated=True).events    # its records stay with the simulated ones


def test_help_lists_the_commands_for_any_folder_and_the_few_for_inside_an_index(workspace):
    shown = CliRunner().invoke(cli, ["--help"]).output
    listed = [line.split()[1] for line in shown.split("Commands")[1].splitlines() if line.startswith("│ ") and line[2] != " "]
    assert sorted(listed) == sorted((*ANYWHERE, *IN_AN_INDEX)) and {"curate", "eval"} <= set(listed)
    assert "benchmarks" in listed and CliRunner().invoke(cli, ["benchmarks", "-w", str(workspace)]).exit_code == 0
    assert CliRunner().invoke(cli, ["status", "-w", str(workspace)]).exit_code == 2                # no hidden commands are left
    alone = CliRunner().invoke(cli, [])
    assert alone.exit_code == 2 and "hm init" in alone.output and "hm curate" in alone.output and "hm eval" in alone.output


async def test_the_run_screen_says_what_it_needs_at_each_point(workspace, ident):
    cfg = config_mod.load(workspace)
    cfg.select.require_brief = cfg.select.require_signoff = True
    new = lambda run_id: Session(workspace, cfg, ident, tasks_mod.read_registry(workspace), simulate=True, speed=0, run_id=run_id)   # noqa: E731
    s = new("t1")
    app = SessionApp(s)
    async with app.run_test(size=(120, 50)) as pilot:
        await wait(pilot.pause, lambda: s.requests)
        await pilot.pause(0.2)
        assert guide.session_tip(s) == ""                   # the run asks in a box of its own: no tip beside it
        assert "There is no /leaderboard command" in interact.execute(s, interact.Command("leaderboard"))   # the index is on the dashboard

        waiting = new("t0")
        run = asyncio.create_task(waiting.run())                     # a run that waits for an answer can still be left
        await wait(asyncio.sleep, lambda: waiting.requests)
        assert "Quitting" in interact.execute(waiting, interact.Command("quit"))
        await asyncio.wait_for(run, 5)
        assert waiting.finished and waiting.benches["demo-bench"].phase == "skipped"
        interact.execute(s, interact.Command("brief", ["demo-bench", "Spectra", "matter."]))
        await wait(pilot.pause, lambda: any(r.kind == "selection" for r in s.requests))
        assert guide.session_tip(s) == ""                   # the selection is asked in its box too
        interact.execute(s, interact.Command("approve", ["demo-bench"]))
        await wait(pilot.pause, lambda: s.finished)
        assert "review page" in guide.session_tip(s)        # reports are ready: the tip moves on to the verdicts
        s.requests.clear()
        assert "finished" in guide.session_tip(s)
        s.finished, s.paused = False, True
        assert "ctrl+p" in guide.session_tip(s)
    await asyncio.sleep(0)
