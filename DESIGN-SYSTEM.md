# AAA Tracker — Design System

> **Direction: "The instrument panel"** — precision software for people who buy traffic.
> A light, white-first surface, one cobalt signal colour, and monospace for anything
> numeric. Rebuilt October 2026 against the ClickFlare-class SaaS convention, then made
> ours. Marketing site only (`site/`); the product UI is untouched.

---

## 1. The idea

A tracker's whole job is to make a number trustworthy. So the design behaves like
instrumentation: **white surfaces, hairline rules, one signal colour, and tabular figures
wherever a figure appears.** Colour is spent only on meaning — cobalt for the brand and
the primary action, green for money made, red for money lost, amber for watch.

This replaces the previous "kept account book" direction. That version leaned on a warm
paper ground, a serif display face and an italic accent word; it read as a print object,
not as software, and it shared nothing with the category's visual grammar. The rebuild
keeps the rigour and drops the metaphor.

Two typefaces carry it:

| Role | Face | Used for |
|------|------|----------|
| Display / UI | **Schibsted Grotesk** (variable 400–800) | `h1`, `h2`, nav, buttons, body, card titles |
| Data / labels | **IBM Plex Mono** (400/500/600) | eyebrows, table headers, KPIs, click ids, figures, footers |

There is no third face and no serif. Anything that is a number, an identifier or a label
is set in mono — that single rule is what makes the product screenshots read as real.

## 2. Identity

- **Mark — "Route A"** (`site/assets/img/logo-mark.svg`, `favicon.svg`, `logo.svg`):
  a bold geometric **A** in a cobalt gradient tile. The apex node is the **click**; the
  two green base nodes are the **destinations**; the faint crossbar is the **attributed
  path** between them. It is name-native (A for AAA), product-native (a routed graph),
  and legible at 16px — tested at 96/64/48/32/24/16 before it shipped.
- **Rejected concepts** live in `brand/route-2026/` with the comparison sheet at
  `brand/route-2026/concepts.png`: *b-crossbar-a* (a cut crossbar — bolder, more
  corporate, less meaningful) and *c-bars* (three ascending bars — clean but reads as a
  chart, not as the brand). Swap by copying one over `logo-mark.svg`, `favicon.svg`,
  `logo.svg`, the inline SVG in all 11 pages, and updating `MARK` in `make_og.py`.
- **The triple motif**: three ascending bars (`.tri`) appear inside every section
  eyebrow and as the bullet rhythm. It is the "AAA" of AAA Tracker and it is the one
  decorative element permitted to repeat.
- **OG cards & icons**: regenerate with `python3 site/tools/make_og.py` (headless Chrome,
  real fonts; Pillow assembles `favicon.ico`). Run it after any headline or mark change.

## 3. Tokens

All in `site/assets/css/site.css` under `:root`. Never hardcode a value downstream.

### Surfaces

| Token | Value | Use |
|---|---|---|
| `--surface` | `#ffffff` | page and card ground |
| `--surface-2` | `#f5f7fb` | tinted bands, app sidebar, table hover |
| `--surface-3` | `#eef2f9` | inert chips, meter tracks, active nav |
| `--surface-4` | `#e6ebf5` | dividers inside tinted areas |
| `--ink` | `#0a0d16` | dark bands, headings, the final CTA button |
| `--ink-2` / `--ink-3` | `#121826` / `#1c2436` | dark-band borders, dark hover |

### Text

| Token | Value | Contrast on `--surface` |
|---|---|---|
| `--text` | `#3a4252` | 10.4:1 |
| `--muted` | `#6b7280` | 5.1:1 |
| `--muted-2` | `#8b93a3` | 3.4:1 — labels and axis text only, never body copy |
| `--on-ink` | `#f4f6fb` | on `--ink` |
| `--on-ink-muted` | `#9aa5bd` | 7.6:1 on `--ink` |

### Lines

`--line` `#e4e9f2` (default rule) · `--line-2` `#eff2f8` (inside tables and cards) ·
`--line-ink` `#232c40` (on dark).

### Signal

| Token | Value | Meaning |
|---|---|---|
| `--brand` | `#1f4bf0` | brand, primary action, active tab |
| `--brand-600` / `--brand-700` | `#1a3fd4` / `#1430a8` | hover / text-on-tint |
| `--brand-soft` / `--brand-soft-2` | `#eef3ff` / `#dfe8ff` | tints, eyebrows, chips |
| `--violet` | `#7a3dff` | the **second accent**: the interactive word in a headline ("click") and the reticle that lands on it. 5.3:1 on `--surface`. `--violet-soft` / `--violet-glow` support it |
| `--pos` / `--pos-soft` | `#0f9d58` / `#e9f8f0` | revenue, captured, healthy |
| `--neg` / `--neg-soft` | `#d93a3a` / `#fdeeee` | blocked, rejected, negative ROI |
| `--warn` / `--warn-soft` | `#b8770a` / `#fdf3e2` | watch, filtered, degraded |

Rule: **green and red are only ever used for money and status**, never for decoration.
If a screen has no good/bad distinction, it has no green or red on it.

### Shape, depth, space

- Radii: `--r-xs 6` · `--r-sm 9` · `--r-md 12` · `--r-lg 16` · `--r-xl 22` · `--r-2xl 28` · `--r-pill 999`
  Buttons and eyebrows are pills. Panels are `--r-xl`. Bands are `--r-2xl`.
- Shadows: `--sh-1` (hairline lift) · `--sh-2` (cards, nav shell) · `--sh-3` (the hero product frame) · `--sh-brand` (primary buttons — cobalt-tinted, not grey).
- Space: `--s1…--s10` = 4, 8, 12, 16, 24, 32, 48, 64, 96, 128. Sections are 96px apart
  (`--s9`), tightened to 64 on small screens.
- Layout: `--max 1200` content, `--max-narrow 900` for prose, `--gutter 24` (20/18 on small).

## 4. Type scale

| Class | Size | Weight | Tracking |
|---|---|---|---|
| `.display` | `clamp(2.5rem, 1.35rem + 4.6vw, 4.5rem)` | 800 | −0.035em |
| `.h2` | `clamp(1.9rem, 1.2rem + 2.6vw, 3rem)` | 800 | −0.03em |
| `.h3` | `clamp(1.3rem, 1.1rem + .8vw, 1.65rem)` | 700 | −0.02em |
| `.h4` | `1.0625rem` | 700 | −0.01em |
| `.lead` | `clamp(1.0625rem, 1rem + .35vw, 1.25rem)` | 400 | — |
| `.small` / `.tiny` | `.875rem` / `.8125rem` | 400 | — |
| `.mono` | `.8125rem` | 400–500 | −0.01em |

Headings are tight and heavy; body is loose (1.6). The gap between the two is the
hierarchy — there are no mid-weight display sizes competing. `.num` turns on tabular
figures; every number in a table or KPI uses it.

## 5. Components

- **`.nav-shell`** — a floating pill header: white, blurred, `--r-lg`, 24px inset from
  the viewport edge. **It shrinks as you scroll and has no divider line.** A `--shrink`
  custom property runs 0 → 1 over the first 140px of scroll (set by the scroll handler)
  and drives the header's vertical padding (16 → 4px), the shell's inset (11 → 7px), the
  corner radius and the mark's scale (1 → 0.86). The backdrop fades in with the same
  variable; the shell's own border and shadow do the separating, so there is never a
  hard rule across the page. On ≤980px the links collapse into a bordered panel under
  the bar; the toggle is 40×40 with a 44px hit area.
- **`.btn`** — pill. `--primary` (cobalt, cobalt shadow) · `--ghost` (white + hairline) ·
  `--dark` (ink, for use on the blue band) · `--onink` (translucent, on dark) ·
  `--quiet` (text only). `--sm`, `--lg`, `--block` are modifiers.
- **`.btn--cta`** — the one attention treatment, reserved for the header's "Start free"
  and used exactly once per page. Two long, quiet animations: a sheen sweeping across
  the fill every 5.6s and a ring pulse every 3.2s. On hover both stop, the sweep speeds
  up, and the button lifts and scales 1.035. Suppressed entirely under
  `prefers-reduced-motion`.
- **`.shot`** — the product frame: 1px border, `--r-xl`, `--sh-3`, browser chrome bar
  (`__bar` with red/amber/green dots, a mono URL, and a `Sample data` tag).
- **`.app`** — the product itself rebuilt in markup: `.app__side` (workspace, nav),
  `.app__top` (search + filters), `.app__filters`, `.app__kpis` (4-up), `.app__table`.
  Rows carry `.risk`, `.delta--up/down`, and a `.lead-row` total. This is the single
  strongest anti-generic device on the site — show the product, don't describe it.
- **`.band`** — a tinted rounded panel. `--dark` (ink with cobalt/violet corner glow),
  `--blue` (cobalt with a white radial sheen), `--flush` (deeper padding). Never two
  dark bands in a row.
- **`.band--bleed`** — a dark band that starts as the inset rounded panel and grows to
  full-bleed as it travels up the viewport, so the section swallows the page as you read
  it. Mark the element `data-bleed`; the scroll handler sets `--expand` 0 → 1 as the
  band's top moves from 92% to 34% of the viewport height (smoothstepped), and expands
  width, cancels the negative side margins, drops the radius to 0 and grows the inline
  padding so the inner measure holds at ~1100px instead of sprawling. `--vw` is set to
  `documentElement.clientWidth` (not `100vw`, which includes the scrollbar and would
  overshoot). With JS off, or under `prefers-reduced-motion`, `--expand` stays 0 and it
  renders as the plain inset panel.
- **`.card` / `.panel`** — white, hairline, `--r-lg` / `--r-xl`. `.card__ico` is a 38px
  tinted icon square with `--v`, `--p`, `--w`, `--n` variants. **A `.card` inside
  `.band--dark` keeps light-surface text** (`h3`/`.h4`/`b` in `--ink`, `p` in `--muted`) —
  without that rule a white card on the ink band has a white heading and reads as empty.
- **`.srcgrid` / `.src`** — the source catalogue: an auto-fill grid of 196px tiles, each
  a 32px icon slot plus a bold name. `.src__ico` holds the real brand mark;
  `.src__ico--mono` is a brand-initial monogram for the companies that publish no logo we
  may use, and `.src__ico--dark` puts a light mark (Snapchat's yellow) on an ink tile.
- **`.tabs` / `.tabpane`** — segmented control, `aria-selected`, arrow-key navigation,
  `paneIn` entrance. Used once, on the home page, to carry four mechanism mockups.
- **`.compare`** — a bad/good pair (red-tinted vs green-tinted) with `.compare__row`
  pills and a `.meter` score. The canonical "what you have vs what you'd get" device.
- **`.dtable` / `.dtable-wrap`** — the marketing-side data table: mono uppercase headers,
  hairline rows, `.r` right-align, `.name` bold first cell, `.yes` / `.no`.
- **`.checks`** — green tick list (`.checks--onink` on dark). **`.numlist`** — auto-numbered
  steps via CSS counters.
- **`.meter`** — 7px pill track with `.meter__fill` (`--good`, `--bad`, `--warn`), width
  set from `data-meter` when it scrolls into view so the fill animates.
- **`.logostrip`** — masked infinite marquee, pauses on hover, disabled under
  `prefers-reduced-motion`. **`.orbs`** — the integration logo field: 52–76px discs with
  brand SVGs, a 78–112px `--center` tile carrying the AAA mark on a cobalt ring, gentle
  float animation. `.orb--dark` gives a light brand mark an ink tile — Snapchat's yellow
  and Intercom's pale cyan are invisible on a white disc otherwise.
- **`.status`** — the uptime card: head, 36-bar `.uptime` chart, axis, 2×N service grid.
- **`.plans` / `.plan`** — pricing cards; `--featured` gets a cobalt ring and `--sh-3`;
  `.plan__quota` is a mono chip that deliberately does not stretch. `.plans--five`
  handles the pricing page's five plans: a 6-track grid where each card spans two, so
  three sit across and the last two centre beneath them (the whole grid collapses to one
  column below 900px).
- **`.faq`** — native `<details>`, chevron rotates, first item open.
- **`.form` / `.field`** — inputs are white with `--r-sm`; focus is a 3px cobalt ring.
- **`.prose`** — legal copy at 74ch, 1.7 line-height, cobalt bullets and underlined links.
- **`.cta`** — the closing blue band: heading left, buttons right, stacks below 800px.
- **`.hero__glow` / `.hero__grid`** — the hero atmosphere: a cobalt radial glow and a 64px grid,
  both masked to a soft ellipse. Decorative only, always `aria-hidden`. **The hero clips its axes
  separately** (`overflow-x: clip; overflow-y: visible`). It used to be `overflow: hidden`, which
  cut the glow off at the hero's top edge exactly where the gradient is brightest and left a hard
  horizontal seam across the page — a 38/765 step at that line, measured from a screenshot. The
  axes are now split so the glow is still contained sideways on narrow screens while the vertical
  axis stays open and both layers overshoot the section and fade out on their own.
- **`.hero__click` / `.hero__target` / `.hero__ripple`** — the word "click" and the reticle that
  lands on it. **It is deliberately not a mouse cursor**: a tracker's job is to *target* a click,
  so the mark is a reticle — a ring, four ticks and a centre dot — which also scales far better
  than a pointer glyph, whose detail turns to mush when blown up. 57px on desktop, 38px under
  560px (at 390px a full 57px would be larger than the type it points at). Both the reticle and
  the ripple are parked just **clear of the word**, not over it, so the comma stays legible.
  Decorative and `aria-hidden`; the headline still reads "Every click, accounted for." to a screen
  reader. The punctuation sits **inside** `.hero__click`, because `display: inline-block` creates
  a line-break opportunity after the box and an outside comma orphans onto the next line.

## 6. Motion

One entrance curve everywhere: `cubic-bezier(.22,.7,.24,1)`.

- **Hero entrance** — the hero stack rises in once on load (opacity 0 → 1, translateY 16px → 0,
  720ms), staggered 0/90/180/300/420ms down the eyebrow → h1 → lead → buttons → notes. It is above
  the fold, so it cannot wait for a scroll observer and runs purely in CSS.
- **The click loop** — a 5.6s cycle, deliberately the same period as the CTA's sheen so the page
  has one rhythm. The reticle travels in from the upper left, **locks** (overshoots to 1.16, settles
  to 1), **contracts** to 0.84 as the click lands, then springs back; at the same instant a violet
  wash sweeps left-to-right under the word and a ring leaves the reticle's centre
  (scale 0.4 → 3, opacity 0.5 → 0). The word itself is violet **permanently** — it does not change
  colour during the loop, so the emphasis is constant and only the movement is periodic.
- `.reveal` — opacity 0 → 1, translateY 14px → 0, 600ms, fired by IntersectionObserver.
  Stagger with `data-d="1|2|3"` (70/140/210ms). Gated behind `html.js`, so the page is
  fully readable with JavaScript off.
- Counters (`data-count`) count up over 1100ms with a cubic ease-out, and format with
  thousands separators.
- Meters (`data-meter`) fill from 0 on entry.
- Tab panes fade and lift 8px over 350ms.
- The orb field floats ±8px on staggered 7s loops; the logo marquee runs 42s linear.
- **Scroll-linked, not time-based:** the header shrink (`--shrink`) and the band
  expansion (`--expand`) are driven from one `requestAnimationFrame`-throttled scroll
  handler, so they track the finger exactly rather than easing behind it. The expansion
  applies a smoothstep to its raw progress so it starts and ends gently. Setting two
  custom properties per frame is the whole cost — no layout reads beyond one
  `getBoundingClientRect()` per band.
- **Everything above is disabled under `prefers-reduced-motion: reduce`** — reveals land
  in their final state, counters jump to their value, marquees and floats stop, the
  CTA's sheen and pulse are removed, and the expanding bands stay as static inset panels
  (the scroll handler is not even attached).

## 7. Accessibility

- Every interactive element has a visible `:focus-visible` ring (2px cobalt, 2px offset).
- `.skip-link` → `main#main` on all 11 pages.
- Header nav uses `aria-current="page"`; tabs use `role="tablist"`/`role="tab"`/
  `role="tabpanel"` with `aria-selected`, roving `tabindex`, and Arrow/Home/End keys.
- Decorative SVG carries `aria-hidden="true"`; the hero product frame carries a
  descriptive `role="img"` + `aria-label`; brand logos carry real `alt` text.
- Text contrast is ≥4.5:1 for all body copy; `--muted-2` is restricted to labels.
- Colour is never the only signal — status always pairs a colour with a word.

## 8. Honesty rules for the marketing surface

These are binding, not stylistic:

1. **No fabricated social proof.** There are no testimonials, no customer logos and no
   star ratings on this site, because there are no customers to quote yet. The proof on
   the page is product proof: the app mockup, the uptime card and the MIT licence.
   When real quotes exist, build the section — the space is designed for it.
2. **Sample data is labelled.** Every mockup frame that shows invented numbers carries a
   `Sample data` tag in its chrome.
3. **Show, then tell.** A mechanism gets a table, a meter or a frame before it gets a
   paragraph.
4. **Numbers must reconcile.** The home-page campaign table's conversions, revenue, cost,
   profit, ROI and CPA are internally consistent; a table that does not add up is the
   fastest way to look fake.

## 9. Competitor reference

The rebuild took its structural grammar from ClickFlare (`clickflare.com`), sampled
directly rather than from memory: floating pill nav → centred display headline with a
coloured key phrase → product screenshot in the hero → logo band → blue stat band →
tinted capability panel → segmented tab control → dark integration band with a logo
field → reliability card → dark Copilot panel → pricing teaser → FAQ → blue CTA →
5-column footer.

What we deliberately did differently: a single cobalt signal instead of Tailwind's
blue-600, the triple-bar motif as a repeatable signature, the "Route A" mark instead of
a chart arrow, and a lighter dark section count (three per page maximum).

## 10. Files

```
site/assets/css/site.css        the whole system (~1.09k lines, token-driven)
site/assets/js/site.js          nav, sticky header shrink, reveal, counters, meters, tabs,
                                expanding bands, form
site/assets/fonts/              Schibsted Grotesk + IBM Plex Mono (self-hosted woff2)
site/assets/img/brands/         26 real brand SVGs (Simple Icons) for the logo wall
site/assets/img/logo-mark.svg   the mark; favicon.svg / logo.svg derive from it
site/tools/make_og.py           regenerates the 8 OG cards + all icons
scripts/md-to-pdf.py            dependency-free Markdown → PDF (this document)
brand/route-2026/               mark concepts + comparison sheet
```

**Reviewing a defect someone reports from a screenshot** — see the
`screenshot-defect-triage` skill (`~/.agents/skills/`). It covers locating the file on the
Desktop, the macOS narrow-no-break-space filename trap, reading Retina captures at full fidelity,
and the measure → reproduce → fix → re-measure loop that produced the hero-seam fix above.
