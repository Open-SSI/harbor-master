"""The list of indexes: which exist, and whether each is what its entry says."""
from __future__ import annotations

import tempfile
from pathlib import Path

import typer
from rich.table import Table

from ..ledger import listing
from ..tui import theme
from .common import app, console, fail

List = typer.Option(None, "--list", help="Another list of indexes: a file or an address. Default: $HM_INDEXES, "
                                         "then the list in the Harbor-Master repository.")


def read_list(given: str | None) -> list[dict]:
    try:
        return listing.read(listing.source(given))
    except listing.ListError as err:
        fail(str(err))


@app.command()
def indexes(name: str = typer.Argument(None, help="Only this index."),
            check: bool = typer.Option(False, help="Also fetch each index, show what it holds, and check that it is "
                                                   "sound and is what its entry says."),
            list_: str = List):
    """The indexes that are listed for anyone to join."""
    entries = read_list(list_)
    wrong = listing.problems(entries)
    shown = [e for e in entries if all(e.get(k) for k in listing.REQUIRED) and name in (None, e["name"])]
    if name and not shown:
        fail(f"{name} is not on the list of indexes. See: hm indexes")
    if not entries:
        console.print(f"No index is listed yet in {listing.source(list_)}.")
        return
    tb = Table(header_style=theme.MU, border_style=theme.BORDER)
    for col in ("name", "owner", "listed", "address") if not check else ("name", "title", "owner", "benchmarks", "accepted tasks", "check"):
        tb.add_column(col)
    for e in shown:
        if not check:
            tb.add_row(e["name"], ", ".join("@" + o.split()[0] for o in listing.owners(e)), str(e.get("listed", "")), e["url"])
            continue
        with tempfile.TemporaryDirectory() as tmp:
            info, found = listing.inspect(e, Path(tmp) / "index")
        wrong += [f"{e['name']}: {p}" for p in found]
        tb.add_row(e["name"], info.get("title", "-"), info.get("owner", "-"), str(len(info.get("benchmarks", []))) if info else "-",
                   str(info.get("accepted", "-")), f"[{theme.RD}]{len(found)} problem{'s' if len(found) != 1 else ''}[/]"
                   if found else f"[{theme.TE}]sound[/]")
    console.print(tb)
    for p in wrong[:50]:
        console.print(f"[{theme.RD}]x[/] {p}")
    if wrong:
        fail(f"{len(wrong)} problem{'s' if len(wrong) != 1 else ''} in the list of indexes.")
    console.print(f"[{theme.MU}]Listed in {listing.source(list_)}. Get one with: hm open NAME. To list yours: hm entry, inside its folder[/]")
