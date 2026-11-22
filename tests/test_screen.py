"""The curate screen sends the terminal only what changed: over a slow link (VS Code Remote, ssh) a screen that redraws
every panel ten times a second keeps the terminal behind, and leaving it waits for the terminal to catch up."""
import asyncio

from textual.widgets import Static

from harbor_master import tasks
from harbor_master.core import config as config_mod
from harbor_master.engine import Session
from harbor_master.tui.app import SessionApp


async def test_a_panel_that_has_not_changed_is_not_redrawn_and_what_moves_moves_twice_a_second(workspace, ident, monkeypatch):
    s = Session(workspace, config_mod.load(workspace), ident, tasks.read_registry(workspace), simulate=True, as_real=True,
                speed=30, no_input=True)
    drawn: dict[str, int] = {}
    real = Static.update

    def counted(self, *a, **k):
        drawn[self.id] = drawn.get(self.id, 0) + 1
        return real(self, *a, **k)
    monkeypatch.setattr(Static, "update", counted)
    app = SessionApp(s)
    async with app.run_test(size=(120, 40)):
        await asyncio.sleep(1.0)            # the run gets going and its screen settles
        drawn.clear()
        await asyncio.sleep(3.0)            # about 30 looks at the screen
        s.kill()
    assert drawn.get("header", 0) <= 1                          # it says the same thing all along
    assert drawn.get("benchmarks", 0) <= 8                      # its spinner moves twice a second, not ten times


async def test_the_curate_line_puts_every_chosen_task_in_one_state_and_they_add_up(workspace, session):
    from harbor_master.tui import stages
    await session.run()
    b = session.benches["demo-bench"]
    counts = stages.standing(session, b)
    assert sum(counts.values()) == len(b.chosen) == 20
    assert counts.get("left for retry", 0) == len(b.left) and "working" not in counts and "waiting" not in counts
    assert stages.standing_line(session, b).startswith("20 tasks: ")
