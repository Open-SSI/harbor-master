"""Colors and small text helpers shared by the screen and the one-shot commands.

Color has one meaning throughout: teal for done or kept, purple for running, amber for
needs attention, red for dropped.
"""
from __future__ import annotations

import math

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


def blend(a: str, b: str, k: float) -> str:
    """The color a share `k` of the way from `a` to `b`."""
    x, y = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (a, b))
    return "#" + "".join(f"{round(p + (q - p) * k):02x}" for p, q in zip(x, y))


def pulse(color: str, now: float, period: float = 1.6) -> str:
    """A color that slowly brightens and settles, for the one thing that is working now."""
    return blend(color, "#ffffff", 0.5 * (0.5 - 0.5 * math.cos(2 * math.pi * now / period)))
