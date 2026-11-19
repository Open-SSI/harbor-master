"""The home screen and the guide: a new person is told what to do at every step, from nothing to a run."""
import asyncio
import subprocess

from textual.widgets import Static

from harbor_master import interact
from harbor_master import tasks as tasks_mod
from harbor_master.cli.home import run_cli
from harbor_master.core import config as config_mod
from harbor_master.engine import Session
from harbor_master.interact import guide
from harbor_master.tui import HomeApp, SessionApp
from harbor_master.tui.home import HomePrompt


def text_of(app, selector: str) -> str:
    from rich.console import Console
    console = Console(width=120, record=True, file=open("/dev/null", "w"))
    console.print(app.query_one(selector, Static).content)
    return console.export_text()


async def enter(pilot, text: str) -> None:
    pilot.app.query_one(HomePrompt).value = text
    await pilot.press("enter")
    for _ in range(400):                    # a command runs beside the screen: wait until it is back
        await pilot.pause(0.02)
        if not pilot.app.busy:
            break
    await pilot.pause()


def commands(app) -> list[str]:
    return [c for c, _ in guide.tip(app.state).lines if c]


async def test_the_home_screen_walks_a_new_person_from_nothing_to_a_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HM_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "indexes.toml").write_text("")
    monkeypatch.setenv("HM_INDEXES", str(tmp_path / "indexes.toml"))
    folder = tmp_path / "work"
    folder.mkdir()
    monkeypatch.chdir(folder)
    app = HomeApp(run_cli, folder)
    async with app.run_test(size=(120, 50)) as pilot:
        assert app.last[0] == "welcome" and guide.next_step(app.state) == "Who you are"       # it opens with nothing set up
        assert "Start here" in app.query_one("#tip").border_title and "/init" in text_of(app, "#tip")
        assert "→" in text_of(app, "#steps").splitlines()[0]
        await enter(pilot, "/new mine")
        assert app.last[0] == "first, who you are" and app.ws is None                          # in order, and it says why
        await enter(pilot, "what now")
        assert app.last[0] == "not a command"

        await enter(pilot, "/init")                                                            # four questions, in the tip panel
        assert "question 1 of 4" in app.query_one("#tip").border_title
        for answer in ("Ada Lovelace", "Analytical University", "ada@example.org", "ada"):
            await enter(pilot, answer)
        assert app.state.ident.label() == "Ada Lovelace (Analytical University, @ada)" and app.form is None
        assert guide.next_step(app.state) == "An index" and commands(app) == ["/indexes", "/join NAME", "/new", "/demo"]

        await enter(pilot, "/indexes")
        assert "No index is listed yet" in app.last[1]
        await enter(pilot, "/status")
        assert app.last[0] == "first, an index"

        prompt = app.query_one(HomePrompt)                                                     # tab completes, the tip's commands first
        prompt.value = "/"
        await pilot.press("tab")
        assert prompt.value == "/indexes"
        prompt.value = "/ne"
        await pilot.press("tab")
        assert prompt.value == "/new "

        await enter(pilot, "/new my-index")                                                    # a short form: only the name is needed
        assert "question 2 of 5" in app.query_one("#tip").border_title and "skips" in text_of(app, "#tip")
        for answer in ("My Index", "Measures made-up things.", "", ""):
            await enter(pilot, answer)
        assert app.ws == folder / "my-index" and app.state.is_owner and not app.last[2]
        about = config_mod.load(app.ws).about
        assert (about.title, about.description, about.tasks) == ("My Index", "Measures made-up things.", "")
        assert guide.next_step(app.state) == "A benchmark" and commands(app) == ["/benchmarks add URL", "/demo"]
        await enter(pilot, "/help")                                                            # by step, not one flat list
        assert app.last[1].index("Start") < app.last[1].index("Set up an index") < app.last[1].index("Results")
        await enter(pilot, "/demo")
        assert list(app.state.registered) == ["demo-bench"] and guide.next_step(app.state) == "A run"
        assert commands(app) == ["/run --simulate"]                                            # no lock and no keys stand in the way

        await enter(pilot, "/lock")                                                            # signing asks first
        assert app.confirming is not None and "confirm" in app.query_one("#tip").border_title
        await pilot.press("n")
        await pilot.pause()
        assert app.confirming is None and app.state.lock == "none"

        await enter(pilot, "/leave")
        assert app.last[0] == "no such command"                                                # on the run screen it means something else
        await enter(pilot, "/close")
        assert app.ws is None and app.state.nearby == ["my-index"] and commands(app)[0] == "/use FOLDER"
        await enter(pilot, "/use my-index")
        assert app.ws == folder / "my-index" and "owner" in app.last[1]

        repo = tmp_path / "toy-bench"                                                          # a benchmark is added by its address
        (repo / "tasks" / "one").mkdir(parents=True)
        (repo / "tasks" / "one" / "task.toml").write_text('version = "1.0"\n')
        (repo / "LICENSE").write_text("MIT License\n")
        (repo / "README.md").write_text("# Toy Bench\n\nTasks that add small numbers.\n\nPaper: https://arxiv.org/abs/2601.00001.\n")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=a", "-c", "user.email=a@b.c", "commit", "-q", "-m", "one"],
                     ["tag", "v1.2.0"]):
            subprocess.run(["git", *args], cwd=repo, check=True)
        from harbor_master.backends import describe
        notes: list[str] = []

        async def agent(cfg, entry, source, note=""):                                          # stands in for the small model
            notes.append(note)
            return ({"name": "Toy Bench", "fields": ["Mathematics"]} if not note else {"description": "Sums of two small integers."}), 0.01

        monkeypatch.setattr(describe, "fill", agent)
        await enter(pilot, f"/benchmarks add {repo}")                                          # an address is all a person gives
        assert not app.last[2] and app.drafting() and "waits for your answer" in app.query_one("#tip").border_title
        assert "Toy Bench" in app.last[1] and "mathematics" in app.last[1] and "arxiv.org/abs/2601.00001" in app.last[1]
        assert "Reply y to add it" in app.last[1] and tasks_mod.supported.supported(app.ws) == []     # nothing is added before a yes
        await enter(pilot, "say that the numbers are integers")                                # a reply in plain words goes to the agent
        assert notes[-1] == "say that the numbers are integers" and "Sums of two small integers." in app.last[1]
        await enter(pilot, "license: Apache-2.0")                                              # and a field with its value is taken as it stands
        assert len(notes) == 2 and "Apache-2.0" in app.last[1]
        await enter(pilot, "y")
        assert not app.last[2] and "Added toy-bench" in app.last[1] and not app.drafting()
        entry = tasks_mod.supported.find(app.ws, "toy-bench")
        assert (entry["ref"], entry["version"], entry["tasks"], entry["format"], entry["license"]) == ("v1.2.0", "1.2.0", "tasks", "harbor", "Apache-2.0")
        assert (entry["name"], entry["fields"], entry["paper"], entry["added_by"]) == ("Toy Bench", ["mathematics"], "https://arxiv.org/abs/2601.00001", "ada")
        assert len(entry["commit"]) == 40 and (config_mod.local(app.ws) / "sources" / "toy-bench" / "tasks" / "one").is_dir()
        assert "Sums of two small integers." in (app.ws / "benchmarks.toml").read_text()
        await enter(pilot, f"/benchmarks add {repo}")
        assert app.last[2] and "on this index's list already, as toy-bench, added by @ada" in app.last[1]

        await enter(pilot, "/run")                                                             # a real run by the owner: the lock is asked for here
        assert app.confirming is not None and app.then == [] and "start the run" in text_of(app, "#tip")
        await pilot.press("y")
        for _ in range(400):
            await pilot.pause(0.02)
            if app.return_value is not None:
                break
    assert app.return_value == ([], folder / "my-index") and guide.look(folder / "my-index").lock == "locked"


def test_a_shell_lists_only_the_run_and_the_results(workspace):
    from typer.testing import CliRunner
    from harbor_master.cli import SHELL, app
    shown = CliRunner().invoke(app, ["--help"]).output
    listed = [line.split()[1] for line in shown.split("Commands")[1].splitlines() if line.startswith("│ ") and line[2] != " "]
    assert sorted(listed) == sorted(SHELL)
    assert CliRunner().invoke(app, ["benchmarks", "-w", str(workspace)]).exit_code == 0           # the others still answer by name


def test_the_guide_names_the_next_step_in_every_state(workspace, ident, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "CODEX_AUTH_JSON_PATH"):
        monkeypatch.delenv(name, raising=False)
    s = guide.look(workspace)                       # the test index: one made-up benchmark, registered as it would be with --dev
    assert s.can_curate and not s.is_owner and s.missing_keys and not s.real
    assert guide.next_step(s) == "A run" and "/run --simulate" in [c for c, _ in guide.tip(s).lines]
    assert [status for _, status, _ in guide.steps(s)].count("next") == 1

    s.registered = {"real-bench": {"supported": "real-bench"}}                 # a benchmark from the index's list: real runs matter
    s.lock = "none"
    assert guide.next_step(s) == "The protocol" and guide.tip(s).title == "Next: wait for the owner's lock"
    s.is_owner = True
    assert "/lock" in [c for c, _ in guide.tip(s).lines]
    s.lock = "locked"
    assert guide.next_step(s) == "Keys" and "/keys" in [c for c, _ in guide.tip(s).lines]
    s.missing_keys = []
    assert guide.next_step(s) == "A run" and [c for c, _ in guide.tip(s).lines if c] == ["/run", "/run --simulate"]
    s.can_curate, s.is_owner, s.me = False, False, None
    assert guide.next_step(s) == "Your place in it" and "/key" in [c for c, _ in guide.tip(s).lines]
    s.ready = 3
    assert guide.next_step(s) == "Your verdicts" and "3 reports are waiting" in guide.tip(s).title
    assert "hm index new NAME" in guide.text(guide.State(ident=ident)) and guide.shell("/run --simulate") == "hm run --simulate"


async def test_the_run_screen_says_what_it_needs_at_each_point(workspace, ident):
    from harbor_master import tasks
    cfg = config_mod.load(workspace)
    cfg.select.require_brief = cfg.select.require_signoff = True
    s = Session(workspace, cfg, ident, tasks.read_registry(workspace), simulate=True, speed=0, run_id="t1")
    app = SessionApp(s)
    async with app.run_test(size=(120, 50)) as pilot:
        for _ in range(200):
            await pilot.pause(0.02)
            if s.requests:
                break
        await pilot.pause(0.2)
        assert "/brief demo-bench" in guide.session_tip(s) and "/brief demo-bench" in text_of(app, "#tip")
        assert ident.name in interact.execute(s, interact.Command("contributors"))      # what a shell shows is here too,
        assert "Simulated" in interact.execute(s, interact.Command("leaderboard"))      # and reads this run's own records

        waiting = Session(workspace, cfg, ident, tasks.read_registry(workspace), simulate=True, speed=0, run_id="t0")
        run = asyncio.create_task(waiting.run())                     # a run that waits for an answer can still be left
        for _ in range(200):
            await asyncio.sleep(0.02)
            if waiting.requests:
                break
        assert "Quitting" in interact.execute(waiting, interact.Command("quit"))
        await asyncio.wait_for(run, 5)
        assert waiting.finished and waiting.benches["demo-bench"].phase == "skipped"
        interact.execute(s, interact.Command("brief", ["demo-bench", "Spectra", "matter."]))
        for _ in range(200):
            await pilot.pause(0.02)
            if any(r.kind == "selection" for r in s.requests):
                break
        assert "/approve demo-bench" in guide.session_tip(s)
        interact.execute(s, interact.Command("approve", ["demo-bench"]))
        for _ in range(400):
            await pilot.pause(0.02)
            if s.finished:
                break
        assert "/review" in guide.session_tip(s)            # reports are ready: the tip moves on to the verdicts
        s.requests.clear()
        assert "finished" in guide.session_tip(s)
        s.finished, s.paused = False, True
        assert "/resume" in guide.session_tip(s)
    await asyncio.sleep(0)
