"""Generate the RelationL logo files.

The wordmark is "Relation" plus an L drawn as a crow's-foot relationship: the
letter's stem and foot are the join, and the foot flares into the "many" end of
a cardinality marker. It is the product in one glyph, which is the only reason
to draw a logo at all.

The text is converted to outlines from DejaVu Sans rather than set as
``<text>``, so the files render identically everywhere and carry no font
dependency. DejaVu is licensed permissively; its licence sits next to this
script.

    uv run python assets/build_logo.py

Regenerate after changing any constant below. Nothing else reads this script at
runtime, so it is safe to edit freely.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

HERE = Path(__file__).parent

#: DejaVu ships with matplotlib; fall back to a few usual locations.
FONT_NAME = "DejaVuSans.ttf"  # regular, not bold: the wordmark reads lighter
FONT_CANDIDATES = [
    Path(
        "C:/Users/Hugo/Desktop/lilhuge/fplbot/.venv/Lib/site-packages/matplotlib"
        "/mpl-data/fonts/ttf/" + FONT_NAME
    ),
    Path("/usr/share/fonts/truetype/dejavu/" + FONT_NAME),
]

WORD = "Relation"

#: Geometry, in SVG user units.  Cap height drives everything else.
CAP = 72.0
UNITS_PER_CAP = 1493.0  # DejaVu Sans cap height, in font units
STEM = CAP * 0.1353  # the font's own vertical stem, so the L reads as a letter
TRACKING = CAP * 0.012  # a little air, which reads as deliberate rather than default
GAP = CAP * 0.05  # tight, so the L reads as the last letter and not a symbol
#: The crow's foot has to be long and wide enough that three strokes meeting at
#: a point read as three strokes rather than as a filled wedge.
FOOT = CAP * 0.58
SPREAD = CAP * 0.34
ARM = CAP * 0.46  # length of the L's foot before the crow's foot begins
PAD = CAP * 0.24

INK_LIGHT = "#1b1b1d"
INK_DARK = "#ededef"
ACCENT_LIGHT = "#9a6408"
ACCENT_DARK = "#e0a13c"


def load_font() -> TTFont:
    for candidate in FONT_CANDIDATES:
        if candidate.exists():
            return TTFont(candidate)
    raise SystemExit("%s not found; edit FONT_CANDIDATES" % FONT_NAME)


def word_outline(font: TTFont, baseline: float, left: float) -> tuple[str, float]:
    """Outline ``WORD`` as one path, returning it with the pen's end x."""
    glyphs = font.getGlyphSet()
    cmap = font.getBestCmap()
    scale = CAP / UNITS_PER_CAP
    pen = SVGPathPen(glyphs, ntos=lambda v: format(round(v, 2), "g"))

    x = left
    for character in WORD:
        name = cmap[ord(character)]
        # Flip the y axis: fonts measure up from the baseline, SVG measures down.
        transform = TransformPen(pen, (scale, 0, 0, -scale, x, baseline))
        glyphs[name].draw(transform)
        x += font["hmtx"][name][0] * scale + TRACKING
    return pen.getCommands(), x - TRACKING


def ell_path(baseline: float, left: float) -> tuple[list[str], float]:
    """The L: a stem, a foot, and a crow's foot flaring off the end."""
    centre = left + STEM / 2  # stroke is centred, the letter's left edge is `left`
    arm_y = baseline - STEM / 2  # so the foot's underside sits on the baseline
    arm_end = left + STEM + ARM
    tip = arm_end + FOOT

    strokes = [
        # Stem down into the foot, as one mitred polyline.
        f"M {centre:g} {baseline - CAP:g} L {centre:g} {arm_y:g} L {arm_end:g} {arm_y:g}",
        # Crow's foot: three prongs diverging from where the foot ends.
        f"M {arm_end:g} {arm_y:g} L {tip:g} {arm_y - SPREAD:g}",
        f"M {arm_end:g} {arm_y:g} L {tip:g} {arm_y:g}",
        f"M {arm_end:g} {arm_y:g} L {tip:g} {arm_y + SPREAD:g}",
    ]
    return strokes, tip


def wordmark(font: TTFont, *, ink: str, accent: str) -> str:
    baseline = PAD + CAP
    text, text_end = word_outline(font, baseline, PAD)
    strokes, tip = ell_path(baseline, text_end + GAP)

    width = tip + PAD
    # The lowest prong dips below the baseline, so the box has to allow for it.
    height = baseline + SPREAD - STEM / 2 + PAD

    joined = " ".join(strokes)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}"\
 width="{width:.0f}" height="{height:.0f}" role="img" aria-label="RelationL">
  <title>RelationL</title>
  <path fill="{ink}" d="{text}"/>
  <path d="{joined}" fill="none" stroke="{accent}" stroke-width="{STEM:g}"\
 stroke-linecap="butt" stroke-linejoin="miter"/>
</svg>
"""


def wordmark_inline(font: TTFont) -> str:
    """The wordmark with CSS classes instead of colours.

    ``index.html`` embeds this markup directly so the page's own tokens drive
    the ink and the accent, which a linked <img> could not do.
    """
    return (
        wordmark(font, ink="INK", accent="ACCENT")
        .replace('fill="INK"', 'class="logo-ink"')
        .replace('stroke="ACCENT"', 'class="logo-mark"')
        .replace(' role="img" aria-label="RelationL"', ' class="logo" aria-hidden="true"')
        .replace("  <title>RelationL</title>" + chr(10), "")
    )


def mark(*, accent: str) -> str:
    """The L on its own, centred in a square, for favicons and avatars."""
    strokes, tip = ell_path(PAD + CAP, PAD)

    content_width = tip - PAD
    content_height = CAP + SPREAD - STEM / 2
    side = max(content_width, content_height) + PAD * 2
    offset_x = (side - content_width) / 2 - PAD
    offset_y = (side - content_height) / 2 - PAD

    joined = " ".join(strokes)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side:.0f} {side:.0f}"\
 width="{side:.0f}" height="{side:.0f}" role="img" aria-label="RelationL">
  <title>RelationL</title>
  <g transform="translate({offset_x:.2f} {offset_y:.2f})">
    <path d="{joined}" fill="none" stroke="{accent}" stroke-width="{STEM:g}"\
 stroke-linecap="butt" stroke-linejoin="miter"/>
  </g>
</svg>
"""


def rasterise(svg: Path, png: Path, width: int) -> bool:
    """Render an SVG to a transparent PNG with headless Edge or Chrome."""
    browsers = [
        r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        r"C:/Program Files/Google/Chrome/Application/chrome.exe",
        "chromium",
    ]
    text = svg.read_text(encoding="utf-8")
    import re

    box = re.search(r'viewBox="0 0 (\d+) (\d+)"', text)
    if not box:
        return False
    ratio = int(box.group(2)) / int(box.group(1))
    height = round(width * ratio)

    wrapper = svg.with_suffix(".tmp.html")
    wrapper.write_text(
        "<!doctype html><style>html,body{margin:0;background:transparent}"
        f"svg{{width:{width}px;height:{height}px;display:block}}</style>" + text,
        encoding="utf-8",
    )
    try:
        for browser in browsers:
            if browser.endswith(".exe") and not Path(browser).exists():
                continue
            result = subprocess.run(
                [
                    browser,
                    "--headless=new",
                    "--disable-gpu",
                    "--default-background-color=00000000",
                    f"--window-size={width},{height}",
                    "--virtual-time-budget=2000",
                    f"--screenshot={png.resolve()}",
                    wrapper.resolve().as_uri(),
                ],
                capture_output=True,
                timeout=90,
                check=False,
            )
            if png.exists():
                return True
            del result
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        wrapper.unlink(missing_ok=True)
    return False


def main() -> None:
    font = load_font()
    variants = {
        "wordmark-light.svg": wordmark(font, ink=INK_LIGHT, accent=ACCENT_LIGHT),
        "wordmark-dark.svg": wordmark(font, ink=INK_DARK, accent=ACCENT_DARK),
        "wordmark-mono.svg": wordmark(font, ink="currentColor", accent="currentColor"),
        "wordmark-ink-light.svg": wordmark(font, ink=INK_LIGHT, accent=INK_LIGHT),
        "wordmark-ink-dark.svg": wordmark(font, ink=INK_DARK, accent=INK_DARK),
        "mark-light.svg": mark(accent=ACCENT_LIGHT),
        "mark-dark.svg": mark(accent=ACCENT_DARK),
        "mark-mono.svg": mark(accent="currentColor"),
        "wordmark-inline.svg": wordmark_inline(font),
    }

    for name, content in variants.items():
        (HERE / name).write_text(content, encoding="utf-8")
        print("wrote", name)

    # The web app needs the mark for its favicon.
    static = HERE.parent / "src" / "relationl" / "web" / "static"
    if static.is_dir():
        (static / "favicon.svg").write_text(mark(accent=ACCENT_DARK), encoding="utf-8")
        print("wrote", (static / "favicon.svg").name)

    for name, width in [
        ("wordmark-light.svg", 960),
        ("wordmark-dark.svg", 960),
        ("wordmark-ink-light.svg", 960),
        ("wordmark-ink-dark.svg", 960),
        ("mark-light.svg", 512),
        ("mark-dark.svg", 512),
    ]:
        source = HERE / name
        target = source.with_suffix(".png")
        print(("wrote " if rasterise(source, target, width) else "SKIPPED ") + target.name)


if __name__ == "__main__":
    main()
