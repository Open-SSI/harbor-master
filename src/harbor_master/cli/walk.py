"""hm init, hm new and hm clone, which work from any folder (DESIGN.md Section 10), and hm view, the dashboard. hm init
asks in boxes, one step at a time. The others are plain commands: what they need is on their line."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

import typer

from .. import ledger as ledger_mod
from ..core import config as config_mod
from ..core import identity as identity_mod
from ..tui.wizard import Step, ask

from .common import Workspace, app, console, fail, need_identity, need_workspace

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


DEMO = "demo-index"
START = "\n".join(f"{c:<22} {w}".rstrip() for c, w in (
    (f"hm clone {DEMO}", "an index in a folder here, with you as its owner"), (f"cd {DEMO}", ""),
    ("hm curate --demo", "curate a made-up benchmark, stage by stage"), ("hm view --demo", "the dashboard of the index")))


def start_step() -> Step:
    """The last box: how to start, on the demo index. Nothing to answer."""
    return Step("start", "Start with an index", kind="note", done="Done",
                text="Try the whole of it on demo-index, a made-up index that comes with Harbor-Master. Everything in it is simulated: "
                     f"it spends nothing and needs no keys.\n\n{START}\n\nFor a real index: hm clone INDEX, or hm new to start your own.")


def init_walk() -> None:
    """Who you are and your signing key, then how to start. Run again, it shows what this machine has in each box, to keep
    or change."""
    from .setup import said
    got = ask("hm init", [*init_steps(identity_mod.load()), start_step()])
    if got is None:
        console.print("Nothing was changed.")
        return
    said(identity_mod.create(str(got["name"]), str(got["affiliation"]), str(got["email"]), str(got["github"]).removeprefix(GITHUB).lstrip("@")))
    console.print(f"Start with:\n{START}", markup=False, highlight=False)


# ----- hm new -----

@app.command()
def new(name: str = typer.Argument(..., metavar="INDEX", help="The new index's short name: lower case, digits and hyphens. It names its folder."),
        tasks: str = typer.Option(..., help="Its task store: a Hugging Face dataset repository, as org/name. It is created, private. "
                                            "HF_TOKEN in your keys file must be able to write it."),
        repo: str = typer.Option(..., help="Its repository on GitHub, as owner/name. Create it there first, and leave it empty."),
        title: str = typer.Option("", help="Its full name."), description: str = typer.Option("", help="What it measures, and for whom.")):
    """Start an index of your own, as its owner: a folder here with the files that define it, shared through its repository."""
    from .setup import index_new, repo_problem, store_problem
    need_identity()
    inside = config_mod.find_workspace()
    if inside is not None:
        fail(f"This folder is inside the index at {inside}. An index is a folder of its own: cd out of it first.")
    if Path(name).exists() and any(Path(name).iterdir()):
        fail(f"{Path(name).resolve()} is not empty.")
    on_github = re.fullmatch(r"[\w.-]+/[\w.-]+", repo.removeprefix(GITHUB).removesuffix(".git")) and not Path(repo).exists()
    path = repo.removeprefix(GITHUB).removesuffix(".git")
    # Both are checked before anything is written: the store this person must write to, and the repository they must push
    # to, over SSH first and then HTTPS, with their own git credentials. Nothing is made unless both work.
    console.print("Checking the task store and the repository ...", highlight=False)
    problems = [p for p in (store_problem(tasks),) if p]
    address, why = repo_problem([f"git@github.com:{path}.git", f"{GITHUB}{path}.git"] if on_github else [repo])
    if why:
        unreachable = on_github and "is not empty" not in why
        problems.append(why + ("" if not unreachable else f". Create it at github.com/new as {path}, empty, and make sure "
                                                           "this machine's git can push to it (an SSH key, or HTTPS credentials)."))
    if problems:
        fail("Nothing was made:\n" + "\n".join(f"- {p}" for p in problems))
    index_new(name, None, title, description, tasks, address)


# ----- hm clone, and hm view -----

@app.command(name="clone")
def clone_(index: str = typer.Argument(..., metavar="INDEX", help="A name from hm indexes, or the address of the index's repository.")):
    """Clone an index into a folder here, and check that it is one. demo-index is a made-up one to try Harbor-Master on."""
    from .setup import clone
    ws = clone(index)
    console.print(f"Next: cd {ws.name}, then " + ("hm curate --demo, then hm view --demo." if ws.name == DEMO
                  else "hm curate to curate a benchmark of it."), markup=False, highlight=False)


@app.command()
def view(demo: bool = typer.Option(False, help="Show the records of the simulated demos on this machine. Anyone may look at those."),
         workspace: Optional[Path] = Workspace):
    """The index's dashboard, for its owner and its leads: how far each benchmark is curated, its settings and its people.
    Read only."""
    ident, ws = need_identity(), need_workspace(workspace)
    me = next((c for c in ledger_mod.load_contributors(ws) if c.get("github") == ident.github), None)
    if not demo and not ledger_mod.can_cut(me):
        fail("The dashboard is for the index's owner and its leads. To contribute: hm curate. "
             "hm view --demo shows the dashboard of the simulated demo.")
    if not on_a_terminal():
        fail("The dashboard needs a terminal. Where each benchmark stands, as text: hm benchmarks")
    if demo:                # the made-up people's part of the demo, made first where it has not run yet
        from .home import demo_bench, demo_world
        with console.capture():
            demo_world(ws)
            demo_bench(ws)
    from ..tui.home import IndexApp
    IndexApp(ws, demo=demo).run()
