# AAA Tracker — Prioritized Task List

Derived from `AUDIT-FINDINGS.md`. This list is ordered by priority, not by section number:
finish a tier before spending on the next one. **Do not move to P2 (marketing, more features)
until P0 is done** — the audit's verdict is that tenancy safety, not features, is the gap.

Priority legend

- **P0 — tenant safety / critical security.** Do not charge money until done.
- **P1 — operable as a service.** Fix before public launch.
- **P2 — sellable.** The commercial funnel and the ad-platform loop.
- **P3 — defensible.** Trust, scale, and the long migrations.

Size: **S** ≈ ≤1 day · **M** ≈ 2–4 days · **L** ≈ 1–2 weeks · **XL** ≈ multi-week/multi-month.
Refs point at `AUDIT-FINDINGS.md` sections and the real code locations.

**P0 status (2026-10-03):** P0.1 P0.2 P0.3 P0.4 P0.5 P0.7 P0.8 **done and verified** (full suite
1807 passed / 0 failed; deployed). P0.6 is the **first slice only** — the cross-tenant
membership hijack is closed and the operator namespace is reserved, but the full
users→tenant migration (per-tenant usernames, tenant-aware login, session re-key) is open.
**P0.9** (landings list had no tenant filter) is now **done** — the list, the metrics
aggregation and every id-based landing route are tenant-scoped.

**P1 status (2026-10-03):** shipped and verified — P1.1 (advisory-lock loop leader), P1.2
(backup/restore + key escrow + cron), P1.4 (opt-in image deploys + health-gated `rollout`/
`rollback`), P1.5 (nightly CI suite + guard typo + pip-audit), P1.6 (pinned/minified assets
with SRI, runtime Terser removed), P1.8 (truncation surfaced in the API), P1.10 (nginx
security headers + edge rate limits), P1.11 (deps pinned, brand-string typo, `noopener`),
P1.12 (cert renewal automation), P1.13 (health banner, deep-link). **Partial:** P1.3 ships
`GET /api/metrics` (uptime, pool, loop lag, per-tenant usage) but durable counters +
structured JSON logs remain; P1.9 fixed the login enumeration oracle, the `LIKE`-wildcard
session revoke and the unbounded failure map, but token-at-rest hashing/scopes and the
`JWT_SECRET` fallback remain. **Not started:** P1.7 (ClickHouse tenancy reshape) is an XL
migration that changes every report query and needs a planned cutover.

**P2 / P3 (not started — most need external input or are non-code):** billing/self-serve
need Stripe keys and a pricing decision; the ad-platform loop needs Google/TikTok developer
credentials; SOC 2 needs an auditor; the pilot needs real traffic; Vue 2→3 and the
users→tenant migration are multi-week. No engineering blocker for the rest — say which to
start.

---

## P0 — Make it safe to have more than one tenant

| ID | Sev | Task | Evidence | Fix direction | Size | Depends on |
|---|---|---|---|---|---|---|
| P0.1 | CRITICAL | **Cage or drop PHP landings.** Unsandboxed code exec on a flat, all-tenant filesystem; an admin of any workspace is effectively root. | `frontend/landings.py:44,329,531-542,320-325,678-695`; nginx `location ~ ^/l/(.*)\.php$` → `tracker_php`; no `open_basedir`/`disable_functions`. §3.1 | Product decision first: **static-only for SaaS**, or per-tenant php-fpm pool + `open_basedir` + `disable_functions` + memory/time caps + no egress. Move storage to `landings/<tenant_id>/<folder>`; per-tenant domain required to serve. Add zip entry/size caps + storage quota. | L | P0.3 |
| P0.2 | CRITICAL | **Stop reading security config from tenant 1 on the tracking plane.** Postback secrets, bot rules, blacklists, custom statuses, GDPR opt-out and the CAPI block are all tenant-1 for every tenant. | `frontend/app.py:1594,1627-1631`; same class `backend/auth.py:384`, `frontend/app.py:446`. §3.2 | Resolve campaign → tenant **first**, then read that tenant's blocks; cache keyed `(tenant_id, block)`. | M | — |
| P0.3 | HIGH | **Authoritative tenant routing.** Same alias/domain across tenants silently resolves to tenant 1; unknown hosts shadow tenant 1's campaigns. | `frontend/app.py:813-815,837,1449`. §3.3 | Require a per-tenant domain (or path prefix); make shared-domain alias collisions a **hard error**, not an ORDER BY. | M | — |
| P0.4 | HIGH | **Close the open doors found in the audit.** Unauthenticated state-changing GETs on the public host, plus three path-traversal holes in the landings manager. | `frontend/domains.py:38,141` (no auth dependency, mounted `app.py:520`); `frontend/landings.py:664,686,711,739` (missing path separator); `:513,586,600-604` (arbitrary `site_folder`). §4 #3,#4,#5 | Add the admin dependency the landings router already has; fix prefix checks to compare `Path` parts, not string prefixes; validate `FOLDER_NAME_RE` on every write path. | S | — |
| P0.5 | CRITICAL | **Stop seeding and never rotate default credentials.** Install seeds `tracker_admin`/`admin` and a fixed API token; the Readme/Makefile/setup print them. | `install/sql/init.sql:478,504-509`; `setup.sh:108`; `Makefile:230`. §3.4, §4 #1 | Generate a random admin password + API token at install, print once, force change on first login; remove all copies from docs/scripts/CI. | S | — |
| P0.6 | CRITICAL | **Make users tenant-scoped.** Usernames are install-global; a workspace admin can create global accounts and pull any global user into their workspace (cross-tenant escalation). | `models/user.py:11-12`; `app_pages/members.py:205-218`. §3.4, §10.2 | The deepest migration in the list: migrate `users`, `auth_sessions`, `api_tokens`, 2FA state, permissions and every `username → tenant` resolution (auth, members, invitations, audit, tracking plane). Split *platform operator* from *workspace owner* while in here. | XL | P0.2 |
| P0.7 | HIGH | **Fix the broken-by-default prod nginx vhost.** Per-domain vhosts are silently ignored in prod; catch-all serves the wrong cert; cert issuance is circular. | `nginx/nginx.prod.conf:24,29-30` (missing `include /var/www/nginx/domains/*.conf;`, hardcoded cert path); `setup.sh:95-100`. §4 #8 | Add the domains include to prod; parameterise the cert path; make `make certificate` work with nginx up (self-signed bootstrap → real cert → reload). | S | — |
| P0.8 | HIGH | **Stop `chmod -R 0777` on the whole app dir** (exposes `.env` and the TLS key); drop the `curl \| sudo sh` Docker install. | `setup.sh:84,41`. §4 #9 | Tighten to the specific dirs that need write (landings), keep `.env` and `ssl/` restricted. | S | — |
| P0.9 | HIGH | **Tenant-filter the landings list.** `list_landings` does `db.query(Landing).all()` with no tenant filter and aggregates ClickHouse metrics, so one workspace sees every tenant's landings — a cross-tenant leak. Missed in the first P0 pass. | `frontend/landings.py` `list_landings`. §4 #7 | Filter by the caller's tenant (the table is per `(tenant_id, folder)`); scope the metric aggregation to the same ids. | S | — |

---

## P1 — Make it operable as a service

| ID | Sev | Task | Evidence | Fix direction | Size | Depends on |
|---|---|---|---|---|---|---|
| P1.1 | HIGH | **One scheduler for background loops.** Multiline loops run once per worker; prod runs 2 workers → double alerts/emails and a double optimizer read-modify-write. | `backend/app.py:613-625`. §3.6 | Extract loops to a single-process scheduler, or guard with `pg_advisory_lock` / a `FOR UPDATE SKIP LOCKED` job table. Covers monitor, rules, optimizer, insights, email, Meta cost sync. | M | — |
| P1.2 | HIGH | **Backup / restore as a product feature.** Nothing dumps Postgres or ClickHouse; the integration key has no recovery path. | §6 Deployment/DR; `INTEGRATIONS_ENCRYPTION_KEY` Fernet, §5 | Scheduled `pg_dump` + ClickHouse backup to object storage; escrow the encryption key; document loudly that losing the key kills all platform tokens. | M | — |
| P1.3 | HIGH | **Observability for a shared fleet.** Everything is `print()`; no metrics, no structured logs. | `backend/app.py` (heartbeats), §6 | `/metrics` (click rate, postback/CAPI failures, pool saturation, loop lag, mutation queue depth); structured JSON logs; external uptime checks. **Add per-tenant usage counters now** — they become the billing meters (see P2.5). | M | P1.1 |
| P1.4 | HIGH | **Image-based, zero-downtime deploys.** Prod runs bind-mounted source; `make update` takes the whole stack (incl. tracking) down. | `docker-compose.prod.yml`; `Makefile` preclean; Readme:103. §6 | Drop prod bind mounts; health-gated rolling restart; `nginx -s reload` for config; migration gate. Tracking must not drop on deploy. Also makes rollback possible. | L | — |
| P1.5 | MEDIUM | **CI that runs the product's own suite.** CI runs compileall + a tiny e2e; the ~1,800-check smoke suite is local-only; the public-routing guard is a no-op (typo). | `ci.yml:144-147`; `scripts/check-public-routing.sh:45`. §6, §4 #29 | Nightly `api_smoke.py` against a fresh-install stack; a Playwright login→dashboard→create-campaign smoke; dependency + image scanning; fix the guard's grep. | M | P1.4 |
| P1.6 | MEDIUM | **Vendor, pin and minify CDN assets with SRI.** Prod ships unminified dev builds and Terser-to-the-browser, plus a floating unpinned chart lib with zero SRI. | `index.html:23-33,57`; `campaigns.html:3161`. §4 #20,#21 | Self-host pinned minified bundles with SRI; delete the runtime Terser path. Prereq for the Vue 3 migration. | M | — |
| P1.7 | HIGH | **Reshape the click store for tenancy + write path.** Sort key has no `tenant_id` (every tenant filter = full scan); per-postback synchronous mutation; per-request client. | `install/sql/clickHouse.sql:52-61`; `frontend/app.py:2575-2607,2594-2604`; `backend/app.py:628-646`. §6 | New installs: `ORDER BY (tenant_id, received_at)`; replace `ALTER UPDATE` mirroring with a `conversions` fact table (or `ReplacingMergeTree`); connection pooling. Changes every report query — plan the migration. | XL | P1.3 |
| P1.8 | MEDIUM | **Fix silent data truncation.** 50k visitor-id cap on click-date attribution truncates with only a stdout warning; the API returns wrong numbers with no signal. | `reports.py:88,362-383`. §6.5 | Raise/parameterise the cap and surface truncation in the API response. | S | — |
| P1.9 | MEDIUM | **Auth hardening pass.** Plaintext API tokens + raw session tokens at rest; bearer bypasses every section gate; JWT fallback secret; user-enumeration oracle; unbounded failure map; `LIKE` wildcard revoke; utcnow/now skew; `tenant_id` server_default=1. | `auth.py:86,202-210,348,353-362,393-426,1083,1123-1129,1222`; `models/base.py:20`; `app.py:445`. §4 #10-#16,#25,#26 | Hash tokens at rest + scopes/expiry/rotation; remove the JWT fallback; enforce section gates on bearer; unify error messages; evict the limiter map; fix the `LIKE` escape; drop the `tenant_id` default. | L | — |
| P1.10 | MEDIUM | **nginx TLS/header quality.** No http2, HSTS, CSP, X-Frame-Options, cipher config or OCSP; no `limit_req` anywhere. | `nginx/nginx.prod.conf:24`. §4 #24 | Enable http2 + HSTS + a baseline CSP + `X-Frame-Options` on auth pages; `limit_req` at the edge for `/backend/api/login` and `/pb`. | S | — |
| P1.11 | LOW | **Consistency cleanups from the audit.** Unpinned install requirements; 0777 default passwords in fallbacks; `except: pass` middleware; sweeping exceptions that skip whole cycles; `target="_blank"` without `noopener`; stray `// todo`; dev scripts served from the marketing root; test landings visible in the UI; doctor user mismatch. | §4 #25,#27,#28,#30,#31,#32,#33,#34; §6 requirements pin | Batch these into one hardening PR. | M | — |
| P1.12 | MEDIUM | **Automate cert renewal.** Nothing runs `certbot renew`; a DNS flake burns LE rate limits; no staging flag. | `frontend/domains.py:105-131`. §6 | Renewal loop/cron + staging flag + per-domain health. | M | P0.7 |
| P1.13 | MEDIUM | **Make the UI honest about half-shipped surfaces.** Health banner contradicts the incident list; fraud KPIs vs feed mismatch; blank Profit card; deep-link hijack; dialog lifecycle; share-link path trap; naming/locale inconsistencies. | §4 #35-#42; §7 | Each is small on its own — batch as a UI-truth pass. | M | — |

---

## P2 — Make it sellable

| ID | Sev | Task | Notes | Size | Depends on |
|---|---|---|---|---|---|
| P2.1 | HIGH | **Self-serve funnel** — signup, published price slider, checkout. Today access is invite-only and pricing has no prices. | §3.7, §7 Marketing | XL | P0.5, P0.6, P2.5 |
| P2.2 | HIGH | **Usage quotas + metering** — click/conversion, disk, storage/compute, report concurrency, email, API-call caps; plan→quota map. Meters must be authoritative before billing. | §3.5, §10.8 | L | P1.3 |
| P2.3 | HIGH | **Billing & plans** — Stripe, plan→quota mapping, one honest meter with humane overage (never pause tracking; no stacked ad-spend/MCP taxes). | §3.5, §8.5 | L | P2.2 |
| P2.4 | HIGH | **Close the ad-platform loop** — cost sync + CAPI/Enhanced Conversions for Google and TikTok first (then Snap/Reddit/Pinterest), plus a **published event-match-quality number**. This is the acquisition story competitors own. | §8.4, §9 Losses #1,#2 | XL | — |
| P2.5 | MEDIUM | **One-click ecom/CRM connectors** — Shopify, WooCommerce, Stripe, HubSpot. A non-technical buyer must reach verified conversions before routing depth matters. | §9 Losses #6 | L | P2.4 |
| P2.6 | MEDIUM | **Done-for-you migration + parallel-run cutover**, live chat, SLA ladder. The service layer is what buyers actually pay for. | §8.6, §9 Losses #3 | M (process) | P1.4 |
| P2.7 | MEDIUM | **Agency package** — per-client workspaces with client logins, per-user column masking, white-label report links, agency discount. | §9 Losses #7 | L | P0.6 |
| P2.8 | MEDIUM | **AI that acts under human guardrails** — visible pause/scale/approve demo, not just scores. | §8.6 | M | P1.1 |
| P2.9 | MEDIUM | **Trust artifacts** — public status page, contractual uptime SLA + credits, DPA + consent-mode v2, refund window, migration data-export promise. SOC 2 Type 2 when enterprise demand appears. | §9 Losses #8 | M–L | P1.3 |

---

## P3 — Make it defensible

| ID | Sev | Task | Notes | Size |
|---|---|---|---|---|
| P3.1 | MEDIUM | **Run one real-traffic pilot.** Nothing else converts until "not yet tested with real traffic" is false. Publish the numbers. | §9 Losses #4, §11.22 | M |
| P3.2 | MEDIUM | **Docs/API maturity bar** — `llms.txt`, OpenAPI, versioned REST, MCP with per-user OAuth scopes and tool docs. | §11.21 | L |
| P3.3 | MEDIUM | **Cross-device identity + offline/call attribution + CRM closed loop** — answer "which ads made money" beyond pure affiliate. | §9 Losses #5, §11.23 | XL |
| P3.4 | MEDIUM | **Vue 2 → Vue 3 / Vuetify 2 → 3.** ~26k lines of Vuetify-2 templates; run behind the Jinja shell, page by page. | §10.7, §11.12 | XL |
| P3.5 | LOW | **Docs polish** — `campaigns.html:2688` todo, naming consistency, login-page trust layer (forgot-password, version, 2FA hint, lockout copy). | §4 #31,#42, §7.7 | S |

---

## Suggested execution order (from the audit)

1. **P0.1–P0.8** — tenant safety. Nothing commercial is honest until these are done.
2. **P1.1, P1.2, P1.4, P1.5** — loops, backups, deploys, CI. Make the service operable.
3. **P1.3 + P2.2 + P2.3** — observability → usage counters → billing. They are one pipeline.
4. **P2.4** — the ad-platform loop. The single highest-leverage feature work.
5. **P2.1, P2.6, P2.9** — self-serve entry, migration service, trust artifacts.
6. **P1.7, P1.9, P3.4** — the deep migrations, scheduled so they never block a launch.
7. **P3.1** — the pilot, as soon as P0 is clear.

## Explicitly dropped (per prior decisions, kept out of this list)

White-label · mobile apps · in-UI update channel · TLS-fingerprint detection · offer marketplace.
Billing/self-serve deferred to P2 by the owner; recorded here anyway because P0/P1 gate them.
