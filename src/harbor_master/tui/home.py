"""The home screen: where `hm` starts, before there is a run.

It needs nothing set up. A checklist shows the steps from a new machine to a signed verdict,
and a tip says what to do at the one you are at. Its "/" commands are the `hm` commands
themselves, run in place, with their output shown here. `/run` leaves for the run screen.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Callable

from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.suggester import Suggester
from textual.widgets import Input, Static

from .. import __version__
from ..core import config as config_mod
from ..core import identity as identity_mod
from ..interact import guide
from .theme import AM, BG, BORDER, FG, MU, PU, RD, SE, TE, t

Execute = Callable[[list[str], int], tuple[int, Text]]      # run `hm ARGS` at a width: exit code and what it printed

# name: (what follows it, what it does, the hm command it runs with -w, or "" when the screen does it itself)
COMMANDS: dict[str, tuple[str, str, str]] = {
    "init": ("", "Say who you are, and make your signing key", ""),
    "indexes": ("", "See the indexes that are listed", ""),
    "join": ("NAME-or-URL", "Join an index: it is cloned into a folder here", ""),
    "new": ("NAME", "Start an index of your own, as its owner", ""),
    "use": ("FOLDER", "Open an index that is in this folder", ""),
    "leave": ("", "Step out of this index, to pick another", ""),
    "demo": ("", "Try it on made-up tasks. Spends nothing", ""),
    "index": ("", "What this index is: owner, benchmarks, accepted tasks", "index"),
    "contributors": ("", "Who may contribute to this index", "contributors"),
    "key": ("", "Show your public key, to send to an index's owner", ""),
    "protocol": ("", "The settings a run uses, and what differs from the locked ones", "protocol"),
    "lock": ("", "Owner: lock the settings and the lists", ""),
    "connect": ("URL", "Owner: share this index through a git repository", "index connect"),
    "entry": ("", "Owner: print this index's entry for the list of indexes", "index entry"),
    "keys": ("", "Create the keys file, or say where it is", ""),
    "doctor": ("", "Which keys are found, and what this machine can run", "doctor"),
    "benchmarks": ("", "The benchmarks this index may curate", "benchmarks"),
    "submit": ("ID", "Fetch a benchmark at its pinned release and register it", "submit"),
    "run": ("[benchmarks] [--simulate]", "Open the run screen. --simulate spends nothing", ""),
    "status": ("", "Where every task stands", "status"),
    "leaderboard": ("", "How each agent configuration does on the accepted tasks", "leaderboard"),
    "todo": ("", "What is still to benchmark", "todo"),
    "roster": ("", "The agent configurations benchmarked on this index", "roster"),
    "credits": ("", "Contributions per person", "credits"),
    "claims": ("", "Which tasks someone is working on now", "claims"),
    "sync": ("", "Pull the shared index and push your records", "sync"),
    "verify": ("", "Check that the ledger is sound", "ledger verify"),
    "help": ("", "This list", ""),
    "quit": ("", "Leave", ""),
}
NEEDS_INDEX = {name for name, (_, _, cli) in COMMANDS.items() if cli} | {"lock", "run", "leave"}
FORM = (("name", "Your name"), ("affiliation", "Your affiliation, such as a university or a company"),
        ("email", "Your email. It is kept private"), ("github", "Your GitHub handle. It is how the ledger credits you"))
WELCOME = ("Harbor-Master curates benchmarks into an index: the tasks that are sound, verifiable, and still too hard "
           "for frontier agents.\n\nThe checklist above shows the steps, and where you are. The tip below it says what to "
           "do now. Type a / command at the prompt: tab completes it, and /help lists them all.")
KEYS = t(("/", SE), (" commands   ", MU), ("tab", SE), (" completes   ", MU), ("esc", SE), (" clears   ", MU),
         ("ctrl+q", SE), (" quit", MU))


class HomeCompleter(Suggester):
    def __init__(self, app: "HomeApp"):
        super().__init__(use_cache=False, case_sensitive=True)
        self.home = app

    async def get_suggestion(self, value: str) -> str | None:
        return next((c for c in self.home.completions(value) if c.startswith(value) and c != value), None)


class HomePrompt(Input):
    async def _on_key(self, event: events.Key) -> None:
        home: HomeApp = self.app  # type: ignore[assignment]
        if event.key == "tab" and self.value.startswith("/") and home.form is None:
            event.stop()
            event.prevent_default()
            options = home.completions(self.value)
            if options:
                at = options.index(self.value) if self.value in options else -1
                self.value = options[(at + 1) % len(options)]
                self.cursor_position = len(self.value)
            return
        if event.key == "escape":
            event.stop()
            home.cancel()
            return
        if self.value == "" and home.confirming and event.key in ("y", "n"):
            event.stop()
            event.prevent_default()
            await home.answer(event.key == "y")
            return
        await super()._on_key(event)


class HomeApp(App[tuple[list[str], Path] | None]):
    """Returns the arguments of a run to start and the index to start it in, or None to leave."""
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [("ctrl+q", "leave", "Quit")]
    CSS = f"""
    Screen {{ background: {BG}; color: {FG}; }}
    #top {{ height: 1fr; scrollbar-size: 0 0; }}
    #bottom {{ height: auto; dock: bottom; }}
    .panel {{ border: round {BORDER}; border-title-color: {MU}; padding: 0 1; height: auto; }}
    #tip {{ border: round {TE}; border-title-color: {TE}; }}
    #output {{ border: round {BORDER}; border-title-color: {MU}; border-subtitle-color: {MU}; padding: 0 1; height: auto; max-height: 100%; }}
    HomePrompt {{ border: round {PU}; background: {BG}; height: 3; padding: 0 1; }}
    HomePrompt:focus {{ border: round {PU}; background-tint: {BG} 0%; }}
    HomePrompt > .input--suggestion {{ color: {MU}; }}
    #keys {{ height: 1; padding: 0 2; }}
    """

    def __init__(self, execute: Execute, folder: Path, ws: Path | None = None, note: tuple[str, Text] | None = None):
        super().__init__()
        self.execute, self.folder = execute, folder.resolve()
        self.ws = ws or config_mod.find_workspace(self.folder)
        self.note = note                    # what the run just left printed, shown on coming back
        self.state = guide.look(self.ws or self.folder)
        self.form: list[str] | None = None  # answers so far, while /init asks its questions
        self.confirming: list[str] | None = None
        self.busy = False
        self.last = ("", "", False)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="top"):
            yield Static(id="header", classes="panel")
            yield Static(id="steps", classes="panel")
            yield Static(id="tip", classes="panel")
            yield Static(id="output")
        with Vertical(id="bottom"):
            yield HomePrompt(placeholder="Type a / command", id="prompt", suggester=HomeCompleter(self))
            yield Static(KEYS, id="keys")

    def on_mount(self) -> None:
        os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")       # a clone must fail, not ask for a password under the screen
        self.query_one("#steps").border_title = "getting started"
        self.query_one("#prompt").focus()
        if self.note:
            self.show(self.note[0], self.note[1])
        else:
            self.show("welcome", Text(WELCOME))
        self.draw()

    # ----- drawing -----

    def look(self) -> None:
        self.state = guide.look(self.ws or self.folder)
        self.draw()

    def draw(self) -> None:
        s = self.state
        g = Table.grid(expand=True)
        g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        g.add_column(justify="right", no_wrap=True)
        g.add_row(t(("harbor-master", f"bold {PU}"), (f" v{__version__}", MU)),
                  t(s.ident.name, (f" (@{s.ident.github})", MU)) if s.ident else t(("not set up yet", AM)))
        g.add_row(t(("index  ", MU), s.title, (f"   {s.ws}", MU)) if s.ws else t(("index  ", MU), ("none yet", AM), (f"   in {s.folder}", MU)),
                  t(("owner", TE)) if s.is_owner else t(""))
        self.query_one("#header", Static).update(g)

        rows = Table.grid(padding=(0, 2, 0, 0))
        for _ in range(3):
            rows.add_column(no_wrap=True)
        rows.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        mark = {"done": ("✓", TE), "next": ("→", AM), "later": ("·", MU)}
        for n, (name, status, detail) in enumerate(guide.steps(s), 1):
            rows.add_row(t(mark[status]), t((str(n), MU)), t((name, "bold" if status == "next" else FG if status == "done" else MU)),
                         t((detail, SE if status != "later" else MU)))
        self.query_one("#steps", Static).update(rows)

        box = self.query_one("#tip", Static)
        if self.form is not None:
            label, question = FORM[len(self.form)]
            box.border_title = f"question {len(self.form) + 1} of {len(FORM)}"
            old = getattr(s.ident, label, "") if s.ident else ""
            box.update(t((question, "bold"), "\n", (f"Type it and press enter. {'Enter alone keeps: ' + old + '. ' if old else ''}esc cancels.", SE)))
        elif self.confirming is not None:
            box.border_title = "confirm"
            box.update(t(("hm " + " ".join(self.confirming[:2]), "bold"), ("   signs with your key, and every run on this index must then use these settings.\n", SE),
                         "Run it?  ", ("[y]", f"bold {TE}"), " yes   ", ("[n]", f"bold {RD}"), " no"))
        else:
            advice = guide.tip(s)
            lines = Table.grid(padding=(0, 2, 0, 0), expand=True)
            lines.add_column(no_wrap=True)
            lines.add_column(ratio=1)
            for command, what in advice.lines:
                lines.add_row(t((command, f"bold {PU}")), t((what, FG if command else SE)))
            box.border_title = advice.title
            box.update(lines)
        self.query_one("#prompt", Input).placeholder = (FORM[len(self.form)][1] if self.form is not None
                                                        else "Type a / command. The tip above says which")

    def show(self, title: str, body: Text | str, failed: bool = False) -> None:
        self.last = (title, body.plain if isinstance(body, Text) else body, failed)      # what is on screen, for tests
        box = self.query_one("#output", Static)
        box.border_title = title
        box.border_subtitle = "did not work" if failed else ""
        box.styles.border = ("round", RD if failed else BORDER)
        box.update(body if isinstance(body, Text) else Text(body))
        self.query_one("#top", VerticalScroll).scroll_end(animate=False)

    # ----- what can be typed -----

    def completions(self, value: str) -> list[str]:
        """The lines a half-typed command could become: its name, then what can follow it."""
        if not value.startswith("/"):
            return []
        head, space, rest = value[1:].partition(" ")
        if not space:
            names = [n for n in COMMANDS if n.startswith(head) and (self.ws is not None or n not in NEEDS_INDEX)]
            first = guide.tip(self.state).lines
            wanted = [c.split()[0][1:] for c, _ in first if c]
            names.sort(key=lambda n: wanted.index(n) if n in wanted else len(wanted))        # what the tip names comes first
            return ["/" + n + (" " if COMMANDS[n][0] else "") for n in names]
        s = self.state
        words = {"use": s.nearby, "submit": s.listed, "run": [*s.registered, "--simulate"]}.get(head, [])
        done = rest.split()[:-1] if rest and not rest.endswith(" ") else rest.split()
        last = "" if rest.endswith(" ") or not rest else rest.split()[-1]
        return [" ".join(["/" + head, *done, w]) for w in words if w.startswith(last) and w not in done]

    def cancel(self) -> None:
        if self.form is not None or self.confirming is not None:
            self.form = self.confirming = None
            self.show("cancelled", "Nothing was changed.")
            self.draw()
        self.query_one("#prompt", Input).value = ""

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if self.busy:
            return
        if self.form is not None:
            await self.fill(text)
        elif self.confirming is not None and text.lower() in ("y", "yes", "n", "no"):
            await self.answer(text.lower().startswith("y"))
        elif text:
            await self.command(text)

    async def fill(self, text: str) -> None:
        """One answer of /init. After the last one, the identity is written."""
        label = FORM[len(self.form)][0]
        old = getattr(self.state.ident, label, "") if self.state.ident else ""
        if not (text or old):
            return
        self.form.append(text or old)
        if len(self.form) < len(FORM):
            self.draw()
            return
        answers, self.form = self.form, None
        await self.call(["init", *[x for (label, _), value in zip(FORM, answers) for x in (f"--{label}", value)]], "hm init")

    async def answer(self, yes: bool) -> None:
        args, self.confirming = self.confirming, None
        if yes and args:
            await self.call(args)
        else:
            self.show("declined", "Nothing was changed.")
            self.draw()

    async def command(self, text: str) -> None:
        if not text.startswith("/"):
            self.show("not a command", "Here, type a / command: the tip above names the ones for this step, and /help lists them all.\n"
                      "Questions in plain words are answered on the run screen, once a run is open: /run", failed=True)
            return
        name, *args = text[1:].split()
        if name not in COMMANDS:
            close = [n for n in COMMANDS if n.startswith(name[:2])]
            self.show("no such command", f"/{name} is not a command here." + (f" Did you mean /{' or /'.join(close[:3])}?" if close else "")
                      + " /help lists them all.", failed=True)
            return
        s = self.state
        if s.ident is None and name not in ("init", "help", "quit", "indexes", "keys"):
            self.show("first, who you are", "That needs your identity, because what you do is signed. Start with /init.", failed=True)
            return
        if self.ws is None and name in NEEDS_INDEX:
            self.show("first, an index", f"/{name} works inside an index. Join one with /join NAME, start one with /new NAME, "
                      "or try made-up tasks with /demo.", failed=True)
            return
        here = ["-w", str(self.ws)] if self.ws else []
        if name == "help":
            self.show("commands", self.help())
        elif name == "quit":
            self.exit(None)
        elif name == "init":
            self.form = []
            self.draw()
        elif name == "key":
            self.show("your public key", f"{s.ident.public_key}\n\nSend this line to the owner of an index. They add it to the index's "
                      "contributors.toml with your GitHub handle and the fields you may curate, and lock the change. "
                      "The private half never leaves this machine.")
        elif name == "use":
            target = (self.folder / args[0]).resolve() if args else None
            if target is None or not (target / config_mod.PROTOCOL_NAME).exists():
                self.show("not an index", f"{'That folder' if args else 'Name a folder: it'} must hold an index. Here: "
                          f"{', '.join(s.nearby) or 'none yet'}.", failed=True)
                return
            self.ws = target
            await self.call(["index", "-w", str(target)], "hm index")
        elif name == "leave":
            self.ws = None
            self.show("left", f"Back in {self.folder}. Pick an index with /use, /join or /new.")
            self.look()
        elif name == "run":
            if not s.registered:
                self.show("nothing to run", "No benchmark is registered here yet. Fetch one with /submit ID, or add a made-up one with /demo.", failed=True)
                return
            self.exit((args, self.ws))
        elif name == "lock":
            self.confirming = ["protocol", "lock", *here]
            self.draw()
        elif name == "keys":
            path = s.keys_file or identity_mod.config_dir() / "secrets.env"
            if path.exists():
                self.show("keys", f"The keys file is {path}. Fill in the keys you have with an editor, then check with /doctor.\n"
                          "Keys are never typed into this screen.")
            else:
                await self.call(["keys", "init"])
        elif name == "new":
            if not args:
                self.show("a name is needed", "/new NAME, for example: /new my-index", failed=True)
                return
            await self.call(["index", "new", args[0], str(self.folder / args[0]), *args[1:]], enter=True)
        elif name in ("join", "demo") and not (name == "demo" and self.ws):
            await self.call([name, *args], enter=True)
        elif name == "indexes":
            await self.call(["indexes", *args])
        else:
            await self.call([*(COMMANDS[name][2] or name).split(), *args, *here])

    async def call(self, args: list[str], title: str = "", enter: bool = False) -> None:
        """Run an hm command and show what it printed. With `enter`, step into the index it made."""
        title = title or "hm " + " ".join(a for a in args if a not in ("-w", str(self.ws)))
        before = set(guide.look(self.folder).nearby) if enter else set()
        self.busy = True
        self.show(title, Text("working…", style=MU))
        try:
            code, out = await asyncio.to_thread(self.execute, args, max(40, self.size.width - 6))
        finally:
            self.busy = False
        if enter and code == 0:
            new = sorted(set(guide.look(self.folder).nearby) - before)
            if new:
                self.ws = self.folder / new[0]
        self.show(title, out if out.plain.strip() else Text("Done."), failed=code != 0)
        self.look()

    def help(self) -> Text:
        out = Text()
        for name, (takes, what, _) in COMMANDS.items():
            usable = self.ws is not None or name not in NEEDS_INDEX
            out.append(f"/{name} {takes}".ljust(34), style=f"bold {PU}" if usable else MU)
            out.append(what + ("" if usable else "   (inside an index)") + "\n", style=FG if usable else MU)
        out.append("\nEvery one is also an hm command in a shell, such as: " + guide.shell("/run --simulate"), style=SE)
        return out

    def action_leave(self) -> None:
        self.exit(None)
