"""Harbor-Master's architecture, as an animated SVG for the README: drawn on a grid, animated with SMIL only, so that it
moves where GitHub shows it as an image.

    python tools/architecture.py assets/architecture.svg

Benchmarks and frontier agents enter the index repository; leads claim benchmarks from it and curate them in parallel;
in each lead's run, tasks (the squares, and the dots that move) pass the tiers in escalation order, fewer at each one;
the accepted ones land in the index and its task store, and every step is a signed record in the ledger."""
import sys
from pathlib import Path

W, H = 1320, 400
FONT = "Inter, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
INK, MUTE, LINE = "#2f2f38", "#7b7a86", "#b9b8c4"
TIER = {   # fill, stroke, text: the paper's palette
    "neutral": ("#f4f3ef", "#8c8a85", "#3a3a3a"),
    "decision": ("#f6f6fe", "#a9a6f4", "#3a36b0"),
    "worker": ("#ece9fd", "#6b67ee", "#3a36b0"),
    "frontier": ("#dedcfb", "#4440c6", "#2e2a9e"),
    "human": ("#fbece6", "#d9602f", "#8a2e12"),
    "out": ("#e9f7f1", "#2fa67a", "#14684a"),
}
out = []
add = out.append


def text(x, y, s, size=12, weight=400, fill=INK, anchor="middle", italic=False):
    st = ' font-style="italic"' if italic else ""
    add(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}"{st}>{s}</text>')


def card(x, y, w, h, kind, title, lines=(), r=10, dash=False):
    fill, stroke, tc = TIER[kind]
    d = ' stroke-dasharray="5 4"' if dash else ""
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="1.4"{d}/>')
    cy = y + h / 2 - 7 * len(lines)
    text(x + w / 2, cy + 4, title, 13.5, 650, tc)
    for i, l in enumerate(lines):
        text(x + w / 2, cy + 22 + 15 * i, l, 11, 400, MUTE)


def arrow(d, label=None, lx=0, ly=0, flow=False, color=LINE):
    add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.6" marker-end="url(#head)"/>')
    if flow:        # a moving dash over the arrow: things travel along it
        add(f'<path d="{d}" fill="none" stroke="#6b67ee" stroke-width="1.6" stroke-dasharray="4 14" opacity=".75">'
            '<animate attributeName="stroke-dashoffset" from="36" to="0" dur="1.2s" repeatCount="indefinite"/></path>')
    if label:
        text(lx, ly, label, 10.5, 500, MUTE)


add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="{FONT}" role="img" '
    'aria-label="Harbor-Master: benchmarks and frontier agents enter an index repository; leads claim benchmarks and curate them '
    'in parallel, each task passing a decision model, worker agents, frontier agents and the lead, fewer at each tier; '
    'accepted tasks land in the index and its task store, every step a signed record.">')
add('<defs><marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
    f'<path d="M0 0 L10 5 L0 10 z" fill="{LINE}"/></marker></defs>')
add(f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="18" fill="#ffffff" stroke="#e7e6ee"/>')

# ----- title, in the corner the flow leaves free -----
text(32, 46, "Harbor-Master", 20, 700, INK, anchor="start")
text(32, 68, "curates benchmarks into an index of hard, verified tasks", 12, 400, MUTE, anchor="start")

# ----- grid -----
LANES = [(108, "Lead 1", "benchmark A"), (190, "Lead 2", "benchmark B"), (272, "+ more", "leads, in parallel")]
DASH54, DASH43 = " stroke-dasharray=\"5 4\"", " stroke-dasharray=\"4 3\""
LH = 66                                    # lane height; LANES hold each lane's top
LX0, LX1 = 446, 1108                       # lane span
COLS = [("decision", "Decision model", "Jev · screen, rank", 9), ("worker", "Worker agents", "Sonnet · probe, fix", 5),
        ("frontier", "Frontier agents", "Claude Code · Codex", 3), ("human", "Lead", "signs the verdict", 1)]
CW, CG, C0 = 126, 16, 556                  # column width, gap, first column's left
cols_x = [C0 + i * (CW + CG) for i in range(4)]

# ----- inputs -----
card(32, 112, 176, 72, "neutral", "Benchmarks", ["any link: repo, paper, page"])
card(32, 232, 176, 72, "neutral", "Frontier agents", ["the difficulty suite"])
# ----- the index repository: the hub -----
add(f'<rect x="252" y="96" width="150" height="240" rx="12" fill="#ffffff" stroke="{TIER["neutral"][1]}" stroke-width="1.4"/>')
text(327, 124, "Index repository", 13.5, 650, INK)
text(327, 141, "git, shared by all", 11, 400, MUTE)
for i, (k, v) in enumerate([("benchmarks.toml", "the list"), ("protocol.toml", "gates, models"),
                            ("contributors.toml", "leads, keys"), ("claims", "one lead each")]):
    y = 168 + 40 * i
    add(f'<rect x="266" y="{y - 14}" width="122" height="32" rx="7" fill="{TIER["neutral"][0]}" stroke="#e1dfd8"/>')
    text(327, y, k, 11, 600, INK)
    text(327, y + 13, v, 10, 400, MUTE)
arrow("M208 148 L248 148", "added", 228, 140, flow=True)
arrow("M208 268 L248 268", "pinned", 228, 260, flow=True)

# ----- column headers: the tiers, in escalation order -----
for (kind, title, sub, _), x in zip(COLS, cols_x):
    fill, stroke, tc = TIER[kind]
    add(f'<rect x="{x}" y="28" width="{CW}" height="54" rx="10" fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>')
    text(x + CW / 2, 51, title, 12.5, 650, tc)
    text(x + CW / 2, 68, sub, 10.5, 400, MUTE)
for x in cols_x[:-1]:
    arrow(f"M{x + CW + 2} 55 L{x + CW + CG - 2} 55")
text(cols_x[1] + CW + CG / 2, 22, "escalates only on a recorded trigger", 10, 500, MUTE)

# ----- lanes -----
for li, (top, lead, bench) in enumerate(LANES):
    more = li == len(LANES) - 1
    add(f'<rect x="{LX0}" y="{top}" width="{LX1 - LX0}" height="{LH}" rx="12" fill="{"#fbfbfd" if not more else "#ffffff"}" '
        f'stroke="#d9d8e2" stroke-width="1.2"{DASH54 if more else ""}/>')
    fill, stroke, tc = TIER["human"]
    add(f'<rect x="{LX0 + 10}" y="{top + 13}" width="86" height="40" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="1.2"'
        f'{DASH43 if more else ""}/>')
    text(LX0 + 53, top + 30, lead, 11.5, 650, tc)
    text(LX0 + 53, top + 44, bench, 9.5, 400, MUTE)
    # claims: the hub hands each lead a whole benchmark
    cy = top + LH / 2
    arrow(f"M402 {216 + (li - 1) * 34} C424 {216 + (li - 1) * 34} 424 {cy} {LX0 - 4} {cy}", flow=not more)
    # each task in a work cell is a square: fewer survive each tier
    for (kind, _, _, n), x in zip(COLS, cols_x):
        fill, stroke, _ = TIER[kind]
        per = 5 if n > 5 else n
        rows = (n + per - 1) // per
        s, g = 10, 5
        gw = per * s + (per - 1) * g
        gx, gy = x + (CW - gw) / 2, top + (LH - (rows * s + (rows - 1) * g)) / 2 - 6
        for k in range(n):
            r, c = divmod(k, per)
            op = ' opacity=".45"' if more else ""
            add(f'<rect x="{gx + c * (s + g):.1f}" y="{gy + r * (s + g):.1f}" width="{s}" height="{s}" rx="2.5" fill="{stroke}"{op}/>')
    # the track the tasks travel, under the squares
    ty = top + LH - 11
    add(f'<line x1="{C0 - 12}" y1="{ty}" x2="{LX1 - 6}" y2="{ty}" stroke="#ecebf2" stroke-width="2" stroke-linecap="round"/>')
    if not more:        # tasks moving through the tiers; most drop out on the way
        dur = 7.0
        # The path: along the track, then a curve up into the Index card. A dot that drops out fades at the end of the
        # tier that dropped it, measured along this lane's own path (the curve differs from lane to lane).
        x0, x1, dy = C0 - 12, LX1 + 8, ty - 150
        curve = 1.15 * ((1148 - x1) ** 2 + dy ** 2) ** 0.5
        length = (x1 - x0) + curve
        ends = [(x + CW + CG / 2 - x0) / length for x in cols_x[:3]]      # the ends of the first three tiers
        drops = [ends[0], ends[1], ends[0], ends[2], ends[0], ends[1], ends[2], 1.0]   # 8 enter: 3, 2, 2 drop, 1 is accepted
        for k, stop in enumerate(drops):
            begin = k * dur / len(drops) + li * 0.45
            color = "#2fa67a" if stop == 1.0 else "#6b67ee"
            end = stop if stop < 1.0 else 0.98
            add(f'<circle r="4" fill="{color}" opacity="0"><animateMotion dur="{dur}s" begin="{begin:.2f}s" repeatCount="indefinite" '
                f'path="M{C0 - 12} {ty} L{LX1 + 8} {ty} C{LX1 + 30} {ty} {LX1 + 24} 150 1148 150" keyPoints="0;1" keyTimes="0;1"/>'
                f'<animate attributeName="opacity" dur="{dur}s" begin="{begin:.2f}s" repeatCount="indefinite" '
                f'values="0;1;1;0;0" keyTimes="0;0.04;{end - 0.03:.2f};{end:.2f};1"/></circle>')

# ----- outputs -----
card(1148, 104, 152, 84, "out", "Index", ["accepted tasks, reports", "signed verdicts"])
card(1148, 236, 152, 72, "neutral", "Task store", ["Hugging Face dataset"])
arrow(f"M{LX1 + 2} 141 C1126 141 1126 146 1144 146", "accepted", 1122, 128)
arrow("M1224 188 L1224 232", "uploads", 1254, 214)

# ----- the ledger: every step of every lane, signed -----
add(f'<rect x="{LX0}" y="352" width="{LX1 - LX0}" height="30" rx="9" fill="#fafaf8" stroke="#dedcd5"/>')
text((LX0 + LX1) / 2, 371, "Signed ledger · every decision, trial and verdict, pushed to the index repository by each run", 11, 500, INK)
arrow(f"M{LX0 - 4} 367 C400 367 330 367 330 340", None)
for x in cols_x:
    add(f'<line x1="{x + CW / 2}" y1="{LANES[-1][0] + LH + 2}" x2="{x + CW / 2}" y2="350" stroke="#dedcd5" stroke-width="1.2" stroke-dasharray="2 3"/>')
add("</svg>")
Path(sys.argv[1]).write_text("\n".join(out) + "\n")
print("written", len(out), "elements")
