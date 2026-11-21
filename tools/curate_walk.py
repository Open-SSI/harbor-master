"""Walk the curate screen through the demo, headless, and take a picture at each step.

    uv run --with pillow --with fonttools python tools/curate_walk.py OUT_DIR

It clones demo-index into a throwaway folder, starts the screen with no benchmark named, and answers each box as a person
would: the benchmark, the brief, the selection, the work cells. Pictures: the first box, each box, the curating stage
with work cell 1 and then 2, and the box that comes when the benchmark is curated."""
import asyncio, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from screenshot import draw, grab

OUT = Path(sys.argv[1]).resolve(); OUT.mkdir(parents=True, exist_ok=True)
COLS, ROWS = int(os.environ.get("COLS", 132)), int(os.environ.get("ROWS", 40))


def prepare(tmp):
    os.environ["HM_CONFIG_DIR"] = f"{tmp}/config"
    os.chdir(tmp)
    from harbor_master.core import identity
    identity.create("Ada Lovelace", "Analytical University", "ada@example.org", "ada")
    from harbor_master.cli.home import invoke, demo_bench, demo_world
    from harbor_master.cli.common import console
    with console.capture():
        invoke(["clone", "demo-index"])
    ws = Path(tmp) / "demo-index"
    with console.capture():
        demo_world(ws)
        demo_bench(ws)
    return ws


async def main(ws):
    from harbor_master.core import config as config_mod
    from harbor_master.engine import Session
    from harbor_master.core import identity
    from harbor_master.tui import SessionApp
    cfg = config_mod.load(ws)
    s = Session(ws, cfg, identity.load(), {}, simulate=True, speed=float(os.environ.get("SPEED", 0.25)), command="hm curate --demo")
    app = SessionApp(s)
    s.is_reviewer = False           # the reports wait until the work cells have been seen
    n = [0]
    def shot(name):
        n[0] += 1
        draw(grab(app), COLS, OUT / f"{n[0]:02d}-{name}.png", None, size=20)
        print(n[0], name, s.benches and {b.id: b.phase for b in s.benches.values()})
    async with app.run_test(size=(COLS, ROWS)) as pilot:
        await pilot.pause(0.6)
        shot("pick")
        line = app.screen.query_one("#line"); line.value = "demo-bench"
        await pilot.press("enter")
        await pilot.pause(0.4)
        for i in range(400):
            await pilot.pause(0.25)
            scr = type(app.screen).__name__
            b = next(iter(s.benches.values()), None)
            if scr == "Screen" and b and b.phase in ("triage", "ranking", "adapt", "fetch") and "screen" not in seen:
                seen.append("screen"); shot("screen")
            if scr == "AskScreen" and "brief" not in seen:
                await pilot.pause(0.4); seen.append("brief"); shot("brief")
                box = app.screen.query_one("#reply"); box.focus()
                box.value = "tasks that need real computation and careful checking"
                await pilot.press("enter")
            elif scr == "SelectionScreen" and "select" not in seen:
                seen.append("select"); shot("select")
                box = app.screen.query_one("#choice"); box.focus(); box.value = "6"
                await pilot.press("enter")
            elif scr == "CellsBox" and "cells" not in seen:
                await pilot.pause(0.4); seen.append("cells"); shot("cells")
                app.screen.query_one("#cells").value = "4"
                app.screen.query_one("#containers").value = "16"
                await pilot.press("enter")
            elif scr == "Screen" and b and b.phase == "ready" and any(c.task is not None and c.stage >= 5 for c in s.cells) and "curate" not in seen:
                seen.append("curate"); shot("curate")
                await pilot.press("2"); await pilot.pause(0.3); shot("cell-2")
            elif "curate" in seen and "end" not in seen and scr == "PickScreen":
                seen.append("end"); shot("end")
                s.is_reviewer = True
            elif "end" in seen:
                break
        await pilot.pause(0.3)
        s.kill()

seen = []
with tempfile.TemporaryDirectory(prefix="hm-") as tmp:
    ws = prepare(tmp)
    asyncio.run(main(ws))
