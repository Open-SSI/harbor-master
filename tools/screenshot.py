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

from harbor_master import ledger, tasks
from harbor_master.cli.dev import fake_benchmark
from harbor_master.core import config as config_mod
from harbor_master.core import identity
from harbor_master.engine import Session
from harbor_master.tui import Prompt, SessionApp
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


def draw(lines: list[list[Segment]], width: int, out: Path, font_dir: Path | None, size: int = 30) -> None:
    def font(name: str, fallback: str) -> str:
        p = font_dir / name if font_dir else None
        return str(p) if p and p.exists() else f"{DEJAVU}/{fallback}"

    paths = {"r": font("JetBrainsMono-Regular.ttf", "DejaVuSansMono.ttf"),
             "b": font("JetBrainsMono-Bold.ttf", "DejaVuSansMono-Bold.ttf")}
    fonts = {k: ImageFont.truetype(p, size) for k, p in paths.items()}
    extra = [ImageFont.truetype(f"{DEJAVU}/{n}", size) for n in ("DejaVuSansMono.ttf", "DejaVuSans.ttf")]
    cmaps = [set(TTFont(paths["r"]).getBestCmap()), set(TTFont(f"{DEJAVU}/DejaVuSansMono.ttf").getBestCmap())]
    theme = TerminalTheme(rgb(BG), rgb(FG), [rgb(FG)] * 8, [rgb(FG)] * 8)
    cw, lh, pad, lw = fonts["r"].getlength("M"), round(size * 1.42), 40, 2
    img = Image.new("RGB", (round(cw * width) + 2 * pad, lh * len(lines) + 2 * pad), rgb(BG))
    d = ImageDraw.Draw(img)
    for y, line in enumerate(lines):
        x = 0
        for seg in line:
            st = seg.style
            fg = tuple(st.color.get_truecolor(theme)) if st and st.color else rgb(FG)
            bg = tuple(st.bgcolor.get_truecolor(theme)) if st and st.bgcolor else None
            if st and st.reverse:
                fg, bg = (bg or rgb(BG)), fg
            for ch in seg.text:
                x0, y0 = pad + x * cw, pad + y * lh
                cx, cy, rad = x0 + cw / 2, y0 + lh / 2, cw / 2
                if bg:
                    d.rectangle([x0, y0, x0 + cw, y0 + lh], fill=bg)
                if ch == "─":
                    d.line([(x0, cy), (x0 + cw, cy)], fill=fg, width=lw)
                elif ch == "│":
                    d.line([(cx, y0), (cx, y0 + lh)], fill=fg, width=lw)
                elif ch in "╭╮╰╯":
                    right, down = ch in "╭╰", ch in "╭╮"
                    ax, ay = (cx + rad if right else cx - rad), (cy + rad if down else cy - rad)
                    start = {"╭": 180, "╮": 270, "╰": 90, "╯": 0}[ch]
                    d.arc([ax - rad, ay - rad, ax + rad, ay + rad], start, start + 90, fill=fg, width=lw)
                    d.line([(cx, ay), (cx, y0 + lh if down else y0)], fill=fg, width=lw)
                elif ch == "█":
                    d.rectangle([x0, y0 + lh * 0.18, x0 + cw, y0 + lh * 0.82], fill=fg)
                elif ch == "░":
                    d.rectangle([x0, y0 + lh * 0.18, x0 + cw, y0 + lh * 0.82], fill=tuple((a + 2 * b) // 3 for a, b in zip(fg, rgb(BG))))
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
        if scene == "home":             # the screen `hm` opens with: someone who has set up who they are, and no index yet
            from harbor_master.cli.home import run_cli
            from harbor_master.tui import HomeApp
            folder = Path(tmp) / "work"
            folder.mkdir()
            os.chdir(folder)
            home = HomeApp(run_cli, folder)
            async with home.run_test(size=(cols, rows)) as pilot:
                await pilot.pause(0.5)
                await pilot.press(*"/j")
                await pilot.pause(0.3)
                draw(grab(home), cols, out, font_dir)
            return
        ws = Path(tmp) / "ws"
        ws.mkdir()
        (ws / ledger.CONTRIBUTORS).write_text(
            f'[[contributor]]\nname = "{ident.name}"\ngithub = "{ident.github}"\nkey = "{ident.public_key}"\n')
        cfg = config_mod.Config()
        cfg.select.require_signoff = scene in ("signoff", "complete")
        cfg.run.budget_usd = 1000        # the pictures should not stop on the budget
        sizes = {"main": (120, 60, 40), "signoff": (120, 60, 0), "complete": (120, 60, 0), "review": (40, 24, 0)}[scene]
        registry = {}
        for (name, field), n in zip((("smdd-bench", "chemistry"), ("terrabench", "earth"), ("autopdebench", "physics")), sizes):
            if n:
                fake_benchmark(name, n, directory=Path(tmp) / "fake", seed=0)
                registry[name] = {"path": f"{tmp}/fake/{name}", "field": field}
        for b in ("smdd-bench", "terrabench"):
            ledger.Shard(config_mod.simulated(ws), b, ident, "brief", cfg.protocol).append(
                "brief", text="Tasks that need real computation.", examples=[], version=1, simulated=True)
        s = Session(ws, cfg, ident, registry, simulate=True, speed=0.02 if scene == "review" else 0.6,
                    command="hm run " + " ".join(b.split("/")[-1] for b in registry) + " --simulate")
        app = SessionApp(s)
        async with app.run_test(size=(cols, rows)) as pilot:
            prompt = app.query_one(Prompt)

            async def say(text: str, wait: float) -> None:
                prompt.value = text
                await pilot.press("enter")
                await pilot.pause(wait)

            if scene == "main":
                await pilot.pause(22)
                await say("how long until cell 1 finishes, and what is it about?", 1.0)
                await say("hold terrabench for now and stop the task in cell 3", 0.4)
            elif scene == "signoff":
                await pilot.pause(8)
            elif scene == "complete":       # a command half typed: the dimmed rest, and what tab offers
                await pilot.pause(8)
                await pilot.press(*"/approve", "tab")
                await pilot.pause(0.5)
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
    ap.add_argument("--scene", choices=["home", "main", "signoff", "complete", "review"], default="main")
    a = ap.parse_args()
    asyncio.run(main(a.out, a.font_dir, a.cols, a.rows, a.scene))
