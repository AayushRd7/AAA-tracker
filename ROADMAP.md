# AAA Tracker — Feature Roadmap

Pending features, organized by area. Suggested order: 1 → 3 → 4 (status editing, % traffic distribution, bot rules), then 12 + 13 (auth), then 7–9 (reporting depth).

## 🎯 Traffic & Campaign Features (core tracker gaps)

- [x] **0. Campaign list with live metrics** — Clicks/Conversions/CR/Cost/Revenue/Profit/ROI columns on the Campaigns page (ClickHouse-backed), with green/red row tinting and a stats-period selector (Today/7d/30d/all time).

- [x] **1. Conversion status editing** — conversions can now be edited (status, payout, revenue, external/transaction ID) and deleted from the Conversion Log in Reports; postback counts + dedupe also handled in the same batch.
- [x] **2. Sub ID / macro tokens in offers & landings** — full macro set (`{click_id}`, `{sub_id_1..10}`, `{campaign_name}`, `{source}`, `{cost}`…) substituted in offer URLs, lander links, and postback URLs; traffic-source token → sub_id mapping (paramsIdMapping) feeds attribution, and the source's own clickid token now flows through as the click id.
- [x] **3. % traffic distribution across flows** — weighted-random split implemented and live-verified (70/30 test); position mode = first match wins; forced flows win; fallback URL + hide-referrer added in the same batch.
- [x] **4. Bot / filter rules** — click-level filtering in Settings: block (404) or mark-as-bot by IP, IP range (CIDR), User-Agent regex, empty referer, duplicate visitor. Live-tested both actions. (VPN/proxy ASN: is_bot/is_using_proxy already detected per-click; rules for those next.)
- [~] **5. Cost auto-sync from ad networks** — **Meta is live**: daily spend/impressions/clicks per campaign (dry-run first, opt-in, driven by the connected OAuth token or a stored token), folded into the tracker's cost so profit and ROI are real. The other networks (Google/TikTok/Snapchat/Taboola/Outbrain/…) are **not built** — each needs its own API module and credentials.
- [x] **6. S2S postback URL builder per network** — Copy button on each network now fills in the tracker domain and appends the postback security key when enabled.

## 📊 Reporting & Data

- [x] **7. Offer / landing / geo / device-level reports** — breakdown covers offer, landing, country, region, city, device, OS, browser, language, source, UTM, keyword, ISP, connection type, status, bot/proxy flags.
- [x] **8. Live/real-time clicks feed** — Live switch on the dashboard auto-refreshes Recent Visits every 10s.
- [x] **9. Landing performance metrics are mocked** — real ClickHouse aggregates per landing (clicks, conversions, cost, revenue, ROI).
- [x] **10. Scheduled report emails per report** — any saved report gets its own schedule (recipients, hour UTC, daily/weekly) with a test-send button; the global daily digest still runs alongside.
- [x] **11. Data retention / cleanup job** — daily prune loop deletes clicks older than the configured window (Settings → Data Retention).

## 🔐 Security & Users

- [x] **12. API token auth** — all backend APIs now require auth (session cookie or `Authorization: Bearer <token>`; token is the Settings API token). Live-tested: unauth → 401, bearer → 200.
- [x] **13. JWT is a fake in-memory store** — replaced with DB-backed opaque sessions (`auth_sessions` table): survive restarts, revocable at logout, TTL 30 days.
- [x] **14. Password change / reset UI** — self-service change (user menu, verifies current password) + admin edit; hashing migrated to bcrypt with transparent md5 → bcrypt upgrade on next login.
- [x] **15. Two-factor auth (TOTP)** — per-user setup with QR + secret, 10 single-use backup codes, admin reset, login-page challenge step; tokens burn after 3 failed attempts and reject replay.
- [x] **16. Roles/permissions refinement** — per-section read/write permissions per user, `campaigns:'own'` scoping (list + all mutations owner-checked), permission-filtered nav and global search.

## ⚙️ Production Hardening

- [x] **17. Remove `--reload` / add uvicorn workers** — `docker-compose.prod.yml` override: uvicorn with 2 workers, no reload, ClickHouse memory cap. Dev compose stays hot-reload. Run with `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d`.
- [x] **18. Automated tests** — `backend/tests/api_smoke.py`: a live end-to-end suite (~1,700 checks) covering auth/2FA/permissions, multi-tenant isolation, campaign CRUD/clone/bulk/CSV, routing rules (filters/stickiness/caps/schedules), the tracking plane (direct JS/pixel/impressions/Click API/simulation with visitor profiles), conversion economics (LTV/custom events/clickless/import/approval lifecycle), reporting depth (36 dimensions, pagination, CSV export, share links), the logs area, retroactive cost updates, domain groups, fraud/cloaking, flow actions, optimizer, funnels, the security/robustness audit regressions, OAuth connect, CAPI/Meta cost-sync dry runs, and the routing-depth batch. Last full run: **1,710 passed, 0 failed** (2026-10-02).
- [x] **19. CI** — GitHub Actions: compileall over backend+frontend and compose YAML sanity on push/PR.
- [ ] **20. HTTPS/Certbot automation check** — Domains page now guides DNS setup (A record / CNAME instructions, per-domain "Check DNS" button) and the cert flow fails fast with a clear message when the domain doesn't route to the server yet; the auto-renewal cron flow itself is still unverified.

## 🎨 UI Polish (smaller)

- [x] **21. Campaign duplicate/clone button** — implemented: `POST /api/campaigns/{id}/clone` (live-verified in the smoke suite).
- [x] **22. Bulk actions on lists** — select multiple campaigns: Activate / Pause / Delete (with confirm).
- [x] **23. Global search** — app-bar search across campaigns/offers/landings/domains/sources/affiliates/conversions (incl. tags + click IDs), permission-filtered, grouped autocomplete.
- [x] **24. Dark mode toggle** — full `[data-theme="dark"]` token layer, Vuetify dark sync, chart re-theming, Settings → General switch, persisted.

## 🚀 Wave 7 — Fraud suite, flow actions, optimizer, funnels (2026-09-27)

- [x] **25. Fraud/cloaking suite** — 0–100 heuristic fraud_score on every click (UA/ISP/duplicate/crawler-spoof signals, verified-crawler ranges), per-campaign Shield (IP/referrer/UA whitelist + blank/404/watch actions), honeypot trap (`/t/hp` + decoy JS), global bot-list editor, live fraud feed page, Bot Clicks/Cost columns in reports, fraud-watch dashboard card.
- [x] **26. Flow delivery actions** — per-flow action: 302 redirect (default), iFrame, Form POST, Serve Content (server-side CURL), Show HTML, Do Nothing; applied at redirect-schema destination and offer click-out.
- [x] **27. AI auto-optimizer** — per-campaign enable, 15-min loop reweights flows toward the best performer (CR/EPC/profit/revenue) with exploration floor and young-flow protection; flow-level conversion attribution (`conversions_data.flow_index`); admin page with live metrics, run-now, weight history; audit-logged.
- [x] **28. Multi-step funnels** — ordered steps per campaign (landing+offers per step), visitor advances via signed bind cookie, per-step visits/click-outs/conversions/revenue/drop-off report (Reports → Funnel view).
- [x] **29. Click/conversion-date basis fix** — conversions-log click-date basis was joining mismatched keys and always returned empty; now joined through the first-party `aaa_vid` visitor cookie stamped on both the ClickHouse click row and the conversion row. Legacy rows with no link fall back to their own conversion date for the window.

## 🗂 Wave 8 — Catalogs, blacklists, status, grabber (2026-09-27)

- [x] **30. Traffic sources catalog** — 187 presets (a full traffic-channel catalog) with API-integration capability badges (cost update / campaign pause / blacklist placement / pause creative); searchable card-grid picker modal; seeding backfills missing presets on existing installs.
- [x] **31. Affiliate networks catalog** — 125 presets with verticals (Dating, Gambling, Nutra…) as filter chips; brand logos via a server-side favicon proxy with initial-letter fallback; logos also shown in the networks table.
- [x] **32. Blacklist workflow (G44)** — named blacklists on sub_id_1..10 / country / city / device / os / browser / ip (CIDR ok), global or per-campaign, mark-as-bot or block; managed on the Fraud page; enforced on the redirect path AND the Click API.
- [x] **33. System status (G78)** — admin System status tab: version, Postgres/ClickHouse health + latency, 24h click/conversion counts, background-loop heartbeats (monitor/rules/optimizer), 15s auto-poll.
- [x] **34. Lander grabber (G73)** — grab any URL into a local landing with asset-URL rewriting, SSRF guards (private-IP rejection, redirect re-validation), size/type caps, operator opt-in for internal targets.
- [x] **35. Domains DNS guidance** — per-domain "Check DNS" chips (points here / resolves elsewhere / no DNS), A-record/CNAME instruction banner with detected server IP, and fail-fast cert requests with human-readable errors instead of 10-minute certbot timeouts.

## 🧠 Wave 9 — Insights, MCP, docs hub, campaign editor (2026-09-27)

- [x] **36. Anomaly insights (G56)** — read-only detectors compare each active campaign's last 24h against the trailing 7-day average and surface findings (ctr_drop, cost_spike, click_drop, bot_surge, revenue_stop, zero_conversion_spend) with severity + magnitude; cached in settings, Run-now button, one Telegram alert per new critical batch; rides the 15-min auto-rules cadence.
- [x] **37. MCP / AI-agent access (G76)** — `POST /api/mcp` speaks JSON-RPC 2.0 (initialize / ping / tools/list / tools/call) behind the same auth plane (Bearer API token or admin session). Nine tools: campaigns.list/get/metrics/set_status, offers.list, sources.list, reports.summary, conversions.recent, insights.latest. `campaigns.set_status` is the only mutator and is audit-logged; business errors come back as isError results, protocol errors as standard JSON-RPC codes.
- [x] **38. In-app documentation hub** — About page rebuilt as a searchable Documentation center: 14 sections from Getting started through API reference and background jobs, live search filter, in-page anchors.
- [x] **39. Campaign editor redesign** — create/edit dialog rebuilt as a centered editor with a vertical left rail of nine tabs (General, Cost, Tracking, Parameters, S2S postbacks, Flows, Shield, Funnel, Notes) instead of the crammed horizontal tab strip; internal scroll so the dialog never wobbles or overflows.

### Bug log (wave 7)

- Fixed: Vuetify's elevation-24 shadow painted on the full-width `.v-dialog` wrapper — every dialog showed a giant shadow band across the page.
- Fixed: Create-campaign dialog was `fullscreen` and wobbled/overflowed horizontally — now a centered 960px dialog with internal scroll.
- Fixed: selected tag chips rendered white text on transparent background (Vuetify `v-chip--outlined` transparent-`!important` outranked our fill); select-dropdown active items were green-on-green (now dark green, semibold).
- Fixed: smoke-suite timezone flake near local midnight — "today" now derived from UTC.
- Known: historical clicks/conversions have empty `visitor_id`, so pre-wave-7 data still won't appear under click-date basis.

## 🔧 Wave 10 — Data tables, dimensions, ops depth (2026-09-27)

- [x] **40. Changelog-parity bug batch** — click-outs and postbacks now write/sync to ClickHouse live (new `click_id` column; `ALTER UPDATE … mutations_sync` conversion sync); report-poisoning, stale postback fanout, offer gating, and binding-reset regressions fixed.
- [x] **41. Clicklog & conversions server-side pagination** — limit/offset with true `total` (ClickHouse `count()` / PG `query.count()`), deterministic page ordering, legacy response shape kept when no offset is sent; Vuetify pagers wired to both tables.
- [x] **42. Clicklog & conversions CSV export** — server-side, UTF-8 BOM, formula-injection guard, honors current filters, blob download from the UI.
- [x] **43. Report dimensions: week, domain, user agent, OS version** — two new ClickHouse columns (`user_agent`, `os_version`) via idempotent startup migration; domain derived from URL, ISO-week keys; dimension count 24 → 28.
- [x] **44. Retroactive cost-update tool** — admin endpoint `POST /api/costs/update` `{campaign_id?, period, cost}` sets per-click cost on matching ClickHouse rows (synced mutation); dialog in Reports with campaign select, date range, and result feedback.
- [x] **45. Bulk owner / network reassignment** — campaigns: set owner for selected rows (user picker incl. unassign, `GET /api/campaigns/users` for editor access); offers: set affiliate network for selected rows (incl. "no network").
- [x] **46. Domain groups with per-user access grants** — `domain_groups` + membership + grant tables (cascading), full CRUD UI on the Domains page; non-admins only see grouped domains they have a grant on in the campaign picker (admins bypass).
- [x] **47. Simulate-dialog visitor profiles** — visitor count slider, country/OS/browser/device/IP-prefix selects (empty = random mix per visitor), optional seed (blank = entropy); backend omitted-seed default fixed from fixed `0` to true randomness.
- [x] **48. Sources single-delete guard** — deleting a campaign-linked source now returns 409 with an actionable message (was an unhandled FK 500); confirm dialog and toast name the source.

## 🌐 Wave 11 — IPv6 + ops polish (2026-09-28)

- [x] **49. IPv6 click storage (G92)** — new `ip_full String` column on `clicks_data` (idempotent migration); every insert path (hits, click-outs, Click API, impressions, conversions) stores the full client address; click-log (list/paged/CSV/search), live feed and recent visits coalesce `ip_full` over the IPv4 column; exact-IP blacklist entries match v6 canonically; IPv4 CIDRs never mis-match v6 clients; GDPR IP anonymization now masks v6 (last 16 bits). ClickHouse alias-substitution quirk worked around via subquery (`ip_display`). Historical v6 rows remain 0.0.0.0 (no backfill, documented).
- [x] **50. Fraud feed IPv6** — top-IPs and fraud feed surface the full address instead of 0.0.0.0.
- [x] **51. SSL-expiry warning (G95)** — `GET /api/domains/ssl-expiry` parses live certbot certs; Domains page shows ok/warning (≤30d)/critical (≤7d)/expired/unknown chips.
- [x] **52. Settings backup/export + restore (G95)** — export downloads `settings-backup.json` with secrets nulled (keys kept); restore deep-merges, null leaves keep live secret values; garbage rejected 400.
- [x] **53. Conversion-log search by offer URL (G95)** — `?url=` substring filter against the offer URL, works across list/pagination/CSV; "Offer URL contains" field in the filter bar.
- [x] **54. Domain-delete guard (G95)** — deleting a campaign-bound domain returns 409 with an actionable message (mirrors the sources guard).
- [x] **55. Click-log + live-clicks `campaigns:'own'` scoping (G95)** — non-admin scoped users see only their own campaigns' rows in click-log (both response shapes + export) and the live feed; admins/'all' users unchanged.

## 🛡 Wave 12 — Security & robustness hardening (2026-09-28, full code audit)

Audit: four independent read-only passes (tracking plane, backend API, templates, ClickHouse/query layer); all findings fixed with smoke coverage.

- [x] **Security (critical/high)** — the landing-management router (grab/list/delete/file IO) is now admin-gated (was fully open on the public tracking host); postback security fails closed on a settings-read DB failure instead of caching an open state for 30s; TOTP failure limiter is keyed on username+IP and survives token refreshes (was reset by replaying /login → 6-digit brute force); `tracker_admin`/last-active-admin can no longer be deleted or deactivated; MCP mounted with the write gate it needed; optimizer/MCP non-dict args rejected properly.
- [x] **Tracking-plane robustness** — NULL campaign config no longer 500s the visitor redirect/pixel; one malformed postback URL no longer aborts the rest of the background queue (templating inside try; unknown keys/format specs stay literal); `None` fields no longer serialize as `"None"` in outgoing postbacks; non-dict JSON bodies can't 500 /t/collect, /i, /c or POST /{alias}; non-ASCII secrets/cookies no longer crash compare_digest; `²`-style aliases 404 instead of 500; click-out route now enforces campaign status, the tracking gate, GDPR opt-out (skip writes), IP anonymization and bot/fraud scoring; bound-flow and Click-API paths re-check offer status; payout NaN/Infinity rejected; over-long /pb fields truncated; explicit `?click_id=` wins over a stale cookie; bind secret fails closed (no constant fallback).
- [x] **Metrics correctness** — direct-tracking (`/t.js` + `/p`) conversions now sync to ClickHouse (`click_id` on visit rows) and the user-agent/OS-version report dimensions actually populate (were dead columns); click-log/visits/metrics/breakdown/funnel/global-search all honor `campaigns:'own'` scoping; backup export/import no longer destroys saved-report share tokens.
- [x] **API hygiene** — typed body for dashboard metrics (no raw JSON 500s); ISO dates validated (400 not 500); ILIKE wildcards escaped; PATCH no longer wipes unset offer/network fields; costs update validates campaign existence + audits; duplicate-name source PATCH → 400; internal error text no longer leaks in 500 details; uvicorn `--proxy-headers` + nginx `X-Forwarded-For` so login rate-limiting and the audit trail see the real client IP.
- [x] **UI fixes** — new flows default to a real schema (`landing_offer`, was `split` → silent 404s); affiliates bulk-delete refreshes the table; NULL JSONB offer/source rows no longer freeze their tables; domain status check can't leave a stuck overlay; settings save shows its toast again; merge duplicate mounted hook; optional-chaining removed from template expressions; saved-report delete confirms; confirm-password field reactive; 3 toasts routed through the sanitizer.

## 🔁 Wave 13 — Postback parity (2026-09-28)

- [x] **56. Postback-processing rules (G85)** — global `settings.postback_rules`: ordered rules reusing the flow-filter engine over the postback payload (status, payout, click_id, sub ids + passthrough), actions `set_status`, `set_payout` (absolute or multiplier), `reject` (drop: 200, no row, no fanout); first match wins, terminal actions stop the chain; editor in the S2S postbacks tab; unconfigured = no behavior change.
- [x] **57. Conversion fanout controls (G86)** — per traffic source (`additional_settings`): `disable_upsell` (upsell forwards skipped, campaign postbacks unaffected) and `sample_percent` (deterministic per-click_id hash sampling, so retries don't flip); UI fields in the source dialog; per-status mapping preserved.
- [x] **58. `/pb` hardening + tokens (G87)** — endpoint accepts GET, POST (form + JSON) and HEAD (validates, writes nothing); merged body/query params; new tokens `{_md5}`, `{unixconversiontime}`, `{status2}`, `event_1..30` (empty while the schema has no event columns), `payout=auto`; fixed a latent bug where httpx dropped admin-authored query params on GET postbacks.
- [x] **59. Source status-map bug** — the fanout status map used `upsell`/`reject` while the UI stores `upsale`/`rejected`, so those per-status toggles never gated; now accepts both spellings.

## 🧭 Wave 14 — Routing depth + access control (2026-09-28)

- [x] **60. rDNS routing criterion (G88)** — `rdns` filter field (PTR of the client IP): worker-thread resolution with a hard 2s ceiling, bounded 300s memo cache incl. negatives; timeout/failure → empty, filters simply don't match.
- [x] **61. Conversion-status criterion (G88)** — `conversion_status` filter field driven by the visitor's last conversion status for the campaign, from a bounded 60s cache keyed visitor+campaign (no DB hit for cookie-less traffic, none on the hot path for fresh visitors).
- [x] **62. Source-declared bot flag (G88)** — per-source opt-in (`additional_settings.is_bot_param`) names a request param; only configured sources are trusted (a bare `?is_bot=1` is ignored); marks the hit for the existing gate/bot paths.
- [x] **63. Prefetch filtering (G89)** — `Purpose`/`Sec-Purpose`/`X-Purpose`/`X-Moz` prefetch requests still get the redirect but write no click/conversion rows (toggle, default on — prefetches inflate stats).
- [x] **64. No cost for bot clicks (G89)** — setting (default off): bot-flagged hits write `cost = 0` on the click row, numerically equivalent for `SUM(cost)` aggregations, no analytics-layer change needed.
- [x] **65. Login IP whitelist (G90)** — `settings.login_security.ip_whitelist` CIDR list enforced at `/login` and the TOTP step using the real client IP (proxy headers); empty list = unchanged; admin card in Settings.
- [x] **66. Hide-referrer secondary domain (G91)** — optional `tracking.referrer_hiding_domain`: the hop becomes a 302 to that domain's `/__hide_referrer?u=…` route (http/https destinations only, `javascript:` rejected 404); unset keeps the current inline meta-refresh.
- [x] **67. Scalability/ops (wave 15 items shipped)** — conversions click-date window capped at 50k visitor ids with loud degradation logging; deterministic pagination (click_id tiebreak for click-log, dimension tiebreak for breakdowns); ClickHouse timezone pinned to UTC; pause/resume offers from report rows (audited `POST /api/offers/{id}/status`); campaign-tag (any) filter in the click log incl. count + CSV export.

## 🔌 Wave 16 — CAPI integrations: platforms, OAuth sign-in, channel binding

**Planned (owner action pending: register the platform apps — target tomorrow).**

Model: pixels are records in **CAPI Integrations**; a **traffic source (channel)** can *connect* to a platform and then select from that account's assets; the same picker exists at **offer level**. Configure a pixel at channel level **or** offer level — never both (duplicate events; the sender resolves channel first and sends once). Everything is dry-run until an admin flips **Active**.

**Two connection paths per platform:**
- **Connect (OAuth)** — needs an app you own per platform. Credentials go into **Settings → Integrations** (Client ID / Secret) — secrets are never shared outside your install. The callback URL to whitelist is `https://<your-tracker-domain>/backend/api/integrations/<platform>/callback`.
- **Paste token (manual)** — works immediately, no app review. Required for platforms with no public OAuth and as the fallback while an app is pending approval.

**Platforms to support** (target 6+):

| Platform | Sign-in (OAuth) | Destination ID | Token | Click-id param | Notes |
|---|---|---|---|---|---|
| Meta | Yes | Dataset/Pixel ID | Conversions API access token | `fbclid` → `fbc`, `_fbp` | built (pixel model in progress) |
| Snapchat | Yes | Pixel ID | Conversions API token | `sc_click_id` | Events Manager → generate token |
| TikTok | Yes | Pixel Code | Access Token | `ttclid` | Events API 2.0; sandbox available |
| Pinterest | Yes | Ad Account ID | Conversions API token | `epik` | OAuth `ads:read`; trial access for own account |
| AppLovin | **No** | Account/app ID + SDK key | S2S/postback API token | their macro (confirm from docs) | manual token only |
| OpenAI / ChatGPT Ads | **No** (partner-gated) | Ads Manager account | API token | TBD | OpenAI and ChatGPT are the **same** platform |
| *candidate 7th: Google Ads* | Yes | Conversion action | OAuth + **developer token** | `gclid` | offline conversion import / Enhanced Conversions |

**Per-platform code work once credentials exist:** platform-aware pixel dialog (fields differ per platform), asset discovery after connect (pixels / datasets / ad accounts listed as *connected*), event-name mapping per platform's allowed list, value/currency handling, test-event support ("Send test event" per pixel), token refresh + storage, and click-id capture for each param in the table.

**Steps to finish:**
- [ ] Settings → Integrations: per-platform app credentials + callback URL display.
- [ ] OAuth start/callback endpoints per platform + token store with refresh.
- [ ] Asset fetch (pixels/datasets/ad accounts) and "connected" labelling in the channel/offer picker.
- [ ] Manual-token path for AppLovin / OpenAI and anything awaiting app review.
- [ ] Meta/Snapchat/TikTok/Pinterest senders; AppLovin + OpenAI once their docs/tokens are confirmed.
- [ ] Optional 7th: Google Ads (needs a developer token application).

## 🗂 Wave 17 — Logs & observability ✅ shipped (reconciled 2026-10-02)

Everything here was already written; the audit surface now exists. It shipped as **six
sidebar routes under a Logs group** rather than in-page sub-tabs — same data, one click apart.

- [x] **Six log views**, each filterable and CSV-exportable with the standard table chrome:
  - **Clicks** and **Conversions** (`logs-clicks`, `logs-conversions`)
  - **S2S postbacks** — ref/click id, status, payout, transaction id, matched/rejected, source IP
  - **Internal postbacks** — outbound CAPI delivery: platform, pixel, event, HTTP status, attempts
  - **Click forwarding** — per click: flow, schema, offer, forwarded URL, status and reason
  - **Cost updates** — who changed cost, which campaign/period, how many rows
- [ ] **Conversion health** on the status page — the 24h CAPI failure count is shown;
  *last successful postback per source* and row deep-links are still missing.
- [ ] Two captured-but-unshown fields: the postback **raw URL** and the CAPI **response
  snippet** are in the API and the CSV, but not rendered as table columns.

## 🧰 Wave 18 — Tools grouping + saved filters ✅ shipped (reconciled 2026-10-02)

- [x] **Tools group** in the sidebar: Domains, Conversion tracking, Scripts, Bot rules,
  Automatic rules, Filter presets, Fallback, Health centre. (Integrations sits in *Manage*,
  and bot blacklists live under *Fraud* — there is no separate "Blacklist bots" entry.)
- [x] **Global fallback URL** with the full token palette, used when no flow matches or a
  cap/filter blocks the click — the campaign fallback wins, then the global one.
- [x] **Saved filter presets** — save, apply and reapply a named filter set on every log and
  on Reports.
- [x] **Funnel templates** — save a campaign's funnel steps and apply them to another.
- [x] **Script library** — reusable titled snippets with copy, and the landing editor can
  insert a snippet into the open file at the position its description implies.

## 📊 Wave 19 — Report & table depth ✅ shipped (reconciled 2026-10-02)

- [x] **Column-set templates** (save/load which columns a table shows), per scope.
- [x] **Conditional row colouring** by column threshold and **number formatting** (decimals,
  thousands divider) as workspace settings, applied across Logs/Reports/Campaigns.
- [x] **Report template gallery** (9 built-in presets) and an **IP report** mode
  (single-day, per-IP, enforced server-side).
- [x] **Conversion approval lifecycle** — pending / approved / declined / other, with approval
  and decline rates as columns plus single and bulk approval actions.
- [x] Conversions log: **duplicate-status column**, **dedupe token**, **bulk status change**
  and **manual conversion add**.
- [x] Report builder depth: custom formula metrics, public share links, 5-level dimension
  drill-down, **36 dimensions**.
- [ ] **Custom columns** — user-defined columns (beyond showing/hiding built-ins) are not built.
- [ ] Google Ads offline-conversion export format (blocked on the Ads API token, like G39/G40).
- [ ] The SubID chain rolls up `sub_id_1`→`sub_id_5`; `sub_id_6..10` exist on the row but are
  not report dimensions.

## 🔐 Wave 20 — Account & operations — mostly shipped (reconciled 2026-10-02)

- [x] **Session management** — per-user active sessions (IP, device, OS, browser, last seen,
  started) with revoke-one and "log out everywhere"; operators can inspect another user's
  devices. **Geo per session is not built.**
- [x] **Audit log filters** — by user, action, object type, title/id and date range, with CSV
  export and facet counts.
- [~] **Workspace settings** — decimal places, thousands divider and per-table default columns
  are shipped; the **grouping-view toggle is not built**.
- [ ] **Health-centre incidents** — the health centre is a flat self-check (DB/analytics
  reachability, row counts, loop heartbeats, 24h CAPI failures). There is no per-integration
  or per-domain incident object.


## 🏢 Multi-tenancy — SaaS foundation (in progress)

Model: **tenant → team members → resources**. A user can belong to several tenants; an agency's
sub-workspaces are tenant rows with a `parent_tenant_id`. The current install becomes **tenant #1**
and its data is backfilled to it, so nothing changes for the existing deployment.

### Phase 1 — schema + isolation ✅ shipped 2026-09-29
- [x] `tenants` (with `parent_tenant_id`) and `tenant_memberships` (user ↔ tenant, role, per-tenant permissions)
- [x] `tenant_id` on 27 tenant-owned tables, backfilled to tenant #1; global tables stay global (users, sessions, tenants, memberships, oauth states)
- [x] unique constraints became per-tenant (campaign name/alias, domain, offer/source/network name, landing folder/name, settings key, domain-group name, integration platform, ad-cost day)
- [x] current tenant resolved from the session (validated against membership) + a workspace switcher in the app bar + `/api/tenants` endpoints
- [x] isolation enforced centrally: `TenantMixin` + `do_orm_execute` scoping + a `before_flush` stamp that overrides caller-supplied tenants; raw SQL audited and given explicit predicates; every ClickHouse query filtered
- [x] acceptance gate: 74 isolation assertions in the smoke suite (cross-tenant read/update/delete, same-alias independence, ClickHouse tenant stamping, host→campaign resolution)
- Also fixed along the way (pre-existing faults the isolation work surfaced): Meta cost-sync and optimizer bound-parameter crashes, a CAPI pixel claim that meant no pixel was ever sent, a cross-tenant read of stored integration connections, and tenant-unaware conversion attribution + retention prune

### Phase 1 — known gaps (carried into phase 2) — reconciled 2026-10-02
- [~] per-tenant users/roles/invites — **roles and invites are per-tenant**; the **users table is still install-global** (no `tenant_id`), so an account is shared across the workspaces it is a member of
- [x] per-tenant settings seeded on tenant creation, per-tenant retention and per-tenant bind secret — all shipped
- [x] `parent_tenant_id` is traversed for access (a manager reaches its descendants; authority flows down only) — roll-up *reporting* is still absent (phase 3)
- [ ] the four `UPDATE settings … WHERE id` sites rely on a preceding tenant-scoped `SELECT … FOR UPDATE` rather than an inline predicate
- [ ] a new tenant's campaign is unreachable until one of its domains exists (host-based resolution)

### Phase 2 — per-tenant everything (in progress)
- [x] permissions/roles per membership rather than per user (owner | admin | editor | viewer with documented defaults; a user can be editor in one tenant and viewer in another) — shipped
- [x] workspace member management: list/add/update/remove members, transfer ownership, seat limit, guardrails (no self-escalation, last owner protected)
- [x] settings rows are already per-tenant (phase 1 composite unique); the audit log, logs area and reports are tenant-scoped
- [x] plan/quota columns on the tenant (plan, seats, retention_days, features) with the seat limit enforced
- [x] seed a tenant's settings document on creation (documented defaults + a fresh API token per workspace)
- [x] per-tenant retention: the prune iterates tenants and applies each one's `retention_days`
- [x] per-tenant bind secret, persisted in that workspace's settings rows
- [x] background loops iterate tenants (monitor, auto-rules, optimizer, insights, email reports, Meta cost sync) inside a per-tenant context, honouring `tenants.features` and isolating failures
- [x] API tokens are workspace-scoped: a request resolves the presented token to its tenant and runs there
- [x] per-tenant CAPI/Meta connection setup surfaced in the UI — each workspace connects its own ad account from Integrations; the OAuth **app credentials stay deployment-owned** (env), which is deliberate

### Phase 3 — onboarding, whitelabel, sub-workspaces (billing deferred)
- [x] onboarding: invite members into a workspace (single-use, hashed, expiring, role ceiling) + a per-workspace setup checklist — API shipped; **neither is surfaced in the UI yet**
- [ ] self-serve signup + a first-campaign wizard on top of invites — **not built** (there is no `register`/`signup` route; access is invite-only)
- [x] ~~whitelabel: per-workspace product name, logo, accent colour, login message, host-resolved branding~~ — **dropped by the owner (2026-10-02)**. No code exists; branding is a single static logo and a fixed accent. (An earlier revision of this file claimed it was "implemented, awaiting verification" — that was wrong.)
- [x] agency sub-workspace management: create nested workspaces, switch a session into any descendant of a workspace you manage, authority flows down the tree only — shipped
- [ ] roll-up reporting across child workspaces — **not built** (reporting is strictly single-tenant; the G58 "roll-up presets" are dimension roll-ups within one workspace)
- [ ] **billing — deferred on purpose (2026-09-29):** plans, seats, usage metering and invoices stay out until the rest of phase 3 is in place, because the tiers, the limits and whether a payment provider (or manual invoicing) is used are product decisions that have not been made yet. The plan/seats/retention columns and seat enforcement from phase 2 are the foundation it will build on.

## 🧰 Wave 22 — Production hardening from live-server incidents (2026-09-30)

Every item here came from a real failure on a running box, with the evidence recorded in the commit.

- **The app stopped answering after a burst of dashboard traffic** — `QueuePool limit of size 5 overflow
  10 reached`. Each request holds its connection until its response finishes and the dashboard loads ~25
  endpoints at once, so the pool starved and every later request queued behind the 30s wait. The pool is
  now env-tunable (**20 + 30** by default, `pool_timeout=15s`, `pool_pre_ping`, `pool_recycle`), and the
  dashboard handlers are plain `def` again so FastAPI runs their blocking SQLAlchemy/ClickHouse work in
  the threadpool instead of on the event loop. Verified with 45 rounds of 48-request bursts: `idle in
  transaction` stayed at 0–1, where it used to fill all 15 connections and hang the site.
- **`uvicorn --reload` in production** — the dev compose file was serving a live box, so any file change
  under the mount restarted the app mid-request. Production runs the `docker-compose.prod.yml` overlay
  (no reload, two workers); the deploy procedure in the Readme says to restart after a `git pull`.
- **A wedged worker can now explain itself** — `faulthandler` is registered on `SIGUSR1`, so
  `docker exec tracker_backend kill -USR1 <worker>` writes every thread's stack to the container log.
  This is what identified the pool stall; containers block `py-spy` by default.
- **The socket-proxy sidecar hardcoded the host's docker group id (991)** — on any other host it
  restarted forever with `connect: permission denied`, silently taking the certificate/reload path with
  it. The id now comes from `DOCKER_GID`, derived by `make env` from the host, and the fresh-install CI
  job asserts the proxy is running *and* reachable from the frontend.
- **The OAuth callback the app advertises was not a route** — `derive_callback_url()` hands the provider
  `…/integrations/<platform>/callback` while the handler lived at `…/oauth/callback`, so the provider's
  redirect landed on a 404 and no connection could complete. Both paths answer now, and the smoke suite
  checks that the advertised URL is served.
- **The token-encryption package was missing from the built image** — `cryptography` was in
  `requirements.txt` but the image predated it, so connecting a platform failed with a message blaming
  `INTEGRATIONS_ENCRYPTION_KEY` (which was set). Rebuilding the image fixed it; the API reports
  `encryption_configured` honestly.
- **The Meta cost sync now uses the connected OAuth token** — connecting Meta already stores a token with
  `ads_management`, so needing a second (System User) token pasted into the settings block was busywork.
  The block's own token still wins; when empty, the connected workspace token is used (cost sync and the
  campaign pause/resume controls). Verified: a dry-run sync read 8 real insight rows for a live account
  with no writes.
- **Workspace currency and spend** — the system currency is set per workspace (INR in production) before
  cost sync is meaningful; campaign spend is allocated per campaign+day from the platform's own numbers.

## 🔜 Queued — will be completed later

**Reconciled 2026-10-02** — after a code-level audit of every earlier wave, this is what
is genuinely still open. Waves 17–20 turned out to be shipped; see above.

**Blocked on a key, a licence or a third party**
- Google Safe Browsing checks (needs an API key) and a server-side GeoIP DB (needs a
  MaxMind-grade licence) — both become opt-in settings the moment a key exists.
- Cost sync and conversion upload for platforms beyond **Meta** — each needs its own app
  and token. Meta is live; Snapchat/TikTok/Pinterest/Google/AppLovin are records marked
  "coming soon" with no sender.
- The certbot auto-renewal flow on a live domain is still unverified (20).

**Deferred by design (volume-dependent)**
- Legacy flow-schema migration for configs carrying the removed `split` schema.
- ReplacingMergeTree redesign to replace the per-postback `ALTER UPDATE` mutation.
- Fraud CIDR signals extended to IPv6 (v6 visitors simply never match today).
- Lazy rDNS resolution (only when a campaign actually filters on `rdns`).
- Domain-group grants are visibility-level; binding-time enforcement is pending (G84).

**Small, well-scoped gaps found by the audit** — *closed 2026-10-02* unless noted
- [x] **Custom columns** — workspace-defined `{name, formula}` columns over the metric
  whitelist, validated on save (unknown metric or unevaluable formula → 400) and evaluated
  by the report builder's formula engine into every breakdown row and its totals.
- [x] **Script library injection** — the landing editor inserts a saved snippet into the open
  file at a position derived from its description; landers stay static (no request-time injection).
- [x] **Conversion health** on the status page — last successful postback per source, with a
  deep-link to that row in the S2S postbacks log.
- [x] Postback **raw URL** and CAPI **response snippet** are now columns in their logs.
- [x] **Health-centre incidents** — derived per-integration, per-domain, delivery and loop
  incidents with severity buckets and fix deep-links.
- [x] **Session geo** — country/region/city captured from Cloudflare's headers (spoof-guarded
  in nginx) and shown per session.
- [x] **Grouping-view toggle** in workspace settings — the breakdown renders one subtotal row
  per first-level group.
- [x] **`sub_id_6..10`** as report dimensions (41 dimensions total; the chain cap stays at 5).
- [x] **Invitations and the onboarding checklist** now have UI (members page / dashboard).
- [x] **Per-user metric restrictions** — a workspace can hide chosen metrics (cost, revenue,
  profit, ROI…) from a specific user. Enforced server-side across the breakdown, metrics,
  logs, report lists and CSV exports (a hidden metric is never sent), and ignored for
  owners/admins.
- [x] **Per-resource ACL scoping** — offers / traffic sources / affiliate networks / domains, with owner stamping, list filtering, mutation guards and scope validation; bulk owner-reassignment is implemented for campaigns and offers only.
- [x] **Tracking-path id validation** — a flow holding a non-numeric id (`lt-caps`) now 404s
  the flow instead of raising `asyncpg.DataError` and killing the worker; guarded by smoke.

**In progress (2026-10-02)**
- Multi-currency rate store (frankfurter.dev → ECB → open.er-api.com, cached with manual override
  and stale-rate disclosure) with conversion applied in reporting.

**Queued**
- Geo-specific payout (G22) — per-offer payout overrides by country/region, resolved at
  conversion time.
- Campaign-as-offer (G11) — use a campaign as an offer inside another campaign.
- Roll-up reporting across child workspaces.
- Bulk offer update.
- Lander views-vs-clicks split.

**Not built (product decisions or new surface)**
- Ecommerce / CRM / call-tracking integrations, iGaming integrations.

**Dropped by the owner (2026-10-02)**
- Offer marketplace / partner directory.
- Mobile apps (iOS/Android).
- In-UI update channel.
- Per-workspace white-label branding (also see Phase 3).
- TLS-fingerprint (JA3/JA4) detection.

**Deferred by the owner (2026-10-02)**
- Self-serve signup + a first-campaign wizard (access is invite-only today).

**Installer portability** (shipped; recorded for context): `make install` works with either
the `docker compose` plugin or the legacy `docker-compose` binary (auto-detected, with an
actionable preflight error and `make check`); `setup.sh` installs the plugin (or the
standalone binary as fallback) and detects the distro; CI guards against re-hardcoding the
binary and dry-runs the Makefile.
