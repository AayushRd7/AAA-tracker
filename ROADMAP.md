# AAA Tracker — Feature Roadmap

Pending features, organized by area. Suggested order: 1 → 3 → 4 (status editing, % traffic distribution, bot rules), then 12 + 13 (auth), then 7–9 (reporting depth).

## 🎯 Traffic & Campaign Features (core tracker gaps)

- [x] **0. Campaign list with live metrics** — Clicks/Conversions/CR/Cost/Revenue/Profit/ROI columns on the Campaigns page (ClickHouse-backed), with Binom-style green/red row tinting and a stats-period selector (Today/7d/30d/all time).

- [x] **1. Conversion status editing** — conversions can now be edited (status, payout, revenue, external/transaction ID) and deleted from the Conversion Log in Reports; postback counts + dedupe also handled in the same batch.
- [ ] **2. Sub ID / macro tokens in offers & landings** — full macro set (`{click_id}`, `{sub_id_2}`…`{sub_id_8}`, `{campaign_name}`, `{source}`, `{cost}`…) usable in offer URLs, lander links, and postback URLs — sub-id mapping exists in Settings, but macro substitution in offer URLs and landing templates is incomplete.
- [x] **3. % traffic distribution across flows** — weighted-random split implemented and live-verified (70/30 test); position mode = first match wins; forced flows win; fallback URL + hide-referrer added in the same batch.
- [x] **4. Bot / filter rules** — click-level filtering in Settings: block (404) or mark-as-bot by IP, IP range (CIDR), User-Agent regex, empty referer, duplicate visitor. Live-tested both actions. (VPN/proxy ASN: is_bot/is_using_proxy already detected per-click; rules for those next.)
- [ ] **5. Cost auto-sync from ad networks** — sources already have `additional_settings` with `taboola_api_key`-style fields; wire actual API pulls (Facebook/Taboola/TikTok) to auto-import spend instead of manual cost per click.
- [x] **6. S2S postback URL builder per network** — Copy button on each network now fills in the tracker domain and appends the postback security key when enabled.

## 📊 Reporting & Data

- [x] **7. Offer / landing / geo / device-level reports** — breakdown covers offer, landing, country, region, city, device, OS, browser, language, source, UTM, keyword, ISP, connection type, status, bot/proxy flags.
- [x] **8. Live/real-time clicks feed** — Live switch on the dashboard auto-refreshes Recent Visits every 10s.
- [x] **9. Landing performance metrics are mocked** — real ClickHouse aggregates per landing (clicks, conversions, cost, revenue, ROI).
- [ ] **10. Scheduled report emails per campaign / per user** — email reports exist globally; add per-campaign digests and non-admin recipients.
- [x] **11. Data retention / cleanup job** — daily prune loop deletes clicks older than the configured window (Settings → Data Retention).

## 🔐 Security & Users

- [x] **12. API token auth** — all backend APIs now require auth (session cookie or `Authorization: Bearer <token>`; token is the Settings API token). Live-tested: unauth → 401, bearer → 200.
- [x] **13. JWT is a fake in-memory store** — replaced with DB-backed opaque sessions (`auth_sessions` table): survive restarts, revocable at logout, TTL 30 days.
- [x] **14. Password change / reset UI** — self-service change (user menu, verifies current password) + admin edit; hashing migrated to bcrypt with transparent md5 → bcrypt upgrade on next login.
- [ ] **15. Two-factor auth (TOTP)** for admin.
- [ ] **16. Roles/permissions refinement** — currently admin vs user; add per-section permissions (e.g. user sees only assigned campaigns).

## ⚙️ Production Hardening

- [x] **17. Remove `--reload` / add uvicorn workers** — `docker-compose.prod.yml` override: uvicorn with 2 workers, no reload, ClickHouse memory cap. Dev compose stays hot-reload. Run with `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d`.
- [x] **18. Automated tests** — `backend/tests/api_smoke.py`: 15-check live suite (auth, 401 protection, campaign CRUD/clone, weighted redirect, fallback, hide-referrer, reports, cleanup). 15/15 passing.
- [x] **19. CI** — GitHub Actions: compileall over backend+frontend and compose YAML sanity on push/PR.
- [ ] **20. HTTPS/Certbot automation check** — certbot scaffolding exists but the auto-renewal flow is unverified.

## 🎨 UI Polish (smaller)

- [ ] **21. Campaign duplicate/clone button** — very common need.
- [x] **22. Bulk actions on lists** — select multiple campaigns: Activate / Pause / Delete (with confirm).
- [ ] **23. Global search** — campaigns/offers/landings by name.
- [ ] **24. Dark mode toggle** — CSS tokens are already set up for it.
