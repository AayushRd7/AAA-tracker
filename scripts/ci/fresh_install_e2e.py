#!/usr/bin/env python3
"""Fresh-install end-to-end check.

Run after `make install` has brought the stack up from EMPTY volumes. It proves
the install is actually usable, not merely that the containers started. Every
assertion here guards a failure that really shipped on a clean box:

* the installer created the admin user but no workspace membership — login
  returned 200 and then every API call returned 403;
* a ClickHouse schema statement was truncated by a ';' inside a SQL comment, so
  the table was missing while the stack looked healthy;
* a stale certificate next to a mismatched key made nginx refuse to start.

Stdlib only: the CI runner must not need extra packages for this.
"""
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = (os.environ.get("FRESH_BASE_URL") or "http://localhost").rstrip("/")
API = f"{BASE}/backend/api"
USER = os.environ.get("TEST_USER", "tracker_admin")
PASS = os.environ.get("TEST_PASS", "admin")
CH_CONTAINER = os.environ.get("TEST_CH_CONTAINER", "tracker_clickhouse")
PG_CONTAINER = os.environ.get("TEST_PG_CONTAINER", "tracker_postgres")

FAILURES = []
PASSED = 0
PID = os.getpid()


def check(label, ok, detail=""):
    global PASSED
    if ok:
        PASSED += 1
        print(f"  \u2713 {label}")
    else:
        FAILURES.append(label)
        print(f"  \u2717 {label}  {str(detail)[:300]}")


def read_env() -> dict:
    """POSTGRES_*/CLICKHOUSE_* from .env, so the checks use the real credentials."""
    values = {}
    try:
        with open(".env", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    except OSError:
        pass
    return values


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep 30x answers readable so the redirect target can be asserted."""

    def redirect_request(self, *args, **kwargs):
        return None


ENV = read_env()
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))
BARE_OPENER = urllib.request.build_opener(NoRedirect)


def call_no_redirect(url, timeout=30):
    """(status, body, headers) without following the redirect."""
    try:
        with BARE_OPENER.open(urllib.request.Request(url), timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)
    except Exception as e:
        return 0, repr(e), {}


def call(method, url, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # connection refused while the stack is booting
        return 0, repr(e)


def docker_exec(container, argv, timeout=60):
    try:
        proc = subprocess.run(["docker", "exec", container, *argv],
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except Exception as e:
        return 1, "", repr(e)


def docker_inspect(fmt, container, timeout=30):
    try:
        proc = subprocess.run(["docker", "inspect", "-f", fmt, container],
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except Exception as e:
        return 1, "", repr(e)


def wait_for_backend(deadline_s=180):
    """Poll the auth-gated status endpoint until the backend answers."""
    end = time.time() + deadline_s
    last = ""
    while time.time() < end:
        status, body = call("GET", f"{API}/status")
        last = f"{status} {body[:120]}"
        if status == 401:
            return status, body
        time.sleep(2)
    return 0, last


def pg_scalar(sql):
    return docker_exec(PG_CONTAINER, [
        "psql", "-U", ENV.get("POSTGRES_USER", "postgres"),
        "-d", ENV.get("POSTGRES_DB", "db"), "-tAc", sql])


def ch_query(sql):
    args = ["clickhouse-client"]
    if ENV.get("CLICKHOUSE_USER"):
        args += ["--user", ENV["CLICKHOUSE_USER"]]
    if ENV.get("CLICKHOUSE_PASSWORD"):
        args += ["--password", ENV["CLICKHOUSE_PASSWORD"]]
    return docker_exec(CH_CONTAINER, args + ["--query", sql])


def main():
    print("== Fresh install: end-to-end ==")

    # 1. the stack is up and the API is behind the auth gate
    status, body = wait_for_backend()
    check("backend answers /backend/api/status with 401 (auth gate live)",
          status == 401, f"{status} {body[:200]}")

    # 2. the installer-created admin can log in
    status, body = call("POST", f"{API}/login", {"username": USER, "password": PASS})
    check(f"login as {USER} succeeds", status == 200, f"{status} {body[:200]}")
    check("login issues a session cookie", len(JAR) > 0, str(list(JAR)))

    # 3. an authenticated call works — this is where a missing workspace
    #    membership used to show up as a 403 on every endpoint
    status, body = call("GET", f"{API}/campaigns/")
    check("authenticated /campaigns/ is not forbidden", status == 200,
          f"{status} {body[:200]}")

    # 4. the tracker can be configured through its own API
    status, body = call("POST", f"{API}/offers/", {
        "name": f"CI fresh offer {PID}",
        "url": "https://ci.example/offer?cid={click_id}"})
    offer_id = (json.loads(body) if status == 200 else {}).get("id")
    check("offer created on a fresh install", status == 200 and bool(offer_id),
          f"{status} {body[:200]}")

    alias = f"ci-fresh-{PID}"
    status, body = call("POST", f"{API}/campaigns/", {
        "name": f"CI fresh campaign {PID}", "alias": alias, "type": "campaign",
        "status": "active", "redirect_mode": "position",
        "config": {"flows": [{"type": "default", "position": 1, "enabled": True,
                              "schema": "direct", "offer": offer_id, "filters": []}],
                   "postbacks": [], "fallback_url": "", "hide_referrer": False},
    })
    campaign_id = (json.loads(body) if status == 200 else {}).get("id")
    check("campaign created on a fresh install",
          status == 200 and bool(campaign_id), f"{status} {body[:200]}")

    if campaign_id:
        status, body = call("GET", f"{API}/campaigns/")
        try:
            aliases = [c.get("alias") for c in json.loads(body)]
        except Exception:
            aliases = []
        check("the new campaign is readable back", alias in aliases, str(aliases)[:200])

    # 5. the databases are real, not just reachable
    code, out, err = pg_scalar(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
    check("postgres holds the installed schema (>=20 tables)",
          code == 0 and out.isdigit() and int(out) >= 20, out or err)
    code, out, err = pg_scalar(
        "SELECT count(*) FROM tenant_memberships WHERE tenant_id = 1")
    check("the admin has a workspace membership",
          code == 0 and out.isdigit() and int(out) >= 1, out or err)
    code, out, err = ch_query("SELECT count() FROM clicks_data")
    check("clickhouse clicks_data exists (install SQL not truncated)",
          code == 0 and bool(re.fullmatch(r"\d+", out)), out or err)
    code, out, err = ch_query(
        "SELECT name FROM system.tables WHERE database = currentDatabase() FORMAT TSV")
    check("clickhouse holds the installed tables", code == 0 and "clicks_data" in out,
          out or err)

    # 6. the docker socket proxy the certificate/nginx-reload flow depends on: it
    #    runs as nobody plus the HOST's docker group, so a wrong group id makes it
    #    restart forever with "permission denied" while the rest of the stack looks
    #    healthy. Both the loop and the group id are visible here.
    code, out, err = docker_inspect("{{.State.Status}} {{.RestartCount}}",
                                    "tracker_socket_proxy")
    parts = out.split()
    check("the docker socket proxy is running, not restart-looping",
          code == 0 and parts and parts[0] == "running"
          and int(parts[-1] or 0) <= 2, out or err)
    code, out, err = docker_exec("tracker_frontend", [
        "python3", "-c",
        "import urllib.request;"
        "print(urllib.request.urlopen('http://docker-socket-proxy:2375/_ping',"
        "timeout=5).read().decode())"])
    check("the frontend reaches the docker socket proxy (group id is right)",
          code == 0 and "OK" in out, (out or "") + (err or ""))

    # 7. the visitor path works: an alias redirect and a tracked visit that lands
    #    in clickhouse (the failure mode a started-container check cannot see)
    if campaign_id:
        status, body, headers = call_no_redirect(f"{BASE}/{alias}")
        location = headers.get("Location") or headers.get("location") or ""
        check("the campaign alias redirects to the offer with a click id",
              status in (301, 302, 307, 308) and "cid=" in location,
              f"{status} -> {location[:160]}")

        status, body = call("POST", f"{BASE}/t/collect",
                            {"c": str(campaign_id), "url": "https://ci.example/fresh"})
        click_id = None
        try:
            click_id = (json.loads(body) or {}).get("click_id")
        except Exception:
            pass
        check("a tracked visit is recorded", status == 200 and bool(click_id),
              f"{status} {body[:200]}")
        if click_id:
            code, out, err = ch_query(
                "SELECT count() FROM clicks_data "
                f"WHERE visitor_id = '{click_id}'")
            check("the visit reached clickhouse",
                  code == 0 and out.isdigit() and int(out) >= 1, out or err)

    # 8. drop the fixtures so a re-run starts clean
    if campaign_id:
        status, _ = call("DELETE", f"{API}/campaigns/{campaign_id}")
        check("the new campaign can be deleted", status == 200, str(status))
    if offer_id:
        status, _ = call("DELETE", f"{API}/offers/{offer_id}")
        check("the new offer can be deleted", status == 200, str(status))

    print()
    print(f"{PASSED} passed, {len(FAILURES)} failed")
    if FAILURES:
        for name in FAILURES:
            print(f"  failed: {name}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
