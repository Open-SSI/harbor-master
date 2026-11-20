"""The one walk-through: hm init asks who you are in boxes, one step at a time (DESIGN.md Section 11)."""
from pathlib import Path

from textual.widgets import Input, Static

from harbor_master.cli import walk
from harbor_master.tui.wizard import Step, WizardApp


async def answer(pilot, app, text=None):
    """Type into the box's line and press enter, or just press enter on what has the focus."""
    if text is not None:
        app.query_one("#line", Input).value = text
    await pilot.press("enter")
    await pilot.pause(0.15)


def said(app) -> str:
    return str(app.query_one("#said", Static).content)


async def test_a_walk_through_asks_one_thing_per_box():
    steps = [Step("name", "Your name", "As credited."),
             Step("site", "Your page", prefix="https://example.org/", optional=True),
             Step("ok", "Go ahead?", kind="note", done="Do it")]
    app = WizardApp("demo", steps)
    row = lambda: [b.label.plain for b in app.query("#row Button")]      # noqa: E731
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.2)
        assert "step 1 of 3" in str(app.query_one("#box").border_title) and isinstance(app.focused, Input)      # typed: a line
        assert row() == ["Cancel", "Next ↵"] and not app.query("#keys")     # two buttons at the right, and no small print under them
        await answer(pilot, app, "")
        assert "needed" in said(app) and app.at == 0                       # it does not go on without an answer
        await answer(pilot, app, "Ada")
        assert str(app.query_one("#prefix", Static).content) == "https://example.org/" and app.query_one("#line", Input).value == ""
        await pilot.press("escape")                                        # back a step, with what was typed still there
        await pilot.pause(0.15)
        assert app.at == 0 and app.query_one("#line", Input).value == "Ada"
        await answer(pilot, app)
        await answer(pilot, app, "ada")                                    # only what follows the prefix is typed
        assert row() == ["Cancel", "Do it ↵"] and app.focused.id == "go"   # only to read: enter goes on
        await pilot.click("#go")
        await pilot.pause(0.15)
    assert app.return_value == {"name": "Ada", "site": "https://example.org/ada", "ok": True}


async def test_hm_init_checks_what_is_typed_before_going_on(tmp_path, monkeypatch, ident):
    monkeypatch.chdir(tmp_path)
    app = WizardApp("hm init", walk.init_steps(None))
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.2)
        await answer(pilot, app, "Ada Lovelace")
        await answer(pilot, app, "Analytical Engines")
        await answer(pilot, app, "not an email")
        assert "email" in said(app) and app.step.key == "email" and "listed with your name" in str(app.query_one("#text", Static).content)
        await answer(pilot, app, "ada@example.org")
        assert str(app.query_one("#prefix", Static).content) == "https://github.com/"                         # there, and not to be typed
        await answer(pilot, app, "ada lovelace")
        assert "GitHub handle" in said(app)
        await answer(pilot, app, "ada")
        assert app.step.kind == "note" and "signing key" in str(app.query_one("#text", Static).content)
        await answer(pilot, app)
    assert app.return_value["github"] == "https://github.com/ada" and app.return_value["ok"] is True


async def test_hm_init_run_again_shows_what_this_machine_has_to_keep_or_change(ident):
    steps = walk.init_steps(ident)
    assert [s.default for s in steps[:4]] == [ident.name, ident.affiliation, ident.email, "https://github.com/" + ident.github]
    assert all("press enter to keep it" in s.text for s in steps[:4]) and "send each index's owner your new line" in steps[3].text
    assert (steps[-1].title, steps[-1].done) == ("Save your details?", "Save") and "signing key you already have is kept" in steps[-1].text
    app = WizardApp("hm init", steps)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.2)
        assert app.query_one("#line", Input).value == ident.name           # there in the line, to keep with enter
        await answer(pilot, app)
        await answer(pilot, app, "Another Lab")                            # or change
        await answer(pilot, app)
        assert app.query_one("#line", Input).value == ident.github         # only what follows the prefix
        await answer(pilot, app)
        await answer(pilot, app)
    got = app.return_value
    assert (got["name"], got["affiliation"], got["email"], got["github"]) == (ident.name, "Another Lab", ident.email, "https://github.com/" + ident.github)


def test_the_line_a_person_sends_carries_who_they_are_and_the_index_lists_it(tmp_path, monkeypatch, ident):
    import tomllib

    from typer.testing import CliRunner

    from harbor_master import ledger
    from harbor_master.cli import app
    monkeypatch.chdir(tmp_path)
    hm = lambda *a: CliRunner().invoke(app, [str(x) for x in a])      # noqa: E731
    import subprocess
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(tmp_path / "mine.git")], check=True)
    assert hm("new", "mine", "--tasks", tmp_path / "store", "--repo", tmp_path / "mine.git").exit_code == 0
    ws = tmp_path / "mine"
    listed = lambda: {c["github"]: c for c in tomllib.loads((ws / ledger.CONTRIBUTORS).read_text())["contributor"]}     # noqa: E731
    assert listed()[ident.github]["email"] == ident.email                 # the owner's own, from hm init
    import shlex
    assert shlex.split(ident.line()) == ["hm", "contributors", "add", ident.github, ident.public_key, "--name", ident.name,
                                         "--affiliation", ident.affiliation, "--email", ident.email]

    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBobBobBobBobBobBobBobBobBobBobBobBobBobBobBobB"
    theirs = f"hm contributors add bob '{key}' --name 'Bob Builder' --affiliation 'Brick Lab' --email bob@example.org"
    monkeypatch.chdir(ws)
    out = hm(*shlex.split(theirs)[1:])                                    # pasted in a shell as it is, inside the index's folder
    assert out.exit_code == 0, out.output
    bob = listed()["bob"]
    assert (bob["name"], bob["affiliation"], bob["email"]) == ("Bob Builder", "Brick Lab", "bob@example.org")
    assert bob["key"] == key and ledger.verify(ws) == []
    assert hm("--version").output.strip().startswith("hm ") and "hm init" in " ".join(hm("-h").output.split())

    assert "cd out of it first" in hm("new", "theirs", "--tasks", tmp_path / "store2", "--repo", "a/b").output
    monkeypatch.chdir(tmp_path)
    made = hm("new", "theirs", "--tasks", tmp_path / "store2", "--repo", tmp_path / "nowhere.git")      # hm new is a plain command: no boxes
    assert made.exit_code == 0 and (tmp_path / "theirs" / "about.toml").is_file() and "cd theirs" in made.output and "hm lock" in made.output
    assert hm("new", "x").exit_code == 2                                  # what it needs is on its line


async def test_after_who_you_are_the_onboarding_goes_on_to_an_index_the_keys_and_a_demo(tmp_path, monkeypatch, ident):
    from textual.widgets import Button

    from harbor_master.core import config as config_mod
    monkeypatch.chdir(tmp_path)
    steps = walk.start_steps(None)
    assert [s.key for s in steps] == ["index", "keys", "demo"] and (tmp_path / "config" / "secrets.env").is_file()     # three boxes, one choice
    app = WizardApp("getting started", steps)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.2)
        assert "practise" in str(app.query_one("#text", Static).content)
        await answer(pilot, app, "")                                       # no index named: a practice one
        assert "secrets.env" in str(app.query_one("#text", Static).content) and "never typed" in str(app.query_one("#text", Static).content)
        await answer(pilot, app)
        assert [b.label.plain.split(":")[0] for b in app.query("#answer Button")] == ["Curate", "Evaluate"]               # the two stages
        assert isinstance(app.focused, Button) and app.focused.name == "curate"
        await pilot.press("down", "enter")
        await pilot.pause(0.15)
    assert app.return_value == {"index": "", "keys": True, "demo": "eval"}

    ws = walk.get_index("")                                                # the practice index, on this machine only
    assert ws == tmp_path / walk.PRACTICE and config_mod.is_index(ws) == "" and walk.get_index("") == ws
    assert (config_mod.load(ws).run.cells, config_mod.load(ws).run.max_containers) == (4, 16)      # nobody is asked: 4 tasks, 16 containers
    assert [s.key for s in walk.start_steps(ws)][0] == "keys"              # inside an index already: it is not asked for


async def test_the_eval_demo_makes_tasks_to_evaluate_and_the_eval_screen_runs_them(tmp_path, monkeypatch, ident):
    from typer.testing import CliRunner

    from harbor_master import tasks as tasks_mod
    from harbor_master.cli import app as cli
    from harbor_master.core import config as config_mod
    from harbor_master.engine import Evaluation, Session
    from harbor_master import ledger
    from harbor_master.ledger import evals
    from harbor_master.tui.evals import EvalApp
    monkeypatch.chdir(tmp_path)
    ws = walk.get_index("")
    import asyncio
    from harbor_master.cli.home import demo_eval
    await asyncio.to_thread(demo_eval, ws)                                 # what hm eval --demo starts from
    root = config_mod.simulated(ws)
    index = ledger.index_entries(ledger.Ledger(root, simulated=True).events)
    assert index and not (ws / "ledger" / "demo-bench").exists() and not (ws / "eval").exists()       # nothing of it is in the index

    cfg = config_mod.load(ws)
    s = Session(ws, cfg, ident, tasks_mod.read_registry(ws), simulate=True, speed=0, no_input=True)
    by_id = {t.task_id: t for t in tasks_mod.load_benchmark(Path(tasks_mod.read_registry(ws)["demo-bench"]["path"]), "demo-bench")}
    run = Evaluation(s, index[:2], config_mod.demo_roster(cfg), lambda entry: by_id[entry["task"]])
    screen = EvalApp(run, "Practice index")
    def text(sel: str) -> str:
        from rich.console import Console
        console = Console(width=120, record=True, file=open("/dev/null", "w"))
        console.print(screen.query_one(sel, Static).content)
        return " ".join(console.export_text().split())

    async with screen.run_test(size=(130, 44)) as pilot:
        for _ in range(200):
            await pilot.pause(0.05)
            if run.finished:
                break
        await pilot.pause(0.5)
        assert run.finished and "evaluation has ended" in text("#tip")
        assert [screen.query_one(f"#{name}").border_title for name in ("summary", "configs", "cells", "recent")] == \
            ["summary", "configurations", "working cells", "recent"]                                # laid out as the curate screen is
        assert not screen.query("Input") and "ctrl+p" in text("#keys") and "ctrl+q" in text("#keys")   # nothing to type: three keys
    cells = evals.cells(index[:2], config_mod.demo_roster(cfg), cfg.eval, evals.agent_trials(ledger.Ledger(root, simulated=True).events, evals.read(root, True)),
                        evals.read(root, True), 0)
    assert len(cells) == 6 and not [c for c in cells if c.open]
    assert sum(e["type"] == "trial" for e in evals.read(root, True)) < 18                           # the suite's difficulty trials were reused
