"""Live API smoke test for AAA Tracker.

Run against a running instance (dev or prod):

    TEST_BASE_URL=https://localhost TEST_INSECURE=1 \
    TEST_USER=tracker_admin TEST_PASS=... \
    python backend/tests/api_smoke.py

Creates temporary test data (campaign + clone + conversions) and cleans up
after itself. Exits non-zero on the first failure.
"""
import os
import re
import sys
import json

import requests

BASE = os.environ.get("TEST_BASE_URL", "http://localhost")
INSECURE = os.environ.get("TEST_INSECURE") == "1"
USER = os.environ.get("TEST_USER", "tracker_admin")
PASS = os.environ.get("TEST_PASS", "admin")

passed = failed = 0


def check(name, condition, extra=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name} {extra}")


def main():
    s = requests.Session()
    s.verify = not INSECURE
    api = f"{BASE}/backend/api"

    print("== Auth ==")
    r = s.post(f"{api}/login", json={"username": USER, "password": PASS})
    check("login", r.status_code == 200, r.text[:120])

    r = requests.get(f"{api}/campaigns/", verify=not INSECURE)
    check("API requires auth (401 unauth)", r.status_code == 401, str(r.status_code))

    print("== Campaigns ==")
    r = s.get(f"{api}/campaigns/")
    check("list campaigns", r.status_code == 200, r.text[:120])
    existing = {c["alias"] for c in r.json()}

    alias = f"smoke-{os.getpid()}"
    payload = {
        "name": "Smoke Test Campaign", "alias": alias, "type": "campaign",
        "status": "active", "redirect_mode": "weight",
        "config": {"flows": [{
            "type": "default", "position": 1, "enabled": True, "schema": "redirect",
            "redirect_url": "https://example.com/smoke-a", "weight": 100, "filters": [],
        }], "postbacks": [], "fallback_url": "https://example.com/smoke-fb",
            "hide_referrer": False},
    }
    r = s.post(f"{api}/campaigns/", json=payload)
    check("create campaign", r.status_code == 200 and "id" in r.json(), r.text[:200])
    cid = r.json().get("id")

    r = s.post(f"{api}/campaigns/{cid}/clone")
    check("clone campaign", r.status_code == 200 and "alias" in r.json(), r.text[:200])
    clone_id = r.json().get("id")

    r = s.get(f"{api}/campaigns/metrics")
    check("campaign metrics endpoint", r.status_code == 200, r.text[:120])

    print("== Tracking plane ==")
    r = requests.get(f"{BASE}/{alias}", verify=not INSECURE, allow_redirects=False)
    check("campaign redirect (302/307)", r.status_code in (301, 302, 307, 308),
          f"got {r.status_code}")
    check("redirect target", "example.com/smoke-a" in (r.headers.get("location") or ""),
          r.headers.get("location", ""))

    # Fallback: a filter that never matches
    payload["config"]["flows"][0]["filters"] = [
        {"parameter": "country", "condition": "equals", "value": "ZZ"}]
    r = s.put(f"{api}/campaigns/{cid}", json=payload)
    check("update campaign with dead filter", r.status_code == 200, r.text[:150])
    r = requests.get(f"{BASE}/{alias}", verify=not INSECURE, allow_redirects=False)
    check("fallback redirect when no flow matches", "example.com/smoke-fb" in (r.headers.get("location") or ""),
          f"got {r.status_code} -> {r.headers.get('location', '')}")

    # Hide referrer: 200 HTML meta refresh instead of a 30x
    payload["config"]["hide_referrer"] = True
    payload["config"]["flows"][0]["filters"] = []
    r = s.put(f"{api}/campaigns/{cid}", json=payload)
    r = requests.get(f"{BASE}/{alias}", verify=not INSECURE, allow_redirects=False)
    body = r.text
    check("hide referrer serves meta refresh",
          r.status_code == 200 and 'name="referrer" content="no-referrer"' in body
          and "smoke-a" in body, f"got {r.status_code}")

    print("== Regression: direct flow ==")
    r = s.post(f"{api}/offers/", json={"name": f"Smoke Offer {os.getpid()}",
                                       "url": "https://example.com/smoke-offer?cid={click_id}"})
    check("create offer", r.status_code == 200 and "id" in r.json(), r.text[:200])
    offer_id = r.json().get("id")

    direct_alias = f"smoke-direct-{os.getpid()}"
    direct_payload = {
        "name": "Smoke Direct Campaign", "alias": direct_alias, "type": "campaign",
        "status": "active", "redirect_mode": "position",
        "config": {"flows": [{
            "type": "default", "position": 1, "enabled": True, "schema": "direct",
            "offer": offer_id, "filters": [],
        }], "postbacks": [], "fallback_url": "", "hide_referrer": False},
    }
    r = s.post(f"{api}/campaigns/", json=direct_payload)
    check("create direct campaign", r.status_code == 200 and "id" in r.json(), r.text[:200])
    direct_id = r.json().get("id")

    r = requests.get(f"{BASE}/{direct_alias}", verify=not INSECURE, allow_redirects=False)
    loc = r.headers.get("location") or ""
    check("direct flow redirects to a real URL (no <coroutine>)",
          r.status_code in (301, 302, 307, 308) and "<coroutine" not in loc,
          f"got {r.status_code} -> {loc[:120]}")
    click_match = re.search(r"cid=([^&]+)", loc)
    check("direct redirect carries click_id", bool(click_match), loc[:120])

    print("== Regression: HEAD on alias ==")
    r = requests.head(f"{BASE}/{direct_alias}", verify=not INSECURE, allow_redirects=False)
    check("HEAD matches GET status (307, not 404)", r.status_code == 307, str(r.status_code))

    print("== Regression: postback upsert ==")
    pb_click = f"smoke-pb-{os.getpid()}"
    r = requests.get(f"{BASE}/pb/{pb_click}/sale/1.25", verify=not INSECURE)
    check("postback for unknown click upserts (no 404)",
          r.status_code == 200 and r.json().get("status") == "ok", r.text[:150])
    r = s.get(f"{api}/reports/", params={"click_id": pb_click})
    rec = [c for c in r.json() if c["click_id"] == pb_click] if r.status_code == 200 else []
    check("upserted conversion recorded", bool(rec), r.text[:150])
    conv_id = rec[0]["id"] if rec else None

    r = requests.get(f"{BASE}/pb/{pb_click}/sale/1.25", verify=not INSECURE)
    check("identical postback within 60s flagged duplicate",
          r.status_code == 200 and r.json().get("duplicate") is True, r.text[:150])

    print("== Regression: click-out landing_id ==")
    r = requests.get(f"{BASE}/c/{direct_alias}/{offer_id}?l_id=98765",
                     verify=not INSECURE, allow_redirects=False)
    loc = r.headers.get("location") or ""
    click_match = re.search(r"click_id=([^&]+)", loc)
    check("click-out redirects with click_id", r.status_code in (301, 302, 307, 308)
          and bool(click_match), f"got {r.status_code} -> {loc[:120]}")
    if click_match:
        co_click = click_match.group(1)
        rec = []
        for _ in range(10):
            r = s.get(f"{api}/reports/", params={"click_id": co_click})
            rec = [c for c in r.json() if c["click_id"] == co_click] if r.status_code == 200 else []
            if rec:
                break
            import time
            time.sleep(0.5)
        check("click-out stores l_id as landing_id",
              bool(rec) and rec[0]["landing_id"] == 98765, r.text[:200])
        if rec:
            r = s.delete(f"{api}/reports/{rec[0]['id']}")
            check("delete click-out conversion", r.status_code == 200, r.text[:120])

    print("== Reports ==")
    r = s.get(f"{api}/reports/?limit=5")
    check("conversions endpoint", r.status_code == 200, r.text[:120])

    print("== Cleanup ==")
    if conv_id:
        r = s.delete(f"{api}/reports/{conv_id}")
        check("delete upserted conversion", r.status_code == 200, r.text[:120])
    if direct_id:
        r = s.delete(f"{api}/campaigns/{direct_id}")
        check("delete direct campaign", r.status_code == 200, r.text[:120])
    if offer_id:
        r = s.delete(f"{api}/offers/{offer_id}")
        check("delete offer", r.status_code == 200, r.text[:120])
    if clone_id:
        r = s.delete(f"{api}/campaigns/{clone_id}")
        check("delete clone", r.status_code == 200, r.text[:120])
    if cid:
        r = s.delete(f"{api}/campaigns/{cid}")
        check("delete campaign", r.status_code == 200, r.text[:120])
    # the clone used an -copy alias; make sure no smoke aliases remain
    r = s.get(f"{api}/campaigns/")
    leftovers = [c for c in r.json() if c["alias"].startswith("smoke-")]
    check("no smoke campaigns left", not leftovers, str(leftovers))
    _ = existing

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
