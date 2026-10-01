#!/usr/bin/env python3
"""Generate the site's raster assets (favicons, app icons, Open Graph cards).

The marketing site is plain static files. Social crawlers do not render SVG, so
the Open Graph images and the touch icons are rendered here and committed
alongside the pages. Re-run after changing a page headline:

    python3 site/tools/make_og.py

Fonts fall back to any installed Helvetica/Arial so the script runs on a bare
machine.
"""
from __future__ import annotations

import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)
IMG = os.path.join(SITE, "assets", "img")
OG = os.path.join(IMG, "og")

INK = (10, 16, 28)
INK_2 = (16, 25, 44)
GREEN = (34, 197, 94)
GREEN_DARK = (21, 128, 61)
TEXT = (234, 240, 248)
MUTED = (154, 169, 191)
MUTED_2 = (111, 127, 150)
LINE = (36, 50, 74)

FONT_CANDIDATES_BOLD = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
FONT_CANDIDATES_REG = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    for path in (FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES_REG):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def mark(size: int, radius_ratio: float = 0.235) -> Image.Image:
    """The app mark: rounded gradient tile with a rising polyline."""
    S = size * 4
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    r = int(S * radius_ratio)
    # vertical gradient inside a rounded rectangle
    grad = Image.new("RGB", (1, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / max(1, S - 1)
        gd.point((0, y), fill=(
            int(GREEN[0] * (1 - t) + GREEN_DARK[0] * t),
            int(GREEN[1] * (1 - t) + GREEN_DARK[1] * t),
            int(GREEN[2] * (1 - t) + GREEN_DARK[2] * t),
        ))
    grad = grad.resize((S, S))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=r, fill=255)
    im.paste(grad, (0, 0), mask)

    w = S * 0.088
    pts = [(0.20, 0.72), (0.41, 0.47), (0.53, 0.59), (0.77, 0.28)]
    pts = [(x * S, y * S) for x, y in pts]
    d.line(pts, fill=(255, 255, 255, 255), width=int(w), joint="curve")
    # arrow head
    d.line([(0.59 * S, 0.28 * S), (0.77 * S, 0.28 * S), (0.77 * S, 0.46 * S)],
           fill=(255, 255, 255, 255), width=int(w), joint="curve")
    for cx in (0.22, 0.375, 0.53):
        rr = S * 0.062
        d.ellipse([cx * S - rr, 0.27 * S - rr, cx * S + rr, 0.27 * S + rr],
                  fill=(255, 255, 255, 140))
    return im.resize((size, size), Image.LANCZOS)


def rounded(im: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.size[0] - 1, im.size[1] - 1],
                                           radius=radius, fill=255)
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def wrap(draw, text, fnt, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=fnt) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def og_card(kicker: str, title: str, subtitle: str, out: str) -> None:
    W, H = 1200, 630
    im = Image.new("RGB", (W, H), INK)
    d = ImageDraw.Draw(im)

    # subtle grid
    for x in range(0, W, 56):
        d.line([(x, 0), (x, H)], fill=(22, 32, 52))
    for y in range(0, H, 56):
        d.line([(0, y), (W, y)], fill=(22, 32, 52))

    # soft green glow: concentric ellipses accumulate into a radial falloff
    glow = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(glow)
    for i in range(46):
        t = i / 45
        pad = int(300 * t)
        gd.ellipse([-160 - pad, -240 - pad, 520 + pad, 300 + pad], fill=3)
    for i in range(34):
        t = i / 33
        pad = int(240 * t)
        gd.ellipse([880 - pad, -240 - pad, 1440 + pad, 200 + pad], fill=2)
    im.paste(Image.new("RGB", (W, H), GREEN), (0, 0), glow)

    d.rectangle([0, 0, W - 1, H - 1], outline=LINE)

    # brand
    m = mark(66)
    im.paste(m, (72, 62), m)
    d.text((156, 78), "AAA", font=font(True, 30), fill=TEXT)
    w_aaa = d.textlength("AAA", font=font(True, 30))
    d.text((156 + w_aaa + 10, 78), "TRACKER", font=font(False, 30), fill=MUTED)

    # kicker
    d.text((72, 196), kicker.upper(), font=font(True, 21), fill=GREEN)

    # title
    tf = font(True, 62)
    lines = wrap(d, title, tf, W - 168)[:3]
    y = 244
    for line in lines:
        d.text((72, y), line, font=tf, fill=TEXT)
        y += 74

    # subtitle
    sf = font(False, 26)
    sy = min(y + 14, 520)
    for line in wrap(d, subtitle, sf, W - 200)[:2]:
        d.text((72, sy), line, font=sf, fill=MUTED)
        sy += 36

    d.line([(72, 548), (W - 72, 548)], fill=LINE)
    d.text((72, 566), "aaatracker.website", font=font(False, 22), fill=MUTED_2)
    d.text((W - 320, 566), "Every click, accounted for.", font=font(False, 22), fill=MUTED_2)

    im = rounded(im, 0)
    im.save(out, "PNG", optimize=True)
    print("wrote", out)


def main() -> None:
    os.makedirs(OG, exist_ok=True)

    for size, name in ((180, "apple-touch-icon.png"), (192, "icon-192.png"),
                       (512, "icon-512.png"), (32, "favicon-32.png")):
        m = mark(size)
        m.save(os.path.join(IMG, name), "PNG")
        print("wrote", os.path.join(IMG, name))

    # multi-size .ico for legacy browsers
    ico = [mark(s) for s in (16, 32, 48)]
    ico[0].save(os.path.join(IMG, "favicon.ico"), sizes=[(16, 16), (32, 32), (48, 48)])

    cards = {
        "og-home.png": (
            "Affiliate & performance tracking",
            "Every click, accounted for.",
            "Traffic routing, conversion tracking and anti-fraud in one place — with the "
            "numbers your ad platforms agree with.",
        ),
        "og-features.png": (
            "Features",
            "The routing engine, and everything around it.",
            "30+ filters, A/B landers, funnels, caps, dayparting, cost sync and an "
            "optimizer that reweights flows from real performance.",
        ),
        "og-integrations.png": (
            "Integrations",
            "Your traffic in, your platforms out.",
            "Traffic sources, affiliate networks, server-side conversion APIs and ad-platform "
            "cost sync — connected, not pasted by hand.",
        ),
        "og-security.png": (
            "Security & anti-fraud",
            "Your data, your numbers, nobody else's.",
            "Per-workspace isolation, encrypted tokens, protected postbacks, bot filtering "
            "and a 0–100 fraud score on every click.",
        ),
        "og-pricing.png": (
            "Pricing",
            "Start free. Scale when the numbers say so.",
            "No per-event surprises, no feature paywalls on the tracking you need to run a "
            "campaign properly.",
        ),
        "og-about.png": (
            "About",
            "Built by performance marketers.",
            "We built the tracker we wanted: fast, honest about attribution, and clear about "
            "where every dollar went.",
        ),
    }
    for name, (kicker, title, sub) in cards.items():
        og_card(kicker, title, sub, os.path.join(OG, name))


if __name__ == "__main__":
    main()
