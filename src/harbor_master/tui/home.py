"""The index's dashboard: what `hm view` shows its owner and its leads, and `hm view --demo` anyone.

It is read only. It shows where the index stands: where each benchmark is (listed, curating, curated or dropped)
and how far it is curated, the version and settings, and the people. A few buttons open the details in a box to
read. Nothing is started from here and nothing is changed: curating is `hm curate`, and what the index is made of
is its owner's files.
"""
from __future__ import annotations

import asyncio
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
from . import theme
from .parts import SCROLLBARS
from .theme import AM, BG, BORDER, FG, MU, PU, RD, SE, TE, t

# The buttons: each opens one thing about the index in full, in a box. name -> label
LABELS = {"status": "Status", "tasks": "Tasks", "benchmarks": "Benchmarks", "credits": "Credits", "skills": "Skills"}
KEYS = t(("tab", SE), (" to a button   ", MU), ("enter", SE), (" opens it   ", MU), ("r", SE), (" read again   ", MU),
         ("ctrl+t", SE), (" light or dark   ", MU), ("ctrl+q", SE), (" quit", MU))
TILES = ("benchmarks", "curated", "accepted", "review", "people", "spent")


def gather(ws: Path, demo: bool = False) -> dict:
    """Where the index stands, from its records. With `demo`, or while it has only simulated runs to show, the figures are theirs."""
    cfg = config_mod.load(ws)
    real = ledger_mod.Ledger(ws)
    worked = lambda events: any(e["type"] in ("decision", "trial") for e in events)      # noqa: E731
    sim = ledger_mod.Ledger(config_mod.simulated(ws), simulated=True)
    simulated = demo or (not worked(real.events) and worked(sim.events))
    led, root = (sim, config_mod.simulated(ws)) if simulated else (real, ws)
    benches: dict[str, Counter] = {b: Counter() for b in [*(e["id"] for e in tasks_mod.supported.supported(ws)),
                                                          *(tasks_mod.read_registry(ws) if simulated else [])]}
    for state in ledger_mod.fold(led.events).values():
        if state.status != "not_selected":
            benches.setdefault(state.benchmark, Counter())[state.status] += 1
    told = ledger_mod.benchmarks.statuses(led.events, list(benches))
    return {"cfg": cfg, "simulated": simulated, "benches": benches, "statuses": told,
            "people": ledger_mod.load_contributors(ws), "credits": ledger_mod.credits(led.events),
            "accepted": sum(c["accepted"] for c in benches.values()), "cost": sum(float(e.get("cost", 0) or 0) for e in led.events)}


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


def table(*columns: tuple[str, str], names: int = 0, flex: int = 1) -> Table:
    """A table for a panel. With `names`, the first column is that wide, for the longest name in it. Column `flex`
    takes the room that is left, and is the one cut when there is too little: never a name or a figure."""
    tb = Table(box=None, expand=True, padding=(0, 2, 0, 0), pad_edge=False, header_style=MU, show_edge=False)
    for n, (name, justify) in enumerate(columns):
        more = {"width": max(names, len(name))} if names and n == 0 else {"ratio": 1} if n == flex else {}
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

    def __init__(self, ws: Path, demo: bool = False):
        super().__init__()
        self.ws, self.demo = ws.resolve(), demo
        self.data = gather(self.ws, demo)
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
                with Vertical(id="right"):
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
            self.data = gather(self.ws, self.demo)
            self.draw()

    def action_theme(self) -> None:
        theme.set_mode("")
        theme.follow(self, self._light)

    # ----- drawing -----

    def draw(self) -> None:
        d = self.data
        cfg, me = d["cfg"], identity_mod.load()
        mine = next((c for c in d["people"] if me and c.get("github") == me.github), None)
        g = Table.grid(expand=True)
        g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        g.add_column(justify="right", no_wrap=True)
        g.add_row(t(("harbor-master", f"bold {PU}"), (f" v{__version__}", MU), ("   index", f"bold {TE}"),
                    ("   simulated runs only: none of this is in the index" if d["simulated"] else "", AM)),
                  t(me.name if me else "", (f" (@{me.github}, {(mine or {}).get('role', 'contributor')})" if me else "", MU)))
        g.add_row(t((cfg.about.title or cfg.about.name or self.ws.name, "bold"), (f"   {self.ws}", MU)),
                  t(("● ", PU), ("curating", PU), (f"   v{cfg.about.version}", MU)))
        self.query_one("#header", Static).update(g)

        waiting = sum(c["report_ready"] + c["contested"] for c in d["benches"].values())
        curated = sum(1 for st in d["statuses"].values() if st["status"] == "curated")
        for name, (figure, color, label) in zip(TILES, (
                (str(len(d["benches"])), FG, "benchmarks"), (f"{curated} of {len(d['benches'])}", TE if curated else MU, "curated"),
                (str(d["accepted"]), TE, "tasks accepted"), (str(waiting), PU if waiting else MU, "to review"),
                (str(len(d["people"])), FG, "contributors"), (f"${d['cost']:,.0f}", FG, "spent"))):
            self.query_one(f"#tile-{name}", Static).update(Text.assemble((figure, f"bold {color}"), "\n", (label, MU)))

        # The figures are the bar's three parts: accepted, waiting for a review, and out (rejected, cut or dropped). The
        # bar is drawn only where there is room for it beside every name, status and figure.
        room = int(self.size.width * 5 / 9) - 4                 # the left column's inside width
        longest = max([len(b) for b in d["benches"]] + [len("benchmark")])
        word = {"listed": ("listed", SE), "curating": ("curating", PU), "curated": ("curated", TE), "dropped": ("dropped", RD)}
        said = {name: word[st["status"]][0] + (f" @{st['by']}" if st["status"] == "curating" else "") for name, st in d["statuses"].items()}
        status_width = max([len(x) for x in said.values()] + [len("status")])
        wide = room - longest - status_width - len("accepted") - len("review") - len("out") - 2 * 5
        heads = [("benchmark", "left"), ("status", "left"), *([("", "left")] if wide >= 8 else []),
                 ("accepted", "right"), ("review", "right"), ("out", "right")]
        tb = table(*heads, names=longest, flex=2 if wide >= 8 else 1)
        for name, c in d["benches"].items():
            st = d["statuses"].get(name, {"status": "listed"})
            label, color = word[st["status"]]
            review, out = c["report_ready"] + c["contested"], c["rejected"] + c["dropped"] + c["cut"]
            shown = [bar([(c["accepted"], TE), (review, PU), (out, RD)], c["accepted"] + review + out, wide)] if wide >= 8 else []
            tb.add_row(name, t((label, color), (f" @{st['by']}" if st["status"] == "curating" else "", MU)), *shown,
                       t((str(c["accepted"]), TE)), t((str(review), PU)), t((str(out), RD)))
        box = self.query_one("#curation", Static)
        box.border_title = "curation"
        box.border_subtitle = f"{d['accepted']} accepted task{'s' if d['accepted'] != 1 else ''}"
        box.update(tb if d["benches"] else Text("No benchmark is on the index's list yet.", style=MU))

        tb = table(("name", "left"), ("role", "left"), ("verdicts", "right"), ("trials", "right"), ("spent", "right"),
                   names=max((len(c.get("name", "?")) for c in d["people"]), default=0))
        for c in d["people"]:
            done = d["credits"].get(c.get("github", ""), {})
            role = c.get("role") or ("" if c.get("key") else "no key yet")      # a contributor is everyone on it: no word
            tb.add_row(c.get("name", "?"), t((role, TE if role in ("owner", "lead") else SE)),
                       str(int(done.get("verdicts", 0))), str(int(done.get("trials", 0))), f"${done.get('cost', 0):,.0f}")
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
