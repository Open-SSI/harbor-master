"""Keeping an index in step with its repository, with no command for it.

Every command inside an index starts here: it pulls what others pushed, the owner's edits to the files that define
the index (about.toml, protocol.toml, skills/, README.md) and to the contributor list are signed and shared, and this
machine's records that did not get pushed are pushed. The ledger is checked before anything is pushed. The owner's
task store is made when about.toml names one that is not there yet, and accepted tasks that did not reach it are
put there. Nothing is printed unless something needs saying.
"""
from __future__ import annotations

from pathlib import Path

from .. import ledger as ledger_mod
from ..core import config as config_mod
from ..core import identity as identity_mod
from ..tui import theme

OWNED = ("about.toml", "README.md", ".gitignore", "protocol.toml", "skills", "hm.example.toml", "secrets.env.example")


def catch_up(ws: Path) -> None:
    from .common import console, is_owner
    ident = identity_mod.load()
    if ident is None:           # the command itself says to run hm init
        return
    owner = is_owner(ws, ident)
    s = ledger_mod.Sync(ws, ident.name, ident.github)
    if s.enabled:
        try:
            s.pull()
            if owner:
                signed = [e for e in ledger_mod.Ledger(ws).events if e["type"] == "contributors"]
                if not signed or max(signed, key=lambda e: e["time"]).get("value") != ledger_mod.list_digest(ws):
                    ledger_mod.sign_list(ws, ident, config_mod.load(ws).protocol)     # the owner's own edit of the list
                    s.define("sign the contributor list", (ledger_mod.CONTRIBUTORS,))
            problems = ledger_mod.verify(ws)
            if problems:
                console.print(f"[{theme.RD}]Nothing was shared: the ledger has {len(problems)} "
                              f"problem{'s' if len(problems) != 1 else ''}.[/] {problems[0]}", highlight=False)
            else:
                if owner:
                    s.define("update the index", only=OWNED)
                s._commit("local records")
                if s.where().get("unpushed") != "0":     # "" when the repository has nothing yet: this starts it
                    s.publish(f"records of @{ident.github}")
        except ledger_mod.SyncError as err:
            console.print(f"[{theme.AM}]Not in step with the index's repository:[/] {err}", highlight=False)
    _store(ws, ident, owner)


def _store(ws: Path, ident, owner: bool) -> None:
    """The task store about.toml names is made by the owner's first command after it is named; accepted tasks that did
    not reach it are put there by the next command of whoever accepted them."""
    from ..tasks import store
    from .common import console
    cfg = config_mod.load(ws)
    if owner and (cfg.about.tasks or "").strip() and store.not_ready(cfg):
        from .store import make_store
        make_store(cfg)
    for line in store.retry(ws, cfg):
        console.print(line, highlight=False)
