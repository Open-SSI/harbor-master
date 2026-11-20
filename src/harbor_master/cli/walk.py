"""The four commands that work from any folder (DESIGN.md Section 11). hm init asks who you are in boxes, one
step at a time. hm new and hm open are plain commands: what they need is on their line."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import typer

from .. import ledger as ledger_mod
from ..core import config as config_mod
from ..core import identity as identity_mod
from ..tui.wizard import Step, ask
from .common import app, console, fail, need_identity
from .home import invoke

GITHUB = "https://github.com/"


def on_a_terminal() -> bool:
    return sys.stdout.isatty() and sys.stdin.isatty()


# ----- hm init -----

def init_steps(old: identity_mod.Identity | None) -> list[Step]:
    handle = lambda typed, _: "" if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", typed.lstrip("@")) else \
        "A GitHub handle is letters, digits and hyphens, such as octocat."      # noqa: E731
    mail = lambda typed, _: "" if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", typed) else "That does not look like an email address."  # noqa: E731
    kept = " This is what this machine has: change it, or press enter to keep it." if old else ""
    return [
        Step("name", "Your name", "As it should appear in the credits of an index." + kept, default=old.name if old else ""),
        Step("affiliation", "Your affiliation", "A university, a lab or a company." + kept, default=old.affiliation if old else ""),
        Step("email", "Your email", "It is listed with your name and affiliation in every index you contribute to, so that people can reach you."
             + kept, default=old.email if old else "", check=mail),
        Step("github", "Your GitHub handle", "It is how the ledger credits you, and how an index's owner adds you." + (
             kept + " If you change it, send each index's owner your new line, so that they add you under it." if old else ""),
             default=GITHUB + old.github if old else "", check=handle, prefix=GITHUB),
        Step("ok", "Create your signing key?", kind="note", done="Create it",
             text="A signing key is made on this machine. Every record you add to an index is signed with it, so nobody can "
                  "add one in your name. The private half never leaves this machine.") if old is None else
        Step("ok", "Save your details?", kind="note", done="Save",
             text="Your details are saved on this machine, and the signing key you already have is kept, so every record you "
                  "signed stays yours."),
    ]


def init_walk() -> None:
    """Who you are, and then the rest of getting started: an index, your keys, and a demo. Run again, it shows what this
    machine has in each box, to keep or change."""
    from .setup import said
    got = ask("hm init", init_steps(identity_mod.load()))
    if got is None:
        console.print("Nothing was changed.")
        return
    ident = identity_mod.create(str(got["name"]), str(got["affiliation"]), str(got["email"]), str(got["github"]).removeprefix(GITHUB).lstrip("@"))
    inside = config_mod.find_workspace()
    got = ask("getting started", start_steps(inside))
    said(ident)
    if got is None or (inside is None and "index" not in got):
        return
    ws = inside or get_index(str(got["index"]))
    console.print(f"Your index is in {ws}.", markup=False, highlight=False, soft_wrap=True)
    invoke([str(got["demo"]), "--demo", "-w", str(ws)])
    console.print(f"Next: cd {ws.name}. Then hm curate builds the index, and hm eval evaluates it once it is released. "
                  "Add --demo to either to try it simulated again.", markup=False, highlight=False)


PRACTICE = "hm-practice"


def start_steps(inside: Path | None) -> list[Step]:
    """The boxes after who you are. The same for everyone: an index, the keys file, and a demo. Cancel skips the demo."""
    from .setup import keys_file
    path = keys_file()
    index = [] if inside is not None else [Step(
        "index", "Join an index", optional=True, hint="a name from hm indexes, or the address of its repository",
        text="An index is a git repository, and joining it clones it into a folder here. Type its name on the list of indexes, or "
             f"the address of its repository.\n\nLeave it empty to practise first: an index of your own is made in {PRACTICE}, on this "
             "machine only and shared with nobody.")]
    return [
        *index,
        Step("keys", "Your keys", kind="note",
             text=f"A real run calls models and agents with your own keys. They are kept in one file, which git ignores:\n\n{path}\n\n"
                  "Open it in an editor and fill in the ones you have: the file says what each is for. Keys are never typed into "
                  "a screen. The demo is simulated and needs none, so this can wait."),
        Step("demo", "Try it, simulated", kind="choice", default="curate",
             text="An index is built, and then it is evaluated. Both can be tried on a made-up benchmark of 10 tasks, on simulated "
                  "models: it spends nothing, and nothing of it enters the index.",
             options=[("curate", "Curate: add the demo benchmark and run it through the stages"),
                      ("eval", "Evaluate: fetch the demo's to-do list and run its tasks")]),
    ]


def invoke_quiet(args: list[str]) -> int:
    with console.capture():
        return invoke(args)


def get_index(wanted: str) -> Path:
    """The index of the onboarding: the one that was named, cloned here, or a practice index on this machine."""
    from .setup import clone, index_new
    if wanted:
        here = Path(wanted).resolve()
        return here if (here / config_mod.PROTOCOL_NAME).is_file() else clone(wanted)
    ws = Path(PRACTICE).resolve()
    if not (ws / config_mod.PROTOCOL_NAME).is_file():
        with console.capture():         # what it says is for an owner setting an index up, not for someone trying one
            index_new("practice", ws, "Practice", "", "", "", trial=True)
    return ws


# ----- hm new -----

@app.command()
def new(name: str = typer.Argument(..., metavar="INDEX", help="The new index's short name: lower case, digits and hyphens. It names its folder."),
        tasks: str = typer.Option(..., help="Its task store: a Hugging Face dataset repository, as org/name. It is created, private. "
                                            "HF_TOKEN in your keys file must be able to write it."),
        repo: str = typer.Option(..., help="Its repository on GitHub, as owner/name. Create it there first, and leave it empty."),
        title: str = typer.Option("", help="Its full name."), description: str = typer.Option("", help="What it measures, and for whom.")):
    """Start an index of your own, as its owner: a folder here with the files that define it, shared through its repository."""
    from .setup import index_new
    inside = config_mod.find_workspace()
    if inside is not None:
        fail(f"This folder is inside the index at {inside}. An index is a folder of its own: cd out of it first.")
    on_github = re.fullmatch(r"[\w.-]+/[\w.-]+", repo.removeprefix(GITHUB).removesuffix(".git")) and not Path(repo).exists()
    path = repo.removeprefix(GITHUB).removesuffix(".git")
    index_new(name, None, title, description, tasks, f"git@github.com:{path}.git" if on_github else repo)
    ws = Path(name).resolve()
    if on_github and not ledger_mod.Sync(ws)._git("remote", "get-url", "origin", check=False).stdout.strip():
        from .setup import connect
        connect(ws, f"{GITHUB}{path}.git")      # no SSH key for GitHub here, perhaps: the same over HTTPS


# ----- hm open -----

@app.command(name="open")
def open_(index: str = typer.Argument("", metavar="[INDEX]", help="A name from hm indexes, or the address of the index's repository. "
                                      "Inside an index's folder, leave it out.")):
    """Clone an index into a folder here. Inside an index's folder, hm open alone shows its dashboard, to its owner and its leads:
    how far it is curated and evaluated, its protocol, its roster and its people. Read only."""
    from .setup import clone
    ident = need_identity()
    if index:
        ws = Path(index).resolve()
        if not (ws / config_mod.PROTOCOL_NAME).is_file():       # not a folder that is here already: get it
            ws = clone(index)
        console.print(f"It is in {ws}. Next: cd {ws.name}, then hm curate to build it, or hm eval once it is released.", markup=False, highlight=False)
        return
    ws = config_mod.find_workspace()
    if ws is None:
        fail("Name the index: hm open INDEX. hm indexes lists them. Inside an index's folder, hm open alone shows its dashboard.")
    me = next((c for c in ledger_mod.load_contributors(ws) if c.get("github") == ident.github), None)
    if not ledger_mod.can_cut(me):
        fail("The dashboard is for the index's owner and its leads. To contribute: hm curate builds the index, and hm eval evaluates it "
             "once it is released.")
    if not on_a_terminal():
        fail("The dashboard needs a terminal. What is left to evaluate, as text: hm todo")
    from ..tui.home import IndexApp
    IndexApp(ws).run()
