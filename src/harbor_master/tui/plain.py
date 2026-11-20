"""No terminal, or --plain: print outcomes as they happen, then a table."""
from __future__ import annotations

from rich.text import Text

from ..engine import Session
from . import render
from .theme import AM, SE


async def run_plain(session: Session, console) -> None:
    """No terminal, or --plain: print outcomes as they happen, then a table."""
    session.on_recent = lambda entry: console.print(render.recent_line(entry))
    await session.run()
    console.print(render.final_table(session))
    console.print(Text(f"Spent ${session.spent:.2f} (curation ${sum(session.cost_by_tier.values()):.2f}, "
                       f"difficulty trials ${session.trial_cost:.2f}). Calls: D {session.calls['decision']}, "
                       f"W {session.calls['worker']}, F {session.calls['frontier']}.", style=SE))
    if session.ready:
        console.print(Text(f"{len(session.ready)} report(s) are ready for your verdict. Review them with: /review", style=AM))
    blocking = [r for r in session.requests if r.blocking]
    if blocking:
        console.print(Text(f"{len(blocking)} request(s) are waiting. hm curate in a terminal asks each in a box.", style=AM))
