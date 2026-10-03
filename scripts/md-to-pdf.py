#!/usr/bin/env python3
"""Render a Markdown document to PDF using headless Chrome.

Kept dependency-free on purpose: no pandoc, no python-markdown. It understands
exactly the subset the repository's docs use — headings, paragraphs, fenced
code, pipe tables, unordered and ordered lists, blockquotes, horizontal rules,
and inline bold / italic / code / links.

    python3 scripts/md-to-pdf.py DESIGN-SYSTEM.md [more.md ...]

Each input produces a sibling .pdf. Chrome must be installed; set CHROME to
override the binary path.
"""
from __future__ import annotations

import html
import os
import re
import subprocess
import sys
import tempfile

CHROME_CANDIDATES = [
    os.environ.get("CHROME", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

CSS = """
*{box-sizing:border-box}
@page{size:A4;margin:16mm 15mm 18mm}
body{margin:0;font:11pt/1.62 -apple-system,"Helvetica Neue",Helvetica,Arial,sans-serif;
     color:#242a36;-webkit-print-color-adjust:exact;print-color-adjust:exact}
h1{font-size:26pt;line-height:1.12;letter-spacing:-.02em;margin:0 0 6mm;color:#0a0d16}
h2{font-size:15pt;line-height:1.2;letter-spacing:-.015em;margin:9mm 0 3mm;color:#0a0d16;
   padding-top:3mm;border-top:1px solid #e4e9f2;break-after:avoid}
h3{font-size:12pt;margin:6mm 0 2mm;color:#0a0d16;break-after:avoid}
h4{font-size:10.5pt;margin:5mm 0 1.5mm;color:#0a0d16;break-after:avoid}
p{margin:0 0 3mm}
ul,ol{margin:0 0 3mm;padding-left:6mm}
li{margin:0 0 1.2mm}
li::marker{color:#1f4bf0}
a{color:#1f4bf0;text-decoration:none}
code{font-family:"SF Mono",Menlo,Consolas,monospace;font-size:9pt;
     background:#f5f7fb;border:1px solid #e4e9f2;border-radius:3px;padding:.4mm 1mm;color:#1430a8}
pre{background:#0a0d16;color:#e7ecf8;border-radius:3mm;padding:4mm 4.5mm;overflow:hidden;
    font-family:"SF Mono",Menlo,Consolas,monospace;font-size:8.6pt;line-height:1.5;margin:0 0 4mm;
    break-inside:avoid}
pre code{background:none;border:0;padding:0;color:inherit;font-size:inherit}
table{width:100%;border-collapse:collapse;margin:0 0 4mm;font-size:9.2pt;break-inside:avoid}
th{text-align:left;font-size:7.8pt;letter-spacing:.07em;text-transform:uppercase;color:#8b93a3;
   border-bottom:1.4px solid #e4e9f2;padding:2mm 2.5mm;font-weight:600}
td{border-bottom:1px solid #eff2f8;padding:2mm 2.5mm;vertical-align:top}
tr:last-child td{border-bottom:0}
blockquote{margin:0 0 4mm;padding:3mm 4mm;background:#f5f7fb;border-left:3px solid #1f4bf0;
           border-radius:0 2mm 2mm 0;color:#3a4252}
blockquote p:last-child{margin:0}
hr{border:0;border-top:1px solid #e4e9f2;margin:6mm 0}
strong{color:#0a0d16;font-weight:600}
"""


def find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    sys.exit("md-to-pdf.py: Google Chrome not found — install it or set CHROME=<path>")


def inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", out)
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', out)
    return out


def convert(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    def flush_list(kind: str, items: list[str]) -> None:
        if items:
            out.append(f"<{kind}>" + "".join(f"<li>{x}</li>" for x in items) + f"</{kind}>")

    while i < n:
        line = lines[i]

        # fenced code
        if line.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].startswith("```"):
                buf.append(html.escape(lines[i]))
                i += 1
            i += 1
            out.append("<pre><code>" + "\n".join(buf) + "</code></pre>")
            continue

        # table
        if line.lstrip().startswith("|") and i + 1 < n and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < n and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            t = ["<table><thead><tr>"]
            t += [f"<th>{inline(c)}</th>" for c in header]
            t.append("</tr></thead><tbody>")
            for r in rows:
                t.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("".join(t))
            continue

        # heading
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline(m.group(2).strip())}</h{lvl}>")
            i += 1
            continue

        # hr
        if re.match(r"^\s*(-{3,}|\*{3,})\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        # blockquote
        if line.lstrip().startswith(">"):
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip()[1:].strip())
                i += 1
            out.append("<blockquote>" + "".join(f"<p>{inline(b)}</p>" for b in buf if b) + "</blockquote>")
            continue

        # lists
        if re.match(r"^\s*[-*]\s+", line) or re.match(r"^\s*\d+\.\s+", line):
            ordered = bool(re.match(r"^\s*\d+\.\s+", line))
            items = []
            while i < n:
                cur = lines[i]
                mm = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)$", cur)
                if not mm:
                    break
                buf = mm.group(1)
                i += 1
                while i < n and lines[i].startswith(("  ", "\t")) and lines[i].strip():
                    buf += " " + lines[i].strip()
                    i += 1
                items.append(inline(buf))
            flush_list("ol" if ordered else "ul", items)
            continue

        # blank
        if not line.strip():
            i += 1
            continue

        # paragraph
        buf = [line.strip()]
        i += 1
        while i < n and lines[i].strip() and not re.match(r"^(#{1,4}\s|```|\s*[-*]\s|\s*\d+\.\s|\||>)", lines[i]):
            buf.append(lines[i].strip())
            i += 1
        out.append(f"<p>{inline(' '.join(buf))}</p>")

    return "\n".join(out)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    chrome = find_chrome()
    for src in args:
        with open(src) as fh:
            md = fh.read()
        title = os.path.splitext(os.path.basename(src))[0].replace("-", " ")
        doc = (f'<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
               f"<style>{CSS}</style></head><body>{convert(md)}</body></html>")
        page = os.path.splitext(src)[0] + ".html"
        with open(page, "w") as fh:
            fh.write(doc)
        pdf = os.path.splitext(src)[0] + ".pdf"
        subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={os.path.abspath(pdf)}", "file://" + os.path.abspath(page)],
            check=True, capture_output=True, timeout=120,
        )
        os.remove(page)
        print("wrote", pdf, f"({os.path.getsize(pdf)} bytes)")


if __name__ == "__main__":
    main()
