# AAA Tracker — Feature Roadmap

Pending features, organized by area. Suggested order: 1 → 3 → 4 (status editing, % traffic distribution, bot rules), then 12 + 13 (auth), then 7–9 (reporting depth).

## 🎯 Traffic & Campaign Features (core tracker gaps)

- [x] **1. Conversion status editing** — conversions can now be edited (status, payout, revenue, external/transaction ID) and deleted from the Conversion Log in Reports; postback counts + dedupe also handled in the same batch.
- [ ] **2. Sub ID / macro tokens in offers & landings** — full macro set (`{click_id}`, `{sub_id_2}`…`{sub_id_8}`, `{campaign_name}`, `{source}`, `{cost}`…) usable in offer URLs, lander links, and postback URLs — sub-id mapping exists in Settings, but macro substitution in offer URLs and landing templates is incomplete.
- [ ] **3. % traffic distribution across flows** — flows have position/weight, but there's no weighted-random split (e.g. send 70% to flow A, 30% to flow B) — probability flows.
- [ ] **4. Bot / filter rules** — click-level filtering: block by IP, IP range, User-Agent regex, empty referer, duplicate visitor, VPN/proxy ASN.
- [ ] **5. Cost auto-sync from ad networks** — sources already have `additional_settings` with `taboola_api_key`-style fields; wire actual API pulls (Facebook/Taboola/TikTok) to auto-import spend instead of manual cost per click.
- [ ] **6. S2S postback URL builder per network** — presets already carry postback templates; add a "copy ready postback URL" that fills in the tracker domain + macros from the settings.

## 📊 Reporting & Data

- [ ] **7. Offer / landing / geo / device-level reports** — breakdowns exist by campaign/country/date; extend the group-by to offer, landing, OS, browser, ISP.
- [ ] **8. Live/real-time clicks feed** — auto-refreshing click stream (today it's a manual reload).
- [ ] **9. Landing performance metrics are mocked** — `frontend/landings.py` returns zeros for clicks/conversions/revenue/ROI; join real ClickHouse data per landing.
- [ ] **10. Scheduled report emails per campaign / per user** — email reports exist globally; add per-campaign digests and non-admin recipients.
- [ ] **11. Data retention / cleanup job** — ClickHouse grows unbounded; add configurable auto-prune (e.g. keep 180 days).

## 🔐 Security & Users

- [ ] **12. API token auth** — all APIs are cookie-session only; add bearer-token auth so external tools/networks can pull reports securely.
- [ ] **13. JWT is a fake in-memory store** — `backend/auth.py` has a "Fake user store" comment: tokens aren't persisted, so restarts log everyone out; move to DB-backed sessions with revocation.
- [ ] **14. Password change / reset UI** — users page has no password change flow; `md5(akm + password)` hashing should move to bcrypt at the same time.
- [ ] **15. Two-factor auth (TOTP)** for admin.
- [ ] **16. Roles/permissions refinement** — currently admin vs user; add per-section permissions (e.g. user sees only assigned campaigns).

## ⚙️ Production Hardening

- [ ] **17. Remove `--reload` / add uvicorn workers** in docker-compose (dev mode still on in prod containers) + ClickHouse memory cap in compose.
- [ ] **18. Automated tests** — zero test suite; start with API smoke tests + tracking pixel/postback integration test.
- [ ] **19. CI** — GitHub Actions: lint, py_compile, build images on push.
- [ ] **20. HTTPS/Certbot automation check** — certbot scaffolding exists but the auto-renewal flow is unverified.

## 🎨 UI Polish (smaller)

- [ ] **21. Campaign duplicate/clone button** — very common need.
- [ ] **22. Bulk actions on lists** — delete/enable multiple.
- [ ] **23. Global search** — campaigns/offers/landings by name.
- [ ] **24. Dark mode toggle** — CSS tokens are already set up for it.
