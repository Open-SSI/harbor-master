"""Take a picture of the real session screen during a simulated run.

    uv run --with pillow --with fonttools python tools/screenshot.py OUT.png [--scene home|main|signoff|complete|review] [--font-dir DIR]

It runs the Textual app headless against a throwaway workspace, types a question and a
plain-text instruction, and draws the screen's character grid with Pillow. Box-drawing
characters are drawn as lines, so borders meet exactly whatever font is used. The font
is only for the picture: in a terminal, the screen uses the terminal's own font.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import tempfile
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont
from rich.console import Console
from rich.segment import Segment
from rich.terminal_theme import TerminalTheme

from harbor_master import ledger
from harbor_master.cli.dev import fake_benchmark
from harbor_master.core import config as config_mod
from harbor_master.core import identity
from harbor_master.engine import Session
from harbor_master.tui import SessionApp
from harbor_master.tui.theme import BG, FG

DEJAVU = "/usr/share/fonts/truetype/dejavu"


def rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def grab(app) -> list[list[Segment]]:
    """The screen as lines of styled segments, the way Textual's own screenshot gets them."""
    w, h = app.size
    console = Console(width=w, height=h, file=io.StringIO(), force_terminal=True, color_system="truecolor",
                      record=True, legacy_windows=False, safe_box=False)
    console.print(app.screen._compositor.render_update(full=True, screen_stack=app._background_screens))
    return list(Segment.split_lines(seg for seg in console._record_buffer if not seg.control))


def draw(lines: list[list[Segment]], width: int, out: Path, font_dir: Path | None, size: int = 30, light: bool = False) -> None:
    """Draw a screen to a PNG. With `light`, on the light theme's ground: what was captured in light mode keeps its own
    colors, and what has none is drawn in the light theme's. Box-drawing characters are lines and arcs on whole pixels,
    stroked centered on their path, so that every side of a box meets its corners exactly."""
    from harbor_master.tui.theme import turned
    ground, ink = (rgb(turned(BG)), rgb(turned(FG))) if light else (rgb(BG), rgb(FG))
    paper = (255, 255, 255) if light else ground        # on a light page, the theme's own ground is drawn white
    def font(name: str, fallback: str) -> str:
        p = font_dir / name if font_dir else None
        return str(p) if p and p.exists() else f"{DEJAVU}/{fallback}"

    paths = {"r": font("JetBrainsMono-Regular.ttf", "DejaVuSansMono.ttf"),
             "b": font("JetBrainsMono-Bold.ttf", "DejaVuSansMono-Bold.ttf")}
    fonts = {k: ImageFont.truetype(p, size) for k, p in paths.items()}
    extra = [ImageFont.truetype(f"{DEJAVU}/{n}", size) for n in ("DejaVuSansMono.ttf", "DejaVuSans.ttf")]
    cmaps = [set(TTFont(paths["r"]).getBestCmap()), set(TTFont(f"{DEJAVU}/DejaVuSansMono.ttf").getBestCmap())]
    theme = TerminalTheme(ground, ink, [ink] * 8, [ink] * 8)
    cw, lh, pad, lw = fonts["r"].getlength("M"), round(size * 1.42), 40, 2
    img = Image.new("RGB", (round(cw * width) + 2 * pad, lh * len(lines) + 2 * pad), paper)
    d = ImageDraw.Draw(img)
    for y, line in enumerate(lines):
        x = 0
        for seg in line:
            st = seg.style
            fg = tuple(st.color.get_truecolor(theme)) if st and st.color else ink
            bg = tuple(st.bgcolor.get_truecolor(theme)) if st and st.bgcolor else None
            if st and st.reverse:
                fg, bg = (bg or ground), fg
            if bg == ground:
                bg = paper
            for ch in seg.text:
                x0, x1, y0 = round(pad + x * cw), round(pad + (x + 1) * cw), pad + y * lh   # whole pixels: a column is
                cx, cy, rad = round(pad + (x + 0.5) * cw), y0 + lh // 2, round(cw / 2)    # the same column on every row
                if bg:
                    d.rectangle([x0, y0, x1, y0 + lh], fill=bg)
                if ch == "─":
                    d.rectangle([x0, cy - lw // 2, x1, cy - lw // 2 + lw - 1], fill=fg)
                elif ch == "│":
                    d.rectangle([cx - lw // 2, y0, cx - lw // 2 + lw - 1, y0 + lh], fill=fg)
                elif ch in "╭╮╰╯":
                    right, down = ch in "╭╰", ch in "╭╮"
                    ax, ay = (cx + rad if right else cx - rad), (cy + rad if down else cy - rad)
                    start = {"╭": 180, "╮": 270, "╰": 90, "╯": 0}[ch]
                    # PIL strokes an arc inside its box: a box half a line wider centres it on the arc's path, where
                    # the straight sides are drawn, so the corner meets them without a step
                    r_out, top = rad + lw // 2, cy - lw // 2
                    d.arc([ax - r_out, ay - r_out, ax + r_out - 1, ay + r_out - 1], start, start + 90, fill=fg, width=lw)
                    end = x1 if right else x0
                    d.rectangle([min(ax, end), top, max(ax, end), top + lw - 1], fill=fg)
                    d.rectangle([cx - lw // 2, min(ay, y0 + lh if down else y0), cx - lw // 2 + lw - 1,
                                 max(ay, y0 + lh if down else y0)], fill=fg)
                elif ch == "█":
                    d.rectangle([x0, y0 + lh * 0.18, x1, y0 + lh * 0.82], fill=fg)
                elif ch == "░":
                    d.rectangle([x0, y0 + lh * 0.18, x1, y0 + lh * 0.82], fill=tuple((a + 2 * b) // 3 for a, b in zip(fg, ground)))
                elif not ch.isspace():
                    f = fonts["b" if st and st.bold else "r"]
                    if ord(ch) not in cmaps[0]:
                        f = extra[0] if ord(ch) in cmaps[1] else extra[1]
                    d.text((x0, y0 + (lh - size * 1.3) / 2), ch, font=f, fill=fg)
                x += 1
    img.save(out, optimize=True)


async def main(out: Path, font_dir: Path | None, cols: int, rows: int, scene: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HM_CONFIG_DIR"] = f"{tmp}/config"
        ident = identity.create("Yibo Wen", "Northwestern University", "yibo@u.northwestern.edu", "Yibo-Wen")
        ws = Path(tmp) / "ws"
        ws.mkdir()
        (ws / ledger.CONTRIBUTORS).write_text(
            f'[[contributor]]\nname = "{ident.name}"\ngithub = "{ident.github}"\nkey = "{ident.public_key}"\n')
        cfg = config_mod.Config()
        cfg.select.require_signoff = scene == "signoff"
        sizes = {"main": (120, 60, 40), "signoff": (120, 60, 0), "review": (40, 24, 0)}[scene]
        registry = {}
        for (name, field), n in zip((("smdd-bench", "chemistry"), ("terrabench", "earth"), ("autopdebench", "physics")), sizes):
            if n:
                fake_benchmark(name, n, directory=Path(tmp) / "fake", seed=0)
                registry[name] = {"path": f"{tmp}/fake/{name}", "field": field}
        for b in registry:
            ledger.Shard(config_mod.simulated(ws), b, ident, "brief", cfg.protocol).append(
                "brief", text="Tasks that need real computation.", examples=[], version=1, simulated=True)
        s = Session(ws, cfg, ident, registry, simulate=True, speed=0.02 if scene == "review" else 0.6,
                    command="hm run " + " ".join(b.split("/")[-1] for b in registry) + " --simulate")
        s.asked_cells = True             # the picture is of the run, not of the box that asks for the work cells
        app = SessionApp(s)
        async with app.run_test(size=(cols, rows)) as pilot:
            if scene == "main":
                await pilot.pause(22)
            elif scene == "signoff":        # the box that asks how many of the ranked tasks go on
                await pilot.pause(8)
            else:
                for _ in range(120):
                    if s.finished:
                        break
                    await pilot.pause(0.5)
                await pilot.pause(0.5)
            draw(grab(app), cols, out, font_dir)
            s.kill()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--font-dir", type=Path)
    ap.add_argument("--cols", type=int, default=112)
    ap.add_argument("--rows", type=int, default=44)
    ap.add_argument("--scene", choices=["main", "signoff", "review"], default="main")
    a = ap.parse_args()
    asyncio.run(main(a.out, a.font_dir, a.cols, a.rows, a.scene))
