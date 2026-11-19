"""The `hm` command (DESIGN.md Section 11). One module per group of commands.

`hm` alone opens the screen, where every step is a / command. A shell lists only `run` and the
commands that show results. The others still answer when called by name, for scripts and for the
screens themselves, which run them in place.
"""
from . import adapt, curate, dev, home, listing, pending, results, setup, skills, store  # noqa: F401  (each registers its commands)
from .common import app

SHELL = ("run", "status", "stats", "leaderboard", "credits", "todo", "sync")

for _info in (*app.registered_commands, *app.registered_groups):
    _name = _info.name or (_info.callback.__name__.rstrip("_").replace("_", "-") if _info.callback else "")
    _info.hidden = _name not in SHELL

__all__ = ["SHELL", "app"]
