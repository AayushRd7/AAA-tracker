#!/usr/bin/env python3
"""Generate the site's raster assets (favicons, app icons, Open Graph cards).

The marketing site is plain static files. Social crawlers do not render SVG, so
the Open Graph images and the touch icons are rendered here and committed
alongside the pages. Re-run after changing a page headline:

    python3 site/tools/make_og.py

Rendering uses headless Google Chrome so the cards pick up the site's real
typefaces (Schibsted Grotesk, IBM Plex Mono) from site/assets/fonts. Chrome must
be installed; on macOS it is found in /Applications, elsewhere set the CHROME
environment variable to the binary. Pillow is only used to assemble favicon.ico
from the rendered PNGs.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)
IMG = os.path.join(SITE, "assets", "img")
FONTS = os.path.join(SITE, "assets", "fonts")
OG = os.path.join(IMG, "og")

CHROME_CANDIDATES = [
    os.environ.get("CHROME", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    sys.exit("make_og.py: Google Chrome not found — install it or set CHROME=<path>")


MARK = """<svg viewBox="0 0 32 32" fill="none" aria-hidden="true" style="width:46px;height:46px">
  <defs><linearGradient id="ogmk" x1="2" y1="0" x2="30" y2="32" gradientUnits="userSpaceOnUse">
    <stop stop-color="#3D6DFF"/><stop offset="1" stop-color="#1230C2"/></linearGradient></defs>
  <rect width="32" height="32" rx="8.6" fill="url(#ogmk)"/>
  <path d="M16 9 L8.2 24.6" stroke="#fff" stroke-width="3.2" stroke-linecap="round"/>
  <path d="M16 9 L23.8 24.6" stroke="#fff" stroke-width="3.2" stroke-linecap="round"/>
  <path d="M11.3 18.6 H20.7" stroke="#fff" stroke-width="2.2" stroke-linecap="round" opacity=".5"/>
  <circle cx="16" cy="9" r="2.7" fill="#fff"/><circle cx="16" cy="9" r="1.3" fill="#1230C2"/>
  <circle cx="8.2" cy="24.6" r="2" fill="#5BF08D"/><circle cx="23.8" cy="24.6" r="2" fill="#5BF08D"/>
</svg>"""

OG_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><style>
@font-face{font-family:'Schibsted Grotesk';font-style:normal;font-weight:400 800;
  src:url(FONTS/schibsted-grotesk-normal-400-800-latin.woff2) format('woff2');}
@font-face{font-family:'IBM Plex Mono';font-style:normal;font-weight:500;
  src:url(FONTS/ibm-plex-mono-normal-500-latin.woff2) format('woff2');}
*{box-sizing:border-box;margin:0}
body{width:1200px;height:630px;background:#ffffff;color:#3a4252;
  font-family:'Schibsted Grotesk',system-ui,sans-serif;padding:64px 72px;position:relative;overflow:hidden;
  display:flex;flex-direction:column}
.glow{position:absolute;left:50%;top:-380px;transform:translateX(-50%);width:1300px;height:820px;
  background:radial-gradient(48% 46% at 50% 50%,rgba(31,75,240,.17) 0%,rgba(31,75,240,.06) 44%,rgba(31,75,240,0) 72%)}
.grid{position:absolute;inset:0;
  background-image:linear-gradient(to right,rgba(10,13,22,.045) 1px,transparent 1px),
    linear-gradient(to bottom,rgba(10,13,22,.045) 1px,transparent 1px);
  background-size:64px 64px;
  -webkit-mask-image:radial-gradient(72% 58% at 50% 18%,#000 0%,transparent 78%)}
.top{display:flex;align-items:center;gap:14px;position:relative}
.word{font-size:29px;letter-spacing:-.03em;color:#0A0D16;line-height:1;font-weight:800}
.word i{font-style:normal;font-weight:500;color:#6b7280;margin-left:1px}
.mid{margin-top:60px;flex:1;position:relative}
.eyebrow{font-family:'IBM Plex Mono';font-size:14px;letter-spacing:.12em;text-transform:uppercase;
  color:#1F4BF0;display:inline-flex;align-items:center;gap:9px;margin-bottom:26px;
  background:#eef3ff;border:1px solid #dfe8ff;border-radius:999px;padding:8px 15px 8px 12px}
.tri{display:inline-flex;align-items:flex-end;gap:2.5px;height:11px}
.tri i{display:block;width:3px;border-radius:1.5px;background:#1F4BF0}
.tri i:nth-child(1){height:5px;opacity:.45}.tri i:nth-child(2){height:8px;opacity:.7}.tri i:nth-child(3){height:11px}
h1{font-weight:800;font-size:66px;line-height:1.04;letter-spacing:-.035em;color:#0A0D16;max-width:17ch}
h1 .ac{color:#1F4BF0}
.sub{margin-top:24px;font-size:21px;line-height:1.5;color:#6b7280;max-width:52ch;font-weight:400}
.foot{position:absolute;left:72px;right:72px;bottom:46px;display:flex;justify-content:space-between;
  font-family:'IBM Plex Mono';font-size:14px;letter-spacing:.1em;text-transform:uppercase;color:#8b93a3}
</style></head><body>
<div class="glow"></div><div class="grid"></div>
<div class="top">MARKUP<span class="word">AAA<i>Tracker</i></span></div>
<div class="mid">
  <div class="eyebrow"><span class="tri"><i></i><i></i><i></i></span>EYEBROW</div>
  <h1>HEADLINE</h1>
  <div class="sub">SUB</div>
</div>
<div class="foot"><span>aaatracker.website</span><span>Every click, accounted for</span></div>
</body></html>"""

# slug -> (eyebrow, headline with an optional <span class="ac">, subtitle)
PAGES = {
    "og-home": (
        "Server-side click tracking",
        'Every click, <span class="ac">accounted&nbsp;for</span>',
        "Traffic routing, server-side conversion tracking, cost sync and anti-fraud — in one panel.",
    ),
    "og-features": (
        "Features",
        'The routing engine, and everything <span class="ac">around&nbsp;it</span>',
        "30+ filters, postbacks with de-duplication, cost sync, fraud scoring, rules and reports.",
    ),
    "og-integrations": (
        "Integrations",
        'Your traffic in. Your platforms <span class="ac">out</span>',
        "Sources with real macros, server-side conversion APIs and ad-platform cost sync.",
    ),
    "og-pricing": (
        "Pricing",
        'One meter. No add-on <span class="ac">store</span>',
        "Start with a 14-day free trial, then pay for what you track. Starter $49, Growth $149, Scale $499. Cost sync and CAPI on every paid plan.",
    ),
    "og-security": (
        "Security &amp; anti-fraud",
        'Your data, your numbers, <span class="ac">nobody&nbsp;else’s</span>',
        "Workspace isolation, encrypted tokens, postback protection and 0–100 fraud scoring.",
    ),
    "og-about": (
        "About",
        'Built by performance <span class="ac">marketers</span>',
        "The tracker we wanted: fast, honest about attribution, clear about where every dollar went.",
    ),
    "og-start": (
        "Get started",
        'Start your <span class="ac">14-day free trial</span>',
        "Create a workspace, connect a traffic source and add a campaign — no credit card to start.",
    ),
    "og-contact": (
        "Contact",
        'Talk to the team behind <span class="ac">AAA&nbsp;Tracker</span>',
        "Send the setup, not a sales form. We reply within one business day.",
    ),
}

ICONS = [("favicon-32.png", 32), ("icon-192.png", 192),
         ("icon-512.png", 512), ("apple-touch-icon.png", 180)]


def render(chrome: str, url: str, out: str, size: tuple[int, int], tmpdir: str) -> None:
    shot = os.path.join(tmpdir, os.path.basename(out) + ".tmp.png")
    subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
         "--default-background-color=00000000",
         f"--window-size={size[0]},{size[1]}", f"--screenshot={shot}", url],
        check=True, capture_output=True, timeout=60,
    )
    os.replace(shot, out)
    print("wrote", os.path.relpath(out, SITE))


def main() -> None:
    chrome = find_chrome()
    os.makedirs(OG, exist_ok=True)
    mark_uri = "file://" + os.path.join(IMG, "logo-mark.svg")
    fonts_uri = "file://" + FONTS

    with tempfile.TemporaryDirectory() as tmpdir:
        # --- Open Graph cards ---
        for slug, (eyebrow, headline, sub) in PAGES.items():
            html = (OG_TEMPLATE.replace("FONTS", fonts_uri)
                    .replace("MARKUP", MARK)
                    .replace("EYEBROW", eyebrow)
                    .replace("HEADLINE", headline)
                    .replace("SUB", sub))
            tpl = os.path.join(tmpdir, slug + ".html")
            with open(tpl, "w") as fh:
                fh.write(html)
            render(chrome, "file://" + tpl, os.path.join(OG, slug + ".png"),
                   (1200, 630), tmpdir)

        # --- icons: sized wrapper so the SVG is not left at its default size ---
        for name, size in ICONS:
            tpl = os.path.join(tmpdir, f"icon-{size}.html")
            with open(tpl, "w") as fh:
                fh.write(
                    f'<!doctype html><body style="margin:0;width:{size}px;height:{size}px">'
                    f'<img src="{mark_uri}" width="{size}" height="{size}" style="display:block">'
                )
            render(chrome, "file://" + tpl, os.path.join(IMG, name), (size, size), tmpdir)

    # --- favicon.ico from the rendered 512px mark ---
    icon = Image.open(os.path.join(IMG, "icon-512.png"))
    icon.save(os.path.join(IMG, "favicon.ico"), format="ICO", sizes=[(32, 32), (16, 16)])
    print("wrote assets/img/favicon.ico")


if __name__ == "__main__":
    main()
