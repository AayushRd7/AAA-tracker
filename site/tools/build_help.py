#!/usr/bin/env python3
"""Build the AAA Tracker help centre.

Reads site/help/_articles/manifest.json and the article fragments
(site/help/_articles/<slug>.html, body-only HTML starting at <h2>) and writes:

  site/help/index.html            the hub (hero, search, categories, popular)
  site/help/<slug>.html           one full page per available fragment
  site/help/search-index.json     client-side search data
  site/sitemap.xml                a delimited help block

Articles whose fragment does not exist yet are skipped with a warning, so the
build keeps working while the rest of the library is still being written.

Stdlib only. Re-running it is safe and idempotent.
"""

import html
import json
import os
import re
import sys

# ── paths ───────────────────────────────────────────────────────────────────
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.dirname(TOOLS_DIR)
HELP_DIR = os.path.join(SITE_DIR, "help")
ARTICLES_DIR = os.path.join(HELP_DIR, "_articles")
MANIFEST = os.path.join(ARTICLES_DIR, "manifest.json")
SITEMAP = os.path.join(SITE_DIR, "sitemap.xml")
START_PAGE = os.path.join(SITE_DIR, "start.html")

SITE_ORIGIN = "https://aaatracker.website"
HELP_URL = SITE_ORIGIN + "/help/"

# Display order of categories on the hub. The manifest's `category` value must
# match one of these exactly.
CATEGORIES = [
    "Getting started",
    "Tracking & postbacks",
    "Traffic sources & networks",
    "Ad platforms",
    "Landings & domains",
    "Reports & logs",
    "Account & access",
    "Troubleshooting",
]

HELP_ASSET_VERSION = "1"


# ── small helpers ───────────────────────────────────────────────────────────

def read_asset_version(filename, pattern, default):
    """Read a `?v=N` cache-buster out of start.html so generated pages match it."""
    try:
        with open(START_PAGE, encoding="utf-8") as fh:
            source = fh.read()
    except OSError:
        return default
    match = re.search(pattern, source)
    return match.group(1) if match else default


SITE_CSS_VERSION = read_asset_version(
    "site.css", r"site\.css\?v=([0-9A-Za-z._-]+)", "7")
FONTS_CSS_VERSION = read_asset_version(
    "fonts.css", r"fonts\.css\?v=([0-9A-Za-z._-]+)", "2")


def esc(value):
    """Escape text for use in HTML text or a double-quoted attribute."""
    return html.escape(str(value), quote=True)


def strip_tags(markup):
    """Collapse an HTML fragment to plain text (used for search + TOC labels)."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def slugify(text):
    text = strip_tags(text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "section"


def pretty_date(iso):
    """2026-10-05 -> 5 October 2026 (for the visible updated line)."""
    try:
        year, month, day = (int(part) for part in iso.split("-"))
    except (ValueError, AttributeError):
        return iso
    months = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
    return "%d %s %d" % (day, months[month - 1], year)


# ── heading extraction: inject ids and build the table of contents ──────────

HEADING_RE = re.compile(r"<h([23])(\s[^>]*)?>(.*?)</h\1>",
                        re.DOTALL | re.IGNORECASE)


def process_headings(body):
    """Return (body_with_ids, headings) where headings is [(level, id, label)]."""
    headings = []
    used = set()

    def replace(match):
        level = int(match.group(1))
        attrs = match.group(2) or ""
        inner = match.group(3)
        label = strip_tags(inner)

        existing = re.search(r'\bid\s*=\s*"([^"]*)"', attrs)
        if existing:
            anchor = existing.group(1)
        else:
            anchor = slugify(label)
            base, n = anchor, 2
            while anchor in used:
                anchor = "%s-%d" % (base, n)
                n += 1
            attrs = '%s id="%s"' % (attrs, anchor)

        used.add(anchor)
        headings.append((level, anchor, label))
        return "<h%d%s>%s</h%d>" % (level, attrs, inner, level)

    return HEADING_RE.sub(replace, body), headings


def build_toc(headings):
    """Nested <ul> of h2 groups with their h3 children."""
    if not headings:
        return ""
    groups = []
    for level, anchor, label in headings:
        if level == 2 or not groups:
            groups.append([(anchor, label), []])
        else:
            groups[-1][1].append((anchor, label))

    out = ['<ul class="help-toc__list">']
    for (anchor, label), subs in groups:
        out.append('<li class="help-toc__item help-toc__item--h2">'
                   '<a href="#%s">%s</a>' % (esc(anchor), esc(label)))
        if subs:
            out.append('<ul class="help-toc__sub">')
            for sub_anchor, sub_label in subs:
                out.append('<li class="help-toc__item help-toc__item--h3">'
                           '<a href="#%s">%s</a></li>'
                           % (esc(sub_anchor), esc(sub_label)))
            out.append("</ul>")
        out.append("</li>")
    out.append("</ul>")
    return "\n".join(out)


# ── shared site chrome (mirrors site/start.html) ────────────────────────────

BRAND_SVG = """<svg class="mark" viewBox="0 0 32 32" fill="none" aria-hidden="true">
          <defs><linearGradient id="{gid}" x1="2" y1="0" x2="30" y2="32" gradientUnits="userSpaceOnUse"><stop stop-color="#3D6DFF"/><stop offset="1" stop-color="#1230C2"/></linearGradient></defs>
          <rect width="32" height="32" rx="8.6" fill="url(#{gid})"/>
          <path d="M16 9 L8.2 24.6" stroke="#fff" stroke-width="3.2" stroke-linecap="round"/>
          <path d="M16 9 L23.8 24.6" stroke="#fff" stroke-width="3.2" stroke-linecap="round"/>
          <path d="M11.3 18.6 H20.7" stroke="#fff" stroke-width="2.2" stroke-linecap="round" opacity=".5"/>
          <circle cx="16" cy="9" r="2.7" fill="#fff"/><circle cx="16" cy="9" r="1.3" fill="#1230C2"/>
          <circle cx="8.2" cy="24.6" r="2" fill="#5BF08D"/><circle cx="23.8" cy="24.6" r="2" fill="#5BF08D"/>
        </svg>"""


def site_header():
    nav = [
        ('/features', 'Features'), ('/integrations', 'Integrations'),
        ('/security', 'Security'), ('/pricing', 'Pricing'),
        ('/help/', 'Docs'), ('/about', 'About'),
    ]
    links = "\n".join(
        '        <a href="%s"%s>%s</a>' % (
            href, ' aria-current="page"' if href == '/help/' else '', label)
        for href, label in nav)
    return """<header class="site-header">
  <div class="wrap">
    <div class="nav-shell">
      <a class="brand" href="/" aria-label="AAA Tracker home">
        %s
        <span class="word">AAA<i>Tracker</i></span>
      </a>
      <button class="nav-toggle" aria-expanded="false" aria-controls="site-nav" aria-label="Toggle navigation">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" aria-hidden="true"><path d="M3.4 6h17.2M3.4 12h17.2M3.4 18h17.2"/></svg>
      </button>
      <nav class="site-nav" id="site-nav" aria-label="Primary">
%s
      </nav>
      <div class="header-actions">
        <a class="btn btn--ghost btn--sm" href="/backend/">Log in</a>
        <a class="btn btn--primary btn--sm btn--cta" href="/start">Start free trial</a>
      </div>
    </div>
  </div>
</header>""" % (BRAND_SVG.format(gid="aat"), links)


def site_footer():
    return """<footer class="site-footer">
  <div class="wrap">
    <div class="footer-grid">
      <div>
        <a class="brand" href="/" aria-label="AAA Tracker home">
          %s
          <span class="word">AAA<i>Tracker</i></span>
        </a>
        <p class="footer-note">Every click, accounted for. Traffic routing, server-side conversion tracking, cost sync and anti-fraud — with the numbers your ad platforms agree with.</p>
      </div>
      <div>
        <h4>Product</h4>
        <ul class="footer-links">
          <li><a href="/features">Features</a></li>
          <li><a href="/integrations">Integrations</a></li>
          <li><a href="/security">Security &amp; anti-fraud</a></li>
          <li><a href="/pricing">Pricing</a></li>
          <li><a href="/start">Start free trial</a></li>
        </ul>
      </div>
      <div>
        <h4>Docs</h4>
        <ul class="footer-links">
          <li><a href="/help/">Help centre</a></li>
          <li><a href="/help/getting-started">Getting started</a></li>
          <li><a href="/help/s2s-postback">Postbacks</a></li>
          <li><a href="/help/troubleshooting-postbacks">Troubleshooting</a></li>
        </ul>
      </div>
      <div>
        <h4>Company</h4>
        <ul class="footer-links">
          <li><a href="/about">About</a></li>
          <li><a href="/contact">Contact</a></li>
          <li><a href="/backend/">Log in</a></li>
        </ul>
      </div>
      <div>
        <h4>Legal</h4>
        <ul class="footer-links">
          <li><a href="/privacy">Privacy</a></li>
          <li><a href="/terms">Terms</a></li>
        </ul>
      </div>
    </div>
    <div class="footer-bottom">
      <span>© <span data-year>2026</span> AAA Tracker</span>
      <span><a href="/privacy">Privacy</a> · <a href="/terms">Terms</a> · hello@aaatracker.website</span>
    </div>
  </div>
</footer>""" % BRAND_SVG.format(gid="aaf")


def page_head(title, description, canonical, og_type, jsonld, og_image):
    """<head> for a help page — mirrors the metadata pattern in start.html."""
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>document.documentElement.className+=' js'</script>
<title>%(title)s</title>
<meta name="description" content="%(desc)s">
<link rel="canonical" href="%(canonical)s">
<meta name="robots" content="index,follow,max-image-preview:large,max-snippet:-1">
<meta name="theme-color" content="#ffffff">
<meta name="author" content="AAA Tracker">
<meta name="format-detection" content="telephone=no">
<link rel="icon" href="/assets/img/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/assets/img/favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="/assets/img/apple-touch-icon.png">
<link rel="manifest" href="/site.webmanifest">
<meta property="og:type" content="%(og_type)s">
<meta property="og:site_name" content="AAA Tracker">
<meta property="og:title" content="%(title)s">
<meta property="og:description" content="%(desc)s">
<meta property="og:url" content="%(canonical)s">
<meta property="og:image" content="%(og_image)s">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="AAA Tracker">
<meta property="og:locale" content="en_US">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="%(title)s">
<meta name="twitter:description" content="%(desc)s">
<meta name="twitter:image" content="%(og_image)s">
<script type="application/ld+json">
%(jsonld)s
</script>
<link rel="preload" href="/assets/fonts/inter-latin.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/assets/fonts/fonts.css?v=%(fonts)s">
<link rel="stylesheet" href="/assets/css/site.css?v=%(css)s">
<link rel="stylesheet" href="/help/assets/help.css?v=%(help_v)s">
</head>""" % {
        "title": esc(title), "desc": esc(description), "canonical": esc(canonical),
        "og_type": esc(og_type), "jsonld": jsonld, "og_image": esc(og_image),
        "fonts": esc(FONTS_CSS_VERSION), "css": esc(SITE_CSS_VERSION),
        "help_v": esc(HELP_ASSET_VERSION),
    }


def page_tail():
    return """<script src="/assets/js/site.js?v=1" defer></script>
<script src="/help/assets/help.js?v=%s" defer></script>
</body>
</html>
""" % esc(HELP_ASSET_VERSION)


def jsonld_dump(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False)


# ── hub ─────────────────────────────────────────────────────────────────────

def article_card(article):
    return ('<a class="card card--link help-card" href="/help/%s">'
            '<span class="tag tag--brand">%s</span>'
            '<h3>%s</h3><p>%s</p></a>' % (
                esc(article["slug"]), esc(article["category"]),
                esc(article["title"]), esc(article["description"])))


def article_row(article):
    return ('<li class="help-list__item"><a href="/help/%s">'
            '<span class="help-list__title">%s</span>'
            '<span class="help-list__desc">%s</span></a></li>' % (
                esc(article["slug"]), esc(article["title"]),
                esc(article["description"])))


def build_hub(available):
    popular = [a for a in available if a.get("popular")]

    categories_html = []
    for category in CATEGORIES:
        items = [a for a in available if a["category"] == category]
        if not items:
            continue
        rows = "\n".join(article_row(a) for a in items)
        categories_html.append("""<section class="section section--sm">
    <div class="wrap">
      <div class="help-cat">
        <header class="help-cat__head">
          <h2 class="h2">%s</h2>
          <span class="help-cat__count">%d article%s</span>
        </header>
        <ul class="help-list">
%s
        </ul>
      </div>
    </div>
  </section>""" % (esc(category), len(items), "" if len(items) == 1 else "s", rows))

    popular_html = ""
    if popular:
        cards = "\n".join(article_card(a) for a in popular)
        popular_html = """<section class="section section--tight section--flush-top" id="help-popular">
    <div class="wrap">
      <div class="section-head">
        <span class="eyebrow"><span class="tri" aria-hidden="true"><i></i><i></i><i></i></span>Popular</span>
        <h2 class="h2 mt-4">Most-read articles</h2>
      </div>
      <div class="grid grid--3 help-cardgrid mt-8 reveal">
%s
      </div>
    </div>
  </section>""" % cards

    jsonld = jsonld_dump({
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "CollectionPage",
                "name": "AAA Tracker Help",
                "url": HELP_URL,
                "description": "Guides and reference for AAA Tracker: campaigns, postbacks, traffic sources, ad platforms, landings, reports and account access.",
                "isPartOf": {"@type": "WebSite", "name": "AAA Tracker", "url": SITE_ORIGIN + "/"},
            },
            {
                "@type": "WebSite",
                "name": "AAA Tracker",
                "url": SITE_ORIGIN + "/",
                "potentialAction": {
                    "@type": "SearchAction",
                    "target": {"@type": "EntryPoint",
                               "urlTemplate": HELP_URL + "?q={search_term_string}"},
                    "query-input": "required name=search_term_string",
                },
            },
        ],
    })

    head = page_head(
        "Help centre | AAA Tracker",
        "Guides and reference for AAA Tracker: campaigns, postbacks, traffic sources, ad platforms, landings, reports and account access.",
        HELP_URL, "website", jsonld, SITE_ORIGIN + "/assets/img/og/og-home.png")

    return """%(head)s
<body>
<a class="skip-link" href="#main">Skip to content</a>

%(header)s

<main id="main">

  <section class="page-hero help-hero">
    <div class="wrap">
      <div class="page-hero__inner">
        <span class="eyebrow"><span class="tri" aria-hidden="true"><i></i><i></i><i></i></span>Help centre</span>
        <h1 class="h1">Help &amp; <span class="accent">documentation</span>.</h1>
        <p class="lead">
          How AAA Tracker works, from your first campaign to the fine detail of postbacks,
          macros and cost. Everything here is written against the tracker you are using.
        </p>
        <form class="help-search" role="search" id="help-search-form">
          <label class="help-search__label" for="help-search">Search the help centre</label>
          <div class="help-search__box">
            <svg class="help-search__ico" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.6-3.6"/></svg>
            <input id="help-search" type="search" name="q" placeholder="Search articles, macros, statuses…" autocomplete="off" spellcheck="false" aria-describedby="help-search-hint">
          </div>
          <p class="help-search__hint" id="help-search-hint">Search by title, category, tag or any phrase in the text.</p>
        </form>
      </div>
    </div>
  </section>

  <section class="section section--sm section--flush-top help-results-section" aria-label="Search results">
    <div class="wrap">
      <div id="help-results" class="help-results" hidden aria-live="polite"></div>
    </div>
  </section>

  %(popular)s

  <div id="help-categories">
%(categories)s
  </div>

  <section class="section">
    <div class="wrap">
      <div class="band band--blue band--flush cta reveal">
        <div>
          <h2>Still stuck?</h2>
          <p>Search the articles here first — the answer is usually a parameter name or a status spelling.
          If it is not covered, tell us what you are tracking and where it breaks.</p>
        </div>
        <div class="btn-row">
          <a class="btn btn--dark btn--lg" href="/contact">Contact support</a>
          <a class="btn btn--onink btn--lg" href="/start">Start free trial</a>
        </div>
      </div>
    </div>
  </section>

</main>

%(footer)s

%(tail)s""" % {
        "head": head, "header": site_header(), "popular": popular_html,
        "categories": "\n".join(categories_html), "footer": site_footer(),
        "tail": page_tail(),
    }


# ── article pages ───────────────────────────────────────────────────────────

def build_article(article, body, prev_article, next_article):
    slug = article["slug"]
    canonical = "%s/help/%s" % (SITE_ORIGIN, slug)
    body_with_ids, headings = process_headings(body)
    toc = build_toc(headings)

    pager = ""
    if prev_article or next_article:
        left = right = "<span></span>"
        if prev_article:
            left = ('<a class="help-pager__link help-pager__link--prev" href="/help/%s">'
                    '<span class="help-pager__dir">Previous</span>'
                    '<span class="help-pager__title">%s</span></a>'
                    % (esc(prev_article["slug"]), esc(prev_article["title"])))
        if next_article:
            right = ('<a class="help-pager__link help-pager__link--next" href="/help/%s">'
                     '<span class="help-pager__dir">Next</span>'
                     '<span class="help-pager__title">%s</span></a>'
                     % (esc(next_article["slug"]), esc(next_article["title"])))
        pager = """<nav class="help-pager" aria-label="More in this category">
        %s
        %s
      </nav>""" % (left, right)

    jsonld = jsonld_dump({
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "TechArticle",
                "headline": article["title"],
                "description": article["description"],
                "dateModified": article["updated"],
                "datePublished": article["updated"],
                "inLanguage": "en",
                "url": canonical,
                "mainEntityOfPage": canonical,
                "keywords": ", ".join(article.get("tags", [])),
                "articleSection": article["category"],
                "publisher": {
                    "@type": "Organization",
                    "name": "AAA Tracker",
                    "url": SITE_ORIGIN + "/",
                },
            },
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "Home",
                     "item": SITE_ORIGIN + "/"},
                    {"@type": "ListItem", "position": 2, "name": "Help",
                     "item": HELP_URL},
                    {"@type": "ListItem", "position": 3, "name": article["title"],
                     "item": canonical},
                ],
            },
        ],
    })

    head = page_head(
        "%s | AAA Tracker Help" % article["title"],
        article["description"], canonical, "article", jsonld,
        SITE_ORIGIN + "/assets/img/og/og-home.png")

    aside = ""
    if toc:
        aside = """<aside class="help-aside">
        <nav class="help-toc" aria-label="On this page">
          <p class="help-toc__title">On this page</p>
          %s
        </nav>
      </aside>""" % toc

    return """%(head)s
<body>
<a class="skip-link" href="#main">Skip to content</a>

%(header)s

<main id="main">

  <div class="wrap">
    <nav class="help-breadcrumb" aria-label="Breadcrumb">
      <ol>
        <li><a href="/">Home</a></li>
        <li><a href="/help/">Help</a></li>
        <li aria-current="page">%(title)s</li>
      </ol>
    </nav>
  </div>

  <article class="help-article section section--sm">
    <div class="wrap">
      <div class="help-layout">
        <div class="help-main">
          <header class="help-article__head">
            <span class="eyebrow">%(category)s</span>
            <h1 class="h1 mt-4">%(title)s</h1>
            <p class="lead mt-5">%(desc)s</p>
            <p class="help-meta mono mt-6">Updated <time datetime="%(updated)s">%(pretty)s</time></p>
          </header>

          <div class="prose help-prose mt-8">
%(body)s
          </div>

          %(pager)s
        </div>

        %(aside)s
      </div>
    </div>
  </article>

</main>

%(footer)s

%(tail)s""" % {
        "head": head, "header": site_header(), "title": esc(article["title"]),
        "category": esc(article["category"]), "desc": esc(article["description"]),
        "updated": esc(article["updated"]), "pretty": esc(pretty_date(article["updated"])),
        "body": body_with_ids, "pager": pager, "aside": aside,
        "footer": site_footer(), "tail": page_tail(),
    }


# ── search index ────────────────────────────────────────────────────────────

def build_search_index(available, bodies):
    index = []
    for article in available:
        index.append({
            "slug": article["slug"],
            "title": article["title"],
            "description": article["description"],
            "category": article["category"],
            "tags": article.get("tags", []),
            "text": strip_tags(bodies[article["slug"]]),
        })
    return index


# ── sitemap ─────────────────────────────────────────────────────────────────

def build_sitemap_block(available, latest_date):
    lines = ["  <!-- help:start -->"]
    lines.append("  <url>")
    lines.append("    <loc>%s</loc>" % HELP_URL)
    lines.append("    <lastmod>%s</lastmod>" % latest_date)
    lines.append("    <changefreq>weekly</changefreq>")
    lines.append("    <priority>0.7</priority>")
    lines.append("  </url>")
    for article in available:
        lines.append("  <url>")
        lines.append("    <loc>%s/help/%s</loc>" % (SITE_ORIGIN, article["slug"]))
        lines.append("    <lastmod>%s</lastmod>" % article["updated"])
        lines.append("    <changefreq>monthly</changefreq>")
        lines.append("    <priority>0.6</priority>")
        lines.append("  </url>")
    lines.append("  <!-- help:end -->")
    return "\n".join(lines)


def update_sitemap(available, latest_date):
    with open(SITEMAP, encoding="utf-8") as fh:
        source = fh.read()

    block = build_sitemap_block(available, latest_date)
    pattern = re.compile(r"[ \t]*<!-- help:start -->.*?<!-- help:end -->[ \t]*\n?",
                         re.DOTALL)

    if pattern.search(source):
        # Refresh in place, keeping the surrounding whitespace tidy.
        new_source = pattern.sub(block + "\n", source, count=1)
    else:
        new_source = source.replace("</urlset>", block + "\n</urlset>", 1)

    if new_source != source:
        with open(SITEMAP, "w", encoding="utf-8") as fh:
            fh.write(new_source)


# ── main ────────────────────────────────────────────────────────────────────

def main():
    with open(MANIFEST, encoding="utf-8") as fh:
        manifest = json.load(fh)

    known = set(CATEGORIES)
    available, skipped = [], []
    bodies = {}

    for article in manifest:
        if article["category"] not in known:
            print("WARNING: unknown category %r for %r — check CATEGORIES"
                  % (article["category"], article["slug"]), file=sys.stderr)
        fragment = os.path.join(ARTICLES_DIR, article["slug"] + ".html")
        if not os.path.isfile(fragment):
            skipped.append(article["slug"])
            print("SKIP: no fragment yet for %r (%s)"
                  % (article["slug"], fragment), file=sys.stderr)
            continue
        with open(fragment, encoding="utf-8") as fh:
            bodies[article["slug"]] = fh.read()
        available.append(article)

    # Stable within-category ordering.
    for article in available:
        article.setdefault("order", 0)
    available.sort(key=lambda a: (CATEGORIES.index(a["category"]), a["order"]))

    # Hub.
    with open(os.path.join(HELP_DIR, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(build_hub(available))

    # Articles + prev/next within a category.
    pages = 0
    for article in available:
        same = [a for a in available if a["category"] == article["category"]]
        pos = same.index(article)
        prev_article = same[pos - 1] if pos > 0 else None
        next_article = same[pos + 1] if pos < len(same) - 1 else None
        out = os.path.join(HELP_DIR, article["slug"] + ".html")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(build_article(article, bodies[article["slug"]],
                                   prev_article, next_article))
        pages += 1

    # Search index.
    index = build_search_index(available, bodies)
    with open(os.path.join(HELP_DIR, "search-index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, separators=(",", ":"))

    # Sitemap.
    dates = [a["updated"] for a in available] or ["2026-10-05"]
    update_sitemap(available, max(dates))

    print("help: %d hub page, %d article page(s) written, %d article(s) skipped."
          % (1, pages, len(skipped)))
    if skipped:
        print("help: still to write — %s" % ", ".join(skipped))


if __name__ == "__main__":
    main()
