"""The index's dashboard: what `hm open` shows its owner and its leads.

It is read only. It shows where the index stands: how far each benchmark is curated, how far the released
index is evaluated, the protocol that is locked, the roster, and the people. A few buttons open the details
in a box to read. Nothing is started from here and nothing is changed: curating is `hm curate`, evaluating
is `hm eval`, and what the index is made of is its owner's files.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from pathlib import Path

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .. import __version__
from .. import ledger as ledger_mod
from .. import tasks as tasks_mod
from ..core import config as config_mod
from ..core import identity as identity_mod
from ..ledger import evals
from . import theme
from .parts import SCROLLBARS
from .theme import AM, BG, BORDER, FG, MU, PU, RD, SE, TE, t

# The buttons: each opens one thing about the index in full, in a box. name -> label
LABELS = {"leaderboard": "Leaderboard", "tasks": "Tasks", "status": "Status", "todo": "To do", "credits": "Credits",
          "benchmarks": "Benchmarks", "protocol": "Protocol", "skills": "Skills"}
KEYS = t(("tab", SE), (" to a button   ", MU), ("enter", SE), (" opens it   ", MU), ("r", SE), (" read again   ", MU),
         ("ctrl+t", SE), (" light or dark   ", MU), ("ctrl+q", SE), (" quit", MU))
TILES = ("benchmarks", "accepted", "review", "evaluated", "people", "spent")


def gather(ws: Path) -> dict:
    """Where the index stands, from its records. While it has only simulated runs to show, the figures are theirs."""
    cfg = config_mod.load(ws)
    real = ledger_mod.Ledger(ws)
    worked = lambda events: any(e["type"] in ("decision", "trial") for e in events)      # noqa: E731
    sim = ledger_mod.Ledger(config_mod.simulated(ws), simulated=True)
    simulated = not worked(real.events) and worked(sim.events)
    led, root = (sim, config_mod.simulated(ws)) if simulated else (real, ws)
    benches: dict[str, Counter] = {b: Counter() for b in [*(e["id"] for e in tasks_mod.supported.supported(ws)),
                                                          *(tasks_mod.read_registry(ws) if simulated else [])]}
    for state in ledger_mod.fold(led.events).values():
        if state.status != "not_selected":
            benches.setdefault(state.benchmark, Counter())[state.status] += 1
    done = evals.read(root, simulated)
    roster = config_mod.roster(ws) or (config_mod.demo_roster(cfg) if simulated else [])
    cells = evals.cells(ledger_mod.index_entries(led.events), roster, cfg.eval, evals.agent_trials(led.events, done), done, time.time())
    lock = ledger_mod.protocol.current(real.events)
    release = evals.release(evals.read(ws))
    return {"cfg": cfg, "simulated": simulated, "benches": benches, "cells": cells, "roster": roster,
            "people": ledger_mod.load_contributors(ws), "credits": ledger_mod.credits([*led.events, *done]),
            "version": ledger_mod.protocol.version(lock) if lock else "", "locked": bool(lock and lock["protocol"] == cfg.protocol),
            "released": release is not None, "accepted": sum(c["accepted"] for c in benches.values()), "cost": sum(float(e.get("cost", 0) or 0) for e in [*led.events, *done])}


def bar(parts: list[tuple[int, str]], total: int, width: int = 24) -> Text:
    """A bar of `width` cells, filled in each part's color to that part's share of the total."""
    out, used = Text(), 0
    for n, color in parts:
        cells = round(width * n / total) if total else 0
        cells = min(cells if n == 0 or cells else 1, width - used)
        out.append("━" * cells, style=color)
        used += cells
    out.append("━" * (width - used), style=BORDER)
    return out


def table(*columns: tuple[str, str], names: int = 0) -> Table:
    """A table for a panel. With `names`, the first column is that wide, for the longest name in it, and the
    second, a bar, takes the room that is left: neither a name nor a figure is cut."""
    tb = Table(box=None, expand=True, padding=(0, 2, 0, 0), pad_edge=False, header_style=MU, show_edge=False)
    for n, (name, justify) in enumerate(columns):
        more = {"width": max(names, len(name))} if names and n == 0 else {"ratio": 1} if n == (1 if names else 0) else {}
        tb.add_column(name, justify=justify, no_wrap=True, overflow="ellipsis", **more)       # type: ignore[arg-type]
    return tb


def pairs(rows: list[tuple[str, str | Text]]) -> Table:
    g = Table.grid(expand=True, padding=(0, 2, 0, 0))
    g.add_column(no_wrap=True, style=MU)
    g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    for label, value in rows:
        g.add_row(label, value)
    return g


class IndexApp(App[None]):
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [("ctrl+q", "quit", "Quit"), ("q", "quit", "Quit"), ("r", "read", "Read again"), ("ctrl+t", "theme", "Light or dark")]
    CSS = SCROLLBARS + f"""
    Screen {{ background: {BG}; color: {FG}; }}
    #top {{ height: 1fr; scrollbar-size: 0 0; }}
    #bottom {{ height: auto; dock: bottom; }}
    .panel {{ border: round {BORDER}; border-title-color: {MU}; border-subtitle-color: {MU}; padding: 0 1; height: auto; }}
    #tiles {{ height: 4; }}
    .tile {{ width: 1fr; height: 4; border: round {BORDER}; padding: 0 1; content-align: center middle; text-align: center; }}
    #columns {{ height: auto; }}
    #left {{ width: 5fr; height: auto; }}
    #right {{ width: 4fr; height: auto; }}
    #views {{ height: 1; padding: 0 2; margin: 0 0 1 0; }}
    #views .group {{ width: 10; height: 1; color: {MU}; }}
    #views Button {{ height: 1; width: auto; min-width: 4; border: none; padding: 0 1; margin: 0 1 0 0; background: {BG}; color: {FG};
                     text-style: underline; }}
    #views Button:focus {{ background: {TE}; color: {BG}; text-style: bold; }}
    #views Button:hover {{ background: {BORDER}; color: {FG}; }}
    #keys {{ height: 1; padding: 0 2; }}
    """

    def __init__(self, ws: Path):
        super().__init__()
        self.ws = ws.resolve()
        self.data = gather(self.ws)
        self.busy = False
        self.last = ("", "")            # the box that was opened last, for tests

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="top", can_focus=False):
            yield Static(id="header", classes="panel")
            with Horizontal(id="tiles"):
                for name in TILES:
                    yield Static(id=f"tile-{name}", classes="tile")
            with Horizontal(id="columns"):
                with Vertical(id="left"):
                    yield Static(id="curation", classes="panel")
                    yield Static(id="evaluation", classes="panel")
                    yield Static(id="roster", classes="panel")
                with Vertical(id="right"):
                    yield Static(id="protocol", classes="panel")
                    yield Static(id="people", classes="panel")
        with Vertical(id="bottom"):
            with Horizontal(id="views"):
                yield Static("details", classes="group")
                for name, label in LABELS.items():
                    yield Button(label, id=f"view-{name}", name=name)
            yield Static(KEYS, id="keys")

    def on_mount(self) -> None:
        self._light = theme.light_filter()
        self._filters.append(self._light)
        self.set_interval(5.0, self.action_read)       # a run that is going moves these figures
        self.draw()

    def action_read(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.data = gather(self.ws)
            self.draw()

    def action_theme(self) -> None:
        theme.set_mode("")
        theme.follow(self, self._light)

    # ----- drawing -----

    def draw(self) -> None:
        d = self.data
        cfg, me = d["cfg"], identity_mod.load()
        mine = next((c for c in d["people"] if me and c.get("github") == me.github), None)
        stage = (("released", TE) if d["released"] else ("building", PU))
        lock = (f"v{d['version']}", TE) if d["locked"] else ("not locked", AM) if not d["version"] else (f"v{d['version']}, changed since", AM)
        g = Table.grid(expand=True)
        g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        g.add_column(justify="right", no_wrap=True)
        g.add_row(t(("harbor-master", f"bold {PU}"), (f" v{__version__}", MU), ("   index", f"bold {TE}"),
                    ("   simulated runs only: none of this is in the index" if d["simulated"] else "", AM)),
                  t(me.name if me else "", (f" (@{me.github}, {(mine or {}).get('role', 'contributor')})" if me else "", MU)))
        g.add_row(t((cfg.about.title or cfg.about.name or self.ws.name, "bold"), (f"   {self.ws}", MU)),
                  t(("● ", stage[1]), (stage[0], stage[1]), ("   protocol ", MU), lock))
        self.query_one("#header", Static).update(g)

        cells = d["cells"]
        total, ran = sum(c.trials for c in cells), sum(len(c.done) for c in cells)
        waiting = sum(c["report_ready"] + c["contested"] for c in d["benches"].values())
        for name, (figure, color, label) in zip(TILES, (
                (str(len(d["benches"])), FG, "benchmarks"), (str(d["accepted"]), TE, "tasks accepted"), (str(waiting), PU if waiting else MU, "reports to review"),
                (f"{100 * ran / total:.0f}%" if total else "-", TE if total else MU, "evaluated"), (str(len(d["people"])), FG, "contributors"),
                (f"${d['cost']:,.0f}", FG, "spent"))):
            self.query_one(f"#tile-{name}", Static).update(Text.assemble((figure, f"bold {color}"), "\n", (label, MU)))

        room = max(8, int(self.size.width * 5 / 9) - 4)        # the left column's width, for the bars
        longest = max((len(b) for b in d["benches"]), default=9)
        wide = max(6, room - max(longest, 9) - 48)
        tb = table(("benchmark", "left"), ("", "left"), ("accepted", "right"), ("review", "right"), ("rejected", "right"), ("dropped", "right"), ("working", "right"),
                   names=max((len(b) for b in d["benches"]), default=0))
        for name, c in d["benches"].items():
            out = c["rejected"] + c["dropped"] + c["cut"]
            tb.add_row(name, bar([(c["accepted"], TE), (c["report_ready"] + c["contested"], PU), (out, RD)], sum(c.values()), wide), t((str(c["accepted"]), TE)),
                       t((str(c["report_ready"] + c["contested"]), PU)), str(c["rejected"] + c["cut"]), t((str(c["dropped"]), RD)), str(c["in_progress"]))
        box = self.query_one("#curation", Static)
        box.border_title = "stage 1   curation"
        box.border_subtitle = f"{d['accepted']} accepted task{'s' if d['accepted'] != 1 else ''}" + (", closed" if d["released"] else "")
        box.update(tb if d["benches"] else Text("No benchmark is on the index's list yet.", style=MU))

        tb = table(("configuration", "left"), ("", "left"), ("trials", "right"), ("passed", "right"), ("held", "right"), ("free", "right"), ("given up", "right"),
                   names=max((len(c.config.name) for c in cells), default=0))
        wide = max(6, room - max(max((len(c.config.name) for c in cells), default=0), 13) - 44)
        left = lambda group: sum(len(c.missing) for c in group)      # noqa: E731
        for name in dict.fromkeys(c.config.name for c in cells):
            own = [c for c in cells if c.config.name == name]
            runs = [e for c in own for e in c.done.values()]
            want, passed = sum(c.trials for c in own), sum(bool(e["passed"]) for e in runs)
            tb.add_row(name, bar([(len(runs), TE)], want, wide), t(str(len(runs)), (f" of {want}", MU)),
                       t((f"{100 * passed / len(runs):.0f}%" if runs else "-", TE)), str(left([c for c in own if c.open and c.holder])),
                       str(left([c for c in own if c.open and not c.holder])), t((str(left([c for c in own if c.failed])), RD)))
        box = self.query_one("#evaluation", Static)
        box.border_title = "stage 2   evaluation"
        box.border_subtitle = (f"{ran} of {total} trials" if cells else "") + ("" if d["released"] or d["simulated"] else "   opens at the release")
        box.update(tb if cells else Text("Nothing to evaluate yet: no task is accepted, or the roster is empty.", style=MU))

        tb = table(("configuration", "left"), ("agent", "left"), ("model", "left"), ("effort", "left"))
        for c in d["roster"]:
            tb.add_row(c.name, f"{c.agent} {c.version}".strip(), c.model, c.reasoning_effort or "-")
        box = self.query_one("#roster", Static)
        box.border_title = "roster"
        box.border_subtitle = f"{len(d['roster'])} configurations, never locked" + (", stand-ins for the demo" if d["simulated"] and not config_mod.roster(self.ws) else "")
        box.update(tb if d["roster"] else Text("roster.toml lists no configuration yet.", style=MU))

        tiers = cfg.tiers
        suite = ", ".join(e.model for e in cfg.difficulty.suite)
        box = self.query_one("#protocol", Static)
        box.border_title = "protocol"
        box.border_subtitle = cfg.protocol
        box.update(pairs([
            ("version", t(lock)), ("trials", f"{cfg.eval.trials} per task and configuration"),
            ("kept when", f"frontier agents pass at most {100 * cfg.difficulty.max_pass_rate:.0f}%"),
            ("selected", f"the top {cfg.select.top_k} of a benchmark, at most {100 * cfg.select.max_fraction:.0f}%"),
            ("claims", f"{cfg.eval.claim_hours:g} hours, at most {cfg.eval.max_claim} cells each"),
            ("frontier", suite), ("worker", tiers.worker.model), ("decision", tiers.decision.model),
            ("task store", cfg.about.tasks or "none named")]))

        tb = table(("name", "left"), ("", "left"), ("role", "left"), ("verdicts", "right"), ("trials", "right"), ("spent", "right"))
        for c in d["people"]:
            done = d["credits"].get(c.get("github", ""), {})
            role = c.get("role") or ("contributor" if c.get("key") else "no key yet")
            tb.add_row(c.get("name", "?"), t((f"@{c.get('github', '?')}", MU)), t((role, TE if role in ("owner", "lead") else SE)),
                       str(int(done.get("verdicts", 0))), str(int(done.get("trials", 0))), f"${done.get('cost', 0):.2f}")
        box = self.query_one("#people", Static)
        box.border_title = "contributors"
        box.border_subtitle = f"{len(d['people'])} listed"
        box.update(Group(tb))

    # ----- the details -----

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.name or ""
        if self.busy or name not in LABELS:
            return
        from ..cli.views import capture
        label = LABELS[name]
        self.busy = True
        try:
            out = await asyncio.to_thread(capture, name, self.ws, self.data["simulated"], max(40, int(self.size.width * 0.9) - 6))
        finally:
            self.busy = False
        self.last = (label, out.plain)
        self.push_screen(ViewScreen(label, out if out.plain.strip() else Text("Nothing to show yet.")))

    def on_key(self, event: events.Key) -> None:
        if event.key in ("left", "right") and isinstance(self.focused, Button):
            event.stop()
            (self.action_focus_previous if event.key == "left" else self.action_focus_next)()


class ViewScreen(ModalScreen[None]):
    """One view of the index, in a box to read and scroll. esc closes it."""

    BINDINGS = [("escape", "dismiss", "Close"), ("enter", "dismiss", "Close"), ("q", "dismiss", "Close"),
                ("pageup", "scroll(-1, True)", "Up"), ("pagedown", "scroll(1, True)", "Down"),
                ("up", "scroll(-1, False)", "Up"), ("down", "scroll(1, False)", "Down")]
    DEFAULT_CSS = f"""
    ViewScreen {{ align: center middle; background: {BG} 70%; }}
    ViewScreen > VerticalScroll {{ width: 94%; height: auto; max-height: 90%; border: round {PU}; border-title-color: {PU};
                                  border-subtitle-color: {MU}; padding: 1 2; background: {BG}; }}
    """

    def __init__(self, title: str, body: Text):
        super().__init__()
        self._title, self._body = title, body

    def compose(self) -> ComposeResult:
        box = VerticalScroll(Static(self._body))
        box.border_title = self._title
        box.border_subtitle = "up and down scroll     esc closes"
        yield box

    def action_scroll(self, way: int, page: bool) -> None:
        area = self.query_one(VerticalScroll)
        area.scroll_relative(y=way * (max(1, area.size.height - 2) if page else 1), animate=False)
