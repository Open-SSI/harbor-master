"""What every `hm` command shares: the app, the console, and the checks before a command runs."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from .. import __version__
from .. import ledger as ledger_mod
from .. import templates
from ..core import config as config_mod
from ..core import identity as identity_mod
from ..tui import theme

app = typer.Typer(no_args_is_help=False, add_completion=False, context_settings={"help_option_names": ["-h", "--help"]},
                  help="Harbor-Master: curate benchmarks into an index of hard, verified tasks.\n\n"
                       "Start with hm init. hm open INDEX clones an index. Inside its folder, hm curate builds it and hm eval "
                       "evaluates it once it is released: each opens a screen that asks what it needs in boxes.\n\n"
                       "Every other command is about one index, and is typed inside that index's folder.")
console = Console()
Workspace = typer.Option(None, "--workspace", "-w", help="The index's folder. Default: the index this folder is in.")


def fail(message: str) -> None:
    console.print(f"[{theme.RD}]{message}[/]")
    raise typer.Exit(1)


def need_identity() -> identity_mod.Identity:
    ident = identity_mod.load()
    if ident is None:
        fail("First say who you are: hm init")
    return ident


def need_workspace(workspace: Optional[Path], say: bool = False) -> Path:
    """The index a command works on: the folder named with -w or HM_WORKSPACE, else the index this folder is inside.

    With `say`, the command first prints which index that is, so that its figures are never read as
    another index's."""
    ws = workspace or (Path(os.environ["HM_WORKSPACE"]) if os.environ.get("HM_WORKSPACE") else None)
    if ws is not None and not (ws / config_mod.PROTOCOL_NAME).exists():
        fail(f"{ws} is not an index: it holds no {config_mod.PROTOCOL_NAME}.")
    if ws is None:
        ws = config_mod.find_workspace()
        if ws is None:
            fail("This folder is not inside an index. cd into an index's folder first. To get one: hm open INDEX.")
    ws = ws.resolve()
    check_owner(ws)
    if say:
        about = config_mod.load(ws).about
        console.print(f"[{theme.MU}]Index: {about.title or about.name or ws.name}   {ws}[/]", highlight=False, soft_wrap=True)
    return ws


def set_up_local(ws: Path) -> None:
    """This machine's own folder in a clone: its settings, and a note to git never to share it."""
    local = config_mod.local(ws)
    local.mkdir(exist_ok=True)
    if not (local / config_mod.CONFIG_NAME).exists():
        (local / config_mod.CONFIG_NAME).write_text(templates.read("hm.toml"))
    exclude = ws / ".git" / "info" / "exclude"
    if exclude.parent.exists() and f"{config_mod.LOCAL}/" not in (exclude.read_text() if exclude.exists() else ""):
        with exclude.open("a") as f:
            f.write(f"{config_mod.LOCAL}/\nsecrets.env\n")


def check_owner(ws: Path) -> None:
    """Remember the owner's key the first time this machine sees the index, and refuse a different one later.

    The owner's signature is what makes the protocol and the contributor list trusted, so a
    changed owner in a pulled copy must never pass unnoticed.
    """
    now = "\n".join(ledger_mod.owner_keys(ledger_mod.load_contributors(ws)))
    pin = config_mod.local(ws) / "owner"
    if not pin.exists():
        if now:
            set_up_local(ws)
            pin.write_text(now + "\n")
        return
    if pin.read_text().strip() != now:
        fail("Refused: the owner on this index's contributor list is not the one this workspace joined under.\n"
             f"  joined under: {pin.read_text().strip() or '(none)'}\n  listed now:   {now or '(none)'}\n"
             f"If the owner really changed, check with them, then remove {pin} and run again.")


def is_owner(ws: Path, ident: identity_mod.Identity) -> bool:
    return any(c.get("github") == ident.github and c.get("role") == "owner" and c.get("key")
               for c in ledger_mod.load_contributors(ws))


def is_contributor(ws: Path, ident: identity_mod.Identity) -> bool:
    return any(c.get("github") == ident.github and ledger_mod.can_contribute(c) for c in ledger_mod.load_contributors(ws))


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


def check_lock(lock: dict | None, cfg, ws: Path) -> None:
    """Refuse to go on when this tool is older than the index asks for, or when this copy's protocol is not the one
    the owner locked."""
    from .. import skills
    need = max(cfg.requires.harbor_master, (lock or {}).get("requires", "0"), key=version_tuple)
    if version_tuple(__version__) < version_tuple(need):
        fail(f"Refused: this index needs Harbor-Master {need} or newer, and this is {__version__}. Update it, then run again.")
    wrong = skills.problems(ws / "skills")
    if wrong:
        for p in wrong[:12]:
            console.print(f"  {p}")
        fail("Refused: the code cannot use these skills as they are.")
    from_owner = ledger_mod.list_problem(ws, ledger_mod.Ledger(ws).events)
    if from_owner:
        fail(f"Refused: {from_owner}. Put it back with: git checkout origin/main -- {ledger_mod.CONTRIBUTORS}")
    if lock is None:
        return
    absent = skills.missing(ws / "skills")
    if absent:
        fail(f"Refused: this index has no copy of {', '.join('skills/' + n for n in absent)}, so what agents are told would "
             "depend on the tool that is installed. Put the files back (git checkout -- skills), or have the owner run hm lock, which copies them in.")
    if lock["protocol"] != cfg.protocol:
        console.print(f"[{theme.RD}]This workspace's settings ({cfg.protocol}) are not the locked protocol ({lock['protocol']}).[/]")
        for line in ledger_mod.protocol.differences(lock.get("settings", {}), cfg.protocol_settings())[:12]:
            console.print(f"  {line}")
        fail("Refused. Put the settings back, or have the owner lock the new ones: hm lock")

