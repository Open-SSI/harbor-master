"""Take a picture of every box of hm init, of hm init run again, of the eval screen, and of an index's dashboard.

    uv run --with pillow --with fonttools python tools/onboarding_shots.py OUT_DIR [--font-dir DIR]

It runs the real boxes headless, answers each as a person would, and draws each one the way
tools/screenshot.py draws the README's pictures. The person in them is made up.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from screenshot import draw, grab  # noqa: E402

COLS, ROWS = 96, 32
WHO = ("Ada Lovelace", "Analytical University", "ada@example.org", "ada")


async def walk(app, answers: list, out: Path, prefix: str, font_dir: Path | None, start: int = 1) -> int:
    """Draw each box before it is answered. An answer is text to type, a number of `down` presses, or None for enter."""
    from textual.widgets import Input
    n = start
    async with app.run_test(size=(COLS, ROWS)) as pilot:
        await pilot.pause(0.3)
        for typed in answers:
            if isinstance(typed, str):                  # shown with the answer typed in, as the person sees it before enter
                line = app.query_one("#line")
                if isinstance(line, Input):
                    line.value, line.cursor_position = typed, len(typed)
                else:
                    line.text = typed
                    line.move_cursor(line.document.end)
            elif isinstance(typed, int):
                for _ in range(typed):
                    await pilot.press("down")
            await pilot.pause(0.15)
            name = f"{prefix}-{n:02d}-{app.step.key}.png"
            draw(grab(app), COLS, out / name, font_dir, size=26)
            print(name, "|", app.step.title)
            n += 1
            await pilot.press("enter")
            await pilot.pause(0.25)
    return n


async def main(out: Path, font_dir: Path | None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hm-") as tmp:
        os.environ["HM_CONFIG_DIR"] = str(Path(tmp) / "config")
        (Path(tmp) / "indexes").mkdir()
        os.chdir(Path(tmp) / "indexes")
        from harbor_master.cli import walk as flows
        from harbor_master.core import identity
        from harbor_master.tui.wizard import WizardApp

        n = await walk(WizardApp("hm init", flows.init_steps(None)), [*WHO, None], out, "init", font_dir)
        me = identity.create(*WHO)
        await walk(WizardApp("getting started", flows.start_steps(None)), ["", None, 1], out, "init", font_dir, n)
        # hm init again: every box shows what this machine has, to keep with enter or to change.
        await walk(WizardApp("hm init", flows.init_steps(me)), [None, None, None, None, None], out, "rerun", font_dir)

        # The eval screen, on the demo: the made-up tasks, accepted in a simulated run, and their simulated evaluation.
        from harbor_master import ledger, tasks
        from harbor_master.core import config as config_mod
        from harbor_master.engine import Evaluation, Session
        from harbor_master.tui.evals import EvalApp
        ws = flows.get_index("")
        from harbor_master.cli.home import demo_eval
        await asyncio.to_thread(demo_eval, ws)
        cfg = config_mod.load(ws)
        index = ledger.index_entries(ledger.Ledger(config_mod.simulated(ws), simulated=True).events)
        by_id = {t.task_id: t for t in tasks.load_benchmark(Path(tasks.read_registry(ws)["demo-bench"]["path"]), "demo-bench")}
        run = Evaluation(Session(ws, cfg, me, tasks.read_registry(ws), simulate=True, speed=0.06, no_input=True), index,
                         config_mod.demo_roster(cfg), lambda entry: by_id[entry["task"]])
        screen = EvalApp(run, "Practice index")
        async with screen.run_test(size=(132, 40)) as pilot:
            await pilot.pause(2.6)
            draw(grab(screen), 132, out / "eval-01-screen.png", font_dir, size=22)
            print("eval-01-screen.png")
            run.quitting = True
            await pilot.pause(1.5)

        # The index's dashboard, with simulated progress in both stages: two made-up benchmarks curated, some reports
        # still to review, and part of the evaluation done by three people.
        import tomli_w

        from harbor_master import interact
        from harbor_master.cli.dev import fake_benchmark
        from harbor_master.ledger import evals
        from harbor_master.tui import IndexApp
        ws = Path("science-index").resolve()
        from harbor_master.cli.setup import index_new
        with flows.console.capture():
            index_new("science-index", None, "Open Science Index", "", str(Path(tmp) / "store"))
        people = [("Grace Hopper", "grace", "lead"), ("Alan Turing", "alan", "evaluator")]
        who = {"ada": me}
        for name, handle, _ in people:
            os.environ["HM_CONFIG_DIR"] = str(Path(tmp) / f"config-{handle}")
            who[handle] = identity.create(name, "Analytical University", f"{handle}@example.org", handle)
        os.environ["HM_CONFIG_DIR"] = str(Path(tmp) / "config")
        listed = ledger.load_contributors(ws) + [{"name": n, "github": h, "key": who[h].public_key, "role": r} for n, h, r in people]
        (ws / ledger.CONTRIBUTORS).write_text(tomli_w.dumps({"contributor": listed}))
        for bench, n in (("chem-bench", 40), ("earth-bench", 32)):
            fake_benchmark(bench, n, directory=config_mod.local(ws) / "fake", seed=3)
            tasks.register(ws, bench, config_mod.local(ws) / "fake" / bench, dev=True)
        cfg = config_mod.load(ws)
        cfg.select.require_brief = cfg.select.require_signoff = False
        s1 = Session(ws, cfg, me, tasks.read_registry(ws), simulate=True, speed=0, no_input=True)
        s1.is_reviewer = True
        await s1.run()
        for n, task in enumerate(list(s1.ready)):
            if n % 4 != 3:                              # some reports still wait for their reviewer
                interact.decide(s1, task, "accept clean and hard" if n % 5 else "reject the tolerance looks arbitrary")
        root = config_mod.simulated(ws)
        index = ledger.index_entries(ledger.Ledger(root, simulated=True).events)
        by_id = {t.task_id: t for b in tasks.read_registry(ws).values() for t in tasks.load_benchmark(Path(b["path"]), Path(b["path"]).name)}
        (ws / "roster.toml").write_text(tomli_w.dumps({"config": [
            *({"name": e.model, **e.model_dump(exclude={"env", "key", "use"}, exclude_none=True)} for e in cfg.difficulty.suite),
            {"name": "claude-opus-5.5", "agent": "claude-code", "model": "claude-opus-5-5", "version": "2.1.292", "reasoning_effort": "high"},
            {"name": "gpt-5.5-codex", "agent": "codex", "model": "gpt-5.5", "version": "0.162.1", "reasoning_effort": "high"},
            {"name": "gemini-3-pro", "agent": "gemini-cli", "model": "gemini-3-pro", "version": "0.31.0"}]}))
        roster = config_mod.roster(ws)
        for handle, k in (("ada", 4), ("grace", 3), ("alan", 4)):
            os.environ["HM_CONFIG_DIR"] = str(Path(tmp) / ("config" if handle == "ada" else f"config-{handle}"))
            part = Evaluation(Session(ws, cfg, who[handle], tasks.read_registry(ws), simulate=True, speed=0, no_input=True), index,
                              roster, lambda entry: by_id[entry["task"]], k)
            await part.run(rounds=1)
        os.environ["HM_CONFIG_DIR"] = str(Path(tmp) / "config-grace")      # a claim that is still held, by someone else
        done = evals.agent_trials(ledger.Ledger(root, simulated=True).events, evals.read(root, True))
        cells = [c for c in evals.cells(index, roster, cfg.eval, done, evals.read(root, True), 0) if c.open][:3]
        import time
        evals.append(root, who["grace"], "claim", True, cells=[c.key for c in cells], machine="other", expires=round(time.time() + 3600))
        os.environ["HM_CONFIG_DIR"] = str(Path(tmp) / "config")
        board = IndexApp(ws)
        async with board.run_test(size=(140, 44)) as pilot:
            await pilot.pause(0.5)
            for name, button in (("index-01-dashboard", None), ("index-02-leaderboard", "#view-leaderboard"), ("index-03-status", "#view-status")):
                if button:
                    await pilot.click(button)
                    await pilot.pause(0.8)
                draw(grab(board), 140, out / f"{name}.png", font_dir, size=22)
                print(f"{name}.png")
                if button:
                    await pilot.press("escape")
                    await pilot.pause(0.3)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--font-dir", type=Path, default=None)
    args = ap.parse_args()
    asyncio.run(main(args.out.resolve(), args.font_dir))
