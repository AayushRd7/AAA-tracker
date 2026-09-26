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
- [x] **18. Automated tests** — `backend/tests/api_smoke.py`: 421-check live suite covering auth/2FA/permissions, campaign CRUD/clone/bulk/CSV, routing rules (filters/stickiness/caps/schedules), tracking plane (direct JS/pixel/impressions/Click API/simulation), conversion economics (LTV/custom events/clickless/import), reporting depth, and the security audit regressions. 421/421 passing.
- [x] **19. CI** — GitHub Actions: compileall over backend+frontend and compose YAML sanity on push/PR.
- [ ] **20. HTTPS/Certbot automation check** — certbot scaffolding exists but the auto-renewal flow is unverified.

## 🎨 UI Polish (smaller)

- [x] **21. Campaign duplicate/clone button** — implemented: `POST /api/campaigns/{id}/clone` (live-verified in the smoke suite).
- [x] **22. Bulk actions on lists** — select multiple campaigns: Activate / Pause / Delete (with confirm).
- [x] **23. Global search** — app-bar search across campaigns/offers/landings/domains/sources/affiliates/conversions (incl. tags + click IDs), permission-filtered, grouped autocomplete.
- [x] **24. Dark mode toggle** — full `[data-theme="dark"]` token layer, Vuetify dark sync, chart re-theming, Settings → General switch, persisted.
