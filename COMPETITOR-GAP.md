# AAA Tracker — Competitor Feature Gap Analysis

Deep research vs the major competitors: **RedTrack**, **Keitaro**, **Voluum**, **Binom**, **BeMob**, **ClickMagick**, **HYROS**. Research date: 2026-09-24 (official sites, pricing pages, and docs of each product).

- Priorities: **P0** = core tracker parity (users expect this before paying) · **P1** = strong differentiator / revenue feature · **P2** = enterprise / nice-to-have.
- "Best ref" = the competitor that does it best.

---

## 1. Where AAA Tracker stands today (baseline)

Already built: click tracking via campaign URL (redirect-based) · S2S postback conversions with statuses (lead/sale/upsale/rejected/hold/trash) · campaigns with **forced/regular/default flows** (position-based, no weights) · offers · 20 affiliate-network presets with postback templates · traffic sources with parameter mapping · PHP-hosted landers + code editor · multi-domain with SSL & 404 handling · ClickHouse analytics (dashboard KPIs, breakdowns by campaign/country/date, click log, CSV export) · Telegram conversion alerts · scheduled daily email reports (Brevo) · admin/user roles · self-hosted docker stack.

---

## 2. Competitor snapshot

| Product | Model | Hosting | Pricing | Signature strength |
|---|---|---|---|---|
| RedTrack | SaaS | Cloud | $79–$999+/mo, per events | CAPI signals to ad platforms + cost auto-sync on every plan |
| Keitaro | License | **Self-hosted** | $40–$400/mo | ~30-filter routing engine, KClient cloaking, flow monitoring, ClickHouse |
| Voluum | SaaS | Cloud | $119–$7,999/mo, per events | Traffic Distribution AI + Automizer (manage ad accounts from tracker) |
| Binom | License | **Self-hosted** | $149/mo flat (v2) | Raw redirect performance (260M clicks/day), Binom Protect cloaking module |
| BeMob | SaaS | Cloud | $0–$499/mo, per events | 10ms redirects, cookieless Direct Pixel, cheap entry |
| ClickMagick | SaaS | Cloud | $79–$349/mo, per visitors | Cross-device TrueTracking + AI Insights analyst |
| HYROS | SaaS | Cloud | custom (≈$199–$2,500+/mo) | Identity-based AI Print attribution (email/calls/offline → original ad) |

---

## 3. THE GAP LIST — features they have that AAA Tracker doesn't

### 3.1 Traffic routing & distribution *(biggest gap area)*

| # | Feature | Best ref | Pri |
|---|---|---|---|
| G1 | **Rule-based flow filters (~30 types)**: geo (country/region/city), device type/model, OS+version, browser+version, language, ISP, mobile carrier, connection type, IP (CIDR/masks/regex), IPv6, referrer (+empty), keyword, sub-id values, User-Agent, bot status — with AND/OR logic, IS/IS-NOT inversion, `/regex/` patterns | Keitaro | **P0** |
| G2 | **Weighted % traffic split** between flows/landers/offers (70/30 etc.) + "equalize weights" button | all | **P0** |
| G3 | **Offer/lander A/B split testing** with visitor stickiness (repeat visitors keep their assigned variant) | Keitaro | **P0** |
| G4 | **Offer conversion caps** — daily limit per offer, auto-overflow to a chained alternative offer, color status indicators | Keitaro | P0 |
| G5 | **Fallback URL** — redirect unmatched/blocked clicks to a backup destination | RedTrack | P0 |
| G6 | **AI / smart auto-optimization** — traffic auto-shifted to best-performing offer/lander in real time (Smartlinks / Traffic Distribution AI / Smart Rotation) | Voluum, RedTrack, Binom | P1 |
| G7 | **Campaign scheduling / dayparting** — run flows only on chosen days/hours/timezone | Voluum, Keitaro | P1 |
| G8 | **Click caps per flow** — per hour / 24h / total, stream blocks after limit | Keitaro | P1 |
| G9 | **Visitor binding** — repeat visitors bound to same flow/landing/offer | Keitaro | P1 |
| G10 | **Multi-step funnels** — multi-page/multi-action funnel tracking with weights/filters/caps | RedTrack, Voluum, BeMob | P1 |
| G11 | **Campaigns-as-offers** — use a campaign as an offer inside another campaign (nested funnels) | Binom | P2 |
| G12 | **Diverse flow actions**: 302/JS/Meta/Double-Meta redirect, form POST, open in iframe, CURL (serve content w/o redirect), show HTML/text, 404, do-nothing | Keitaro | P1 |
| G13 | **Referrer hiding** (double meta redirect, blank referrer) | Keitaro, BeMob, Binom | P1 |
| G14 | **Clean URLs** — tracking links without visible tracker params | Binom | P2 |

### 3.2 Tracking methods & conversion capture

| # | Feature | Best ref | Pri |
|---|---|---|---|
| G15 | **Direct/no-redirect tracking** — first-party JS tracking script for landers/sites (needed for Google/Meta traffic) | all | **P0** |
| G16 | **Client-side conversion pixel** (JS pixel alternative to S2S) | all | **P0** |
| G17 | **Impression tracking** + view-through conversions | RedTrack, Voluum, BeMob | P1 |
| G18 | **Post-conversion updates** — edit status/payout after the fact (refunds, chargebacks, late network postbacks) | RedTrack | **P0** |
| G19 | **Custom conversion event types** — define own events beyond the fixed status set | all | P0 |
| G20 | **Register conversions without click IDs** (fallback attribution) | RedTrack, Voluum | P2 |
| G21 | **Multi-currency** payouts/costs with conversion | Voluum, RedTrack | P1 |
| G22 | **Geo-specific payout** — different payout per country/target | Voluum, RedTrack | P1 |
| G23 | **LTV / rebill / subscription tracking** (multiple events per visitor, repeat purchases) | Binom, Voluum | P1 |
| G24 | **Deduplication of duplicate postbacks** — auto-detect & filter | RedTrack | P0 |
| G25 | **Manual conversion import** (`subid,payout,tid,status` per line) to fix/audit after the fact | Keitaro | P1 |
| G26 | **Conversion log** — dedicated chronological log of every incoming conversion for postback debugging | Keitaro, RedTrack | P0 |
| G27 | **Server-side click processing API** (Click API: pass IP/UA from an app/webview, get JSON routing decision) — mobile-funnel enabler | Keitaro, Binom | P2 |
| G28 | **Cross-device / cross-browser attribution** + configurable attribution windows & models | RedTrack, ClickMagick, HYROS | P2 |
| G29 | **Offline/phone sales tracking** attributed to originating click | ClickMagick, HYROS | P2 |

### 3.3 Bot filtering & fraud protection

| # | Feature | Best ref | Pri |
|---|---|---|---|
| G30 | **Bot filter engine** — UA/IP/spambot lists + maintained bot database, toggleable per campaign, "Bots" column in reports | Keitaro | **P0** |
| G31 | **Proxy/VPN/datacenter detection** | Keitaro (PX2), Voluum | P0 |
| G32 | **Lander protection vs spy tools** — whitelist shield; bots/competitors get blank page | Voluum, BeMob | P1 |
| G33 | **Honeypot bot traps** on landers | Voluum | P2 |
| G34 | **Cloaking engine** (KClient PHP/JS content spoofing without redirect; TLS-fingerprint detection; anti-detect browser detection) | Keitaro, Binom Protect | P1 |
| G35 | **Anti-fraud scoring + live fraud feed** — real-time per-visit checks, fraud metrics in raw logs | Voluum, ClickMagick | P2 |
| G36 | **Postback protection** — secret keys + IP allowlists on conversion endpoints | RedTrack, Binom | P0 |
| G37 | **Traffic-quality reports** for refund claims vs ad networks | Voluum, ClickMagick | P2 |
| G38 | **Click fraud cost savings surfaced in UI** (blocked clicks shown as saved spend) | ClickMagick | P2 |

### 3.4 Cost sync & integrations

| # | Feature | Best ref | Pri |
|---|---|---|---|
| G39 | **Cost auto-sync from ad networks** (Facebook/Google/TikTok/Taboola/Outbrain/ExoClick/PropellerAds/MGID/Zeropark…) — pull spend via API instead of manual CPC | RedTrack, Voluum, Keitaro | **P0** |
| G40 | **CAPI / conversion upload back to ad platforms** (Meta Conversions API, Google enhanced/offline conversions, TikTok Events API, Snap, Bing UET) — retrain platform AI | RedTrack, HYROS, Voluum | P1 |
| G41 | **60+ traffic-source templates** with token presets (AAA has a handful) | Voluum | P0 |
| G42 | **Ecommerce integrations** — Shopify, WooCommerce, PrestaShop, BigCommerce (order/LTV sync) | RedTrack, Voluum, HYROS | P2 |
| G43 | **CRM & call tracking** — GoHighLevel, HubSpot, Ringba, Retreaver, CallRail | RedTrack, HYROS | P2 |
| G44 | **Black/white-list workflow** — report grouping by creative/source to build placement blacklists for ad networks | Keitaro | P1 |
| G45 | **Offer-marketplace / partner directory** | Voluum | P2 |
| G46 | **iGaming vertical integrations** (1xBet, DraftKings, Affilka…) | Voluum | P2 |

### 3.5 Reporting & analytics

| # | Feature | Best ref | Pri |
|---|---|---|---|
| G47 | **Custom report builder** — group by ANY dimension up to 5 drill-down levels, column picker, saved reports (AAA is fixed to campaign/country/date) | all | **P0** |
| G48 | **Dimension coverage**: OS, browser, device, ISP/carrier, connection type, city/region, language, referrer, offer, landing | all | **P0** |
| G49 | **Custom metrics/formulas** — user-defined KPIs (uCR, CPL…) as report columns | Keitaro | P1 |
| G50 | **Period comparison + custom columns/column templates** | Voluum, RedTrack | P1 |
| G51 | **Live real-time feed** — second-by-second click stream with per-visit detail | Voluum | P1 |
| G52 | **Shared/public reports** — shareable links with view rights, client dashboards (no account needed) | Voluum, BeMob, RedTrack, ClickMagick | P1 |
| G53 | **Scheduled report emails** per report (choose recipients/frequency per report, not one global email) | Voluum, RedTrack | P1 |
| G54 | **Traffic-loss % / postback-% metrics** per campaign | Binom, BeMob | P1 |
| G55 | **Conversion-date vs click-date reporting toggle** | Keitaro, RedTrack | P2 |
| G56 | **Anomaly detection / AI insights** — auto-flag statistically significant changes, recommendations to scale/pause | Voluum Copilot, ClickMagick | P2 |
| G57 | **Annotations/markers** on reports (colored flags for campaign events) | Voluum | P2 |
| G58 | **Offer/lander/geo SubID hierarchy roll-ups** (sub1→sub8 chain views) | RedTrack | P2 |
| G59 | **BI/data-warehouse export** (BigQuery, Snowflake, Looker, Sheets) | RedTrack, Voluum | P2 |

### 3.6 Platform, API, team & operations

| # | Feature | Best ref | Pri |
|---|---|---|---|
| G60 | **Public REST API with token auth** — reports + entity management (AAA is cookie-session only) | Keitaro, Voluum, RedTrack | **P0** |
| G61 | **DB-backed sessions + password change** (AAA's JWT store is in-memory; restarts log users out; md5 hashing) | — | **P0** |
| G62 | **2FA (TOTP)** | Voluum, Binom, ClickMagick | P1 |
| G63 | **Per-resource permissions** — ACL per campaign/offer/etc. (full/read/own-only), per-user metric restrictions | Keitaro, Binom, BeMob | P1 |
| G64 | **Workspaces / multi-tenant isolation** per client or brand | Voluum, BeMob, RedTrack | P2 |
| G65 | **Audit log** — who changed what and when | Keitaro, RedTrack, Voluum | P2 |
| G66 | **Archive & restore** deleted campaigns/streams/landings (trash, not hard delete) | Keitaro, Voluum | P1 |
| G67 | **Bulk edit / CSV import-export of entities** (campaigns, offers, landers) | Keitaro, Binom, Voluum, BeMob | P1 |
| G68 | **Campaign duplicate/clone** | all | P1 |
| G69 | **Flow monitoring** — scheduled URL health checks; auto-disable dead flow / auto-grab new URL / auto-replace domain / webhook alert | Keitaro | P1 |
| G70 | **Triggers / auto-rules** — "if ROI < X in 3h → pause campaign", auto bid/whitelist adjustments | Voluum Automizer, Binom, RedTrack | P1 |
| G71 | **Ads-manager control from the tracker** (pause/launch campaigns on networks via API) | Voluum, RedTrack | P2 |
| G72 | **White-label publisher portal** — partner signups, per-publisher campaigns/payouts/visibility, payout logs | RedTrack | P2 |
| G73 | **Lander grabber** — import/copy third-party landers into the tracker | Binom | P2 |
| G74 | **Traffic simulation** — send synthetic test clicks through flow logic without real traffic | Keitaro | P2 |
| G75 | **Global entity search** | Keitaro | P2 |
| G76 | **MCP / AI-agent access** — let Claude/ChatGPT query data and manage campaigns | RedTrack, Voluum, HYROS | P2 |
| G77 | **Mobile apps** (iOS/Android) | RedTrack, Voluum, ClickMagick | P2 |
| G78 | **In-UI update channel + status page** (self-host polish) | Keitaro, Binom | P2 |
| G79 | **GDPR tools** — IP anonymization toggle, opt-out endpoint | RedTrack, Voluum, Keitaro | P1 |
| G80 | **Configurable data retention** + auto-prune | RedTrack, Voluum | P1 |

---

## 4. What AAA Tracker has that competitors DON'T (or charge heavily for)

- **Self-hosted with full data ownership at zero license cost** — Keitaro charges $40–$400/mo and Binom $149/mo for the same privilege; Voluum/RedTrack/BeMob/ClickMagick/HYROS are cloud-only, priced per event.
- **Unlimited clicks/campaigns/domains** — no per-event billing, no overage fees, no click caps (Binom also offers this, but paid).
- **ClickHouse analytics core** — real-time aggregations on raw click data; most SaaS rivals pre-aggregate.
- **Built-in Telegram notifications + daily email reports** — most competitors gate alerts behind higher plans; email reports are per-report scheduled CSVs elsewhere.
- **Open source (MIT)** — no competitor is open source; agencies can self-audit and white-label freely.

---

## 5. Recommended build order

| Wave | Items | Why |
|---|---|---|
| **Wave 1 — routing parity** | G1 (filter rules), G2 (weights), G3 (A/B testing), G5 (fallback URL), G15/G16 (direct JS tracking + pixel) | This is the actual "tracker engine" — without these, serious media buyers can't run traffic |
| **Wave 2 — conversion trust** | G18 (post-conversion updates), G19 (custom events), G24 (dedupe), G26 (conversion log), G36 (postback protection) | Conversions are the money metric; trust in them decides purchases |
| **Wave 3 — money integrations** | G39 (cost auto-sync), G41 (source templates), G40 (CAPI upload) | Cost auto-sync is the #1 upsell lever for every competitor |
| **Wave 4 — reporting depth** | G47/G48 (custom report builder + dimensions), G51 (live feed), G52 (shared reports) | Dashboards are the daily-use surface |
| **Wave 5 — platform** | G60/G61 (API + sessions), G62 (2FA), G66–G68 (archive/bulk/clone), G70 (auto-rules) | Retention, agencies, and power users |
| **Wave 6 — moat features** | G6 (AI optimization), G30–G34 (fraud/cloaking suite), G72 (publisher portal), G76 (MCP) | Where AAA can out-position, not just match |

**Biggest single gap:** the routing engine (G1/G2/G3). Keitaro's ~30-filter stream system and Voluum's rule paths are what define a "tracker"; AAA currently routes by flow type/position only.
