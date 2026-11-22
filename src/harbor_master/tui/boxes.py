"""The curate screen's boxes for starting: which benchmark to curate, a new one's draft, and how much runs at once.

The run asks in a box and waits; nothing else can be done on the screen until it is answered (ctrl+z still ends
the run). Each box says what it is for in its first line.
"""
from __future__ import annotations

import asyncio

from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from .. import ledger as ledger_mod
from .. import tasks as tasks_mod
from ..engine.state import MAX_CELLS
from .parts import QUIT_KEYS, Fits
from .theme import AM, BG, BORDER, FG, MU, PU, RD, SE, TE, t

CSS = f"""
{{name}} {{{{ align: center bottom; background: {BG} 25%; }}}}
{{name}} > Vertical {{{{ width: 92; max-width: 96%; height: auto; max-height: 94%; border: round {PU}; border-title-color: {PU};
                     padding: 1 2; background: {BG}; margin: 0 0 1 0; overflow-y: auto; scrollbar-size: 0 0; }}}}
{{name}} VerticalScroll {{{{ height: auto; margin: 0 0 1 0; }}}}
{{name}} #text {{{{ height: auto; color: {FG}; margin: 0 0 1 0; }}}}
{{name}} Button, {{name}} Button.-style-default {{{{ width: 100%; height: 3; border: round {BORDER}; background: {BG}; color: {FG}; }}}}
{{name}} Button:focus {{{{ border: round {TE}; color: {TE}; text-style: bold; background-tint: {BG} 0%; }}}}
{{name}} Input {{{{ margin: 0 0 1 0; }}}}
{{name}} #said {{{{ height: auto; color: {AM}; }}}}
"""


def candidates(s) -> list[tuple[str, dict, str]]:
    """The benchmarks a run could take: the index's list, and in a simulated run the made-up ones registered here.
    Each with its status, and its number of tasks ("to adapt" when they are not in Harbor format yet)."""
    registry = tasks_mod.read_registry(s.ws)
    listed = {e["id"]: e for e in tasks_mod.supported.supported(s.ws)}
    names = [*listed, *(b for b in registry if b not in listed and s.simulate)]
    told = ledger_mod.benchmarks.statuses(s.ledger.events, names)
    out = []
    for name in names:
        e = listed.get(name)
        about = ((str(e["task_count"]) if e.get("task_count") else "to adapt") if e else      # to adapt: not in Harbor format yet
                 str(len(tasks_mod.find_tasks(__import__('pathlib').Path(registry[name]['path'])))))
        out.append((name, told.get(name, {"status": "listed"}), about))
    return sorted(out, key=lambda x: (x[1]["status"] not in OPEN, x[1]["status"] != "listed", x[0] != "demo-bench", x[0]))


OPEN = ("listed", "dropped")        # what a run may take: nobody holds it, and it is not finished. A dropped one may be tried again


def _until(when: str) -> str:
    """A claim's end: "10:35" today, "tomorrow 10:35", and with its date on any other day."""
    import time
    if when[:10] == time.strftime("%Y-%m-%d"):
        return when[-5:]
    return f"tomorrow {when[-5:]}" if when[:10] == time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400)) else when


def taken_words(st: dict, why: str = "") -> str:
    """Where a benchmark stands, in a few words: listed, who is curating it, or that it is curated or dropped."""
    return {"listed": "listed · nobody has started it",
            "curating": f"curating · @{st.get('by', '?')} until {_until(st.get('until', '?'))}",
            "curated": "curated · nothing is left to curate in it",
            "dropped": "dropped" + (f" · {why}" if why else "") + " · it may be curated again"}[st["status"]]


def refusal(st: dict, why: str = "") -> str:
    """What follows "Refused: that is NAME" for a link to a benchmark that is on the list and cannot be taken."""
    return {"curating": f", on the list already. @{st.get('by', '?')} is curating it until {_until(st.get('until', '?'))}.",
            "curated": ", on the list already, and curated: nothing is left to curate in it."}[st["status"]]


def cannot_take(st: dict, why: str = "") -> str:
    """What follows "Refused: NAME" for a name typed of a benchmark that is not listed."""
    return {"curating": f" cannot be taken: @{st.get('by', '?')} is curating it until {_until(st.get('until', '?'))}.",
            "curated": " cannot be taken: it is curated, and nothing is left to curate in it."}[st["status"]]


class PickScreen(Fits, ModalScreen[str]):
    """Which benchmark to curate. Every benchmark on the list is shown, in a list that scrolls, with where it stands.
    Below it, one line takes what is typed: the name of a listed one (nobody holds it and it is not finished) curates
    it, and a repository link adds a new one. Typing a name narrows the list, and tab completes it. A name that is not
    listed is refused with where it stands, and so is a link to a benchmark that is on the list already, in any
    spelling of it: nothing is added twice."""
    DEFAULT_CSS = CSS.format(name="PickScreen") + f"""
    PickScreen > Vertical {{ max-height: 92%; }}
    PickScreen #list {{ height: auto; border: round {BORDER}; border-title-color: {MU}; padding: 0 0 0 1; margin: 0 0 1 0; }}
    /* The scrollbar keeps its column against the frame's right edge, with a space between it and the figures, whether
       or not there is anything to scroll: the header above, which does not scroll, leaves the same two columns. */
    PickScreen #list VerticalScroll {{ height: auto; max-height: 11; margin: 0; overflow-x: hidden; scrollbar-size: 0 1;
                                    overflow-y: scroll; }}
    PickScreen #head, PickScreen #rows {{ height: auto; }}
    PickScreen #rows {{ padding: 0 1 0 0; }}
    PickScreen #head {{ padding: 0 2 0 0; }}
    """
    FLEX, SCROLL, CAP = "#list", "#scroll", 18
    BINDINGS = [("ctrl+z", "app.quit_run", "End"), ("ctrl+l", "app.leave", "Leave"), ("ctrl+t", "app.theme", "Light or dark"),
                Binding("tab", "complete", "Complete", priority=True, show=False),
                ("up", "scroll(-1)", "Scroll"), ("down", "scroll(1)", "Scroll"),
                ("pageup", "scroll(-8)", "Scroll"), ("pagedown", "scroll(8)", "Scroll")]

    def __init__(self, s, note: str = ""):
        super().__init__()
        self.s, self.note = s, note
        self.found = candidates(s)
        self.gone = ledger_mod.abandoned(s.ledger.events)
        self.filled = ""            # a name put on the line for the person, whose message stays when the line changes to it
        self.searching = ""         # the link the worker agent is searching for, while it does

    def compose(self) -> ComposeResult:
        free = [n for n, st, _ in self.found if st["status"] in OPEN]
        text = t(("Which benchmark?", "bold"), "\n",
                 (self.note + "\n" if self.note else "", AM if "not finished" in self.note else TE),
                 ("Type the name of a listed or dropped one to curate it, or paste a link to add a new one: its repository, paper or page." if free else
                  "None is free: each is being curated, or curated. Paste a link to add a new one: its repository, paper or page.", SE))
        box = Vertical(Static(text, id="text"),
                       *([Vertical(Static(id="head"), VerticalScroll(Static(id="rows"), id="scroll", can_focus=False), id="list")]
                         if self.found else []),
                       Input(placeholder="a benchmark's name, or a link to a new one", id="line"),
                       Static(id="said"))
        box.border_title = "curate a benchmark"
        box.border_subtitle = QUIT_KEYS
        yield box

    def on_mount(self) -> None:
        self._draw("")
        self.query_one("#line", Input).focus()

    def _why(self, name: str) -> str:
        return (self.gone.get(name) or {}).get("reason", "")

    def _status(self, name: str) -> dict:
        return next((st for n, st, _ in self.found if n == name), {"status": "dropped"})

    def _matches(self, typed: str) -> list[tuple[str, dict, str]]:
        """The rows a typed name narrows the list to: those whose name has it in them. A link narrows nothing."""
        typed = typed.strip().lower()
        if not typed or "/" in typed:
            return self.found
        return [r for r in self.found if typed in r[0].lower()]

    def _draw(self, typed: str) -> None:
        if not self.found:
            return
        rows = self._matches(typed)
        names = max(len(n) for n, _, _ in self.found)
        counts = max([len(a) for _, _, a in self.found] + [len("tasks")])

        def table(header: bool) -> Table:      # the header stays put above the rows, which scroll: both with the same columns
            tb = Table(box=None, expand=True, pad_edge=False, padding=(0, 2, 0, 0), header_style=MU, show_edge=False, show_header=header)
            tb.add_column("", no_wrap=True, width=1)
            tb.add_column("benchmark", no_wrap=True, width=names)
            tb.add_column("where it stands", no_wrap=True, overflow="ellipsis", ratio=1)
            tb.add_column("tasks", no_wrap=True, justify="right", width=counts)
            return tb
        tb = table(False)
        for name, st, about in rows:
            free = st["status"] in OPEN
            color = {"listed": TE, "curated": TE, "dropped": RD}.get(st["status"], AM)
            tb.add_row(t(("●" if free else "·", TE if free else MU)), t((name, f"bold {FG}" if free else MU)),
                       t((taken_words(st, self._why(name) if st["status"] == "dropped" else ""), color)), t((about, MU)))
        listed = sum(1 for _, st, _ in self.found if st["status"] in OPEN)
        box = self.query_one("#list", Vertical)
        self.query_one("#head", Static).update(table(True))
        box.border_title = (f"{len(rows)} of {len(self.found)} match" if rows is not self.found else
                            f"{len(self.found)} on the list · {listed} free to curate")
        self.query_one("#rows", Static).update(tb if rows else Text("None on the list is called that. A new one is added by its "
                                                                     "repository link.", style=MU))

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        if event.value != self.filled:
            self.query_one("#said", Static).update("")
        self.filled = ""
        self._draw(event.value)

    def action_complete(self) -> None:
        """Tab: the name of the first listed benchmark that matches what is typed, its start first."""
        line = self.query_one("#line", Input)
        typed = line.value.strip().lower()
        free = [n for n, st, _ in self.found if st["status"] in OPEN]
        best = next((n for n in free if n.lower().startswith(typed)), None) or next((n for n in free if typed in n.lower()), None)
        if best and "/" not in typed:
            line.value, line.cursor_position = best, len(best)

    def action_scroll(self, by: int) -> None:
        if self.query("#scroll"):
            box = self.query_one("#scroll", VerticalScroll)
            box.scroll_to(y=box.scroll_y + by, animate=False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        from ..tasks import adding
        event.stop()
        typed = event.value.strip()
        said = self.query_one("#said", Static)
        if not typed:
            said.update("Type the name of a listed or dropped benchmark, or paste a link to a new one.")
            return
        if "/" in typed:                    # a link: a new benchmark, unless it is one on the list already
            name = adding.on_the_list(self.s.ws, typed, made_up=self.s.simulate)
            if not name:
                self._search(typed)
                return
            st = self._status(name)
            if st["status"] in OPEN:
                line = self.query_one("#line", Input)
                self.filled = name
                line.value, line.cursor_position = name, len(name)
                said.update(t((f"That is {name}, on the list already, so it is not added again. ", f"bold {AM}"),
                              ("Enter curates it.", AM)))
                return
            said.update(t((f"Refused: that is {name}", f"bold {RD}"), (refusal(st, self._why(name)), RD)))
            return
        name = next((n for n, _, _ in self.found if n.lower() == typed.lower()), None)
        if name is None:
            said.update(t((f"No benchmark called {typed} is on the list. ", f"bold {AM}"),
                          ("To add it, paste a link to it: its repository, its paper or its page.", AM)))
            return
        st = self._status(name)
        if st["status"] in OPEN:
            self.dismiss(f"pick:{name}")
            return
        said.update(t((f"Refused: {name}", f"bold {RD}"), (cannot_take(st, self._why(name)), RD)))

    def _search(self, link: str) -> None:
        """Search for a new benchmark here, with what is happening on the line under the box, and open its draft, filled
        in, once it is found. What goes wrong is said here, and the link can be changed and given again."""
        self.searching, self.step = link, ""
        self.query_one("#line", Input).disabled = True
        self._tick = self.set_interval(0.1, self._searching)
        self._searching()
        self.run_worker(self._found(link), exclusive=True)

    def _searching(self) -> None:
        import time

        from .theme import spinner
        if self.searching:
            from ..tasks import supported
            step = getattr(self, "step", "")
            got = supported.received["bytes"]
            what = ("a stand-in makes it up (simulated)" if self.s.simulate else
                    (step + (f" · {got / 1e6:,.0f} MB so far" if got else "")) if step else "the worker agent searches the web for it")
            self.query_one("#said", Static).update(t((spinner(time.time()) + " ", PU), (f"{what} ...", SE)))

    async def _found(self, link: str) -> None:
        from ..tasks import adding
        try:
            entry, sources = await find_draft(self.s, link, lambda said: setattr(self, "step", said))
        except adding.AddError as err:
            self.searching = ""
            self._tick.stop()
            line = self.query_one("#line", Input)
            line.disabled = False
            line.focus()
            self.query_one("#said", Static).update(t(("This benchmark cannot be added. ", f"bold {RD}"), (str(err), RD)))
            return
        self.searching = ""
        self._tick.stop()
        self.dismiss(("draft", entry, sources))


async def find_draft(s, link: str, say=lambda said: None) -> tuple[dict, list[str]]:
    """A new benchmark's draft, from any link, and the pages it was found from: the worker agent searches the web for its
    repository and what is known about it, and the code downloads that repository's release and finds its tasks. In a
    simulated run the agent is a stand-in that makes it up; a folder on this machine is read by the code alone.
    Raises adding.AddError."""
    from pathlib import Path

    from ..tasks import adding
    if s.simulate and Path(link).expanduser().exists():
        return await asyncio.to_thread(adding.draft, s.ws, s.ident, link), [f"the folder {link}"]
    if s.simulate:
        await asyncio.sleep(0.8)                # a stand-in for the agent's search
        return adding.made_up(s.ws, link), ["simulated: no web search was made"]
    found, _ = await adding.find(s.ws, s.ident, link)
    from ..tasks import supported
    supported.received["bytes"] = 0
    say(f"reading {str(found.get('repository', 'its repository')).removeprefix('https://')} at its release, small files only "
        "(the rest downloads once it is added)")
    entry = await asyncio.to_thread(adding.from_found, s.ws, s.ident, found)
    return entry, list(found.get("sources") or [])


class DraftScreen(Fits, ModalScreen[str]):
    """A new benchmark's draft, as it was found (find_draft), for the person to check once: enter adds it, signed, and
    the run goes on to curate it. A correction is typed below it, as `field: value` or in words for the agent, which
    looks again while the draft stays on the box."""
    DEFAULT_CSS = CSS.format(name="DraftScreen") + f"""
    DraftScreen #from {{ height: auto; color: {MU}; margin: 0 0 1 0; }}
    """
    BINDINGS = [("ctrl+z", "app.quit_run", "End"), ("ctrl+l", "app.leave", "Leave"), ("escape", "back", "Back")]
    FLEX = "#info"

    def __init__(self, s, entry: dict, sources: list[str]):
        super().__init__()
        self.s, self.entry, self.sources = s, entry, sources

    def compose(self) -> ComposeResult:
        box = Vertical(VerticalScroll(Static(id="text"), Static(id="from"), id="info", can_focus=False),
                       Input(placeholder="a correction: paper: https://..., or in words", id="line"),
                       Static(id="said"),
                       Button("Add it, and curate it", id="add"), Button("Back to the list", id="back"))
        box.border_title = "add a benchmark"
        box.border_subtitle = QUIT_KEYS
        yield box

    def on_mount(self) -> None:
        self._show(self.entry)

    def _show(self, entry: dict | None = None, error: str = "") -> None:
        from ..tasks import adding
        if error:
            self.query_one("#text", Static).update(t(("This benchmark cannot be added.", f"bold {RD}"), "\n", (error, SE)))
            self.query_one("#back", Button).focus()
            return
        self.entry = entry
        lacks = adding.lacking(entry)
        from rich.console import Group
        from rich.markup import escape
        head = t((f"{entry.get('name') or entry['id']}", "bold"), (f"   {entry['id']}, not added yet", MU))
        rows = adding.table([(k, escape(v)) for k, v in adding.lines(entry)], MU, AM)
        foot = t((f"Still needed: {', '.join(lacks)}. Type it below, such as paper: https://..." if lacks else
                  "Check it once. Enter adds it, on this machine only, and starts curating it." if entry.get("made_up") else
                  "Check it once. Enter adds it, signed with your key, and starts curating it.", AM if lacks else TE))
        self.query_one("#text", Static).update(Group(head, rows, foot))
        self.query_one("#from", Static).update(
            adding.table([("found from", f"[{MU}]{escape(' · '.join(self.sources))}[/]")], MU, AM) if self.sources else "")
        line = self.query_one("#line", Input)
        line.disabled = False
        self.query_one("#add", Button).disabled = bool(lacks)
        (self.query_one("#add", Button) if not lacks else line).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        said, event.input.value = event.value.strip(), ""
        if said and self.entry is not None:
            self.run_worker(self._correct(said), exclusive=True)

    async def _correct(self, said: str) -> None:
        from ..tasks import adding
        if adding.direct(self.s.ws, self.entry, said):
            self.query_one("#said", Static).update("")
            self._show(self.entry)
            return
        if self.s.simulate:
            self.query_one("#said", Static).update("Simulated: there is no agent to read that. Write it as field: value, such as paper: https://...")
            return
        self.query_one("#said", Static).update(t(("The worker agent looks again, with what you said ...", SE)))
        self.query_one("#line", Input).disabled = self.query_one("#add", Button).disabled = True
        try:
            entry, _ = await adding.refine(self.s.ws, self.s.ident, self.entry, said)
            self.query_one("#said", Static).update("")
        except adding.AddError as err:
            self.query_one("#said", Static).update(str(err))
            entry = self.entry
        self._show(entry)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        from ..tasks import adding
        event.stop()
        if event.button.id == "back" or self.entry is None:
            self.dismiss("back")
            return
        if adding.lacking(self.entry):
            return
        self.dismiss(("check", self.entry, self.sources))         # the benchmark-level checks, in their own box

    def action_back(self) -> None:
        self.dismiss("back")


def add(s, entry: dict, checked: list) -> dict:
    """Add a benchmark that passed its checks: signed onto the list (a made-up one is only registered here), with what
    its checks found recorded, so that a run does not ask them again."""
    from ..tasks import adding
    entry = adding.add_made_up(s.ws, entry) if entry.get("made_up") else adding.confirm(s.ws, s.ident, entry)
    record_checks(s, entry, checked, "pass")
    return entry


def record_checks(s, entry: dict, checked: list, outcome: str) -> None:
    from ..tasks import checks as checks_mod
    s.record(entry["id"], "decision", stage="benchmark", gate="checks", tier="code", value=checks_mod.summary(checked),
             outcome=outcome, checks={c.key: [c.status, c.detail[:240]] for c in checked}, digest_override=checks_mod.key(entry))


class ChecksScreen(Fits, ModalScreen[str]):
    """The benchmark-level checks of a benchmark being added (tasks/checks.py), asked one by one in a list that scrolls
    inside the box, each with what it found. Passed, the person continues: it is added and curated. Failed, the box says
    why it is rejected, and the person confirms: it is not added. Either way, back goes to the draft."""
    DEFAULT_CSS = CSS.format(name="ChecksScreen") + f"""
    ChecksScreen > Vertical {{ max-height: 94%; }}
    ChecksScreen #list {{ height: auto; border: round {BORDER}; border-title-color: {MU}; padding: 0 0 0 1; margin: 0 0 1 0; }}
    ChecksScreen #list VerticalScroll {{ height: auto; margin: 0; overflow-x: hidden; scrollbar-size: 0 1;
                                         overflow-y: scroll; }}
    ChecksScreen #rows {{ height: auto; padding: 0 1 0 0; }}
    ChecksScreen #verdict {{ height: auto; margin: 0 0 1 0; }}
    """
    BINDINGS = [("ctrl+z", "app.quit_run", "End"), ("ctrl+l", "app.leave", "Leave"), ("escape", "back", "Back to the draft"),
                ("up", "scroll(-1)", "Scroll"), ("down", "scroll(1)", "Scroll")]
    LOOK = {"pass": ("✓", TE), "warn": ("!", AM), "fail": ("✗", RD), "unknown": ("?", MU)}
    FLEX, SCROLL, CAP = "#list", "#scroll", 14

    def __init__(self, s, entry: dict, sources: list[str] | None = None):
        from ..tasks import checks as checks_mod
        super().__init__()
        self.s, self.entry, self.sources = s, entry, sources or []
        self.checks = checks_mod.planned(s.cfg, s.simulate)
        self.at = 0                 # the check being asked
        self.done = False

    def compose(self) -> ComposeResult:
        box = Vertical(Static(t((f"Checking {self.entry.get('name') or self.entry['id']}", "bold"), (f"   {self.entry['id']}", MU), "\n",
                                ("Cheap checks on the whole benchmark, before anything costly is spent on it: no trial is run, "
                                 "and no model is asked.", SE)), id="text"),
                       Vertical(VerticalScroll(Static(id="rows"), id="scroll", can_focus=False), id="list"),
                       Static(id="verdict"),
                       Button("Continue", id="go", disabled=True), Button("Back", id="back"))
        box.border_title = "checks"
        box.border_subtitle = QUIT_KEYS
        yield box

    def on_mount(self) -> None:
        self.set_interval(0.1, self._draw)
        self.run_worker(self._ask(), exclusive=True)

    @property
    def failed(self) -> bool:
        return any(c.failed for c in self.checks)

    async def _ask(self) -> None:
        from ..tasks import adding
        from ..tasks import checks as checks_mod
        source = adding.source_of(self.s.ws, self.entry)
        source = source if source.is_dir() else None
        for i, check in enumerate(self.checks):
            self.at = i
            await asyncio.to_thread(checks_mod.ask, check, self.entry, source, self.s.cfg)
            await asyncio.sleep(0.12)           # one at a time, so that each is seen as it is answered
        self.at, self.done = len(self.checks), True
        self._draw()
        self.query_one("#scroll", VerticalScroll).scroll_home(animate=False)     # from the top: the most telling come first
        go = self.query_one("#go", Button)
        if self.failed:
            self.query_one("#verdict", Static).update(t(("Rejected. ", f"bold {RD}"), (checks_mod.why(self.checks), RD), "\n",
                                                         ("Confirm, and it is not added.", SE)))
            go.label = "Confirm"
        else:
            self.query_one("#verdict", Static).update(t((f"No check rejects it ({checks_mod.summary(self.checks)}). ", f"bold {TE}"),
                                                         ("Continue, and it is added and curated.", TE)))
        go.disabled = False
        go.focus()

    def _draw(self) -> None:
        import time

        from rich.table import Table

        from .theme import spinner
        g = Table.grid(expand=True, padding=(0, 2, 0, 0))
        g.add_column(width=1, no_wrap=True)
        g.add_column(no_wrap=True)
        g.add_column(ratio=1)
        for i, c in enumerate(self.checks):
            if i < self.at or self.done:
                mark, color = self.LOOK[c.status]
                g.add_row(t((mark, f"bold {color}")), t((c.label, FG)), t((c.detail, color if c.status in ("fail", "warn") else SE)))
            elif i == self.at:
                g.add_row(t((spinner(time.time()), PU)), t((c.label, f"bold {PU}")), t(("checking ...", MU)))
            else:
                g.add_row(t(("·", MU)), t((c.label, MU)), "")
        self.query_one("#rows", Static).update(g)
        self.query_one("#list", Vertical).border_title = f"{min(self.at, len(self.checks))} of {len(self.checks)} checked"
        if not self.done:           # the check being asked stays in sight: the ones still to ask are below it
            self.query_one("#scroll", VerticalScroll).scroll_end(animate=False)

    def action_scroll(self, by: int) -> None:
        box = self.query_one("#scroll", VerticalScroll)
        box.scroll_to(y=box.scroll_y + by, animate=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        from ..tasks import adding
        event.stop()
        if event.button.id == "back":
            self.action_back()
        elif event.button.id == "go" and self.done and self.failed:
            record_checks(self.s, self.entry, self.checks, "reject")      # not added: not dropped for good either
            self.dismiss(f"rejected:{self.entry['id']}")
        elif event.button.id == "go" and self.done:
            try:
                entry = add(self.s, self.entry, self.checks)
            except adding.AddError as err:
                self.query_one("#verdict", Static).update(t(("It cannot be added. ", f"bold {RD}"), (str(err), RD)))
                return
            self.dismiss(f"added:{entry['id']}")

    def action_back(self) -> None:
        self.dismiss(("draft", self.entry, self.sources))


class CellsBox(ModalScreen[str]):
    """How much runs at once, asked when a benchmark's tasks are chosen: work cells (1 to 8), and containers."""
    DEFAULT_CSS = CSS.format(name="CellsBox") + """
    CellsBox Horizontal { height: auto; }
    CellsBox .label { width: 26; height: 3; content-align: left middle; color: #aaa89f; }
    CellsBox Input { width: 12; }
    """
    BINDINGS = [("ctrl+z", "app.quit_run", "End"), ("ctrl+l", "app.leave", "Leave")]

    def __init__(self, request, cells: int, containers: int):
        super().__init__()
        self.request, self.cells, self.containers = request, cells, containers

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal
        head, _, rest = self.request.text.partition("\n")
        box = Vertical(Static(t((head, "bold"), "\n", (rest, SE)), id="text"),
                       Horizontal(Static(f"work cells (1-{MAX_CELLS})", classes="label"), Input(str(self.cells), id="cells")),
                       Horizontal(Static("containers", classes="label"), Input(str(self.containers), id="containers")),
                       Static(id="said"), Button("Start curating", id="start"))
        box.border_title = "curate"
        box.border_subtitle = QUIT_KEYS
        yield box

    def on_mount(self) -> None:
        self.query_one("#start", Button).focus()

    def _start(self) -> None:
        try:
            cells, boxes = int(self.query_one("#cells", Input).value), int(self.query_one("#containers", Input).value)
        except ValueError:
            self.query_one("#said", Static).update("Both are whole numbers.")
            return
        if not 1 <= cells <= MAX_CELLS or boxes < 1:
            self.query_one("#said", Static).update(f"Work cells: 1 to {MAX_CELLS}. Containers: at least 1.")
            return
        self.dismiss(f"{cells},{max(boxes, cells)}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._start()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._start()


__all__ = ["CellsBox", "ChecksScreen", "DraftScreen", "PickScreen", "candidates", "taken_words"]
