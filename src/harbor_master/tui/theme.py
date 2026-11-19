"""Colors and small text helpers shared by the screen and the one-shot commands.

Color has one meaning throughout: teal for done or kept, purple for running, amber for
needs attention, red for dropped.
"""
from __future__ import annotations

from rich.text import Text

FG, BG = "#d8d6ce", "#16161d"
MU, SE = "#7c7b75", "#aaa89f"
PU, TE, AM, RD = "#a59ef0", "#5dcaa5", "#efa63a", "#f08a8a"
BORDER = "#3a3a46"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
KIND_COLOR = {"kept": TE, "dropped": RD, "to fix": AM, "stopped": AM, "budget": AM, "verdict": PU, "checked": TE, "error": RD, "claimed": AM, "fields": AM, "rejected": AM, "config": RD}


def t(*parts) -> Text:
    out = Text()
    for p in parts:
        if isinstance(p, tuple):
            out.append(p[0], style=p[1])
        else:
            out.append(p)
    return out


def spinner(now: float) -> str:
    return SPINNER[int(now * 10) % len(SPINNER)]


def clock(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, m, s = seconds // 3600, seconds % 3600 // 60, seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def shimmer(text: str, color: str, now: float) -> Text:
    """A bright band sweeping across a word, as coding agents show while they work."""
    out = Text(text, style=f"bold {color}")
    pos = int(now * 14) % (len(text) + 8) - 2
    for i in range(max(0, pos), min(len(text), pos + 3)):
        out.stylize("bold #ffffff", i, i + 1)
    return out
