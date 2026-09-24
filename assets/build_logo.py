"""Generate the RelationL logo files.

The wordmark is "Relation" with the final L carrying a crow's foot: the
letter's own arm runs out and forks into the "many" end of a cardinality
marker. The product in one glyph.

Two things keep the L consistent with the rest of the word:

* it is the font's *real* L outline, not a redrawn one, so its stem weight,
  arm height and proportions are the typeface's own;
* the fork is derived from that glyph's measured arm, and each prong is cut on
  a vertical, so the prongs leave the arm exactly flush with it and end on a
  common line. Angled prongs take the arm's *vertical* thickness rather than
  its perpendicular thickness, which makes the junction seamless and is the
  optical correction a type designer would apply anyway.

The text is converted to outlines, so the files carry no font dependency and
render identically everywhere. Space Grotesk is under the SIL Open Font
Licence, which permits this; the licence sits beside the font.

    uv run python assets/build_logo.py

Nothing reads this script at runtime, so it is safe to edit freely.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

HERE = Path(__file__).parent
FONT = HERE / "fonts" / "SpaceGrotesk-Medium.ttf"

WORD = "Relation"

#: Geometry.  Cap height in SVG units drives everything; the rest is measured
#: from the typeface so the drawn parts match the drawn letters.
CAP = 72.0
TRACKING = -0.012  # em, slightly tight: a wordmark should read as one object
GAP = -0.012  # em between "Relation" and the L, so the L is the last letter

#: The fork, as fractions of cap height.
FOOT = 0.54  # how far the prongs run past the end of the L's arm
SPREAD = 0.42  # how far the outer prongs rise and fall

#: Prong weight, as a fraction of the arm's.  Lighter than the arm on purpose:
#: three prongs at full arm weight overlap for most of their length and read as
#: a solid arrowhead rather than a fork.  At this weight the three bands tile
#: the arm's terminal exactly, then separate a fifth of the way along.
PRONG = 0.78

PAD = 0.22  # margin around the lockup, as a fraction of cap height

INK_LIGHT = "#17171a"
INK_DARK = "#f0f0f2"
ACCENT_LIGHT = "#9a6408"
ACCENT_DARK = "#e0a13c"


class Face:
    """A typeface, measured once and reused."""

    def __init__(self, path: Path) -> None:
        if not path.exists():
            raise SystemExit("font not found: %s" % path)
        self.font = TTFont(path)
        self.glyphs = self.font.getGlyphSet()
        self.cmap = self.font.getBestCmap()
        self.upem = self.font["head"].unitsPerEm
        self.cap = self._bounds("I")[3]
        self.scale = CAP / self.cap

    def _bounds(self, character: str):
        pen = BoundsPen(self.glyphs)
        self.glyphs[self.cmap[ord(character)]].draw(pen)
        return pen.bounds

    def advance(self, character: str) -> int:
        return self.font["hmtx"][self.cmap[ord(character)]][0]

    def corners(self, character: str) -> list[tuple[float, float]]:
        """On-curve points of a straight-sided glyph, in font units."""
        pen = RecordingPen()
        self.glyphs[self.cmap[ord(character)]].draw(pen)
        return [p for op, args in pen.value if op in ("moveTo", "lineTo") for p in args]

    def outline(self, text: str, baseline: float, left: float) -> tuple[str, float]:
        """Outline ``text`` as one path, returning it and the pen's end x."""
        pen = SVGPathPen(self.glyphs, ntos=lambda v: format(round(v, 2), "g"))
        tracking = TRACKING * self.upem * self.scale
        x = left
        for character in text:
            name = self.cmap[ord(character)]
            # Fonts measure up from the baseline; SVG measures down.
            self.glyphs[name].draw(
                TransformPen(pen, (self.scale, 0, 0, -self.scale, x, baseline))
            )
            x += self.advance(character) * self.scale + tracking
        return pen.getCommands(), x - tracking


def measure_ell(face: Face) -> dict[str, float]:
    """Read the L's stem, arm and terminal straight off the glyph.

    Space Grotesk's L is a six-point polygon, so its distinct x and y values
    are exactly the stem edges, the arm's top, and the arm's right end.
    """
    points = face.corners("L")
    xs = sorted({round(x, 3) for x, _ in points})
    ys = sorted({round(y, 3) for _, y in points})
    if len(xs) < 3 or len(ys) < 3:
        raise SystemExit("unexpected L outline; this script assumes a straight-sided L")
    return {
        "arm_right": xs[-1],
        "baseline": ys[0],
        "arm_top": ys[1],
    }


def crows_foot(face: Face, baseline: float, left: float) -> tuple[str, float]:
    """The fork, as a filled path in SVG units, flush with the L's arm.

    ``left`` is where the L glyph starts, so the fork lands on its terminal.
    """
    ell = measure_ell(face)
    start = left + ell["arm_right"] * face.scale
    top = baseline - ell["arm_top"] * face.scale
    bottom = baseline - ell["baseline"] * face.scale
    thickness = bottom - top

    end = start + FOOT * CAP
    rise = SPREAD * CAP
    weight = thickness * PRONG

    # Three parallelograms with vertical ends.  Their start edges tile the
    # arm's terminal (top band, centre band, bottom band) so the junction is
    # seamless and full width; they then diverge, and the gaps between them
    # open early because each band is lighter than the arm.  Cutting every tip
    # on `end` is what stops the fork looking ragged.
    centre = top + thickness / 2
    bands = [
        (top, top - rise),  # upper prong: starts flush with the arm's top
        (centre - weight / 2, centre - weight / 2),  # middle: straight on
        (bottom - weight, bottom + rise - weight),  # lower prong
    ]
    prongs = [
        f"M {start:g} {y0:g} L {end:g} {y1:g} L {end:g} {y1 + weight:g} L {start:g} {y0 + weight:g} Z"
        for y0, y1 in bands
    ]
    return " ".join(prongs), end


def lockup(face: Face) -> tuple[str, str, float, float]:
    """Return (text path, L path, width, height) for the whole wordmark."""
    pad = PAD * CAP
    baseline = pad + CAP

    text, text_end = face.outline(WORD, baseline, pad)
    ell_left = text_end + GAP * face.upem * face.scale

    ell, _ = face.outline("L", baseline, ell_left)
    fork, tip = crows_foot(face, baseline, ell_left)

    width = tip + pad
    height = baseline + SPREAD * CAP + pad  # the lowest prong drops below the baseline
    return text, ell + " " + fork, width, height


def svg(body: str, width: float, height: float, *, label: bool = True) -> str:
    title = "  <title>RelationL</title>\n" if label else ""
    attrs = (
        ' role="img" aria-label="RelationL"'
        if label
        else ' class="logo" aria-hidden="true"'
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}"'
        f' width="{width:.0f}" height="{height:.0f}"{attrs}>\n{title}{body}</svg>\n'
    )


def wordmark(face: Face, *, ink: str, accent: str) -> str:
    text, ell, width, height = lockup(face)
    body = f'  <path fill="{ink}" d="{text}"/>\n  <path fill="{accent}" d="{ell}"/>\n'
    return svg(body, width, height)


def wordmark_inline(face: Face) -> str:
    """The wordmark with CSS classes instead of colours.

    ``index.html`` embeds this markup so the page's own tokens drive the ink
    and the accent, which a linked <img> could not do.
    """
    text, ell, width, height = lockup(face)
    body = f'  <path class="logo-ink" d="{text}"/>\n  <path class="logo-mark" d="{ell}"/>\n'
    return svg(body, width, height, label=False)


def mark(face: Face, *, accent: str) -> str:
    """The L on its own, centred in a square, for favicons and avatars."""
    pad = PAD * CAP
    baseline = pad + CAP
    ell, _ = face.outline("L", baseline, pad)
    fork, tip = crows_foot(face, baseline, pad)

    content_width = tip - pad
    content_height = CAP + SPREAD * CAP
    side = max(content_width, content_height) + pad * 2
    dx = (side - content_width) / 2 - pad
    dy = (side - content_height) / 2 - pad

    body = (
        f'  <g transform="translate({dx:.2f} {dy:.2f})">\n'
        f'    <path fill="{accent}" d="{ell} {fork}"/>\n  </g>\n'
    )
    return svg(body, side, side)


def update_index(path: Path, inline: str) -> bool:
    """Replace the logo embedded in index.html, so the two cannot drift."""
    if not path.exists():
        return False
    markup = path.read_text(encoding="utf-8")
    pattern = re.compile(r'[ \t]*<svg[^>]*class="logo"[^>]*>.*?</svg>\n', re.S)
    if not pattern.search(markup):
        return False
    indented = "".join("        " + line + "\n" for line in inline.strip().splitlines())
    path.write_text(pattern.sub(lambda _match: indented, markup, count=1), encoding="utf-8")
    return True


def rasterise(source: Path, target: Path, width: int) -> bool:
    """Render an SVG to a transparent PNG with headless Edge or Chrome."""
    browsers = [
        r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        r"C:/Program Files/Google/Chrome/Application/chrome.exe",
        "chromium",
    ]
    text = source.read_text(encoding="utf-8")
    box = re.search(r'viewBox="0 0 (\d+) (\d+)"', text)
    if not box:
        return False
    height = round(width * int(box.group(2)) / int(box.group(1)))

    wrapper = source.with_suffix(".tmp.html")
    wrapper.write_text(
        "<!doctype html><style>html,body{margin:0;background:transparent}"
        f"svg{{width:{width}px;height:{height}px;display:block}}</style>" + text,
        encoding="utf-8",
    )
    try:
        for browser in browsers:
            if browser.endswith(".exe") and not Path(browser).exists():
                continue
            subprocess.run(
                [
                    browser,
                    "--headless=new",
                    "--disable-gpu",
                    "--default-background-color=00000000",
                    f"--window-size={width},{height}",
                    "--virtual-time-budget=2000",
                    f"--screenshot={target.resolve()}",
                    wrapper.resolve().as_uri(),
                ],
                capture_output=True,
                timeout=90,
                check=False,
            )
            if target.exists():
                return True
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        wrapper.unlink(missing_ok=True)
    return False


def main() -> None:
    face = Face(FONT)
    variants = {
        "wordmark-light.svg": wordmark(face, ink=INK_LIGHT, accent=ACCENT_LIGHT),
        "wordmark-dark.svg": wordmark(face, ink=INK_DARK, accent=ACCENT_DARK),
        "wordmark-mono.svg": wordmark(face, ink="currentColor", accent="currentColor"),
        "wordmark-ink-light.svg": wordmark(face, ink=INK_LIGHT, accent=INK_LIGHT),
        "wordmark-ink-dark.svg": wordmark(face, ink=INK_DARK, accent=INK_DARK),
        "mark-light.svg": mark(face, accent=ACCENT_LIGHT),
        "mark-dark.svg": mark(face, accent=ACCENT_DARK),
        "mark-mono.svg": mark(face, accent="currentColor"),
        "wordmark-inline.svg": wordmark_inline(face),
    }
    for name, content in variants.items():
        (HERE / name).write_text(content, encoding="utf-8")
        print("wrote", name)

    static = HERE.parent / "src" / "relationl" / "web" / "static"
    if static.is_dir():
        (static / "favicon.svg").write_text(
            mark(face, accent=ACCENT_DARK), encoding="utf-8"
        )
        print("wrote static/favicon.svg")
        if update_index(static / "index.html", variants["wordmark-inline.svg"]):
            print("updated static/index.html")

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
