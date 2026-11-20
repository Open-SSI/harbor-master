"""The `hm` command (DESIGN.md Section 11). One module per group of commands, and no hidden ones.

Four commands work from any folder: `hm init` says who you are, `hm new` starts an index, `hm open INDEX`
clones one, and `hm indexes` lists them. Every other command is about one index, and is typed inside that
index's folder. `hm` alone is an error that says so.
"""
import os
import sys

from . import curate, evals, home, listing, results, setup, store, walk  # noqa: F401  (each registers its commands)
from .common import app

ANYWHERE = ("init", "new", "open", "indexes")
IN_AN_INDEX = ("curate", "benchmarks", "eval", "todo", "sync", "contributors", "lock", "release", "cut", "restore", "entry")
USAGE = """In any folder:
  hm init            say who you are, get an index, and try a simulated demo. Everyone starts here
  hm indexes         the indexes that are listed
  hm open INDEX      clone an index into a folder here
  hm new INDEX --tasks ORG/NAME --repo OWNER/NAME
                     start an index of your own

Inside an index's folder, to contribute:
  hm benchmarks      the benchmarks it curates. hm benchmarks add URL adds one
  hm curate          stage 1: build the index. Opens the curate screen (--demo tries it, simulated)
  hm eval            stage 2: evaluate the released index. Opens the eval screen (--demo tries it)
  hm todo            what is left to evaluate
  hm sync            pull the index and push your records

Inside an index's folder, for its owner and leads:
  hm open            the dashboard: progress, protocol, roster and people, read only
  hm contributors    its owner: who may contribute (add, role, remove)
  hm lock            its owner: lock the settings and the skills. The roster is never locked:
                     edit roster.toml at any time, then hm sync
  hm release         its owner, once: close curation and open evaluation, as v1.0.0
  hm cut TASK why    its owner or a lead: the final cut. hm restore TASK undoes it
  hm entry           its owner: its entry for the list of indexes"""

def main() -> None:
    """What `hm` is in a shell."""
    from ..core import config as config_mod
    from .common import console
    args = sys.argv[1:]
    typed = next((a for a in args if not a.startswith("-")), None)
    if not args:
        console.print(USAGE, markup=False, highlight=False, soft_wrap=True)
        raise SystemExit(2)
    named = "-w" in args or "--workspace" in args or os.environ.get("HM_WORKSPACE")
    if typed is not None and typed not in ANYWHERE and not named and config_mod.find_workspace() is None:
        console.print(f"hm {typed} works inside an index's folder, and this folder is not in one.\n"
                      "cd into the index's folder first. To get one: hm open INDEX. To see which exist: hm indexes.", markup=False, highlight=False)
        raise SystemExit(2)
    app()


__all__ = ["ANYWHERE", "USAGE", "app", "main"]
