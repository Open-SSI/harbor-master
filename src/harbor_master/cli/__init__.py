"""The `hm` command (DESIGN.md Section 10). One module per group of commands, and no hidden ones.

Four commands work from any folder: `hm init` says who you are, `hm new` starts an index, `hm clone INDEX`
clones one, and `hm indexes` lists them. Every other command is about one index, and is typed inside that
index's folder. `hm` alone is an error that says so.
"""
import os
import sys

from . import curate, home, listing, setup, store, walk  # noqa: F401  (each registers its commands)
from .common import app

ANYWHERE = ("init", "new", "clone", "indexes")
IN_AN_INDEX = ("curate", "benchmarks", "view", "contributors", "cut", "restore", "publish")
USAGE = """In any folder:
  hm init            say who you are and make your signing key. Everyone starts here
  hm indexes         the indexes that are listed
  hm clone INDEX     clone an index into a folder here. hm clone demo-index to try it
  hm new INDEX --tasks ORG/NAME --repo OWNER/NAME
                     start an index of your own

Inside an index's folder, to contribute:
  hm curate          curate a benchmark: the curate screen asks which one, then shows its stages
  hm benchmarks      the benchmarks it curates, and where each stands. hm benchmarks add URL adds one
  In demo-index, add --demo to hm curate and hm view: all simulated

Inside an index's folder, for its owner and leads:
  hm view            the dashboard: progress, settings and people, read only
  hm contributors    its owner: who may contribute (add, role, remove)
  hm cut TASK why    its owner or a lead: the final cut. hm restore TASK undoes it
  hm publish         its owner: check the index, and ask Harbor-Master to list it"""


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
                      "cd into the index's folder first. To get one: hm clone INDEX. To see which exist: hm indexes.", markup=False, highlight=False)
        raise SystemExit(2)
    app()


__all__ = ["ANYWHERE", "USAGE", "app", "main"]
