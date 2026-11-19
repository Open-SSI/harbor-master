"""An index's own copies of the curation skills: which it has, how they differ from the templates, and taking a template in."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from .. import skills
from ..tui import theme
from .common import Workspace, app, console, fail, is_owner, need_identity, need_workspace

skills_app = typer.Typer(invoke_without_command=True, help="What agents are told in this index: its own copies of the curation skills.")
app.add_typer(skills_app, name="skills")
STATE = {"missing": (theme.RD, "not in the index: the tool's template is used"), "same": (theme.TE, "same as the template"),
         "own": (theme.PU, "this index's own wording")}


@skills_app.callback()
def skills_show(ctx: typer.Context, workspace: Optional[Path] = Workspace):
    """Each curation skill, and whether this index has its own copy of it."""
    if ctx.invoked_subcommand:
        return
    ws = need_workspace(workspace)
    folder = ws / "skills"
    tb = Table(header_style=theme.MU, border_style=theme.BORDER)
    for col in ("skill", "in this index"):
        tb.add_column(col)
    rows = skills.status(folder)
    for name, state in rows:
        color, words = STATE[state]
        tb.add_row(f"skills/{name}", f"[{color}]{words}[/]")
    console.print(tb)
    wrong = skills.problems(folder)
    for p in wrong:
        console.print(f"[{theme.RD}]x[/] {p}")
    if any(state == "missing" for _, state in rows):
        console.print(f"[{theme.AM}]The protocol cannot be locked until every skill is in the index.[/] The owner copies them with: hm skills adopt")
    console.print(f"[{theme.MU}]An index may word any of them its own way: edit the file, then lock. hm skills diff shows what differs "
                  "from the template, and hm skills adopt NAME takes the template again.[/]")
    if wrong:
        fail(f"{len(wrong)} problem{'s' if len(wrong) != 1 else ''}: the code could not use these files as they are.")


def _named(folder: Path, given: list[str]) -> list[str]:
    known = skills.names()
    out = []
    for g in given:
        g = g.removeprefix("skills/")
        match = [n for n in known if n == g or n.rsplit(".", 1)[0] == g]
        if not match:
            fail(f"{g} is not a curation skill. They are: {', '.join(known)}")
        out += match
    return out


@skills_app.command("diff")
def skills_diff(names: list[str] = typer.Argument(None, help="Skills to compare. Default: every one that differs."),
                workspace: Optional[Path] = Workspace):
    """How this index's skills differ from the tool's templates."""
    ws = need_workspace(workspace)
    folder = ws / "skills"
    chosen = _named(folder, names) if names else [n for n, state in skills.status(folder) if state == "own"]
    shown = 0
    for n in chosen:
        text = skills.diff(folder, n)
        if text:
            shown += 1
            console.print(text, markup=False, highlight=False)
    if not shown:
        console.print("Nothing differs from the templates." if not names else "The same as the template.")


@skills_app.command("adopt")
def skills_adopt(names: list[str] = typer.Argument(None, help="Skills to replace with the template. Default: only the ones the index lacks."),
                 all_: bool = typer.Option(False, "--all", help="Replace every skill with its template."),
                 workspace: Optional[Path] = Workspace):
    """Owner only: copy the tool's templates into this index. With no name, only the missing ones."""
    ws, ident = need_workspace(workspace), need_identity()
    if not is_owner(ws, ident):
        fail(f"Only the owner changes what agents are told in this index, and @{ident.github} is not its owner.")
    folder = ws / "skills"
    which = skills.names() if all_ else _named(folder, names) if names else skills.missing(folder)
    changed = skills.adopt(folder, which)
    if not changed:
        console.print("Nothing to copy: the index already has " + ("these as the templates have them." if which else "every skill."))
        return
    for n in changed:
        console.print(f"  skills/{n}")
    console.print(f"[{theme.TE}]Copied {len(changed)} skill{'s' if len(changed) != 1 else ''} into the index.[/] "
                  "They take effect for everyone when you lock: hm protocol lock")
