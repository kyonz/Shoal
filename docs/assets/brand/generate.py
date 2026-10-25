# Regenerates the Shoal brand SVGs (needs fonttools and Inter Bold). Usage: python generate.py <out-dir>
import sys, os
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
out = sys.argv[1]; os.makedirs(out, exist_ok=True)

NAVY, TEAL, CYAN, CORAL = "#0B2E4F", "#0E9F8E", "#38BDF8", "#FF7A59"
# One fish facing right, nose at (10,0), ~20 long.
FISH = "M10 0C7 -4.6 -0.5 -5.2 -4.6 -1.4L-9.4 -5L-8 0L-9.4 5L-4.6 1.4C-0.5 5.2 7 4.6 10 0Z"
# The school: small fish arranged as one big fish swimming right; the lead fish is coral.
SCHOOL = [  # (x, y, scale, colour)
    (12, 32, .78, "a"), (12, 48, .78, "a"),          # tail fins
    (28, 40, .86, "b"),                              # tail stem
    (44, 27, .92, "b"), (44, 40, .92, "c"), (44, 53, .92, "b"),
    (62, 22, 1.0, "c"), (62, 40, 1.0, "b"), (62, 58, 1.0, "c"),
    (80, 31, 1.0, "b"), (80, 49, 1.0, "c"),
    (96, 40, 1.08, "lead"),
]
def school(palette):
    return "".join(
        f'<use href="#f" transform="translate({x} {y}) scale({s})" fill="{palette[c]}"/>' for x, y, s, c in SCHOOL)
DEFS = f'<defs><path id="f" d="{FISH}"/></defs>'
light = {"a": CYAN, "b": TEAL, "c": "#0C7C8C", "lead": CORAL}
dark = {"a": "#7DD3FC", "b": "#2DD4BF", "c": "#5EEAD4", "lead": CORAL}
mono = {k: "currentColor" for k in light}

def svg(w, h, body, vb=None, title="Shoal"):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'viewBox="{vb or f"0 0 {w} {h}"}" width="{w}" height="{h}" role="img" aria-label="{title}">'
            f'<title>{title}</title>{DEFS}{body}</svg>\n')

def wordmark_path():
    f = TTFont("/usr/share/fonts/opentype/inter/Inter-Bold.otf")
    gs, cmap = f.getGlyphSet(), f.getBestCmap()
    upm = f["head"].unitsPerEm
    pen = SVGPathPen(gs); x = 0
    for ch in "shoal":
        g = cmap[ord(ch)]
        gs[g].draw(TransformPen(pen, (1, 0, 0, -1, x, 0)))
        x += gs[g].width - 30  # slightly tight tracking
    return pen.getCommands(), x, upm

d, adv, upm = wordmark_path()
k = 64 / upm  # cap ~ 46px
def wordmark(fill, palette, w=330, h=80):
    return (f'<g>{school(palette)}</g>'
            f'<path transform="translate(122 62) scale({k:.5f})" fill="{fill}" d="{d}"/>')
wm_w = int(122 + adv * k + 6)
open(f"{out}/shoal-icon.svg", "w").write(svg(112, 80, school(light), "0 0 112 80"))
open(f"{out}/shoal-icon-mono.svg", "w").write(svg(112, 80, f'<g style="color:#0B2E4F">{school(mono)}</g>', "0 0 112 80"))
open(f"{out}/shoal-logo.svg", "w").write(svg(wm_w, 80, wordmark(NAVY, light)))
open(f"{out}/shoal-logo-dark.svg", "w").write(svg(wm_w, 80, wordmark("#F1F5F9", dark)))
# Favicon: square tile, school centred, adapts to dark tabs.
fav = (f'<style>.t{{fill:{NAVY}}}@media (prefers-color-scheme:dark){{.t{{fill:#0F172A}}}}</style>'
       f'<rect class="t" width="128" height="128" rx="28"/>'
       f'<use href="#f" transform="translate(38 41) scale(2.4)" fill="#2DD4BF"/>'
       f'<use href="#f" transform="translate(38 87) scale(2.4)" fill="#7DD3FC"/>'
       f'<use href="#f" transform="translate(86 64) scale(2.6)" fill="{CORAL}"/>')
open(f"{out}/favicon.svg", "w").write(svg(128, 128, fav, "0 0 128 128"))
# Social preview card 1280x640 for the GitHub repo settings.
card = (f'<rect width="1280" height="640" fill="{NAVY}"/>'
        f'<g transform="translate(250 200) scale(2.4)">{wordmark("#F8FAFC", dark)}</g>'
        f'<text x="640" y="480" text-anchor="middle" font-family="Inter, Helvetica, Arial, sans-serif" font-size="34" fill="#94A3B8">'
        f'Open-source orchestration for multi-agent AI workflows</text>')
open(f"{out}/social-preview.svg", "w").write(svg(1280, 640, card))
print("wordmark width", wm_w)
