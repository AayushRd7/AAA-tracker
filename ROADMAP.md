# AAA Tracker — Feature Roadmap

Pending features, organized by area. Suggested order: 1 → 3 → 4 (status editing, % traffic distribution, bot rules), then 12 + 13 (auth), then 7–9 (reporting depth).

## 🎯 Traffic & Campaign Features (core tracker gaps)

- [x] **0. Campaign list with live metrics** — Clicks/Conversions/CR/Cost/Revenue/Profit/ROI columns on the Campaigns page (ClickHouse-backed), with green/red row tinting and a stats-period selector (Today/7d/30d/all time).

- [x] **1. Conversion status editing** — conversions can now be edited (status, payout, revenue, external/transaction ID) and deleted from the Conversion Log in Reports; postback counts + dedupe also handled in the same batch.
- [x] **2. Sub ID / macro tokens in offers & landings** — full macro set (`{click_id}`, `{sub_id_1..10}`, `{campaign_name}`, `{source}`, `{cost}`…) substituted in offer URLs, lander links, and postback URLs; traffic-source token → sub_id mapping (paramsIdMapping) feeds attribution, and the source's own clickid token now flows through as the click id.
- [x] **3. % traffic distribution across flows** — weighted-random split implemented and live-verified (70/30 test); position mode = first match wins; forced flows win; fallback URL + hide-referrer added in the same batch.
- [x] **4. Bot / filter rules** — click-level filtering in Settings: block (404) or mark-as-bot by IP, IP range (CIDR), User-Agent regex, empty referer, duplicate visitor. Live-tested both actions. (VPN/proxy ASN: is_bot/is_using_proxy already detected per-click; rules for those next.)
- [ ] **5. Cost auto-sync from ad networks** — sources already have `additional_settings` with `taboola_api_key`-style fields; wire actual API pulls (Facebook/Taboola/TikTok) to auto-import spend instead of manual cost per click.
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
- [x] **18. Automated tests** — `backend/tests/api_smoke.py`: 748-check live suite covering auth/2FA/permissions, campaign CRUD/clone/bulk/CSV, routing rules (filters/stickiness/caps/schedules), tracking plane (direct JS/pixel/impressions/Click API/simulation with visitor profiles), conversion economics (LTV/custom events/clickless/import), reporting depth (28 dimensions, pagination, CSV export), retroactive cost updates, domain groups, fraud/cloaking, flow actions, optimizer, funnels, and the security audit regressions. 748/748 passing.
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

- [x] **30. Traffic sources catalog** — 187 presets (full RedTrack channel list) with API-integration capability badges (cost update / campaign pause / blacklist placement / pause creative); searchable card-grid picker modal; seeding backfills missing presets on existing installs.
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

## 🔜 Next waves (queued, no external credentials needed)

- **Wave 11 — tracking-plane parity**: IPv6 ClickHouse storage (G92) · postback rules layer + fanout controls (G85/G86) · `/pb` POST/HEAD + extra postback tokens (G87) · rDNS / conversion-status / source-`is_bot` routing criteria (G88) · prefetch filtering + no-cost-for-bot-clicks (G89) · login IP whitelist (G90) · hide-referrer secondary domain (G91).
- **Wave 12 — ops polish**: SSL-expiry warning · settings backup/export · conversion-log search by URL · Google Safe Browsing check · pause offers from report rows · server-side GeoIP DB · domain-deletion reassignment · campaign-group filter in clicklog · click-log `campaigns:'own'` scoping.
- **Still blocked on user input**: cost auto-sync (5) and CAPI/conversion upload to Meta/Google — need ad-platform API tokens; certbot auto-renewal verification (20).
