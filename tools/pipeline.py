"""The README's main picture: how a benchmark becomes index tasks, as a static SVG on a white page.

    python tools/pipeline.py assets/pipeline.svg

One benchmark goes through its own stages once; each selected task then goes through a work cell's stages, in
parallel; the kept ones a person accepts land in the index. One colour per tier, as on the curate screen: who does
a stage. The three cells are drawn at different moments: at its verdict, being repaired, and in the difficulty run."""
import sys
from pathlib import Path

W, H = 1720, 690
BG, INK, NOTE, DIM = "#ffffff", "#1f1f2a", "#6f6e7a", "#9a99a6"
LINE, LANE, DASH = "#9a99a6", "#fbfbfd", "#cfced8"
FONT = "Inter, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace"
TIER = {    # stroke, fill, text
    "code": ("#b9b8c4", "#f6f6f8", "#2a2a33"),
    "D": ("#5dc7a4", "#e6f7f1", "#14684a"),
    "W": ("#9a92ee", "#efedfd", "#3a36b0"),
    "F": ("#e9a94a", "#fdf3e1", "#8a5a0a"),
    "H": ("#e07f97", "#fdecef", "#9c2f4a"),
}
out: list[str] = []
add = out.append


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text(x, y, s, size=17, fill=NOTE, weight=400, anchor="start", italic=False, mono=False):
    extra = (' font-style="italic"' if italic else "") + (f' font-family="{MONO}"' if mono else "")
    add(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}"{extra}>{esc(s)}</text>')


def box(x, y, w, h, tier, lines, lit=False, dim=False):
    """A stage: its name, and under it who does it. `lit` is the stage a task is at; `dim` one it has not reached."""
    s, f, t = TIER[tier]
    add(f'<g opacity="{0.4 if dim else 1}">')
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{f}" stroke="{s}" stroke-width="{4 if lit else 1.6}"/>')
    ty = y + (h - sum(size * 1.24 for _, size, _ in lines)) / 2
    for s_, size, kind in lines:
        ty += size * 1.24
        text(x + w / 2, round(ty - size * 0.3, 1), s_, size, t if kind == "b" else NOTE, 700 if kind == "b" else 400, "middle", kind == "i")
    add("</g>")


def arrow(d, color=LINE, dash=False, marker="ah", dim=False):
    add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.8" marker-end="url(#{marker})"'
        + (' stroke-dasharray="6 6"' if dash else "") + (' opacity="0.4"' if dim else "") + "/>")


add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{FONT}">')
add("<defs>" + "".join(
    f'<marker id="{i}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
    f'<path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>' for i, c in (("ah", LINE), ("ahp", "#7a72e6"))) + "</defs>")
add(f'<rect width="{W}" height="{H}" rx="16" fill="{BG}"/>')
add('<g transform="translate(14 14)">')

# ----- once per benchmark -----
add(f'<rect x="1" y="30" width="1228" height="162" rx="14" fill="none" stroke="{DASH}" stroke-dasharray="8 7" stroke-width="1.5"/>')
text(14, 20, "Once per benchmark")
BX, BW, BH, BY = [20, 222, 424, 626, 828, 1030], 180, 104, 48
box(BX[0], BY, BW, BH, "code", [])
text(BX[0] + BW / 2, BY + 27, "Benchmark", 20, INK, 700, "middle")
FAILED, CHOSEN = {2, 7, 11, 13, 18, 22}, {0, 4, 9, 15, 20}
for n in range(24):
    fill = "#5dc7a4" if n in CHOSEN else "#e6e5ec" if n in FAILED else "#bdbcc8"
    add(f'<rect x="{BX[0] + 16.5 + (n % 8) * 19}" y="{BY + 40 + (n // 8) * 19}" width="14" height="14" rx="3" fill="{fill}"/>')
for k, (tier, name, what, who) in enumerate((
        ("code", "Fetch", "its pinned release", "code"),
        ("code", "Check", "cheap checks first", "code"),
        ("W", "Adapt", "into Harbor tasks", "worker agent"),
        ("D", "Screen", "admission gates", "decision model"),
        ("D", "Rank, select", "by your brief", "decision model")), 1):
    box(BX[k], BY, BW, BH, tier, [(name, 21, "b"), (what, 14.5, ""), (who, 13.5, "i")])
    arrow(f"M{BX[k - 1] + BW + 3},{BY + BH / 2} H{BX[k] - 4}")
text(BX[5] + BW / 2 - 14, 178, "you give the brief and approve the selection", 16, TIER["H"][2], anchor="end", italic=True)

# ----- who does what: the escalation order, which is also the colour key -----
text(1262, 20, "Who does a stage", 17, INK, 700)
text(1262, 43, "the next is asked only when one is unsure or stuck", 14)
for k, (tier, a, b, c) in enumerate((("D", "Decision", "model", "Jev"), ("W", "Worker", "agent", "Sonnet"), ("F", "Frontier", "agents", "Opus"),
                                     ("H", "You", "sign", ""))):
    box(1262 + k * 108, 62, 92, 84, tier, [(a, 15, "b"), (b, 14, ""), *([(c, 13, "i")] if c else [])])
    if k:
        arrow(f"M{1262 + k * 108 - 14},104 H{1262 + k * 108 - 3}")

# ----- the work cells -----
LY, SW = [244, 354, 464], 146
SX = lambda k: 196 + k * 170                                                              # noqa: E731
NAMES = ["Verify", "Probe", "Worker", "Audit", "Difficulty", "Report", "Verdict"]
WHO = ["solution vs no-op", "wrong answers fail", "twice = too easy", "task's fault or not", "frontier, ≤ 3 of 6", "worker agent",
       "you, signed"]
TIERS = ["code", "W", "W", "W", "F", "W", "H"]
arrow(f"M{BX[5] + BW / 2},{BY + BH} V214 H12 V{LY[2] + 49} H27")
for y in LY[:2]:
    arrow(f"M12,{y + 49} H27")
text(250, 207, "each selected task goes into a work cell of its own, and the cells run in parallel", fill=INK)
CELLS = [("t0036", "verdict", 6, TIER["H"][2]), ("t0008", "in repair", 3, TIER["F"][2]), ("t0021", "difficulty", 4, TIER["F"][2])]
for i, (y, (task, status, at, color)) in enumerate(zip(LY, CELLS)):
    add(f'<rect x="30" y="{y}" width="1350" height="98" rx="12" fill="{LANE}" stroke="{DASH}" stroke-dasharray="8 7" stroke-width="1.5"/>')
    text(48, y + 32, f"cell {i + 1}", 16, DIM)
    text(48, y + 58, task, 20, INK, mono=True)
    text(48, y + 81, status, 15, color)
    for k, name in enumerate(NAMES):
        repair = status == "in repair" and k == at
        box(SX(k), y + 17, SW, 64, "F" if repair else TIERS[k], [(name, 19, "b"), (WHO[k], 12.5, "i")], lit=k == at, dim=k > at)
        if k:
            arrow(f"M{SX(k - 1) + SW + 2},{y + 49} H{SX(k) - 4}", dim=k > at)
    arrow(f"M{SX(6) + SW + 2},{y + 49} H1428", dim=at < 6)
    if status == "in repair":       # the fix loop: a broken task is repaired, and checked again from the start
        arrow(f"M{SX(3) + SW / 2},{y + 83} V{y + 91} H{SX(0) + SW / 2} V{y + 84}", "#7a72e6", marker="ahp")
        label, lx = "Fix · broken, so repaired and verified again", (SX(0) + SX(3) + SW) / 2
        add(f'<rect x="{lx - 200}" y="{y + 82}" width="400" height="16" fill="{LANE}"/>')
        text(lx, y + 96, label, 15, TIER["W"][2], 700, "middle")

# ----- the index, and the repository that holds every record -----
add('<rect x="1432" y="244" width="250" height="318" rx="14" fill="#f3f1fe" stroke="#9a92ee" stroke-width="2.5"/>')
text(1557, 284, "Index", 25, TIER["W"][2], 700, "middle")
text(1557, 308, "accepted tasks, e.g. OpenSSI", 15, NOTE, anchor="middle", italic=True)
for k, task in enumerate(("t0036", "t0029", "t0011")):
    add(f'<rect x="1462" y="{328 + k * 58}" width="190" height="44" rx="9" fill="#e6f7f1" stroke="#5dc7a4" stroke-width="1.8"'
        + (' stroke-dasharray="6 5"' if k == 0 else "") + "/>")
    text(1557, 357 + k * 58, task, 18, TIER["D"][2], anchor="middle", mono=True)
text(1557, 521, "kept in the task store,", 14, NOTE, anchor="middle")
text(1557, 540, "a Hugging Face dataset", 14, NOTE, anchor="middle")
box(1, 590, 1681, 70, "code", [])
text(24, 621, "Index repository", 21, INK, 700)
text(262, 621, "protocol · pinned benchmarks · contributors' keys · signed ledger", fill=INK)
text(24, 646, "every stage writes signed records, keyed by the task's content digest: nothing runs twice, and anyone can check how a task got in", 15, NOTE)
for x in (400, 1060, 1557):
    arrow(f"M{x},566 V587", dash=True)

add("</g></svg>")
Path(sys.argv[1]).write_text("\n".join(out) + "\n")
