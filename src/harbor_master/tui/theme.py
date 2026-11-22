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
KIND_COLOR = {"started": PU, "kept": TE, "dropped": RD, "to fix": AM, "to drop": AM, "stopped": AM, "verdict": PU, "checked": TE, "error": RD, "claimed": AM, "rejected": AM, "config": RD}


def t(*parts) -> Text:
    out = Text()
    for p in parts:
        if isinstance(p, tuple):
            out.append(p[0], style=p[1])
        else:
            out.append(p)
    return out


ANIMATE = 2     # frames a second for what moves on the screen. Each frame redraws the panels it is in, and a terminal
                # over a slow link (VS Code Remote, ssh) falls behind at 10: leaving the screen then waits for it to catch up


def spinner(now: float) -> str:
    return SPINNER[int(now * ANIMATE) % len(SPINNER)]


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


# ----- light and dark -----
# The colors above are the dark ones. In light mode every color the screen draws is turned over in
# lightness as it goes out, so that the two modes can be switched while a run is on the screen.

MODES = ("dark", "light")
_mode: str | None = None


def _file():
    from ..core import identity
    return identity.config_dir() / "theme"


def mode() -> str:
    """The mode in use: what HM_THEME says, else what was chosen last on this machine, else dark."""
    global _mode
    if _mode is None:
        import os
        try:
            saved = _file().read_text().strip()
        except OSError:
            saved = ""
        _mode = next((m for m in (os.environ.get("HM_THEME", ""), saved) if m in MODES), "dark")
    return _mode


def set_mode(name: str) -> str:
    """Choose a mode, and remember it for the next screen that opens."""
    global _mode
    _mode = name if name in MODES else ("light" if mode() == "dark" else "dark")
    try:
        _file().parent.mkdir(parents=True, exist_ok=True)
        _file().write_text(_mode + "\n")
    except OSError:
        pass
    return _mode


def turned(color: str) -> str:
    """A dark-mode color as light mode shows it: the same hue, with its lightness turned over."""
    import colorsys
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = min(0.97, 1.05 - l) if s < 0.2 else 0.95 - 0.75 * l   # greys span the whole range. Hues stay a little darker, to read on white
    return "#" + "".join(f"{round(c * 255):02x}" for c in colorsys.hls_to_rgb(h, l, s))


def light_filter():
    """What a screen adds to its filters: off in dark mode, and in light mode it turns every color over."""
    from functools import lru_cache

    from rich.color import Color
    from rich.segment import Segment
    from rich.style import Style
    from textual.filter import LineFilter

    @lru_cache(maxsize=4096)
    def over(style: Style) -> Style:
        def flip(color):
            if color is None or color.triplet is None:
                return color
            return Color.parse(turned(color.triplet.hex))
        return style + Style(color=flip(style.color), bgcolor=flip(style.bgcolor)) if (style.color or style.bgcolor) else style

    class Light(LineFilter):
        def apply(self, segments, background):
            return [seg if seg.control or seg.style is None else Segment(seg.text, over(seg.style), None) for seg in segments]

    return Light(enabled=mode() == "light")


def follow(app, chosen: object) -> None:
    """Make a screen show the mode in use, and draw itself again when that changed."""
    want = mode() == "light"
    if chosen.enabled != want:
        chosen.enabled = want
        app.refresh_css(animate=False)
        app.screen.refresh(repaint=True, layout=True)
