"""Sharing an index and locking it: hm sync, and hm lock."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .. import ledger as ledger_mod
from ..core import config as config_mod
from ..tui import theme
from .common import Workspace, app, console, fail, is_owner, need_identity, need_workspace


@app.command()
def sync(workspace: Optional[Path] = Workspace):
    """Bring in what others added to this index, and share your new records, and any accepted task that did not reach the
    task store. Its owner's edits to about.toml and roster.toml are shared too."""
    ws = need_workspace(workspace, say=True)
    ident = need_identity()
    ledger_dir = ws
    s = ledger_mod.Sync(ledger_dir, ident.name, ident.github)
    if not s.enabled:
        fail("This index is not shared yet, so there is nothing to pull or push: its records are only in this folder. "
             "Its owner shares it from inside its folder: git remote add origin URL, then hm sync.")
    try:
        s.pull()
        problems = ledger_mod.verify(ledger_dir)
        if problems:
            for p in problems[:20]:
                console.print(f"[{theme.RD}]x[/] {p}")
            fail(f"Not pushed: the ledger has {len(problems)} problem{'s' if len(problems) != 1 else ''}.")
        if is_owner(ws, ident):         # the description and the roster are the owner's to change at any time
            s.define("describe the index", only=("about.toml", "README.md", ".gitignore", "roster.toml"))
        s.publish(f"sync by @{ident.github}")
    except ledger_mod.SyncError as err:
        fail(str(err))
    from ..tasks import store
    for line in store.retry(ws, config_mod.load(ws)):       # accepted tasks that did not reach the store when they were accepted
        console.print(line, highlight=False)
    console.print(f"[{theme.TE}]Synced.[/] {len(ledger_mod.Ledger(ledger_dir).events)} events in the shared ledger.")


@app.command("lock")
def lock(bump: str = typer.Argument("", metavar="[patch | minor]", help="Give this lock a new version: patch (0.0.1 to 0.0.2) or "
                                    "minor (0.1.0 to 0.2.0). Default: the version stays."),
         note: str = typer.Option("", help="Why the protocol is being set or changed."), workspace: Optional[Path] = Workspace):
    """Owner only: lock this index's settings and skills as its folder has them now. Every run must then use them.
    The roster is not locked: its owner edits roster.toml at any time and shares it with hm sync."""
    lock_protocol(need_workspace(workspace), note, bump)


def lock_protocol(ws: Path, note: str = "", bump: str = "") -> None:
    """The owner's lock: the settings and the skills, signed. Skills the index has no copy of are copied in from the templates."""
    ident = need_identity()
    cfg = config_mod.load(ws)
    try:
        ledger_mod.protocol.bumped(ledger_mod.protocol.START, bump)
    except ValueError as err:
        fail(str(err))
    if not is_owner(ws, ident):
        fail(f"Only the owner can lock the protocol, and @{ident.github} is not the owner on the contributor list.")
    sync = ledger_mod.Sync(ws, ident.name, ident.github)
    if sync.enabled:
        sync.pull()
    from .. import skills
    absent = skills.missing(ws / "skills")
    if absent:          # what agents are told must be the index's own, not whatever the installed tool has
        skills.adopt(ws / "skills", absent)
        console.print(f"Copied {len(absent)} skill{'s' if len(absent) != 1 else ''} the index had no copy of from the templates: "
                      + ", ".join(absent), highlight=False)
    wrong = skills.problems(ws / "skills")
    if wrong:
        for p in wrong[:12]:
            console.print(f"  {p}")
        fail("Not locked: the code cannot use these skills as they are.")
    from ..tasks import store
    if (cfg.about.tasks or "").strip() and store.not_ready(cfg):      # a real run needs the store, so it is made with the lock
        from .store import make_store
        make_store(cfg)
    events = ledger_mod.Ledger(ws).events
    people = [e for e in events if e["type"] == "contributors"]
    if not people or max(people, key=lambda e: e["time"]).get("value") != ledger_mod.list_digest(ws):
        ledger_mod.sign_list(ws, ident, cfg.protocol)       # the owner's own edit of the list: signed here, with no new version
        sync.define("sign the contributor list", (ledger_mod.CONTRIBUTORS,))
        if sync.enabled:
            sync.publish("sign the contributor list")
        console.print("Signed the contributor list as it is here.")
    lock = ledger_mod.protocol.current(events)
    requires = cfg.requires.harbor_master
    if not bump and lock is not None and (lock["protocol"], lock.get("requires", requires)) == (cfg.protocol, requires) and not absent:
        console.print(f"Protocol v{ledger_mod.protocol.version(lock)} ({cfg.protocol}) is already the locked one.")
        return
    made = ledger_mod.protocol.write(ws, ident, cfg, note, bump=bump)
    sync.define(f"lock protocol {cfg.protocol}")        # the files travel with the lock that covers them
    if sync.enabled:
        sync.publish(f"lock protocol {cfg.protocol}")
    same = lock is not None and lock["protocol"] == cfg.protocol
    console.print(f"[{theme.TE}]Locked protocol v{made['version']}[/] ({cfg.protocol}). " + (
        "The settings are the same as before, so earlier records stand." if same else
        "Every run on this index must now use these settings."
        + (f" It replaces {lock['protocol']}: tasks finished under that one keep their records, and may be "
           "run again under the new one." if lock else "")))
    if lock is not None and not bump:
        console.print(f"[{theme.MU}]The version stays v{made['version']}. To give this change a version of its own: "
                      + ("hm lock patch" if same else "hm lock minor") + "[/]")
