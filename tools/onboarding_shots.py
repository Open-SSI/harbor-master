"""Take a picture of every box of hm init, of hm init run again, and of the dashboard of demo-index.

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

        await walk(WizardApp("hm init", [*flows.init_steps(None), flows.start_step()]), [*WHO, None, None], out, "init", font_dir)
        me = identity.create(*WHO)
        # hm init again: every box shows what this machine has, to keep with enter or to change.
        await walk(WizardApp("hm init", flows.init_steps(me)), [None, None, None, None, None], out, "rerun", font_dir)

        # The index's dashboard, on demo-index: the made-up people's curation, and the person's own demo benchmark,
        # curated in a simulated run with its reports signed.
        from harbor_master import interact
        from harbor_master.cli.home import demo_bench, demo_world
        from harbor_master.cli.setup import make_demo
        from harbor_master.core import config as config_mod
        from harbor_master.engine import Session
        from harbor_master.tasks import read_registry
        from harbor_master.tui import IndexApp
        with flows.console.capture():
            ws = make_demo(Path("demo-index").resolve())
            await asyncio.to_thread(demo_world, ws)
            demo_bench(ws)
        cfg = config_mod.load(ws)
        cfg.select.require_brief = cfg.select.require_signoff = False
        s = Session(ws, cfg, me, {"demo-bench": read_registry(ws)["demo-bench"]}, simulate=True, speed=0, no_input=True)
        s.is_reviewer = True
        await s.run()
        for task in list(s.ready):
            interact.decide(s, task, "accept clean and hard")
        board = IndexApp(ws, demo=True)
        async with board.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.5)
            for name, button in (("index-01-dashboard", None), ("index-02-status", "#view-status"), ("index-03-benchmarks", "#view-benchmarks")):
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
