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

Don't forget to open ports 443 and 80 for nginx.

```bash
git clone https://github.com/AayushRd7/AAA-tracker.git
cd AAA-tracker
make install
```

### Restart

```bash
make restart
```

### Super admin user login 

```bash
login tracker_admin 
password admin 
```

### Run Locally

```bash
make install-local
```

By default:
- API will be available at `https://localhost`
- Dashboard will be available at `https://localhost/backend`

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

- Fully self configurable via docker-compose.yml.
- Should be open ports 443 and 80 for nginx.


## ⚙ Testing:

A live API smoke test suite exists at `backend/tests/api_smoke.py` — **421 checks** running against a running instance (dev or prod): auth + 2FA + permissions, campaign CRUD/clone/bulk/CSV, routing rules (filters/stickiness/caps/schedules), the tracking plane (direct JS tracking, pixel, impressions, Click API, simulation, GDPR), conversion economics (LTV, custom events, clickless, import), reporting depth, and security-regression checks. It cleans up all temporary data it creates. Exits non-zero on the first failure.

```bash
TEST_BASE_URL=https://localhost TEST_INSECURE=1 \
TEST_USER=tracker_admin TEST_PASS=admin \
python3 backend/tests/api_smoke.py
```

Environment variables:

- `TEST_BASE_URL` — base URL of the running instance (default: `http://localhost`).
- `TEST_INSECURE=1` — skip TLS certificate verification (needed for the self-signed local cert).
- `TEST_USER` / `TEST_PASS` — login credentials (default: `tracker_admin` / `admin`).

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
