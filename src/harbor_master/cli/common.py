"""What every `hm` command shares: the app, the console, and the checks before a command runs."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Callable, Optional

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
                       "Start with hm init. hm clone INDEX clones an index, and hm clone demo-index makes one to try it on. Inside its folder, "
                       "hm curate opens the curate screen, which asks what it needs in boxes.\n\n"
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
            fail("This folder is not inside an index. cd into an index's folder first. To get one: hm clone INDEX.")
    ws = ws.resolve()
    check_owner(ws)
    from .sharing import catch_up
    catch_up(ws)                # in step with the index's repository: there is no command for it
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


def check_index(cfg, ws: Path) -> None:
    """Refuse to go on when this tool is older than the index asks for, when its skills cannot be used or are missing,
    or when its contributor list is not the one its owner signed."""
    from .. import skills
    need = cfg.requires.harbor_master
    if version_tuple(__version__) < version_tuple(need):
        fail(f"Refused: this index needs Harbor-Master {need} or newer, and this is {__version__}. Update it, then run again.")
    wrong = skills.problems(ws / "skills")
    if wrong:
        for p in wrong[:12]:
            console.print(f"  {p}")
        fail("Refused: the code cannot use these skills as they are.")
    absent = skills.missing(ws / "skills")
    if absent:
        fail(f"Refused: this index has no copy of {', '.join('skills/' + n for n in absent)}, so what agents are told would "
             "depend on the tool that is installed. Put the files back: git checkout -- skills")
    from_owner = ledger_mod.list_problem(ws, ledger_mod.Ledger(ws).events)
    if from_owner:
        fail(f"Refused: {from_owner}. Put it back with: git checkout origin/main -- {ledger_mod.CONTRIBUTORS}")

def need_keys(missing: Callable[[], list[tuple[str, str]]], doing: str) -> None:
    """Go on only once the keys file has the keys `doing` needs: `missing` gives (what for, which variable) for each one
    still absent. In a terminal it waits while they are added, and checks again."""
    from .setup import keys_file
    path = keys_file()          # made from the example the first time, to fill in with an editor
    while lacking := missing():
        console.print(f"[{theme.AM}]{doing} needs {'a key' if len(lacking) == 1 else 'keys'} this machine does not have yet:[/]", highlight=False)
        for what, var in lacking:
            console.print(f"  {var:<40} [{theme.MU}]for {what}[/]", highlight=False)
        console.print(f"Add {'it' if len(lacking) == 1 else 'them'} to {path}. The file says what each is for. "
                      "Keys are never typed here.", highlight=False, soft_wrap=True)
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise typer.Exit(1)
        try:
            input("Press enter once they are in, to check again. ctrl+c stops. ")
        except (KeyboardInterrupt, EOFError):
            console.print()
            raise typer.Exit(1)


def on_hub(cfg) -> bool:
    """Whether the index's task store is on Hugging Face, which needs each person's own token. A folder needs none."""
    store = (cfg.about.tasks or "").strip()
    return bool(store) and not store.startswith(("/", "~", "."))


def curate_keys(cfg) -> list[tuple[str, str]]:
    """The keys a real curation needs that this machine lacks: every model tier, the difficulty suite, and the task store's token."""
    from ..core import keys
    return [(f"{what} ({uses})" if what != "task store" else "the task store", var) for what, var, present, uses in keys.doctor(cfg)
            if not present and (what != "task store" or on_hub(cfg))]
