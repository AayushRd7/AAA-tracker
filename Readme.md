![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Build Status](https://img.shields.io/badge/build-passing-brightgreen)
![FastAPI](https://img.shields.io/badge/FastAPI-Backend-blue)
![Vue.js](https://img.shields.io/badge/Vue-2.x-green)
![Vuetify](https://img.shields.io/badge/Vuetify-Material--UI-purple)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Database-blue)
![Docker](https://img.shields.io/badge/Docker-Container--orchestration-blue)
![Nginx](https://img.shields.io/badge/Nginx-Web--server-green)
![Python](https://img.shields.io/badge/Python-3.11+-blue)
![ClickHouse](https://img.shields.io/badge/ClickHouse-Analytics-blue)
![codemirror](https://img.shields.io/badge/codemirror-Editor-blue)


# 📚 AAA Tracker

**Description:**  
> AAA Tracker is a modern traffic tracking and campaign management platform built with FastAPI and Vue 2 + Vuetify. It provides powerful features like real-time data analysis, campaign optimization, affiliate tracking, and detailed reporting. The backend is optimized for high performance and scalability, while the frontend delivers a fully responsive, Material Design-based user experience. Perfect for marketing professionals, ad networks, and affiliate managers who need reliable traffic management and advanced analytics.
---

## 📂 Table of Contents

- [About the Project](#about-the-project)
- [Tech Stack](#tech-stack)
- [Getting Started](#getting-started)
- [Running the Project](#running-the-project)
- [Backup & disaster recovery](#-backup--disaster-recovery)
- [Health-gated rollout](#-health-gated-rollout-opt-in)
- [Project Structure](#project-structure)
- [Environment Variables](#environment-variables)
- [Development Scripts](#development-scripts)
- [Testing](#testing)
- [Contribution Guidelines](#contribution-guidelines)
- [License](#license)
- [Contact](#contact)

---

## 📖 About the Project

AAA Tracker is a free, open-source traffic tracker:

- **Backend:** FastAPI — a high-performance asynchronous API server.
- **Frontend:** Vue 2 + Vuetify — a Material Design UI framework for Vue.js.

Key features:
- **Campaign routing engine** — rule-based flow filters (AND/OR groups, IS-NOT, regex, CIDR, 20+ fields: geo, device, OS/browser+version, ISP, connection, referrer, keyword, sub-IDs, bot status), weighted % splits, visitor stickiness, dayparting/schedules, click caps, offer conversion caps with overflow, fallback URLs, referrer hiding.
- **Every tracking method** — redirect links, direct/no-redirect JS tracking (`/t.js`), client-side conversion pixel (`/p`), clean URLs, impression tracking, server-side Click API for apps/webviews.
- **Conversion management** — S2S postbacks with secret-key/IP protection and dedupe, custom event types, LTV/rebill accumulation with per-click event history, clickless attribution, manual CSV import, post-conversion editing.
- **ClickHouse analytics** — multi-dimension drill-down report builder (up to 5 levels) with saved reports and shareable public links, custom formula metrics, period comparison, live click feed, per-report email schedules, date-basis toggle, annotations, 24 dimensions, CSV export everywhere.
- **Platform** — TOTP 2FA with backup codes, per-section permissions, audit log, archive/restore, bulk edit + CSV import/export, flow monitoring with dead-offer auto-disable, auto-rules engine, global search, GDPR opt-out + IP anonymization, dark mode.
- Landing pages storage with PHP support + built-in code editor.
- Ability to make custom routes for different domains and countries.
- Telegram conversion notifications and scheduled email reports.
- Built-in affiliate network presets (79) with ready-made postback templates.

---

## 🚀 Tech Stack

**Backend:**
- [Python 3.11+](https://www.python.org/)
- [FastAPI](https://fastapi.tiangolo.com/)
- [Uvicorn](https://www.uvicorn.org/)
- [Pydantic](https://docs.pydantic.dev/)
- [PostgreSQL](https://www.postgresql.org/)

**Frontend:**
- [Vue 2](https://v2.vuejs.org/)
- [Vuetify](https://vuetifyjs.com/en/)
- [Axios](https://axios-http.com/)

**DevOps (Optional):**
- [Docker](https://www.docker.com/)
- [Nginx](https://nginx.org/en/)

---

## 🛠️ Getting Started

### Install

Requirements: Docker Engine with **either** the Compose v2 plugin (`docker compose`) **or** the standalone
`docker-compose` binary. `make install` detects which one you have and uses it — no need to install the
legacy binary on modern Docker.

Don't forget to open ports 443 and 80 for nginx.

```bash
git clone https://github.com/AayushRd7/AAA-tracker.git
cd AAA-tracker
make install
```

### Running in production

`make install` uses `docker-compose.yml`, which is the **development** file: it runs uvicorn with
`--reload` (the app restarts whenever a file under the bind mount changes, which drops in-flight
requests) and a single worker. For a live box, run the production overlay instead — it drops
`--reload`, runs two workers and applies the memory caps:

```bash
docker compose --compatibility -f docker-compose.yml -f docker-compose.prod.yml up -d
```

**After every `git pull` restart the app** — with `--reload` gone, code changes are not picked up live:

```bash
make update          # pull + recreate, once the checkout is clean
# or: docker compose --compatibility -f docker-compose.yml -f docker-compose.prod.yml up -d backend
```

A failed install never reports success: `install.py` exits non-zero, so `make` stops instead of
claiming the database was initialised when it was not.

---

## 🗄️ Backup & disaster recovery

All durable state lives in two databases: **Postgres** (workspaces, campaigns, conversions,
settings) and **ClickHouse** (clicks). `make backup` dumps both into a timestamped directory;
the TLS certificates are re-issued by certbot and are not part of the backup.

```bash
make backup                        # Postgres + ClickHouse + manifest
make restore DIR=backups/<stamp>   # DESTRUCTIVE: restore that run into this stack
make restore FORCE=1               # allow restoring over a non-empty database
make backup-cron                   # install the daily /etc/cron.d/aaa-tracker-backup entry
```

Each run lands under `${BACKUP_DIR:-./backups}/<UTC-timestamp>/`:

```text
<UTC-timestamp>/
├── postgres.dump        # pg_dump -Fc (custom format, restored with pg_restore)
├── clickhouse/          # BACKUP DATABASE ... TO File(...) archive(s)
│   └── clickhouse-<stamp>.zip
└── manifest.txt         # app git SHA, DB versions, dump sizes
```

**Configuration** (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `BACKUP_DIR` | `./backups` | Output root. Each run gets its own timestamped subdirectory. |
| `BACKUP_KEEP` | `7` | Runs to keep; older runs are pruned after a successful backup. |
| `BACKUP_S3_BUCKET` | empty | Optional off-box copy. When set, `aws s3 cp` uploads each run to `s3://$BACKUP_S3_BUCKET/<stamp>/` (needs the AWS CLI and bucket credentials). Empty keeps backups local. |

`make backup-cron` installs a daily root cron entry (`23 2 * * *`) that runs
`scripts/backup.sh`; it is idempotent and only rewrites `/etc/cron.d/aaa-tracker-backup`
when the script path is missing from it. Keep the backup directory out of git — a
`postgres.dump` is a full copy of the database.

**ClickHouse caveat.** This stack does not configure a ClickHouse `backups` disk, so the
`Disk(…)` backup engine is unavailable (`backups.allowed_disk is not set`). The script
therefore uses the `File` engine, writes a zip inside the container and copies it out. If
the ClickHouse backup fails, the script says so loudly and still produces the Postgres
dump and manifest — ClickHouse clicks will be missing from that run, so check the output.

**Restore is destructive.** `make restore` overwrites the databases and refuses to run
against a stack that already holds tables unless `FORCE=1` is set. It is built for a
fresh/empty stack (e.g. after `make reset && make install` recreated the schema). After a
restore, run `make restart` so the app reconnects.

### Key escrow — the one secret a backup does NOT contain

Stored ad-platform OAuth tokens are encrypted with the Fernet key in
`INTEGRATIONS_ENCRYPTION_KEY`. The in-app settings export deliberately nulls secrets, and
`make backup` does **not** copy this key. A restored database is useless without it: every
stored token becomes unrecoverable.

Print it once and store it **outside this server** (password manager or secrets vault):

```bash
make show-integration-key
```

---

## 🚀 Health-gated rollout (opt-in)

The default deploy path is unchanged: `make update` still pulls and recreates the stack
from **bind-mounted source**. If you want built images with no source mounts and a rollout
that does not drop tracking traffic, use the opt-in profile.

```bash
make rollout      # build images; recreate backend, then frontend, each health-gated; reload nginx
make rollback     # retag + recreate the images saved before the last rollout
```

`make rollout` uses `docker-compose.images.yml` (composed with the base and
`docker-compose.prod.yml`):

```bash
docker compose --compatibility \
  -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.images.yml up -d
```

- It recreates the built `backend` image first and waits for its container healthcheck to
  pass, then the `frontend`, then reloads nginx (`nginx -s reload`) instead of recreating
  it — so in-flight tracking requests are not dropped.
- On failure it stops and tells you to run `make rollback`, which re-tags the images saved
  into `.rollout/` before the rollout and recreates the two app containers from them.
- The image profile drops the `./backend:/app` and `./frontend:/app` source mounts but
  keeps the certificate/config mounts those services still need. It uses Compose's
  `!override` volume tag; the older `!reset []` form would also drop the cert mounts, and
  both need a recent Compose. `docker compose -f docker-compose.yml -f docker-compose.prod.yml
  -f docker-compose.images.yml config >/dev/null` is the quick compatibility check.

Because the images have no source mounts, a code change reaches the container only via
`make rollout` (rebuild + recreate) — not by editing a file on disk.

### Troubleshooting

**`make: docker-compose: No such file or directory`** — you have the modern Compose plugin and no legacy
binary (or the reverse). Current Docker installs ship only `docker compose`. Fix it with either:

```bash
# Recommended — the Compose plugin (Debian/Ubuntu)
sudo apt-get update && sudo apt-get install -y docker-compose-plugin

# or RHEL/Fedora
sudo dnf install -y docker-compose-plugin

# or the standalone v2 binary (any distro)
sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
  -o /usr/local/bin/docker-compose && sudo chmod +x /usr/local/bin/docker-compose
```

Then `make install` again. Check what you have with `make check` (it prints which compose command is used).

**`env file .env not found`** — `make install` creates `.env` from `.env.example` automatically on first run
(with generated Postgres/ClickHouse/JWT secrets), so this should not happen on a current checkout. If you see
it, you are on an older revision or deleted `.env` mid-setup:

```bash
git pull
make env        # recreates .env from .env.example
make install
```

An existing `.env` is never overwritten — delete it first if you want fresh credentials (note: an existing
Postgres data volume keeps its old password, so only do this on a fresh install).

**`Conflict. The container name "/tracker_clickhouse" is already in use`** — leftover containers from a
previous attempt (or a checkout you deleted) still hold the fixed names this stack uses. `make install` now
clears them automatically before starting (`make preclean` does it on its own) — **data volumes are not
touched**. If you want to start truly from scratch instead:

**`❓ Do you really want to DROP ALL TABLES? … EOF when reading a line`** — the installer used to require an
interactive confirmation, which `docker exec` cannot provide. It is now **non-interactive and idempotent**: the
schema SQL uses `IF NOT EXISTS` / `ON CONFLICT`, so installing over an existing database adds whatever is
missing and drops nothing. Just run it again:

```bash
make install-db            # ensure schema — safe to re-run, never drops data
make install-db-fresh      # DESTRUCTIVE: drop all tables, then recreate
```

**`make reset`** removes containers *and* data volumes, then `make install` gives you a clean slate.

**A failed install silently "succeeded"** — this is fixed: `install.py` now exits non-zero, so `make` stops
instead of reporting success while the database was never initialised.

**Containers fail to start / port already in use** — `docker compose down` (or `make stop`), then
`make install`. Ports 80 and 443 must be free.

### Troubleshooting first stop

```bash
make doctor
```

Checks containers, backend→database reachability, the auth gate on HTTP and HTTPS, the
bare-host redirect, and runs a real login probe on both schemes — printing the backend
traceback when login returns 500.

### Restart

```bash
make restart
```

### Super admin user login

The seeded `tracker_admin` account has no usable default password. On the first
`make install` the installer generates a random password (or uses the
`AAA_ADMIN_PASSWORD` you set in `.env`) and prints it once — store it then; it is
not shown again. The tenant-1 API token is generated the same way.

### Run Locally

```bash
make install-local
```

By default:
- API will be available at `https://localhost`
- Dashboard will be available at `https://localhost/backend`

Opening the bare host (`http://your-server-ip/`) redirects to the dashboard, so you don't
need to remember the `/backend` path. The admin password and API token are generated at
install and printed once — store them then, or set `AAA_ADMIN_PASSWORD` before `make install`
to pick the password yourself.

Both HTTP and HTTPS installs work: the session cookie is marked `Secure` only when the
request actually arrived over HTTPS (nginx forwards `X-Forwarded-Proto`), so browsers don't
silently drop the session on a plain-HTTP install.

---

## 🏗️ Project Structure

```bash
backend/
  ├── app_pages/
  │   ├── about.py
  │   ├── campaigns.py
  │   ├── dashboard.py
  │   ├── ...
  ├── install
  │   ├── sql/
  │   ├── install.py
  │   └── requirements.txt
  ├── models/
  │   ├── campaign.py
  │   ├── domain.py
  │   ├── ...
  ├── themes/
  │   ├── default/
  ├── app.py
  ├── auth.py
  └── Dockerfile
  └── requirements.txt
nginx/
  ├── default.conf
  ├── nginx.dev.conf
  ├── nginx.prod.conf
  └── Dockerfile
certbot-var/
letsencrypt/
ssl/
  ├──
frontend/
  ├── app.py
  ├── requirements.txt
  └── Dockerfile
├── docker-compose.yml
├── Makefile
└── README.md
```

---

## ⚙️ Environment Variables

Configuration lives in `.env` (created from `.env.example` by `make env`, then filled with generated
secrets). `make install` derives `DOCKER_GID` from the host automatically. The keys that matter:

| Key | Purpose |
|---|---|
| `POSTGRES_HOST/PORT/DB/USER/PASSWORD` | Postgres connection (the backend and the installer read it) |
| `CLICKHOUSE_HOST/PORT/USER/PASSWORD/DB` | ClickHouse connection (clicks live here) |
| `JWT_SECRET` | Session signing — generated on first install |
| `AAA_ADMIN_PASSWORD` / `AAA_ADMIN_USER` | Optional: the password/user the installer rotates the seeded admin account to. Generated and printed once (and recorded in `.env`) when unset |
| `PUBLIC_BASE_URL` | The public origin, used for the OAuth callback URL and outbound links |
| `META_APP_ID` / `META_APP_SECRET` | Meta app credentials for the OAuth Connect flow (env, never the UI) |
| `META_GRAPH_VERSION` | Graph API version (default `v26.0`); a per-workspace setting overrides it |
| `INTEGRATIONS_ENCRYPTION_KEY` | Fernet key that encrypts stored platform tokens at rest |
| `SNAPCHAT_*`, `TIKTOK_*`, `PINTEREST_*`, `GOOGLE_*`, `GOOGLE_ADS_DEVELOPER_TOKEN` | Client credentials for the other ad platforms (placeholders until their senders ship) |
| `DOCKER_GID` | The host's docker group id — used only by the socket-proxy sidecar; `make env` fills it in |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` / `DB_POOL_TIMEOUT` / `DB_POOL_RECYCLE` | Backend Postgres pool (defaults 20 / 30 / 15s / 1800s). Size it for the worker count: `workers × (size + overflow)` must stay under Postgres' `max_connections` |

Ports 80 and 443 must be reachable for nginx.

## ⚙ Testing:

A live API smoke test suite exists at `backend/tests/api_smoke.py` — **1,500+ checks** against a running
instance (dev or prod): auth + 2FA + permissions, workspaces/tenancy (query isolation, roles, invitations,
agency sub-workspace traversal), campaign CRUD/clone/bulk/CSV, routing rules
(filters/stickiness/caps/schedules), the tracking plane (direct JS tracking, pixel, impressions, Click API,
simulation, GDPR), conversion economics (LTV, custom events, clickless, import), reporting depth, the Logs
area, CAPI pixel integrations (delivery verified against a mock receiver), the ad-platform OAuth Connect
flow, Meta Ads cost sync (mock Graph), and security-regression checks. It cleans up all temporary data it
creates and exits non-zero on the first failure.

```bash
TEST_BASE_URL=https://localhost TEST_INSECURE=1 \
TEST_USER=tracker_admin TEST_PASS=<the password printed at install> \
python3 backend/tests/api_smoke.py
```

Environment variables:

- `TEST_BASE_URL` — base URL of the running instance (default: `http://localhost`).
- `TEST_INSECURE=1` — skip TLS certificate verification (needed for the self-signed local cert).
- `TEST_USER` / `TEST_PASS` — login credentials. The password has no default: use the one
  printed once at install (also recorded as `AAA_ADMIN_PASSWORD` in `.env`).
- The Copilot provider round-trip checks need the **instance** (not this command) to point
  `OPENROUTER_BASE_URL` at a bindable mock on the host, e.g. `http://host.docker.internal:18999`; the
  provider endpoint is env-fixed, so without it those checks are skipped and the contract checks still run.

Two other checks sit next to it:

- `python3 scripts/ci/fresh_install_e2e.py` — run after `make install` on **empty volumes**: proves the
  install is usable (admin can log in, an authenticated call is not forbidden, an offer and a campaign can
  be created, the alias redirect carries a click id, a tracked visit reaches ClickHouse, the databases hold
  the seeded schema and the socket proxy is reachable).
- `make check-ci` — parses the workflow files; a YAML typo there stops every CI run from even starting.

CI (`.github/workflows/ci.yml`) has three jobs:

- **compile / guards** — on every push and PR: Python compile, YAML + Makefile guards, shell syntax, the
  install-SQL idempotency checks, and a Python dependency vulnerability scan (`pip-audit` over
  `backend/requirements.txt` and `backend/install/requirements.txt`).
- **fresh-install** — on every push and PR: installs the whole stack from empty volumes on a clean runner,
  runs `make doctor` and then the end-to-end assertions.
- **api-smoke** — **nightly** (`schedule`) and on `workflow_dispatch`: installs the stack from empty volumes
  and runs the full live API suite (`backend/tests/api_smoke.py`) against it. It reads `AAA_ADMIN_PASSWORD`
  and `CLICKHOUSE_PASSWORD` back from the generated `.env`, so it logs in as the install's real admin. The
  suite takes ~14 minutes, which is why it is not run on every push. The same suite can still be run locally
  against a dev server with the command above.

## 🩹 Security & bug-fix log

2026-09-26 full-codebase audit (two review passes, 58 findings — all criticals/highs fixed, most mediums/lows fixed, remainder documented inline). The significant ones:

- **IP spoofing closed** — client IP now resolved X-Real-IP-first; nginx rewrites `X-Forwarded-For`; postback IP allowlist and bot IP rules use the same resolution.
- **Atomic conversion upsert** — concurrent postbacks can no longer double-count LTV (single guarded UPDATE with server-side event append).
- **Visitor PII no longer leaks into click-out URLs** — only the click ID and mapped passthrough params.
- **Paused campaigns stop tracking**; redirect chains capped at 3 levels with per-level ClickHouse attribution.
- **Event-loop blocking removed** — Telegram notifications, landing fetches, and per-hit config loads no longer stall the tracker; ClickHouse client pool bounded.
- **Auth hardening** — TOTP tokens burn after 3 failures and reject replay (single-use jti), session cookie `Secure`, constant-time secret compares, login + TOTP rate limiting.
- **XSS/CSV hardening** — report emails HTML-escape all values, CSV exports neutralize spreadsheet formula injection.
- **Authorization** — `campaigns:'own'` enforced on every mutation (not just listing); archive/restore requires write; global search respects section permissions.
- Ops: `/simulate` and the debug log are admin-gated and secret-redacted; GDPR opt-out honored on every tracking endpoint.

2026-09-29/30 — tenancy, integrations and live-server fixes (each one is a commit; ROADMAP wave 22 has the
long versions):

- **Multi-tenancy** — tenant-scoped schema with query isolation (no cross-tenant reads), roles per
  membership (owner/admin/editor/viewer) with workspace member management, per-workspace settings,
  retention, bind secret and API token, single-use hashed invitations with a role ceiling, and agency
  sub-workspace traversal (authority flows *down* the tree only).
- **CI had never run at all** — the workflow file was invalid YAML (an unquoted `ok: ` inside a `run:`), so
  every push failed in 0s with "workflow file issue" and no job ever reported. Fixed, plus `make check-ci`.
- **Fresh installs were broken twice** — the admin was created with no workspace (login returned 200 and
  then every call 403'd) and a `;` inside a SQL comment truncated the ClickHouse `CREATE TABLE`. Both
  fixed, and CI now installs the stack from empty volumes on every push.
- **A burst of dashboard traffic froze the whole API** — the dashboard's handlers ran blocking
  SQLAlchemy/ClickHouse work on the event loop and the Postgres pool was undersized (5 + 10, 30s wait).
  Fixed with a tunable pool and threadpool handlers; verified over 45 rounds of 48-request bursts.
- **The OAuth callback the app advertises was not a route** — the provider's redirect landed on a 404, so
  no platform connection could ever complete. Both paths answer now, and the suite asserts the advertised
  URL is served.
- **Token encryption was impossible in the built image** — `cryptography` was in `requirements.txt` but the
  image predated it, and the failure blamed `INTEGRATIONS_ENCRYPTION_KEY` instead. Rebuilt.
- **The Meta cost sync uses the connected account token** when the block carries none, so a second (System
  User) token is not required to read spend.
- **Socket proxy** — the host's docker group id is derived (`DOCKER_GID`) instead of hardcoded, so the
  sidecar no longer restart-loops on a host whose group differs.

## 🧰 Contribution Guidelines

- Follow PEP8 coding standards for Python.
- Keep the API modular and organized.
- Write reusable Vue components.
- Use Vuetify components for consistent UI.
- Regularly update dependencies.

---

## 📜 License

This project is licensed under the MIT License.  
See the [LICENSE](LICENSE) file for more details.

---

## 📞 Contact

**Website:** [aaadigital.marketing](https://aaadigital.marketing)  
**Author:** [AayushRd7](https://github.com/AayushRd7)

---
