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

app = typer.Typer(no_args_is_help=False, add_completion=False,
                  help="Harbor-Master: curate benchmarks into an index of hard, verified tasks.")
keys_app = typer.Typer(no_args_is_help=True, help="API keys, kept in a git-ignored file.")
ledger_app = typer.Typer(no_args_is_help=True, help="The ledger of decisions, trials and verdicts.")
dev_app = typer.Typer(no_args_is_help=True, help="Tools for developing Harbor-Master itself.")
index_app = typer.Typer(invoke_without_command=True, help="The index this workspace curates: what it is, and its accepted tasks.")
app.add_typer(index_app, name="index")
app.add_typer(keys_app, name="keys")
app.add_typer(ledger_app, name="ledger")
app.add_typer(dev_app, name="dev")
console = Console()
Workspace = typer.Option(None, "--workspace", "-w", help="The clone of the index to work in. Default: found from here upward.")


def fail(message: str) -> None:
    console.print(f"[{theme.RD}]{message}[/]")
    raise typer.Exit(1)


def need_identity() -> identity_mod.Identity:
    ident = identity_mod.load()
    if ident is None:
        fail("Set up who you are first: hm init")
    return ident


def need_workspace(workspace: Optional[Path]) -> Path:
    ws = workspace or (Path(os.environ["HM_WORKSPACE"]) if os.environ.get("HM_WORKSPACE") else None) \
        or config_mod.find_workspace()
    if ws is None or not (ws / config_mod.PROTOCOL_NAME).exists():
        fail("No index here. Join one with: hm join URL. Or start your own with: hm index new NAME")
    ws = ws.resolve()
    check_owner(ws)
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


Simulated = typer.Option(False, "--simulated", help="Read the records of simulated runs on this machine, not the index's ledger.")


def records(ws: Path, simulated: bool) -> Path:
    """Where to read records from: the index's ledger, or the folder where simulated runs keep theirs."""
    if simulated:
        console.print(f"[{theme.AM}]Simulated.[/] These are the records of simulated runs on this machine. None of it is in the index.")
    return config_mod.simulated(ws) if simulated else ws


def mention_simulated(ws: Path, simulated: bool) -> None:
    """After a view of the real ledger, say that simulated runs left records too, and how to see them."""
    n = len(ledger_mod.shard_paths(config_mod.simulated(ws)))
    if n and not simulated:
        console.print(f"[{theme.MU}]Simulated runs on this machine left records too. They are kept apart: add --simulated to see them.[/]")


def is_owner(ws: Path, ident: identity_mod.Identity) -> bool:
    return any(c.get("github") == ident.github and c.get("role") == "owner" and c.get("key")
               for c in ledger_mod.load_contributors(ws))


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


def check_lock(lock: dict | None, cfg, ws: Path) -> None:
    """Refuse to go on when this tool is older than the index asks for, or when this copy's protocol or
    locked files are not the ones the owner locked."""
    from .. import skills
    need = max(cfg.requires.harbor_master, (lock or {}).get("requires", "0"), key=version_tuple)
    if version_tuple(__version__) < version_tuple(need):
        fail(f"Refused: this index needs Harbor-Master {need} or newer, and this is {__version__}. Update it, then run again.")
    wrong = skills.problems(ws / "skills")
    if wrong:
        for p in wrong[:12]:
            console.print(f"  {p}")
        fail("Refused: the code cannot use these skills as they are. See: hm skills")
    if lock is None:
        return
    absent = skills.missing(ws / "skills")
    if absent:
        fail(f"Refused: this index has no copy of {', '.join('skills/' + n for n in absent)}, so what agents are told would "
             "depend on the tool that is installed. Put the files back (git checkout -- skills), or have the owner copy them: hm skills adopt")
    if lock["protocol"] != cfg.protocol:
        console.print(f"[{theme.RD}]This workspace's settings ({cfg.protocol}) are not the locked protocol ({lock['protocol']}).[/]")
        for line in ledger_mod.protocol.differences(lock.get("settings", {}), cfg.protocol_settings())[:12]:
            console.print(f"  {line}")
        fail("Refused. Put the settings back, or have the owner lock the new ones: hm protocol lock")
    here = templates.digests(ws)
    changed = [name for name, digest in (lock.get("registry") or {}).items() if here.get(name) != digest]
    if changed:
        fail(f"Refused: {', '.join(changed)} differ{'s' if len(changed) == 1 else ''} from what the owner locked. "
             "Put the file back (git checkout -- FILE), or have the owner lock the new one: hm protocol lock")


def not_built(what: str) -> None:
    console.print(f"[{theme.AM}]{what} is not built yet.[/] See DESIGN.md for what it will do.")
    raise typer.Exit(2)
