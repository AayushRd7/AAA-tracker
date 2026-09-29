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
- [x] **18. Automated tests** — `backend/tests/api_smoke.py`: 967-check live suite covering auth/2FA/permissions, campaign CRUD/clone/bulk/CSV, routing rules (filters/stickiness/caps/schedules), tracking plane (direct JS/pixel/impressions/Click API/simulation with visitor profiles), conversion economics (LTV/custom events/clickless/import), reporting depth (28 dimensions, pagination, CSV export), retroactive cost updates, domain groups, fraud/cloaking, flow actions, optimizer, funnels, the security/robustness audit regressions, postback rules/fanout/tokens, and the routing-depth batch. 967/967 passing.
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

## 🗂 Wave 17 — Logs & observability (queued, high value)

Everything here is data we already write; the gap is that there is no surface to audit it.

- [ ] **Logs area in the sidebar** with sub-tabs, each filterable + exportable (CSV) and using the standard table chrome:
  - **Clicks** and **Conversions** (the existing click log / conversion log move here and keep working)
  - **S2S postbacks** — every inbound postback: ref/click id, status, payout, matched or rejected (incl. rule rejects), source IP, raw URL
  - **API postbacks** — outbound CAPI delivery: pixel/destination, platform, event name, HTTP status, response, attempt count (backed by the existing `meta_capi_log` / `capi_pixel_sent` tables)
  - **Click forwarding** — per click: chosen flow, schema, offer, forwarded URL, status (this is the "why did this click go there / why did it not work" view)
  - **Cost updates** — who changed cost, for which campaign/period, how many rows
- [ ] **Conversion health** additions to the system status page: last successful postback per source, CAPI failure counts, and links to the offending row.

## 🧰 Wave 18 — Tools grouping + saved filters (queued)

- [ ] Sidebar **Tools** group holding Domains, Scripts, Conversion tracking, Integrations, Filter presets, Fallback URL, Blacklist bots (pure information-architecture change — no new pages except the two below).
- [ ] **Global fallback URL** with the full token palette, used when no flow matches or caps/filters block a click (today fallback is per campaign only).
- [ ] **Saved filter presets** — name a filter set on any log/report and reapply it later.
- [ ] **Script library** — reusable titled snippets (tracking script, pixel, custom JS) with copy, injected into landers.
- [ ] **Funnel templates** — save a campaign's funnel steps as a reusable template and apply it to a new campaign.

## 📊 Wave 19 — Report & table depth (queued)

- [ ] **Column-set templates** (save/load which columns a table shows) + **custom columns**.
- [ ] **Conditional row colouring** by column value (threshold bands) and **number formatting** (decimal places, thousands divider) as workspace settings.
- [ ] **Report template gallery** for saved reports, plus an **IP report** mode (single-day, per-IP).
- [ ] **Conversion status lifecycle** for networks that approve conversions: pending / approved / declined / other, with approval and decline rates as report columns and an approval status per conversion row.
- [ ] Conversions log: **duplicate-status column**, **deduplicate token**, **bulk status change**, and **manual conversion add** (we have import + single-row edit today).
- [ ] Google Ads offline-conversion export format (blocked on the Ads API token, like G39/G40).

## 🔐 Wave 20 — Account & operations (queued)

- [ ] **Session management** — list active sessions per user (IP, geo, device, OS, browser, last seen) with "log out everywhere" (DB-backed sessions already exist).
- [ ] **Audit log filters** — by object type, title/id, user and date range in the UI.
- [ ] **Workspace settings** — decimal places, divider, default table template, grouping-view toggle.
- [ ] **Health-center style incidents** — see Wave 17's conversion health item.


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

### Phase 1 — known gaps (carried into phase 2)
- [ ] per-tenant users/roles/invites (users and `is_admin` are still install-global)
- [ ] per-tenant settings seeded on tenant creation; per-tenant retention and bind secret
- [ ] `parent_tenant_id` stored but not traversed (no inherited access/settings yet)
- [ ] the four `UPDATE settings … WHERE id` sites rely on a preceding tenant-scoped `SELECT … FOR UPDATE` rather than an inline predicate
- [ ] a new tenant's campaign is unreachable until one of its domains exists (host-based resolution)

### Phase 2 — per-tenant everything
- [ ] settings document per tenant (each tenant gets its own rows; the shared "name" key becomes per-tenant)
- [ ] permissions/roles per membership rather than per user (a user can be an editor in one tenant, viewer in another)
- [ ] audit log, logs area and every report scoped and filtered by tenant
- [ ] per-tenant CAPI/Meta connections, cost sync accounts and integration connections
- [ ] plan/quota fields on the tenant (seat count, retention, feature flags)

### Phase 3 — onboarding, whitelabel, billing, sub-workspaces
- [ ] self-serve signup + tenant onboarding (invite members, first campaign)
- [ ] whitelabel: custom domain and branding per tenant
- [ ] billing: plans, seats, usage metering, invoices
- [ ] agency sub-workspace management (create/switch/inherit, roll-up reporting across children)

## 🔜 Queued — will be completed later

- **Remaining G95 leftovers**: Google Safe Browsing checks (needs an API key) · server-side GeoIP DB (needs a MaxMind license or equivalent) — both implemented as opt-in settings the moment a key/license exists.
- **Wave 15 — scalability follow-ups** (deferred by design, volume-dependent): legacy flow-schema migration for configs carrying the removed `split` schema, ReplacingMergeTree redesign to replace the per-postback `ALTER UPDATE` mutation, fraud CIDR signals extended to IPv6, lazy rDNS resolution (only when a campaign actually filters on `rdns`).
- **Follow-ups noted by implementation**: domain-group grants are visibility-level (binding-time enforcement pending — G84); fraud CIDR signals are v4-only (v6 visitors simply never match); true IPv6-through-nginx path untested locally.
- **Installer portability**: `make install` now works with either the modern `docker compose` plugin or the legacy `docker-compose` binary (auto-detected, with an actionable preflight error and `make check`); `setup.sh` installs the plugin (or the standalone binary as fallback) and detects the distro; CI guards against re-hardcoding the binary and dry-runs the Makefile.
- **Still blocked on user input**: cost auto-sync (5) and CAPI/conversion upload to Meta/Google — need ad-platform API tokens; certbot auto-renewal verification (20).
