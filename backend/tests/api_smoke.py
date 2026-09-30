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
import base64
import shutil
import subprocess
from datetime import datetime, timezone

import requests

try:
    import pyotp
except ImportError:
    pyotp = None

BASE = os.environ.get("TEST_BASE_URL", "http://localhost")
INSECURE = os.environ.get("TEST_INSECURE") == "1"
USER = os.environ.get("TEST_USER", "tracker_admin")
PASS = os.environ.get("TEST_PASS", "admin")
CH_CONTAINER = os.environ.get("TEST_CH_CONTAINER", "tracker_clickhouse")
CH_USER = os.environ.get("TEST_CH_USER", "user")
CH_PASS = os.environ.get("TEST_CH_PASS", "password_password_password")
PIXEL_GIF = base64.b64decode("R0lGODlhAQABAAAAACw=")

passed = failed = 0


def check(name, condition, extra=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name} {extra}")


def ch_query(sql):
    """Run a read-only ClickHouse query via docker exec (no HTTP exposure in tests)."""
    try:
        out = subprocess.run(
            ["docker", "exec", CH_CONTAINER, "clickhouse-client",
             "-u", CH_USER, "--password", CH_PASS, "-q", sql],
            capture_output=True, text=True, timeout=30)
        return out.stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"


def settle_settings_cache():
    """The frontend caches settings blocks for 30s (event-loop safety, no
    invalidation channel from the backend) — wait out the TTL so a just-saved
    setting is guaranteed visible to the tracking plane."""
    import time
    time.sleep(31)


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
        # pid-suffixed so parallel suite runs never collide on the unique name
        "name": f"Smoke Test Campaign {os.getpid()}", "alias": alias, "type": "campaign",
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
        "name": f"Smoke Direct Campaign {os.getpid()}", "alias": direct_alias, "type": "campaign",
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
    r = requests.get(f"{BASE}/pb?clickid={pb_click}&status=sale&payout=1.25", verify=not INSECURE)
    check("postback for unknown click upserts (no 404)",
          r.status_code == 200 and r.json().get("status") == "ok", r.text[:150])
    r = s.get(f"{api}/reports/", params={"click_id": pb_click})
    rec = [c for c in r.json() if c["click_id"] == pb_click] if r.status_code == 200 else []
    check("upserted conversion recorded", bool(rec), r.text[:150])
    conv_id = rec[0]["id"] if rec else None

    r = requests.get(f"{BASE}/pb?clickid={pb_click}&status=sale&payout=1.25", verify=not INSECURE)
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

    print("== Regression: direct tracking ==")
    # -- G15: /t.js client --
    r = requests.get(f"{BASE}/t.js", verify=not INSECURE)
    check("t.js serves JS (200, javascript content-type)",
          r.status_code == 200 and "javascript" in r.headers.get("content-type", ""),
          f"got {r.status_code} {r.headers.get('content-type')}")
    check("t.js is cacheable (max-age=3600)",
          "max-age=3600" in r.headers.get("cache-control", ""), r.headers.get("cache-control", ""))
    check("t.js exposes the aaaTrack client", "aaaTrack" in r.text)

    # -- G15: /t/collect records a visit (click=false) and returns the click id --
    dt_sess = requests.Session()
    dt_sess.verify = not INSECURE
    r = dt_sess.post(f"{BASE}/t/collect", json={
        "c": str(direct_id), "url": "https://landing.example/direct",
        "referrer": "https://google.com/", "title": "Smoke Direct LP",
        "utm_source": "smoke"})
    check("collect returns a click_id", r.status_code == 200 and bool(r.json().get("click_id")),
          r.text[:150])
    dt_click = r.json().get("click_id") if r.status_code == 200 else None
    check("collect sets the aaa_cid first-party cookie", "aaa_cid" in dt_sess.cookies,
          str(dt_sess.cookies))

    ch_row = ch_query(
        f"SELECT click, visitor_id, url FROM clicks_data "
        f"WHERE visitor_id = '{dt_click}' ORDER BY received_at DESC LIMIT 1") if dt_click else ""
    check("collect CH row is a visit (click=false)",
          bool(ch_row) and not ch_row.startswith("ERROR") and ch_row.split("\t")[0] == "false",
          ch_row[:150])
    check("collect CH row carries page context",
          bool(ch_row) and "https://landing.example/direct" in ch_row, ch_row[:150])

    # GET fallback (noscript <img>)
    r = dt_sess.get(f"{BASE}/t/collect",
                    params={"c": direct_alias, "url": "https://landing.example/gif-fallback"})
    check("collect GET fallback returns click_id",
          r.status_code == 200 and bool(r.json().get("click_id")), r.text[:150])
    gif_click = r.json().get("click_id")

    r = requests.get(f"{BASE}/t/collect", params={"c": f"smoke-nope-{os.getpid()}"},
                     verify=not INSECURE)
    check("collect unknown campaign -> 404", r.status_code == 404, str(r.status_code))
    r = requests.post(f"{BASE}/t/collect", json={}, verify=not INSECURE)
    check("collect missing campaign -> 400", r.status_code == 400, str(r.status_code))

    # -- G15 click-through: /c reuses the visitor's click id (clean funnels) --
    r = requests.get(f"{BASE}/c/{direct_alias}/{offer_id}", params={"click_id": dt_click},
                     verify=not INSECURE, allow_redirects=False)
    loc = r.headers.get("location") or ""
    check("click-out reuses the JS click_id", r.status_code in (301, 302, 307, 308)
          and f"cid={dt_click}" in loc, f"got {r.status_code} -> {loc[:120]}")

    # -- G16: conversion pixel fires from the aaa_cid cookie; GIF for <img> --
    r = dt_sess.get(f"{BASE}/p/{direct_alias}", params={"status": "sale", "payout": "3.25"})
    check("pixel fires (200 image/gif)", r.status_code == 200
          and r.headers.get("content-type", "").startswith("image/gif"), r.text[:100])
    check("pixel returns 1x1 transparent GIF bytes", r.content == PIXEL_GIF, repr(r.content[:20]))
    check("pixel has permissive CORS header",
          r.headers.get("access-control-allow-origin") == "*", r.headers.get("access-control-allow-origin", ""))

    rec = []
    for _ in range(10):
        r = s.get(f"{api}/reports/", params={"click_id": dt_click})
        rec = [c for c in r.json() if c["click_id"] == dt_click] if r.status_code == 200 else []
        if rec:
            break
        import time
        time.sleep(0.5)
    check("pixel upserted the conversion (one row, sale)",
          len(rec) == 1 and rec[0]["status"] == "sale" and abs(float(rec[0]["payout"]) - 3.25) < 0.001,
          r.text[:200])
    dt_conv_id = rec[0]["id"] if rec else None

    # immediate refire -> duplicate (60s window), also proves /pb dedupes against the pixel
    r = dt_sess.get(f"{BASE}/p/{direct_alias}",
                    params={"status": "sale", "payout": "3.25", "fmt": "json"})
    check("pixel refire within 60s flagged duplicate",
          r.status_code == 200 and r.json().get("duplicate") is True, r.text[:150])
    r = requests.get(f"{BASE}/pb?clickid={dt_click}&status=sale&payout=3.25", verify=not INSECURE)
    check("postback against pixel-fired conversion dedupes",
          r.status_code == 200 and r.json().get("duplicate") is True, r.text[:150])

    # fmt=json fresh conversion + edge cases
    px_click = f"smoke-px-{os.getpid()}"
    r = requests.get(f"{BASE}/p/{direct_alias}",
                     params={"click_id": px_click, "status": "lead", "payout": "0", "fmt": "json"},
                     verify=not INSECURE)
    check("pixel fmt=json works", r.status_code == 200 and r.json().get("status") == "ok",
          r.text[:150])
    px_rec = []
    for _ in range(10):
        r = s.get(f"{api}/reports/", params={"click_id": px_click})
        px_rec = [c for c in r.json() if c["click_id"] == px_click] if r.status_code == 200 else []
        if px_rec:
            break
        import time
        time.sleep(0.5)
    px_conv_id = px_rec[0]["id"] if px_rec else None

    r = requests.get(f"{BASE}/p/smoke-no-such-alias-{os.getpid()}", params={"click_id": "x"},
                     verify=not INSECURE)
    check("pixel unknown alias -> 404", r.status_code == 404, str(r.status_code))
    r = requests.get(f"{BASE}/p/{direct_alias}", params={"fmt": "json"}, verify=not INSECURE)
    check("pixel missing click_id and no cookie -> 400",
          r.status_code == 400 and "click_id" in r.text, f"{r.status_code} {r.text[:120]}")
    r = requests.get(f"{BASE}/p/{direct_alias}",
                     params={"click_id": "x", "status": "bogus", "fmt": "json"}, verify=not INSECURE)
    check("pixel bogus status -> 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    r = requests.get(f"{BASE}/p/{direct_alias}",
                     params={"click_id": "x", "payout": "abc", "fmt": "json"}, verify=not INSECURE)
    check("pixel bogus payout -> 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")

    print("== Regression: direct tracking cleanup ==")
    for conv in (dt_conv_id, px_conv_id):
        if conv:
            r = s.delete(f"{api}/reports/{conv}")
            check(f"delete direct-tracking conversion {conv}", r.status_code == 200, r.text[:120])
    for visitor in (dt_click, gif_click):
        if visitor:
            ch_query(f"ALTER TABLE clicks_data DELETE WHERE visitor_id = '{visitor}'")

    print("== Regression: routing rules ==")
    # Country is derived from the Accept-Language header by enrich_meta
    h_us = {"Accept-Language": "en-US"}
    h_de = {"Accept-Language": "de-DE"}
    ua_sticky = {"User-Agent": f"SmokeStickiness/{os.getpid()} Chrome/120"}

    r = s.post(f"{api}/offers/", json={"name": f"Smoke Offer A {os.getpid()}",
                                       "url": "https://example.com/offer-a?cid={click_id}"})
    check("create offer A", r.status_code == 200 and "id" in r.json(), r.text[:200])
    offer_a = r.json().get("id")
    r = s.post(f"{api}/offers/", json={"name": f"Smoke Offer B {os.getpid()}",
                                       "url": "https://example.com/offer-b?cid={click_id}"})
    check("create offer B", r.status_code == 200 and "id" in r.json(), r.text[:200])
    offer_b = r.json().get("id")

    def rules_flow(name, position, offer, filters=None, **extra):
        flow = {"type": "regular", "name": name, "position": position, "enabled": True,
                "schema": "direct", "offer": offer, "filters": filters or []}
        flow.update(extra)
        return flow

    def rules_payload(alias, flows, redirect_mode="position", **cfg_extra):
        config = {"flows": flows, "postbacks": [], "hide_referrer": False,
                  "fallback_url": f"https://example.com/smoke-{os.getpid()}-fb"}
        config.update(cfg_extra)
        return {"name": alias, "alias": alias, "type": "campaign", "status": "active",
                "redirect_mode": redirect_mode, "config": config}

    rules_ids = []

    # -- G1: group filters — country equals US routes flow A, else flow B --
    us_filter = {"combinator": "and", "groups": [
        {"logic": "and", "conditions": [{"field": "country", "operator": "equals", "value": "US"}]}]}
    payload = rules_payload(f"smoke-rules-{os.getpid()}",
                            [rules_flow("US flow", 1, offer_a, us_filter),
                             rules_flow("Fallback flow", 2, offer_b)])
    r = s.post(f"{api}/campaigns/", json=payload)
    check("create rules campaign", r.status_code == 200 and "id" in r.json(), r.text[:200])
    rules_id = r.json().get("id")
    rules_ids.append(rules_id)
    rules_alias = payload["alias"]

    loc = requests.get(f"{BASE}/{rules_alias}", headers=h_us, verify=not INSECURE,
                       allow_redirects=False).headers.get("location") or ""
    check("group filter: US click routes flow A", "offer-a" in loc, loc[:120])
    loc = requests.get(f"{BASE}/{rules_alias}", headers=h_de, verify=not INSECURE,
                       allow_redirects=False).headers.get("location") or ""
    check("group filter: DE click routes flow B", "offer-b" in loc, loc[:120])

    # -- G1: OR group + invert --
    or_filter = {"combinator": "and", "groups": [
        {"logic": "or", "conditions": [
            {"field": "country", "operator": "equals", "value": "US"},
            {"field": "keyword", "operator": "contains", "value": "deal"}]}]}
    payload["config"]["flows"][0]["filters"] = or_filter
    r = s.put(f"{api}/campaigns/{rules_id}", json=payload)
    check("update campaign with OR group", r.status_code == 200, r.text[:150])
    loc = requests.get(f"{BASE}/{rules_alias}", headers=h_de, verify=not INSECURE,
                       allow_redirects=False, params={"keyword": "bigdeal"}).headers.get("location") or ""
    check("OR group: keyword match routes flow A", "offer-a" in loc, loc[:120])
    loc = requests.get(f"{BASE}/{rules_alias}", headers=h_de, verify=not INSECURE,
                       allow_redirects=False).headers.get("location") or ""
    check("OR group: no match routes flow B", "offer-b" in loc, loc[:120])

    not_us_filter = {"combinator": "and", "groups": [
        {"logic": "and", "conditions": [
            {"field": "country", "operator": "equals", "value": "US", "invert": True}]}]}
    payload["config"]["flows"][0]["filters"] = not_us_filter
    s.put(f"{api}/campaigns/{rules_id}", json=payload)
    loc = requests.get(f"{BASE}/{rules_alias}", headers=h_us, verify=not INSECURE,
                       allow_redirects=False).headers.get("location") or ""
    check("invert: US click excluded from flow A", "offer-b" in loc, loc[:120])
    loc = requests.get(f"{BASE}/{rules_alias}", headers=h_de, verify=not INSECURE,
                       allow_redirects=False).headers.get("location") or ""
    check("invert: DE click matches flow A", "offer-a" in loc, loc[:120])

    # -- G1: legacy flat filter format still works (positive match) --
    payload["config"]["flows"][0]["filters"] = [
        {"key": "country", "operator": "equals", "value": "US", "condition": ""}]
    s.put(f"{api}/campaigns/{rules_id}", json=payload)
    loc = requests.get(f"{BASE}/{rules_alias}", headers=h_us, verify=not INSECURE,
                       allow_redirects=False).headers.get("location") or ""
    check("legacy filter: US click routes flow A", "offer-a" in loc, loc[:120])
    loc = requests.get(f"{BASE}/{rules_alias}", headers=h_de, verify=not INSECURE,
                       allow_redirects=False).headers.get("location") or ""
    check("legacy filter: DE click routes flow B", "offer-b" in loc, loc[:120])

    # -- G3+G9: stickiness — weighted A/B split stable across visits --
    sticky_alias = f"smoke-sticky-{os.getpid()}"
    payload = rules_payload(sticky_alias,
                            [rules_flow("A", 1, offer_a, weight=50),
                             rules_flow("B", 3, offer_b, weight=50)],
                            redirect_mode="weight", stickiness=True)
    r = s.post(f"{api}/campaigns/", json=payload)
    check("create sticky campaign", r.status_code == 200 and "id" in r.json(), r.text[:200])
    sticky_id = r.json().get("id")
    rules_ids.append(sticky_id)

    sess = requests.Session()
    sess.verify = not INSECURE
    loc1 = sess.get(f"{BASE}/{sticky_alias}", headers=ua_sticky,
                    allow_redirects=False).headers.get("location") or ""
    check("sticky: first visit lands on an offer",
          "offer-a" in loc1 or "offer-b" in loc1, loc1[:120])
    check("sticky: aaa_bind cookie set", "aaa_bind" in sess.cookies, str(sess.cookies))
    loc2 = sess.get(f"{BASE}/{sticky_alias}", headers=ua_sticky,
                    allow_redirects=False).headers.get("location") or ""
    check("sticky: repeat visit keeps same offer",
          loc1.split("?")[0] == loc2.split("?")[0], f"{loc1[:80]} vs {loc2[:80]}")

    # config-hash invalidation: edit weights → old binding must not win
    payload["config"]["flows"][0]["weight"] = 100
    payload["config"]["flows"][1]["weight"] = 0
    s.put(f"{api}/campaigns/{sticky_id}", json=payload)
    sess2 = requests.Session()
    sess2.verify = not INSECURE
    loc3 = sess2.get(f"{BASE}/{sticky_alias}", headers=ua_sticky,
                     allow_redirects=False).headers.get("location") or ""
    check("sticky: 100/0 weight routes offer A", "offer-a" in loc3, loc3[:120])
    # config-hash invalidation: a real routing change (offer swap) must
    # invalidate old bindings — weight-only edits intentionally do not
    # (they were resetting every bound visitor, changelog-parity fix)
    payload["config"]["flows"][0]["offer"] = offer_b
    payload["config"]["flows"][1]["offer"] = offer_a
    s.put(f"{api}/campaigns/{sticky_id}", json=payload)
    loc4 = sess2.get(f"{BASE}/{sticky_alias}", headers=ua_sticky,
                     allow_redirects=False).headers.get("location") or ""
    check("sticky: routing edit invalidates binding (routes offer B)",
          "offer-b" in loc4, loc4[:120])

    # -- G8: click caps — total cap 1, second click skips the flow --
    caps_alias = f"smoke-caps-{os.getpid()}"
    payload = rules_payload(caps_alias,
                            [rules_flow("Capped", 1, offer_a, caps={"total": 1}),
                             rules_flow("Fallback", 2, offer_b)])
    r = s.post(f"{api}/campaigns/", json=payload)
    check("create caps campaign", r.status_code == 200 and "id" in r.json(), r.text[:200])
    rules_ids.append(r.json().get("id"))

    loc = requests.get(f"{BASE}/{caps_alias}", verify=not INSECURE,
                       allow_redirects=False).headers.get("location") or ""
    check("cap: first click passes", "offer-a" in loc, loc[:120])
    loc = requests.get(f"{BASE}/{caps_alias}", verify=not INSECURE,
                       allow_redirects=False).headers.get("location") or ""
    check("cap: second click skips to next flow", "offer-b" in loc, loc[:120])

    # -- G7: schedule — flow only open in a window that never contains now --
    hour_utc = datetime.now(timezone.utc).hour
    sched_alias = f"smoke-sched-{os.getpid()}"
    payload = rules_payload(sched_alias,
                            [rules_flow("Scheduled", 1, offer_a,
                                        schedule={"timezone": "UTC",
                                                  "hours": {"from": (hour_utc + 1) % 24,
                                                            "to": (hour_utc + 2) % 24}}),
                             rules_flow("Fallback", 2, offer_b)])
    r = s.post(f"{api}/campaigns/", json=payload)
    check("create schedule campaign", r.status_code == 200 and "id" in r.json(), r.text[:200])
    rules_ids.append(r.json().get("id"))

    loc = requests.get(f"{BASE}/{sched_alias}", verify=not INSECURE,
                       allow_redirects=False).headers.get("location") or ""
    check("schedule: closed window skips to next flow", "offer-b" in loc, loc[:120])
    payload["config"]["flows"][0]["schedule"] = {
        "timezone": "UTC", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "hours": {"from": 0, "to": 23}}
    s.put(f"{api}/campaigns/{rules_ids[-1]}", json=payload)
    loc = requests.get(f"{BASE}/{sched_alias}", verify=not INSECURE,
                       allow_redirects=False).headers.get("location") or ""
    check("schedule: always-open window routes flow A", "offer-a" in loc, loc[:120])

    print("== Regression: routing rules cleanup ==")
    for rid in rules_ids:
        r = s.delete(f"{api}/campaigns/{rid}")
        check(f"delete routing campaign {rid}", r.status_code == 200, r.text[:120])
    for oid in (offer_a, offer_b):
        r = s.delete(f"{api}/offers/{oid}")
        check(f"delete routing offer {oid}", r.status_code == 200, r.text[:120])

    print("== Regression: conversion economics ==")
    econ_conv_ids = []
    econ_offer_ids = []
    econ_campaign_ids = []

    def poll_first(params):
        for _ in range(10):
            r = s.get(f"{api}/reports/", params=params)
            if r.status_code == 200 and r.json():
                return r.json()[0]
            import time
            time.sleep(0.5)
        return None

    # -- G23: LTV / rebill accumulation --
    ltv_click = f"smoke-ltv-{os.getpid()}"
    r = requests.get(f"{BASE}/pb?clickid={ltv_click}&status=sale&payout=5", verify=not INSECURE)
    check("LTV: first sale postback ok", r.status_code == 200 and r.json().get("duplicate") is False,
          r.text[:150])
    r = requests.get(f"{BASE}/pb?clickid={ltv_click}&status=sale&payout=5", verify=not INSECURE)
    check("LTV: identical refire within 60s dedupes", r.status_code == 200
          and r.json().get("duplicate") is True, r.text[:150])
    r = requests.get(f"{BASE}/pb?clickid={ltv_click}&status=sale&payout=5",
                     params={"transaction_id": f"ltv-tid-{os.getpid()}"}, verify=not INSECURE)
    check("LTV: repeat sale with unique transaction id accumulates", r.status_code == 200
          and r.json().get("duplicate") is False, r.text[:150])
    rec = poll_first({"click_id": ltv_click})
    check("LTV: revenue accumulated to 10", rec and abs(float(rec["revenue"] or 0) - 10) < 0.001,
          str(rec and rec.get("revenue")))
    check("LTV: payout accumulated to 10", rec and abs(float(rec["payout"] or 0) - 10) < 0.001,
          str(rec and rec.get("payout")))
    check("LTV: events history has 2 entries", rec and len(rec.get("events") or []) == 2,
          str(rec and rec.get("events")))
    check("LTV: postback_count is 3 (deduped refire still counted)", rec and rec["postback_count"] == 3,
          str(rec and rec.get("postback_count")))
    if rec:
        econ_conv_ids.append(rec["id"])

    # -- G19: custom conversion statuses --
    r = s.get(f"{api}/settings/")
    settings_all = r.json() if r.status_code == 200 else {}
    cfg = dict(settings_all.get("settings") or {})
    saved_custom = list(cfg.get("custom_statuses") or [])
    cfg["custom_statuses"] = saved_custom + [{"name": "ReGistration Bonus"}]
    r = s.post(f"{api}/settings/", json={"settings": cfg})
    check("custom status saved via settings API", r.status_code == 200, r.text[:150])
    settle_settings_cache()  # frontend settings cache (30s TTL)
    cs_click = f"smoke-cs-{os.getpid()}"
    r = requests.get(f"{BASE}/pb?clickid={cs_click}&status=Registration%20Bonus&payout=2.5", verify=not INSECURE)
    check("postback accepts custom status (weird casing/spaces)", r.status_code == 200
          and r.json().get("status") == "ok", r.text[:150])
    rec = poll_first({"click_id": cs_click})
    check("custom status normalized and stored", rec and rec["status"] == "registration_bonus",
          str(rec and rec.get("status")))
    if rec:
        econ_conv_ids.append(rec["id"])
    r = requests.get(f"{BASE}/pb?clickid={cs_click}&status=definitely_not_a_status&payout=1", verify=not INSECURE)
    check("unknown status still rejected", r.status_code == 400, f"{r.status_code} {r.text[:80]}")
    r = s.get(f"{api}/reports/", params={"status": "registration_bonus"})
    check("reports filters by custom status", r.status_code == 200
          and any(c["click_id"] == cs_click for c in r.json()), r.text[:150])

    # -- G20: clickless conversions --
    cl_tid = f"smoke-clickless-{os.getpid()}"
    r = requests.get(f"{BASE}/pb?clickid=none&status=sale&payout=2", params={"transaction_id": cl_tid}, verify=not INSECURE)
    check("clickless postback accepted", r.status_code == 200 and r.json().get("clickless") is True
          and r.json().get("attributed") is False, r.text[:150])
    rec = poll_first({"search": cl_tid})
    check("clickless row stored as 'none'", rec and rec["click_id"] == "none"
          and abs(float(rec["payout"] or 0) - 2) < 0.001, str(rec))
    if rec:
        econ_conv_ids.append(rec["id"])

    # sub_id attribution fallback: a click-out carrying a unique sub_id, then a
    # clickless postback with that sub_id attaches to the existing click
    sub_token = f"smoketok{os.getpid()}"
    r = requests.get(f"{BASE}/c/{direct_alias}/{offer_id}", params={"sub_id_1": sub_token},
                     verify=not INSECURE, allow_redirects=False)
    sub_match = re.search(r"click_id=([^&]+)", r.headers.get("location") or "")
    check("sub_id click-out recorded", r.status_code in (301, 302, 307, 308) and bool(sub_match),
          f"got {r.status_code}")
    r = requests.get(f"{BASE}/pb?clickid=none&status=sale&payout=4", params={"sub_id_1": sub_token}, verify=not INSECURE)
    check("clickless postback attributes via sub_id", r.status_code == 200
          and r.json().get("attributed") is True, r.text[:150])
    rec = poll_first({"sub_id_1": sub_token})
    check("attributed to the real click with accumulated payout", rec and rec["status"] == "sale"
          and abs(float(rec["payout"] or 0) - 4) < 0.001
          and rec["click_id"] == (sub_match.group(1) if sub_match else None),
          str(rec and rec.get("click_id")))
    if rec:
        econ_conv_ids.append(rec["id"])

    # -- G4: offer daily conversion cap + overflow --
    r = s.post(f"{api}/offers/", json={"name": f"Smoke CapOv {os.getpid()}",
                                       "url": "https://example.com/cap-ov?cid={click_id}"})
    check("create overflow offer", r.status_code == 200 and "id" in r.json(), r.text[:150])
    cap_ov = r.json().get("id")
    econ_offer_ids.append(cap_ov)
    r = s.post(f"{api}/offers/", json={"name": f"Smoke CapA {os.getpid()}",
                                       "url": "https://example.com/cap-a?cid={click_id}",
                                       "daily_conversions_cap": 1, "overflow_offer_id": cap_ov})
    check("create capped offer with overflow", r.status_code == 200 and "id" in r.json(), r.text[:150])
    cap_a = r.json().get("id")
    econ_offer_ids.append(cap_a)
    r = s.get(f"{api}/offers/")
    off = [o for o in r.json() if o["id"] == cap_a]
    check("offer returns cap + overflow fields", bool(off) and off[0].get("daily_conversions_cap") == 1
          and off[0].get("overflow_offer_id") == cap_ov, r.text[:200])

    cap_alias = f"smoke-ocap-{os.getpid()}"
    cap_payload = {"name": cap_alias, "alias": cap_alias, "type": "campaign", "status": "active",
                   "redirect_mode": "position",
                   "config": {"flows": [{"type": "default", "position": 1, "enabled": True,
                                         "schema": "direct", "offer": cap_a, "filters": []}],
                              "postbacks": [], "hide_referrer": False,
                              "fallback_url": f"https://example.com/smoke-{os.getpid()}-ocap-fb"}}
    r = s.post(f"{api}/campaigns/", json=cap_payload)
    check("create offer-cap campaign", r.status_code == 200 and "id" in r.json(), r.text[:200])
    econ_campaign_ids.append(r.json().get("id"))

    loc = requests.get(f"{BASE}/{cap_alias}", verify=not INSECURE,
                       allow_redirects=False).headers.get("location") or ""
    check("offer cap: first click routes to capped offer", "cap-a" in loc, loc[:120])

    # confirm one conversion for the capped offer (click-out + postback)
    r = requests.get(f"{BASE}/c/{cap_alias}/{cap_a}", verify=not INSECURE, allow_redirects=False)
    cap_match = re.search(r"click_id=([^&]+)", r.headers.get("location") or "")
    check("offer cap: click-out for capped offer", bool(cap_match), r.headers.get("location", ""))
    if cap_match:
        requests.get(f"{BASE}/pb?clickid={cap_match.group(1)}&status=sale&payout=5", verify=not INSECURE)
        rec = poll_first({"click_id": cap_match.group(1)})
        check("offer cap: confirmed conversion recorded", bool(rec) and rec["status"] == "sale"
              and rec["offer_id"] == cap_a, str(rec))
        if rec:
            econ_conv_ids.append(rec["id"])
    loc = requests.get(f"{BASE}/{cap_alias}", verify=not INSECURE,
                       allow_redirects=False).headers.get("location") or ""
    check("offer cap: second click overflows to overflow offer", "cap-ov" in loc, loc[:120])

    # capped offer without overflow → flow ineligible → campaign fallback
    r = s.post(f"{api}/offers/", json={"name": f"Smoke CapB {os.getpid()}",
                                       "url": "https://example.com/cap-b?cid={click_id}",
                                       "daily_conversions_cap": 1})
    check("create capped offer without overflow", r.status_code == 200 and "id" in r.json(), r.text[:150])
    cap_b = r.json().get("id")
    econ_offer_ids.append(cap_b)
    capb_alias = f"smoke-ocapb-{os.getpid()}"
    capb_payload = dict(cap_payload, name=capb_alias, alias=capb_alias)
    capb_payload["config"] = json.loads(json.dumps(cap_payload["config"]))
    capb_payload["config"]["flows"][0]["offer"] = cap_b
    r = s.post(f"{api}/campaigns/", json=capb_payload)
    check("create no-overflow cap campaign", r.status_code == 200 and "id" in r.json(), r.text[:200])
    econ_campaign_ids.append(r.json().get("id"))
    requests.get(f"{BASE}/{capb_alias}", verify=not INSECURE, allow_redirects=False)
    r = requests.get(f"{BASE}/c/{capb_alias}/{cap_b}", verify=not INSECURE, allow_redirects=False)
    capb_match = re.search(r"click_id=([^&]+)", r.headers.get("location") or "")
    if capb_match:
        requests.get(f"{BASE}/pb?clickid={capb_match.group(1)}&status=sale&payout=5", verify=not INSECURE)
        rec = poll_first({"click_id": capb_match.group(1)})
        if rec:
            econ_conv_ids.append(rec["id"])
    loc = requests.get(f"{BASE}/{capb_alias}", verify=not INSECURE,
                       allow_redirects=False).headers.get("location") or ""
    check("offer cap: no overflow → fallback URL", f"smoke-{os.getpid()}-ocap-fb" in loc, loc[:120])

    # -- G25: manual conversion import --
    imp_missing = f"smoke-imp-missing-{os.getpid()}"
    lines = "\n".join([
        f"{ltv_click},7.5,imp-1-{os.getpid()},sale",
        f"{imp_missing},3.0,imp-2-{os.getpid()},lead",
        f"{cs_click},1.0,imp-4-{os.getpid()},registration_bonus",
        f"{ltv_click},5.0,imp-3-{os.getpid()},not_a_status",
        "onlyonecolumn",
    ])
    r = s.post(f"{api}/reports/import", json={"lines": lines})
    check("import endpoint processes all lines", r.status_code == 200, r.text[:200])
    res = (r.json().get("results") or []) if r.status_code == 200 else []
    by_line = {x["line"]: x for x in res}
    check("import: existing click accumulates", by_line.get(1, {}).get("ok") is True, str(res))
    check("import: unknown click creates unattributed row", by_line.get(2, {}).get("ok") is True,
          str(res))
    check("import: custom status accepted", by_line.get(3, {}).get("ok") is True, str(res))
    check("import: invalid status reported", by_line.get(4, {}).get("ok") is False, str(res))
    check("import: malformed line reported", by_line.get(5, {}).get("ok") is False, str(res))
    body = r.json() if r.status_code == 200 else {}
    check("import summary counts", body.get("imported") == 3 and body.get("failed") == 2, str(body))
    rec = poll_first({"click_id": ltv_click})
    check("import: LTV row accumulated to 17.5 with 3 events", rec
          and abs(float(rec["revenue"] or 0) - 17.5) < 0.001
          and len(rec.get("events") or []) == 3, str(rec and (rec.get("revenue"), rec.get("events"))))
    rec = poll_first({"search": f"imp-2-{os.getpid()}"})
    check("import: unattributed row stored as 'none'", rec and rec["click_id"] == "none"
          and rec["status"] == "lead" and abs(float(rec["payout"] or 0) - 3) < 0.001, str(rec))
    if rec:
        econ_conv_ids.append(rec["id"])

    print("== Regression: conversion economics cleanup ==")
    cfg["custom_statuses"] = saved_custom
    r = s.post(f"{api}/settings/", json={"settings": cfg})
    check("restore settings custom statuses", r.status_code == 200, r.text[:120])
    for econ_cid in sorted(set(econ_conv_ids)):
        if econ_cid:
            r = s.delete(f"{api}/reports/{econ_cid}")
            check(f"delete economics conversion {econ_cid}", r.status_code == 200, r.text[:120])
    for cid_ in econ_campaign_ids:
        if cid_:
            r = s.delete(f"{api}/campaigns/{cid_}")
            check(f"delete economics campaign {cid_}", r.status_code == 200, r.text[:120])
    for oid_ in econ_offer_ids:
        if oid_:
            r = s.delete(f"{api}/offers/{oid_}")
            check(f"delete economics offer {oid_}", r.status_code == 200, r.text[:120])

    print("== Regression: tracking plane extras ==")
    extra_offer_ids = []
    extra_campaign_ids = []

    r = s.post(f"{api}/offers/", json={"name": f"Smoke Extra A {os.getpid()}",
                                       "url": "https://example.com/extra-a?cid={click_id}"})
    check("extras: create offer A", r.status_code == 200 and "id" in r.json(), r.text[:200])
    offer_xa = r.json().get("id")
    extra_offer_ids.append(offer_xa)
    r = s.post(f"{api}/offers/", json={"name": f"Smoke Extra B {os.getpid()}",
                                       "url": "https://example.com/extra-b?cid={click_id}"})
    check("extras: create offer B", r.status_code == 200 and "id" in r.json(), r.text[:200])
    offer_xb = r.json().get("id")
    extra_offer_ids.append(offer_xb)

    # Flow 1 carries an impossible group filter (country ZZ) so every visitor
    # must fall through to flow 2 — used by /simulate assertions below.
    zz_filter = {"combinator": "and", "groups": [
        {"logic": "and", "conditions": [
            {"field": "country", "operator": "equals", "value": "ZZ"}]}]}
    extra_alias = f"smoke-extra-{os.getpid()}"
    extra_payload = {"name": extra_alias, "alias": extra_alias, "type": "campaign",
                     "status": "active", "redirect_mode": "position",
                     "config": {"flows": [
                         {"type": "default", "position": 1, "enabled": True, "schema": "direct",
                          "offer": offer_xa, "filters": zz_filter},
                         {"type": "default", "position": 2, "enabled": True, "schema": "direct",
                          "offer": offer_xb, "filters": []}],
                         "postbacks": [], "hide_referrer": False,
                         "fallback_url": f"https://example.com/smoke-{os.getpid()}-extra-fb"}}
    r = s.post(f"{api}/campaigns/", json=extra_payload)
    check("extras: create campaign", r.status_code == 200 and "id" in r.json(), r.text[:200])
    extra_id = r.json().get("id")
    extra_campaign_ids.append(extra_id)

    # -- G17: impression tracking —
    imp_sess = requests.Session()
    imp_sess.verify = not INSECURE
    r = imp_sess.get(f"{BASE}/i/{extra_alias}", params={"utm_medium": "cpm",
                                                        "sub_id_1": f"smoke-imp-{os.getpid()}"})
    check("impression returns 1x1 GIF bytes", r.status_code == 200 and r.content == PIXEL_GIF,
          f"{r.status_code} {r.headers.get('content-type')}")
    check("impression content-type is image/gif",
          r.headers.get("content-type", "").startswith("image/gif"),
          r.headers.get("content-type", ""))
    check("impression sets the aaa_cid cookie", "aaa_cid" in imp_sess.cookies, str(imp_sess.cookies))
    imp_cid = imp_sess.cookies.get("aaa_cid")
    imp_row = ch_query(
        f"SELECT impression, click, utm_medium FROM clicks_data "
        f"WHERE visitor_id = '{imp_cid}' ORDER BY received_at DESC LIMIT 1") if imp_cid else ""
    check("impression CH row (impression=1, click=false, utm_medium=cpm)",
          bool(imp_row) and not imp_row.startswith("ERROR")
          and imp_row.split("\t")[:3] == ["1", "false", "cpm"], imp_row[:150])

    r = requests.get(f"{BASE}/i/smoke-nope-{os.getpid()}", verify=not INSECURE)
    check("impression unknown alias -> 404", r.status_code == 404, str(r.status_code))

    bot_sess = requests.Session()
    bot_sess.verify = not INSECURE
    r = bot_sess.get(f"{BASE}/i/{extra_alias}",
                     headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"})
    bot_cid = bot_sess.cookies.get("aaa_cid")
    bot_row = ch_query(
        f"SELECT is_bot, impression FROM clicks_data WHERE visitor_id = '{bot_cid}' LIMIT 1") \
        if bot_cid else ""
    check("impression bot UA flagged is_bot=1", bool(bot_row) and not bot_row.startswith("ERROR")
          and bot_row.split("\t")[0] == "true", bot_row[:100])

    # -- G27: click API (server-side click processing) --
    r = requests.post(f"{BASE}/click-api/{extra_alias}", verify=not INSECURE, json={
        "ip": "203.0.113.7", "user_agent": "SmokeClickApi/1.0 Chrome/120",
        "referrer": "https://ads.example/", "sub_id_2": f"smoke-ca-{os.getpid()}"})
    ca_click = r.json().get("click_id") if r.status_code == 200 else None
    ca_dec = (r.json().get("decision") or {}) if r.status_code == 200 else {}
    check("click-api returns click_id + decision JSON", r.status_code == 200 and bool(ca_click)
          and ca_dec.get("schema") == "direct", r.text[:200])
    check("click-api decision URL carries the click_id",
          bool(ca_dec.get("url")) and "extra-b" in ca_dec["url"]
          and f"cid={ca_click}" in ca_dec["url"], str(ca_dec.get("url")))
    ca_row = ch_query(
        f"SELECT ip, sub_id_2 FROM clicks_data WHERE visitor_id = '{ca_click}' LIMIT 1") \
        if ca_click else ""
    check("click-api CH row stored with body IP and sub_id",
          bool(ca_row) and not ca_row.startswith("ERROR")
          and ca_row.split("\t")[0] == "203.0.113.7"
          and f"smoke-ca-{os.getpid()}" in ca_row, ca_row[:150])

    r = requests.post(f"{BASE}/click-api/{extra_alias}", verify=not INSECURE,
                      json={"ip": "not-an-ip", "user_agent": "x"})
    check("click-api invalid IP -> 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")
    r = requests.post(f"{BASE}/click-api/{extra_alias}", verify=not INSECURE,
                      json={"ip": "198.51.100.9"})
    check("click-api missing UA still works", r.status_code == 200
          and bool(r.json().get("click_id")), r.text[:120])
    r = requests.post(f"{BASE}/click-api/smoke-nope-{os.getpid()}", verify=not INSECURE,
                      json={"ip": "1.2.3.4"})
    check("click-api unknown alias -> 404", r.status_code == 404, str(r.status_code))

    # -- G74: traffic simulation (dry-run, zero side effects) --
    # admin-gated since the audit fixes: the authenticated session sends the
    # backend's session_token cookie through nginx to the frontend.
    sim_before = ch_query("SELECT count() FROM clicks_data")
    r = s.post(f"{BASE}/simulate/{extra_alias}", json={"count": 50, "seed": 42})
    sim = r.json().get("stats") if r.status_code == 200 else {}
    check("simulate routes 100% around the impossible filter",
          r.status_code == 200 and sim.get("flow_distribution") == {"1": 50}, r.text[:200])
    check("simulate counts filter rejections", sim.get("rejected_by_filters") == 50, str(sim))
    check("simulate reports the catch-all offer",
          sim.get("offer_distribution") == {str(offer_xb): 50}, str(sim.get("offer_distribution")))
    r2 = s.post(f"{BASE}/simulate/{extra_alias}", json={"count": 50, "seed": 42})
    check("simulate is deterministic for a fixed seed",
          r2.status_code == 200 and r2.json().get("stats") == r.json().get("stats"))
    sim_after = ch_query("SELECT count() FROM clicks_data")
    check("simulate wrote no ClickHouse rows", sim_before == sim_after,
          f"{sim_before} -> {sim_after}")
    r = s.post(f"{BASE}/simulate/{extra_alias}",
               json={"count": 40, "seed": 7, "profile": {"country": "ZZ"}})
    sim = r.json().get("stats") if r.status_code == 200 else {}
    check("simulate profile option (country=ZZ) routes through the matched filter",
          r.status_code == 200 and sim.get("flow_distribution") == {"0": 40}, r.text[:200])
    r = s.post(f"{BASE}/simulate/{extra_alias}", json={"count": 1001})
    check("simulate count > 1000 -> 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")
    r = s.post(f"{BASE}/simulate/smoke-nope-{os.getpid()}", json={"count": 1})
    check("simulate unknown alias -> 404", r.status_code == 404, str(r.status_code))

    # -- G79: GDPR opt-out + IP anonymization --
    opt_sess = requests.Session()
    opt_sess.verify = not INSECURE
    r = opt_sess.get(f"{BASE}/optout")
    check("optout page confirms and sets the aaa_optout cookie",
          r.status_code == 200 and "Opt-out confirmed" in r.text
          and "aaa_optout" in opt_sess.cookies, f"{r.status_code} {r.text[:80]}")
    opt_before = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {extra_id}")
    r = opt_sess.get(f"{BASE}/{extra_alias}", allow_redirects=False)
    opt_after = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {extra_id}")
    check("optout: campaign still redirects", r.status_code in (301, 302, 307, 308)
          and "extra-b" in (r.headers.get("location") or ""),
          f"{r.status_code} {r.headers.get('location', '')[:100]}")
    check("optout: no ClickHouse row written", opt_before == opt_after,
          f"{opt_before} -> {opt_after}")
    check("optout: no tracking cookies set on the response",
          "aaa_cid" not in (r.headers.get("set-cookie") or "")
          and "aaa_bind" not in (r.headers.get("set-cookie") or ""),
          (r.headers.get("set-cookie") or "")[:100])

    r = s.get(f"{api}/settings/")
    cfg = dict(r.json().get("settings") or {})
    saved_privacy = cfg.get("privacy")
    cfg["privacy"] = {"anonymize_ip": True}
    r = s.post(f"{api}/settings/", json={"settings": cfg})
    check("privacy: anonymize_ip saved via settings API", r.status_code == 200, r.text[:150])
    settle_settings_cache()  # frontend settings cache (30s TTL)
    # Through nginx the X-Forwarded-For header is rewritten to the real client
    # IP, so push the test IP directly to uvicorn (XFF is the trusted fallback
    # when no nginx X-Real-IP is present).
    import time
    subprocess.run(["docker", "exec", "tracker_frontend", "curl", "-s", "-o", "/dev/null",
                    "-H", "Host: localhost", "-H", "X-Forwarded-For: 203.0.113.55",
                    f"http://127.0.0.1:8000/{extra_alias}"], capture_output=True, timeout=30)
    time.sleep(1)
    masked = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {extra_id} "
                      f"AND ip = toIPv4('203.0.113.0')")
    check("anonymize_ip masks new rows (…55 → …0)", masked == "1", masked[:80])
    if saved_privacy is None:
        cfg["privacy"] = None  # null deletes the key under merge semantics
    else:
        cfg["privacy"] = saved_privacy
    r = s.post(f"{api}/settings/", json={"settings": cfg})
    check("privacy: setting restored", r.status_code == 200, r.text[:120])

    print("== Regression: tracking plane extras cleanup ==")
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE campaign_id = {extra_id}")
    leftover = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {extra_id}")
    check("extras CH rows removed", leftover == "0", leftover[:80])
    for xcid in extra_campaign_ids:
        if xcid:
            r = s.delete(f"{api}/campaigns/{xcid}")
            check(f"delete extras campaign {xcid}", r.status_code == 200, r.text[:120])
    for xoid in extra_offer_ids:
        if xoid:
            r = s.delete(f"{api}/offers/{xoid}")
            check(f"delete extras offer {xoid}", r.status_code == 200, r.text[:120])

    print("== Reports ==")
    r = s.get(f"{api}/reports/?limit=5")
    check("conversions endpoint", r.status_code == 200, r.text[:120])

    print("== Fraud ==")
    # Seed one flagged click (bot + fraud_score) and clean it up afterwards.
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, "
        f"country, browser, ip, cost, is_bot, fraud_score) "
        f"VALUES (now(), 1, true, '', 'smoke-fraud-{os.getpid()}', 'US', 'SmokeBot/1.0', "
        f"toIPv4('198.51.100.66'), 0.10, true, 80)")

    r = s.get(f"{api}/fraud/summary")
    body = r.json() if r.status_code == 200 else {}
    check("fraud: summary 200 with card shape",
          r.status_code == 200 and all(k in body for k in (
              "total_clicks", "bot_clicks", "bot_share_pct", "avg_fraud_score",
              "est_savings", "top_ips", "top_uas", "shields")), r.text[:200])
    check("fraud: summary counts are ints",
          isinstance(body.get("total_clicks"), int) and isinstance(body.get("bot_clicks"), int),
          str({k: body.get(k) for k in ("total_clicks", "bot_clicks")}))
    check("fraud: seeded bot click raises the 24h totals",
          body.get("bot_clicks", 0) >= 1 and body.get("avg_fraud_score", 0) > 0, r.text[:200])
    top_ip = (body.get("top_ips") or [{}])[0]
    check("fraud: top_ips row shape",
          isinstance(body.get("top_ips"), list)
          and all(k in top_ip for k in ("ip", "hits", "avg_score", "last_seen")), str(top_ip))

    r = s.get(f"{api}/fraud/feed")
    feed = r.json() if r.status_code == 200 else []
    check("fraud: feed 200, newest first, capped at 100",
          r.status_code == 200 and isinstance(feed, list) and len(feed) <= 100
          and all(feed[i].get("received_at") >= feed[i + 1].get("received_at")
                  for i in range(len(feed) - 1)), r.text[:200])
    check("fraud: feed carries bot/score fields and only flagged rows",
          bool(feed) and all(k in feed[0] for k in ("ip", "campaign_id", "is_bot", "fraud_score"))
          and all(row.get("is_bot") or (row.get("fraud_score") or 0) >= 50 for row in feed),
          str(feed[0].keys() if feed else None))
    r_future = s.get(f"{api}/fraud/feed", params={
        "after": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")})
    check("fraud: feed with future 'after' returns nothing newer",
          r_future.status_code == 200 and r_future.json() == [], r_future.text[:150])

    r = s.get(f"{api}/fraud/honeypot-hits")
    check("fraud: honeypot-hits 200 with hits list",
          r.status_code == 200 and isinstance(r.json().get("hits"), list), r.text[:150])

    # bot-lists round trip (save -> read back -> restore the previous value)
    r = s.get(f"{api}/settings/")
    saved_lists = (r.json().get("settings") or {}).get("bot_lists")
    test_lists = {"ua_regex": [f"smoke-bot-{os.getpid()}"],
                  "ip_cidrs": ["203.0.113.0/24"],
                  "referer_regex": []}
    r = s.put(f"{api}/fraud/bot-lists", json={"bot_lists": test_lists})
    check("fraud: bot-lists PUT 200", r.status_code == 200, r.text[:200])
    r = s.get(f"{api}/fraud/bot-lists")
    check("fraud: bot-lists round trip",
          r.status_code == 200 and r.json().get("bot_lists") == test_lists, r.text[:200])
    r = s.put(f"{api}/fraud/bot-lists", json={
        "bot_lists": {"ua_regex": ["(["], "ip_cidrs": [], "referer_regex": []}})
    check("fraud: invalid ua_regex -> 422", r.status_code == 422, f"{r.status_code} {r.text[:120]}")
    r = s.put(f"{api}/fraud/bot-lists", json={
        "bot_lists": {"ua_regex": [], "ip_cidrs": ["not-a-cidr"], "referer_regex": []}})
    check("fraud: invalid ip_cidr -> 422", r.status_code == 422, f"{r.status_code} {r.text[:120]}")
    cfg = dict((s.get(f"{api}/settings/").json().get("settings") or {}))
    cfg["bot_lists"] = saved_lists  # null removes the key under merge semantics
    r = s.post(f"{api}/settings/", json={"settings": cfg})
    check("fraud: bot_lists restored", r.status_code == 200, r.text[:120])
    r = s.get(f"{api}/fraud/bot-lists")
    restored = (r.json().get("bot_lists") or {})
    check("fraud: bot_lists gone after restore",
          saved_lists is None and restored.get("ua_regex") == []
          or restored == {k: (saved_lists or {}).get(k, []) for k in
                          ("ua_regex", "ip_cidrs", "referer_regex")}, r.text[:200])

    # shield stats surface campaigns with an enabled shield block
    shield_alias = f"smoke-shield-{os.getpid()}"
    r = s.post(f"{api}/campaigns/", json={
        "name": shield_alias, "alias": shield_alias, "type": "campaign",
        "status": "active", "redirect_mode": "position",
        "config": {"flows": [], "postbacks": [], "hide_referrer": False,
                   "fallback_url": f"https://example.com/smoke-{os.getpid()}-shield-fb",
                   "shield": {"enabled": True, "action": "404", "honeypot": True,
                              "whitelists": {"ips": ["10.0.0.0/8"], "referers": ["facebook.com"],
                                             "ua_regex": ""}}}})
    shield_cid = r.json().get("id") if r.status_code == 200 else None
    check("fraud: shield campaign created", bool(shield_cid), r.text[:200])
    r = s.get(f"{api}/fraud/summary")
    shields = [x for x in (r.json().get("shields") or []) if x.get("campaign_id") == shield_cid]
    check("fraud: enabled shield listed in summary",
          bool(shields) and shields[0]["action"] == "404" and shields[0]["honeypot"] is True
          and shields[0]["whitelist_ips"] == 1, str(shields[:1]))
    if shield_cid:
        r = s.delete(f"{api}/campaigns/{shield_cid}")
        check("fraud: shield campaign cleaned up", r.status_code == 200, r.text[:120])
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE visitor_id = 'smoke-fraud-{os.getpid()}'")

    print("== Fraud tracking plane ==")
    # Tracking-side behavior of the fraud suite: heuristic scoring writes
    # fraud_score on every row, the honeypot flags scrapers, the campaign
    # shield blanks non-whitelisted visitors without tracking them, and a
    # verified-crawler IP is flagged is_bot with the top score.

    def fraud_pg(sql):
        out = subprocess.run(
            ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db",
             "-tAc", sql], capture_output=True, text=True, timeout=30)
        return out.stdout.strip()

    tp_alias = f"smoke-fraudtp-{os.getpid()}"
    tp_payload = {"name": tp_alias, "alias": tp_alias, "type": "campaign",
                  "status": "active", "redirect_mode": "position",
                  "config": {"flows": [{
                      "type": "default", "position": 1, "enabled": True, "schema": "redirect",
                      "redirect_url": f"https://example.com/smoke-fraudtp-{os.getpid()}-a",
                      "filters": []}], "postbacks": [], "hide_referrer": False,
                      "fallback_url": ""}}
    r = s.post(f"{api}/campaigns/", json=tp_payload)
    check("fraudtp: create campaign", r.status_code == 200 and "id" in r.json(), r.text[:200])
    tp_id = r.json().get("id")

    # (a) Verified crawler from a real crawler net: is_bot + fraud_score 100.
    # nginx overwrites X-Real-IP, so push the crawler IP straight to uvicorn
    # (X-Forwarded-For fallback) the same way the anonymize_ip test does.
    # NB: the row is matched by browser, not ip — the privacy test earlier in
    # this suite restores anonymize_ip but the frontend's 30s settings cache
    # can still mask the last octet when this probe runs (scoring itself uses
    # the true ip; masking happens after the score is computed).
    crawler_ua = f"Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html) SmokeCrawler/{os.getpid()}"
    subprocess.run(["docker", "exec", "tracker_frontend", "curl", "-s", "-o", "/dev/null",
                    "-H", "Host: localhost", "-H", "X-Forwarded-For: 66.249.66.1",
                    "-A", crawler_ua,
                    f"http://127.0.0.1:8000/{tp_alias}"],
                   capture_output=True, timeout=30)
    import time
    time.sleep(1)
    row = ch_query(f"SELECT is_bot, fraud_score, ip FROM clicks_data "
                   f"WHERE campaign_id = {tp_id} AND browser = 'GoogleBot' LIMIT 1")
    check("fraudtp: verified crawler IP -> is_bot + fraud_score=100",
          bool(row) and not row.startswith("ERROR")
          and row.split("\t")[:2] == ["true", "100"], row[:120])

    # (b) Honeypot: /t/hp 204s, rate-limits to one row/hour, and the flagged
    # visitor's next campaign click is recorded as a bot with fraud_score 100.
    hp_ua = f"SmokeHp/{os.getpid()}"
    r = requests.get(f"{BASE}/t/hp", params={"c": tp_alias}, verify=not INSECURE,
                     headers={"User-Agent": hp_ua})
    check("fraudtp: /t/hp returns 204", r.status_code == 204, str(r.status_code))
    requests.get(f"{BASE}/t/hp", params={"c": tp_alias}, verify=not INSECURE,
                 headers={"User-Agent": hp_ua})
    hits = fraud_pg(f"SELECT count(*) FROM honeypot_hits WHERE ua = '{hp_ua}'")
    check("fraudtp: honeypot rate-limited to one row/hour", hits == "1", hits[:50])
    r = requests.get(f"{BASE}/t/hp.js", verify=not INSECURE)
    check("fraudtp: /t/hp.js serves the decoy script",
          r.status_code == 200 and "/t/hp" in r.text
          and "javascript" in r.headers.get("content-type", ""), f"{r.status_code}")
    r = requests.get(f"{BASE}/{tp_alias}", verify=not INSECURE, allow_redirects=False,
                     params={"sub_id_1": f"smoke-hp-{os.getpid()}"},
                     headers={"User-Agent": hp_ua})
    check("fraudtp: honeypot-flagged visitor still redirected",
          r.status_code in (301, 302, 307, 308), str(r.status_code))
    time.sleep(1)
    row = ch_query(f"SELECT is_bot, fraud_score FROM clicks_data "
                   f"WHERE campaign_id = {tp_id} AND sub_id_1 = 'smoke-hp-{os.getpid()}' LIMIT 1")
    check("fraudtp: honeypot-flagged click -> is_bot + fraud_score=100",
          bool(row) and not row.startswith("ERROR") and row.split("\t") == ["true", "100"],
          row[:120])

    # (c) Shield "blank": non-whitelisted visitor gets a blank 200 and is NOT
    # tracked; a whitelisted UA regex passes through to the normal redirect.
    tp_payload["config"]["shield"] = {
        "enabled": True, "action": "blank", "honeypot": True,
        "whitelists": {"ips": ["192.0.2.0/24"], "referers": [], "ua_regex": ""}}
    r = s.put(f"{api}/campaigns/{tp_id}", json=tp_payload)
    check("fraudtp: shield config saved", r.status_code == 200, r.text[:150])
    before = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {tp_id}")
    shield_ua = f"SmokeShield/{os.getpid()}"
    r = requests.get(f"{BASE}/{tp_alias}", verify=not INSECURE, allow_redirects=False,
                     params={"sub_id_1": f"smoke-shield-{os.getpid()}"},
                     headers={"User-Agent": shield_ua})
    after = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {tp_id}")
    check("fraudtp: shield blank serves blank 200",
          r.status_code == 200 and "<body></body>" in r.text, f"{r.status_code} {r.text[:60]}")
    check("fraudtp: shield blank is untracked", before == after, f"{before} -> {after}")

    tp_payload["config"]["shield"]["whitelists"] = {
        "ips": [], "referers": [], "ua_regex": shield_ua}
    r = s.put(f"{api}/campaigns/{tp_id}", json=tp_payload)
    check("fraudtp: shield whitelist updated", r.status_code == 200, r.text[:150])
    r = requests.get(f"{BASE}/{tp_alias}", verify=not INSECURE, allow_redirects=False,
                     params={"sub_id_1": f"smoke-shield-wl-{os.getpid()}"},
                     headers={"User-Agent": shield_ua})
    check("fraudtp: whitelisted UA regex redirects normally",
          r.status_code in (301, 302, 307, 308)
          and f"smoke-fraudtp-{os.getpid()}-a" in (r.headers.get("location") or ""),
          f"{r.status_code} {r.headers.get('location', '')[:80]}")
    time.sleep(1)
    row = ch_query(f"SELECT is_bot, fraud_score FROM clicks_data "
                   f"WHERE campaign_id = {tp_id} AND sub_id_1 = 'smoke-shield-wl-{os.getpid()}' LIMIT 1")
    check("fraudtp: whitelisted click tracked with a low fraud score",
          bool(row) and not row.startswith("ERROR") and row.split("\t") == ["false", "0"],
          row[:120])

    print("== Fraud tracking plane cleanup ==")
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE campaign_id = {tp_id}")
    leftover = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {tp_id}")
    check("fraudtp: CH rows removed", leftover == "0", leftover[:80])
    fraud_pg(f"DELETE FROM honeypot_hits WHERE ua = '{hp_ua}'")
    hits = fraud_pg(f"SELECT count(*) FROM honeypot_hits WHERE ua = '{hp_ua}'")
    check("fraudtp: honeypot rows removed", hits == "0", hits[:50])
    if tp_id:
        r = s.delete(f"{api}/campaigns/{tp_id}")
        check("fraudtp: delete campaign", r.status_code == 200, r.text[:120])

    print("== Regression: reporting depth ==")
    # Seed ClickHouse traffic (self-cleaned below; rows are visitor_id 'seed-%').
    ch_query(
        "INSERT INTO clicks_data (received_at, campaign_id, offer_id, click, status, visitor_id, country, device_type, os, browser, language, url, referrer, keyword, utm_source, utm_campaign, traffic_source_name, ip, cost, revenue, profit) "
        "SELECT now() - INTERVAL number HOUR, 1, 1, if(number % 3 = 0, true, false), "
        "multiIf(number % 40 = 0, 'sale', number % 25 = 0, 'lead', number % 60 = 0, 'rejected', ''), "
        "'seed-' || toString(100000 + number), "
        "['US','IN','GB','BR','DE'][1 + number % 5], ['Mobile','Desktop','Tablet'][1 + number % 3], "
        "['Android','iOS','Windows'][1 + number % 3], ['Chrome','Safari','Firefox'][1 + number % 3], "
        "'en', 'https://example.com/landing', 'https://fb.com/feed', 'kw' || toString(number % 7), "
        "'facebook', 'test-campaign', 'Facebook', "
        "toIPv4(concat('10.0.', toString(number % 250), '.', toString((number * 7) % 250))), "
        "if(number % 3 = 0, 0.05, 0), "
        "multiIf(number % 40 = 0, 5.0, number % 25 = 0, 1.0, 0), "
        "multiIf(number % 40 = 0, 5.0, number % 25 = 0, 1.0, 0) "
        "FROM numbers(500)")

    # Containers run on UTC — derive "today" from UTC so the suite is safe to
    # run near local midnight (host-local date could lag/lead CH/PG by a day).
    today = datetime.now(timezone.utc).date()
    d_from, d_to = str(today.fromordinal(today.toordinal() - 25)), str(today)

    # -- G47: multi-dimension drill-down breakdown --
    r = s.post(f"{api}/dashboard/breakdown", json={
        "dimensions": ["country", "device_type"],
        "filters": {"date_from": d_from, "date_to": d_to},
    })
    check("multi-dim breakdown returns 200", r.status_code == 200, r.text[:150])
    rows = r.json().get("rows") if r.status_code == 200 else []
    l1 = [x for x in rows if x.get("level") == 1]
    l2 = [x for x in rows if x.get("level") == 2]
    check("multi-dim: two levels returned", bool(l1) and bool(l2), str(len(rows)))
    l1_values = {x["value"] for x in l1}
    check("multi-dim: level-2 rows nested under level-1 parents",
          bool(l2) and all(x["parent_key"] in l1_values for x in l2),
          str([x.get("parent_key") for x in l2[:3]]))
    check("multi-dim: legacy single-dimension key still present",
          r.status_code == 200 and r.json().get("dimension") == "country", r.text[:100])
    bad = s.post(f"{api}/dashboard/breakdown", json={"dimensions": ["nope"],
                                                     "filters": {"date_from": d_from, "date_to": d_to}})
    check("multi-dim: unknown dimension -> 400", bad.status_code == 400, bad.text[:100])

    # -- G47: saved reports CRUD via the settings API --
    saved_cfg = {"dimensions": ["country", "os"], "columns": ["visits", "clicks"],
                 "customMetrics": [], "dateBasis": "click_date"}
    r = s.post(f"{api}/settings/saved-reports", json={"name": f"smoke-report-{os.getpid()}", "config": saved_cfg})
    check("saved report created", r.status_code == 200 and r.json().get("report", {}).get("id"), r.text[:150])
    report_id = r.json().get("report", {}).get("id") if r.status_code == 200 else None
    r = s.get(f"{api}/settings/saved-reports")
    names = [x["name"] for x in r.json().get("reports", [])] if r.status_code == 200 else []
    check("saved report listed", f"smoke-report-{os.getpid()}" in names, str(names))
    if report_id:
        r = s.delete(f"{api}/settings/saved-reports/{report_id}")
        check("saved report deleted", r.status_code == 200, r.text[:120])
    r = s.get(f"{api}/settings/saved-reports")
    names = [x["name"] for x in r.json().get("reports", [])] if r.status_code == 200 else []
    check("saved report gone after delete", f"smoke-report-{os.getpid()}" not in names, str(names))

    # -- G49: custom metrics --
    r = s.post(f"{api}/dashboard/breakdown", json={
        "dimensions": ["country"],
        "filters": {"date_from": d_from, "date_to": d_to},
        "custom_metrics": [{"name": "uCR", "formula": "conversions*100/clicks"}],
    })
    rows = r.json().get("rows") if r.status_code == 200 else []
    ok = bool(rows) and all(
        abs((x.get("uCR") or 0) - (x["conversions"] * 100 / x["clicks"] if x["clicks"] else 0)) < 0.01
        for x in rows)
    check("custom metric uCR evaluates on every row", r.status_code == 200 and ok, r.text[:200])
    r = s.post(f"{api}/dashboard/breakdown", json={
        "dimensions": ["hour"],
        "filters": {"date_from": d_from, "date_to": d_to},
        "custom_metrics": [{"name": "rpc", "formula": "revenue/clicks"}],
    })
    rows = r.json().get("rows") if r.status_code == 200 else []
    check("custom metric divide-by-zero safe (null cells, no 500)",
          r.status_code == 200 and rows and any(x.get("rpc") is None for x in rows), r.text[:200])
    bad = s.post(f"{api}/dashboard/breakdown", json={
        "dimensions": ["country"], "filters": {},
        "custom_metrics": [{"name": "evil", "formula": "__import__('os')"}]})
    check("custom metric rejects non-arithmetic formula -> 400", bad.status_code == 400, bad.text[:120])

    # -- G50: period comparison (deterministic: isolated campaign 999) --
    cmp_pid = os.getpid()
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, country, cost) "
        f"SELECT toDateTime(toDate(now()) - 1) + toIntervalHour(number % 24), 999, NULL, '', "
        f"'seed-rd-{cmp_pid}-cur-' || toString(number), 'US', 0.01 FROM numbers(60)")
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, country, cost) "
        f"SELECT toDateTime(toDate(now()) - 3) + toIntervalHour(number), 999, NULL, '', "
        f"'seed-rd-{cmp_pid}-prev-' || toString(number), 'US', 0.01 FROM numbers(3)")
    cur_from, cur_to = str(today.fromordinal(today.toordinal() - 1)), str(today)
    r = s.post(f"{api}/dashboard/metrics", json={
        "campaigns": [999], "date_from": cur_from, "date_to": cur_to, "compare": True})
    prev = r.json().get("previous") if r.status_code == 200 else None
    check("period comparison returns previous block", bool(prev), r.text[:200])
    check("period comparison previous totals below current",
          bool(prev) and prev["metrics"]["visits"] < r.json()["metrics"]["visits"]
          and prev["metrics"]["visits"] == 3, str(prev and prev["metrics"]))
    check("period comparison windows are same length and adjacent",
          bool(prev) and (datetime.fromisoformat(cur_from) - datetime.fromisoformat(prev["to"])).days == 1,
          str(prev and (prev["from"], prev["to"])))
    r_nc = s.post(f"{api}/dashboard/metrics", json={
        "campaigns": [999], "date_from": cur_from, "date_to": cur_to})
    check("period comparison off by default", r_nc.status_code == 200 and "previous" not in r_nc.json(), r.text[:100])

    # -- G55: date-basis toggle (conversion_date vs click_date) --
    basis_pid, basis_cid = os.getpid(), 888
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, offer_id, click, status, visitor_id, country, cost) "
        f"VALUES (toDateTime(toDate(now()) - 10), {basis_cid}, 5, true, '', 'seed-rd-{basis_pid}-basis', 'US', 0.1)")
    subprocess.run(
        ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db", "-c",
         f"INSERT INTO conversions_data (received_at, click_id, campaign_id, offer_id, status, payout, revenue, profit, visitor_id) "
         f"VALUES (NOW(), 'seed-rd-{basis_pid}-basis', {basis_cid}, 5, 'sale', 9, 9, 9, 'seed-rd-{basis_pid}-basis')"],
        capture_output=True, text=True, timeout=30)
    today_s = str(today)
    r_click = s.post(f"{api}/dashboard/breakdown", json={
        "dimensions": ["campaign_id"], "date_basis": "click_date",
        "filters": {"date_from": today_s, "date_to": today_s}})
    r_conv = s.post(f"{api}/dashboard/breakdown", json={
        "dimensions": ["campaign_id"], "date_basis": "conversion_date",
        "filters": {"date_from": today_s, "date_to": today_s}})
    conv_rows = {x["value"]: x for x in (r_conv.json().get("rows") or [])} if r_conv.status_code == 200 else {}
    click_rows = {x["value"]: x for x in (r_click.json().get("rows") or [])} if r_click.status_code == 200 else {}
    crafted = conv_rows.get(str(basis_cid))
    check("conversion_date basis counts the crafted conversion",
          r_conv.status_code == 200 and crafted and crafted["conversions"] == 1
          and abs(crafted["revenue"] - 9) < 0.001, r_conv.text[:200])
    check("conversion_date basis adds conversions beyond the click window (synthetic row)",
          bool(crafted) and crafted["visits"] == 0, str(crafted))
    check("click_date basis does not count it (click is 10 days old)",
          str(basis_cid) not in click_rows
          or click_rows[str(basis_cid)]["conversions"] == 0, r_click.text[:200])
    r_fb = s.post(f"{api}/dashboard/breakdown", json={
        "dimensions": ["country"], "date_basis": "conversion_date",
        "filters": {"date_from": today_s, "date_to": today_s}})
    check("unsupported conversion_date dim falls back with note",
          r_fb.status_code == 200 and r_fb.json().get("date_basis") == "click_date"
          and bool(r_fb.json().get("fallback_note")), r_fb.text[:200])
    r_log_c = s.get(f"{api}/reports/", params={
        "date_from": today_s, "date_to": today_s, "date_basis": "conversion_date",
        "click_id": f"seed-rd-{basis_pid}-basis"})
    r_log_k = s.get(f"{api}/reports/", params={
        "date_from": today_s, "date_to": today_s, "date_basis": "click_date",
        "click_id": f"seed-rd-{basis_pid}-basis"})
    check("conversions log conversion_date basis shows the conversion",
          r_log_c.status_code == 200 and any(c["click_id"] == f"seed-rd-{basis_pid}-basis" for c in r_log_c.json()),
          r_log_c.text[:150])
    check("conversions log click_date basis hides it (old click)",
          r_log_k.status_code == 200
          and not any(c["click_id"] == f"seed-rd-{basis_pid}-basis" for c in r_log_k.json()),
          r_log_k.text[:150])
    # Positive click-date case: the CLICK is today, the conversion landed
    # yesterday — click_date basis must show it, conversion_date must not.
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, offer_id, click, status, visitor_id, country, cost) "
        f"VALUES (now(), {basis_cid}, 5, true, '', 'seed-rd-{basis_pid}-today', 'US', 0.1)")
    subprocess.run(
        ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db", "-c",
         f"INSERT INTO conversions_data (received_at, click_id, campaign_id, offer_id, status, payout, revenue, profit, visitor_id) "
         f"VALUES (now() - interval '1 day', 'seed-rd-{basis_pid}-today', {basis_cid}, 5, 'sale', 3, 3, 3, 'seed-rd-{basis_pid}-today')"],
        capture_output=True, text=True, timeout=30)
    r_log_tk = s.get(f"{api}/reports/", params={
        "date_from": today_s, "date_to": today_s, "date_basis": "click_date",
        "click_id": f"seed-rd-{basis_pid}-today"})
    r_log_tc = s.get(f"{api}/reports/", params={
        "date_from": today_s, "date_to": today_s, "date_basis": "conversion_date",
        "click_id": f"seed-rd-{basis_pid}-today"})
    check("conversions log click_date basis shows conversion whose click is today",
          r_log_tk.status_code == 200 and any(c["click_id"] == f"seed-rd-{basis_pid}-today" for c in r_log_tk.json()),
          r_log_tk.text[:150])
    check("conversions log conversion_date basis hides yesterday-dated conversion",
          r_log_tc.status_code == 200
          and not any(c["click_id"] == f"seed-rd-{basis_pid}-today" for c in r_log_tc.json()),
          r_log_tc.text[:150])
    # Legacy rows (pre visitor attribution) have visitor_id NULL — under
    # click_date they must fall back to their own date, not vanish.
    subprocess.run(
        ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db", "-c",
         f"INSERT INTO conversions_data (received_at, click_id, campaign_id, offer_id, status, payout, revenue, profit) "
         f"VALUES (now(), 'seed-rd-{basis_pid}-legacy', {basis_cid}, 5, 'sale', 2, 2, 2)"],
        capture_output=True, text=True, timeout=30)
    r_log_lg = s.get(f"{api}/reports/", params={
        "date_from": today_s, "date_to": today_s, "date_basis": "click_date",
        "click_id": f"seed-rd-{basis_pid}-legacy"})
    check("conversions log click_date basis keeps legacy NULL-visitor rows on their own date",
          r_log_lg.status_code == 200 and any(c["click_id"] == f"seed-rd-{basis_pid}-legacy" for c in r_log_lg.json()),
          r_log_lg.text[:150])
    subprocess.run(
        ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db", "-c",
         f"DELETE FROM conversions_data WHERE visitor_id LIKE 'seed-rd-{basis_pid}-%' OR click_id LIKE 'seed-rd-{basis_pid}-%'"],
        capture_output=True, text=True, timeout=30)

    # -- click-log free-text search: no more 500 on LIKE metacharacters --
    for term in ("seed-", "100%_", '"quoted"', "\\"):
        r = s.post(f"{api}/dashboard/click-log", json={"search": term})
        check(f"click-log search {term!r} does not 500", r.status_code == 200, r.text[:120])
    r = s.post(f"{api}/dashboard/click-log", json={"search": "seed-rd-"})
    check("click-log search finds seeded visitors",
          r.status_code == 200 and any("seed-" in str(c.get("visitor_id")) for c in r.json()), r.text[:150])

    print("== Regression: reporting depth cleanup ==")
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE visitor_id LIKE 'seed-rd-{cmp_pid}-%'")
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE visitor_id LIKE 'seed-rd-{basis_pid}-%'")
    ch_query("ALTER TABLE clicks_data DELETE WHERE visitor_id LIKE 'seed-%'")
    subprocess.run(
        ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db", "-c",
         f"DELETE FROM conversions_data WHERE visitor_id LIKE 'seed-rd-{basis_pid}-%'"],
        capture_output=True, text=True, timeout=30)
    leftover = ch_query("SELECT count() FROM clicks_data WHERE visitor_id LIKE 'seed-%'")
    check("seeded CH rows removed", leftover == "0", leftover[:80])

    print("== Regression: reporting polish ==")
    # Seed isolated campaign 776: 10 visits (2 rejected) + 4 click rows, so
    # rejected_rate=20% and ctr=40% under visits = non-click rows.
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, country, cost) "
        f"SELECT now(), 776, number >= 10, if(number IN (0, 1), 'rejected', ''), "
        f"'seed-g5-{os.getpid()}-' || toString(number), 'US', 0.01 FROM numbers(14)")
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, country, sub_id_1, sub_id_2, utm_source, utm_medium, utm_campaign, keyword) "
        f"SELECT now() - INTERVAL number SECOND, 777, NULL, '', 'seed-g5-{os.getpid()}-live-' || toString(number), 'DE', "
        f"'sub' || toString(number % 3), 'sub' || toString(number % 2), 'google', 'cpc', 'brand', 'kw' || toString(number) "
        f"FROM numbers(5)")

    today_s = str(today)

    # -- G51: live click feed --
    r = s.get(f"{api}/dashboard/live-clicks")
    feed = r.json() if r.status_code == 200 else []
    stamps = [row.get("received_at") or "" for row in feed]
    check("live-clicks returns rows ordered desc", r.status_code == 200 and len(feed) > 0
          and all(stamps[i] >= stamps[i + 1] for i in range(len(stamps) - 1)), r.text[:150])
    check("live-clicks caps at 20 rows", len(feed) <= 20, str(len(feed)))
    check("live-clicks feed carries campaign/country/referrer fields",
          bool(feed) and all(k in feed[0] for k in ("campaign_id", "country", "device_type", "referrer", "status")),
          str(feed[0].keys() if feed else None))
    r_future = s.get(f"{api}/dashboard/live-clicks",
                     params={"after": (datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S")})
    check("live-clicks with future 'after' returns nothing newer",
          r_future.status_code == 200 and r_future.json() == [], r_future.text[:150])
    r_bad = s.get(f"{api}/dashboard/live-clicks", params={"after": "not-a-date"})
    check("live-clicks invalid 'after' -> 400", r_bad.status_code == 400, r_bad.text[:120])

    # -- G52: shared/public reports --
    pub_cfg = {"dimensions": ["country"], "columns": ["visits", "clicks"],
               "dateRange": [str(today.fromordinal(today.toordinal() - 7)), today_s],
               "sortBy": "visits", "sortDir": "desc"}
    r = s.post(f"{api}/settings/saved-reports", json={"name": f"smoke-share-{os.getpid()}", "config": pub_cfg})
    check("saved report for sharing created", r.status_code == 200 and r.json().get("report", {}).get("id"), r.text[:150])
    share_rid = r.json().get("report", {}).get("id") if r.status_code == 200 else None
    r = s.post(f"{api}/settings/saved-reports/{share_rid}/share")
    share_tok = r.json().get("share", {}).get("token") if r.status_code == 200 else None
    check("share token created", r.status_code == 200 and bool(share_tok), r.text[:150])
    created_at = r.json().get("share", {}).get("created_at") if r.status_code == 200 else None
    check("share record has created_at", bool(created_at), r.text[:150])
    r2 = s.post(f"{api}/settings/saved-reports/{share_rid}/share")
    check("re-sharing returns the same token", r2.status_code == 200
          and r2.json().get("share", {}).get("token") == share_tok, r2.text[:150])

    bare = requests.Session()
    bare.verify = not INSECURE
    r = bare.post(f"{api}/dashboard/public/report/{share_tok}")
    pub = r.json() if r.status_code == 200 else {}
    pub_rows = pub.get("rows") or []
    check("public report served WITHOUT auth cookie", r.status_code == 200
          and pub.get("name") == f"smoke-share-{os.getpid()}", r.text[:200])
    check("public report returns breakdown rows with metrics",
          bool(pub_rows) and all(k in pub_rows[0] for k in ("visits", "clicks", "cr", "level", "dim", "value")),
          str(pub_rows[0].keys() if pub_rows else None))
    check("public report response is self-contained (name/dimensions/totals)",
          bool(pub.get("dimensions")) and "totals" in pub, str(pub.keys()))
    r = bare.post(f"{api}/dashboard/public/report/does-not-exist-token")
    check("public report unknown token -> 404", r.status_code == 404, str(r.status_code))
    r = bare.get(f"{BASE}/backend/public-report", params={"t": share_tok})
    check("public report HTML page served without auth",
          r.status_code == 200 and "Shared report" in r.text, f"{r.status_code}")
    r = s.delete(f"{api}/settings/saved-reports/{share_rid}/share")
    check("share revoked", r.status_code == 200, r.text[:120])
    r = bare.post(f"{api}/dashboard/public/report/{share_tok}")
    check("revoked token -> 404 on public endpoint", r.status_code == 404, str(r.status_code))

    # -- G53: per-report email schedules --
    r = s.post(f"{api}/settings/saved-reports", json={"name": f"smoke-sched-{os.getpid()}", "config": pub_cfg})
    sched_rid = r.json().get("report", {}).get("id") if r.status_code == 200 else None
    check("saved report for schedule created", r.status_code == 200 and bool(sched_rid), r.text[:150])
    now_hour = datetime.now(timezone.utc).hour
    r = s.post(f"{api}/settings/report-email-schedules", json={
        "report_id": sched_rid, "recipients": "ops@example.com, boss@example.com",
        "hour_utc": now_hour, "frequency": "daily"})
    sched = r.json().get("schedule") or {}
    check("schedule created", r.status_code == 200 and sched.get("report_id") == sched_rid, r.text[:200])
    check("fresh schedule is due at the current UTC hour", r.status_code == 200 and sched.get("due") is True,
          str(sched))
    r = s.get(f"{api}/settings/report-email-schedules")
    ids = [x.get("report_id") for x in r.json().get("schedules", [])] if r.status_code == 200 else []
    check("schedule listed", sched_rid in ids, str(ids))
    r = s.post(f"{api}/settings/report-email-schedules", json={
        "report_id": sched_rid, "recipients": "only@example.com", "hour_utc": now_hour, "frequency": "weekly"})
    scheds = [x for x in (r.json().get("schedule"),)] if r.status_code == 200 else []
    check("schedule upserts in place (weekly)", r.status_code == 200 and scheds[0].get("frequency") == "weekly"
          and scheds[0].get("recipients") == "only@example.com", r.text[:200])
    check("weekly with no last_sent is due", r.status_code == 200 and scheds[0].get("due") is True, r.text[:200])
    r = s.post(f"{api}/settings/report-email-schedules", json={
        "report_id": sched_rid, "recipients": "x@example.com", "hour_utc": 25, "frequency": "daily"})
    check("schedule hour 25 -> 400", r.status_code == 400, r.text[:120])
    r = s.post(f"{api}/settings/report-email-schedules", json={
        "report_id": sched_rid, "recipients": "x@example.com", "hour_utc": 9, "frequency": "hourly"})
    check("schedule bad frequency -> 400", r.status_code == 400, r.text[:120])
    r = s.post(f"{api}/settings/report-email-schedules", json={
        "report_id": "no-such-report", "recipients": "x@example.com", "hour_utc": 9, "frequency": "daily"})
    check("schedule for missing report -> 404", r.status_code == 404, r.text[:120])

    def set_schedule_last_sent(date_str):
        out = subprocess.run(
            ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db", "-tAc",
             "SELECT value FROM settings WHERE name='report_email_schedules'"],
            capture_output=True, text=True, timeout=30)
        try:
            data = json.loads(out.stdout.strip())
            for item in data:
                item["last_sent"] = date_str
            open("/tmp/_smoke_sched.json", "w").write(json.dumps(data))
            subprocess.run(["docker", "cp", "/tmp/_smoke_sched.json", "tracker_postgres:/tmp/_smoke_sched.json"],
                           capture_output=True, timeout=30)
            subprocess.run(
                ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db", "-q", "-c",
                 "UPDATE settings SET value = pg_read_file('/tmp/_smoke_sched.json') WHERE name='report_email_schedules'"],
                capture_output=True, timeout=30)
            subprocess.run(["docker", "exec", "tracker_postgres", "rm", "-f", "/tmp/_smoke_sched.json"],
                           capture_output=True, timeout=30)
        except Exception:
            pass

    set_schedule_last_sent(today_s)
    r = s.get(f"{api}/settings/report-email-schedules")
    due_map = {x.get("report_id"): x.get("due") for x in r.json().get("schedules", [])} if r.status_code == 200 else {}
    check("schedule already sent today is not due", due_map.get(sched_rid) is False, str(due_map))
    week_ago = str(today.fromordinal(today.toordinal() - 7))
    set_schedule_last_sent(week_ago)
    r = s.get(f"{api}/settings/report-email-schedules")
    due_map = {x.get("report_id"): x.get("due") for x in r.json().get("schedules", [])} if r.status_code == 200 else {}
    check("weekly schedule 7 days after last send is due again", due_map.get(sched_rid) is True, str(due_map))

    # -- G54: traffic-loss / postback computed metrics on seeded data --
    r = s.post(f"{api}/dashboard/breakdown", json={
        "dimensions": ["campaign_id"],
        "filters": {"date_from": today_s, "date_to": today_s, "campaigns": [776]}})
    rows776 = {x["value"]: x for x in (r.json().get("rows") or [])} if r.status_code == 200 else {}
    crafted776 = rows776.get("776")
    check("breakdown exposes rejected_rate on seeded data",
          bool(crafted776) and abs(float(crafted776.get("rejected_rate") or -1) - 20.0) < 0.01,
          str(crafted776 and crafted776.get("rejected_rate")))
    check("breakdown exposes click_through_rate on seeded data",
          bool(crafted776) and abs(float(crafted776.get("click_through_rate") or -1) - 40.0) < 0.01,
          str(crafted776 and crafted776.get("click_through_rate")))
    r = s.post(f"{api}/dashboard/breakdown", json={
        "dimensions": ["country"],
        "filters": {"date_from": today_s, "date_to": today_s},
        "custom_metrics": [{"name": "loss", "formula": "rejected_rate/2"}]})
    cm_rows = r.json().get("rows") or [] if r.status_code == 200 else []
    check("computed rates usable in custom-metric formulas",
          bool(cm_rows) and all(abs((x.get("loss") or 0) - (x.get("rejected_rate") or 0) / 2) < 0.01 for x in cm_rows),
          r.text[:200])

    # -- G58: roll-up preset dimensions --
    r = s.post(f"{api}/dashboard/breakdown", json={
        "dimensions": ["sub_id_1", "sub_id_2", "sub_id_3", "sub_id_4", "sub_id_5"],
        "filters": {"date_from": today_s, "date_to": today_s, "campaigns": [776]}})
    check("sub_id chain breakdown works", r.status_code == 200 and r.json().get("rows"), r.text[:150])
    r = s.post(f"{api}/dashboard/breakdown", json={
        "dimensions": ["utm_source", "utm_medium", "utm_campaign", "keyword"],
        "filters": {"date_from": today_s, "date_to": today_s, "campaigns": [776]}})
    check("traffic chain (utm_source/medium/campaign/keyword) works", r.status_code == 200
          and r.json().get("rows"), r.text[:150])

    # -- G57: annotations CRUD --
    r = s.post(f"{api}/settings/annotations", json={
        "date": today_s, "text": f"smoke note {os.getpid()}", "color": "warning"})
    ann = r.json().get("annotation") or {}
    ann_id = ann.get("id")
    check("annotation created", r.status_code == 200 and bool(ann_id), r.text[:150])
    check("annotation stored with color + date",
          ann.get("color") == "warning" and ann.get("date") == today_s, r.text[:150])
    r = s.get(f"{api}/settings/annotations")
    anns = [a for a in r.json().get("annotations", []) if a.get("id") == ann_id] if r.status_code == 200 else []
    check("annotation listed", bool(anns), r.text[:150])
    r = s.post(f"{api}/settings/annotations", json={"date": "09/26/2026", "text": "x", "color": "info"})
    check("annotation bad date -> 400", r.status_code == 400, r.text[:120])
    r = s.post(f"{api}/settings/annotations", json={"date": today_s, "text": "x", "color": "purple"})
    check("annotation bad color -> 400", r.status_code == 400, r.text[:120])
    r = s.post(f"{api}/settings/annotations", json={"date": today_s, "text": "", "color": "info"})
    check("annotation empty text -> 400", r.status_code == 400, r.text[:120])
    if ann_id:
        r = s.delete(f"{api}/settings/annotations/{ann_id}")
        check("annotation deleted", r.status_code == 200, r.text[:120])
        r = s.delete(f"{api}/settings/annotations/{ann_id}")
        check("annotation double-delete -> 404", r.status_code == 404, str(r.status_code))

    print("== Regression: reporting polish cleanup ==")
    if sched_rid:
        r = s.delete(f"{api}/settings/report-email-schedules/{sched_rid}")
        check("schedule deleted", r.status_code == 200, r.text[:120])
        r = s.delete(f"{api}/settings/saved-reports/{sched_rid}")
        check("scheduled saved report deleted", r.status_code == 200, r.text[:120])
    if share_rid:
        r = s.delete(f"{api}/settings/saved-reports/{share_rid}")
        check("shared saved report deleted", r.status_code == 200, r.text[:120])
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE visitor_id LIKE 'seed-g5-{os.getpid()}-%'")
    leftover = ch_query(f"SELECT count() FROM clicks_data WHERE visitor_id LIKE 'seed-g5-{os.getpid()}-%'")
    check("polish seeded CH rows removed", leftover == "0", leftover[:80])
    r = s.get(f"{api}/settings/report-email-schedules")
    check("no smoke schedules left", all("smoke" not in json.dumps(x) for x in r.json().get("schedules", [])),
          r.text[:150])
    r = s.get(f"{api}/settings/annotations")
    check("no smoke annotations left", all("smoke" not in (x.get("text") or "") for x in r.json().get("annotations", [])),
          r.text[:150])

    print("== Regression: platform security ==")
    sec_pid = os.getpid()

    def pg_query(sql):
        out = subprocess.run(
            ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db", "-tAc", sql],
            capture_output=True, text=True, timeout=30)
        return out.stdout.strip()

    # ===== G62: 2FA (TOTP) full lifecycle =====
    sec_user = f"smoke-2fa-{sec_pid}"
    r = s.post(f"{api}/users/", json={"username": sec_user, "password": "smokepass1", "active": True})
    check("2FA: test user created", r.status_code == 200, r.text[:150])
    sec_uid = r.json().get("id")

    if pyotp is None:
        check("2FA: pyotp available on host", False, "pip install pyotp")
    else:
        su = requests.Session()
        su.verify = not INSECURE
        r = su.post(f"{api}/login", json={"username": sec_user, "password": "smokepass1"})
        check("2FA: plain login before enabling", r.status_code == 200 and "message" in r.json(),
              r.text[:150])

        r = su.post(f"{api}/users/me/totp/setup")
        check("2FA: setup returns secret + QR data URI",
              r.status_code == 200 and bool(r.json().get("secret"))
              and (r.json().get("qr") or "").startswith("data:image/"), r.text[:200])
        sec_secret = r.json().get("secret")

        r = su.post(f"{api}/users/me/totp/enable", json={"code": pyotp.TOTP(sec_secret).now()})
        check("2FA: enable returns 10 backup codes",
              r.status_code == 200 and len(r.json().get("backup_codes") or []) == 10, r.text[:200])
        backup = (r.json().get("backup_codes") or [None])[0]

        r = requests.post(f"{api}/login", json={"username": sec_user, "password": "smokepass1"},
                          verify=not INSECURE)
        check("2FA: login returns totp challenge (no session)",
              r.status_code == 200 and r.json().get("requires_totp") is True
              and bool(r.json().get("totp_token")), r.text[:200])
        ttok = r.json().get("totp_token")

        # Note: the failure limiter spans token refreshes (security audit fix),
        # so the burn-out checks run at the END of this section — a fresh token
        # must not reset the budget.
        r = requests.post(f"{api}/login", json={"username": sec_user, "password": "smokepass1"},
                          verify=not INSECURE)
        ttok = r.json().get("totp_token")
        su2 = requests.Session()
        su2.verify = not INSECURE
        r = su2.post(f"{api}/login/totp",
                     json={"totp_token": ttok, "code": pyotp.TOTP(sec_secret).now()})
        check("2FA: TOTP code login issues session", r.status_code == 200, r.text[:150])
        r = su2.get(f"{api}/users/me")
        check("2FA: session after TOTP login works",
              r.status_code == 200 and r.json().get("totp_enabled") is True, r.text[:150])

        r = requests.post(f"{api}/login", json={"username": sec_user, "password": "smokepass1"},
                          verify=not INSECURE)
        r = requests.post(f"{api}/login/totp",
                          json={"totp_token": r.json().get("totp_token"), "code": backup},
                          verify=not INSECURE)
        check("2FA: backup code accepted", r.status_code == 200, r.text[:150])
        r = requests.post(f"{api}/login", json={"username": sec_user, "password": "smokepass1"},
                          verify=not INSECURE)
        r = requests.post(f"{api}/login/totp",
                          json={"totp_token": r.json().get("totp_token"), "code": backup},
                          verify=not INSECURE)
        check("2FA: backup code is single-use", r.status_code == 401, r.text[:150])

        # Failure limiter spans token refreshes (security audit fix): after 3
        # wrong codes, a freshly minted token with the CORRECT code stays refused.
        r = requests.post(f"{api}/login", json={"username": sec_user, "password": "smokepass1"},
                          verify=not INSECURE)
        ttok = r.json().get("totp_token")
        codes = [requests.post(f"{api}/login/totp",
                               json={"totp_token": ttok, "code": "000000"},
                               verify=not INSECURE).status_code for _ in range(3)]
        check("2FA: repeated wrong codes refused (401 then lockout)",
              codes[0] == 401 and codes[1] == 401 and codes[2] in (401, 429), str(codes))
        r = requests.post(f"{api}/login/totp",
                          json={"totp_token": ttok, "code": pyotp.TOTP(sec_secret).now()},
                          verify=not INSECURE)
        check("2FA: token burned after 3 fails -> 429",
              r.status_code == 429 and "Too many TOTP attempts" in r.text, r.text[:150])
        r = requests.post(f"{api}/login", json={"username": sec_user, "password": "smokepass1"},
                          verify=not INSECURE)
        r = requests.post(f"{api}/login/totp",
                          json={"totp_token": r.json().get("totp_token"),
                                "code": pyotp.TOTP(sec_secret).now()},
                          verify=not INSECURE)
        check("2FA: fresh token does not reset the failure budget -> 429",
              r.status_code == 429 and "Too many TOTP attempts" in r.text, r.text[:150])

        # admin reset
        r = s.post(f"{api}/users/{sec_uid}/totp/reset")
        check("2FA: admin reset works", r.status_code == 200, r.text[:150])
        r = requests.post(f"{api}/login", json={"username": sec_user, "password": "smokepass1"},
                          verify=not INSECURE)
        check("2FA: plain login after admin reset",
              r.status_code == 200 and "message" in r.json(), r.text[:150])

        # self disable
        su3 = requests.Session()
        su3.verify = not INSECURE
        su3.post(f"{api}/login", json={"username": sec_user, "password": "smokepass1"})
        r = su3.post(f"{api}/users/me/totp/setup")
        sec_secret = r.json().get("secret")
        su3.post(f"{api}/users/me/totp/enable", json={"code": pyotp.TOTP(sec_secret).now()})
        r = su3.post(f"{api}/users/me/totp/disable", json={"password": "smokepass1"})
        check("2FA: disable with password", r.status_code == 200, r.text[:150])
        r = requests.post(f"{api}/login", json={"username": sec_user, "password": "smokepass1"},
                          verify=not INSECURE)
        check("2FA: plain login after disable",
              r.status_code == 200 and "message" in r.json(), r.text[:150])

    # ===== G63: per-resource permissions =====
    perm_user = f"smoke-perm-{sec_pid}"
    r = s.post(f"{api}/users/", json={
        "username": perm_user, "password": "smokepass1",
        "permissions": {"sections": {"campaigns": True}, "write": False}})
    check("perms: restricted user created", r.status_code == 200, r.text[:150])
    perm_uid = r.json().get("id")

    pu = requests.Session()
    pu.verify = not INSECURE
    r = pu.post(f"{api}/login", json={"username": perm_user, "password": "smokepass1"})
    check("perms: restricted user login", r.status_code == 200, r.text[:150])
    r = pu.get(f"{api}/campaigns/")
    check("perms: allowed section readable", r.status_code == 200, r.text[:120])
    r = pu.get(f"{api}/users/")
    check("perms: admin section denied (403)", r.status_code == 403, str(r.status_code))
    r = pu.get(f"{api}/settings/")
    check("perms: settings denied (403)", r.status_code == 403, str(r.status_code))
    r = pu.post(f"{api}/campaigns/", json={"name": "x", "alias": f"smoke-pw-{sec_pid}",
                                           "type": "campaign", "status": "active",
                                           "redirect_mode": "weight", "config": {"flows": []}})
    check("perms: write denied (403)", r.status_code == 403, str(r.status_code))
    r = pu.get(f"{api}/users/me")
    check("perms: self-service /me reachable", r.status_code == 200, r.text[:120])

    # ===== G65: audit log =====
    r = s.get(f"{api}/audit/", params={"user": sec_user})
    entries = r.json().get("entries", []) if r.status_code == 200 else []
    actions = {e["action"] for e in entries}
    check("audit: login + totp events recorded for user",
          r.status_code == 200 and r.json().get("total", 0) >= 4
          and {"login_success", "totp_enabled"} <= actions, r.text[:250])
    r = s.get(f"{api}/audit/", params={"user": perm_user, "action": "user_created"})
    check("audit: user_created recorded", r.status_code == 200 and r.json().get("total") >= 1,
          r.text[:150])
    r = s.get(f"{api}/audit/")
    ids = [e["id"] for e in r.json().get("entries", [])] if r.status_code == 200 else []
    check("audit: newest first, paginated 50/page",
          r.status_code == 200 and r.json().get("page_size") == 50
          and ids == sorted(ids, reverse=True) and len(ids) <= 50, str(ids[:5]))

    # ===== G66: archive & restore =====
    r = s.post(f"{api}/offers/", json={"name": f"Smoke Arch Offer {sec_pid}",
                                       "url": "https://example.com/arch?cid={click_id}"})
    arch_offer = r.json().get("id")
    r = s.post(f"{api}/campaigns/", json={
        "name": f"smoke-arch-{sec_pid}", "alias": f"smoke-arch-{sec_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "weight",
        "config": {"flows": [], "postbacks": [], "hide_referrer": False,
                   "fallback_url": "https://example.com/smoke-arch"}})
    check("archive: campaign created", r.status_code == 200 and "id" in r.json(), r.text[:200])
    arch_cid = r.json().get("id")

    r = s.post(f"{api}/archive/campaigns/{arch_cid}")
    check("archive: campaign archived", r.status_code == 200, r.text[:150])
    r = s.get(f"{api}/archive/")
    check("archive: id listed while archived",
          r.status_code == 200 and arch_cid in r.json().get("campaigns", []), r.text[:150])
    flag = pg_query(f"SELECT archived FROM campaigns WHERE id = {arch_cid}")
    check("archive: flag set in DB", flag == "t", flag[:80])
    r = s.post(f"{api}/archive/campaigns/{arch_cid}/restore")
    check("archive: campaign restored", r.status_code == 200, r.text[:150])
    r = s.get(f"{api}/archive/")
    check("archive: id gone after restore",
          r.status_code == 200 and arch_cid not in r.json().get("campaigns", []), r.text[:150])
    flag = pg_query(f"SELECT archived FROM campaigns WHERE id = {arch_cid}")
    check("archive: flag cleared in DB", flag == "f", flag[:80])

    # bulk unarchive — the bulk bar used to offer only Archive, even when the
    # whole selection was already archived
    r = s.post(f"{api}/campaigns/bulk", json={"ids": [arch_cid], "action": "archive"})
    check("bulk: archive accepted", r.status_code == 200 and r.json().get("updated") == 1,
          r.text[:150])
    flag = pg_query(f"SELECT archived FROM campaigns WHERE id = {arch_cid}")
    check("bulk: flag set in DB", flag == "t", flag[:80])
    r = s.post(f"{api}/campaigns/bulk", json={"ids": [arch_cid], "action": "unarchive"})
    check("bulk: unarchive accepted", r.status_code == 200 and r.json().get("updated") == 1,
          r.text[:150])
    flag = pg_query(f"SELECT archived FROM campaigns WHERE id = {arch_cid}")
    check("bulk: unarchive flag cleared in DB", flag == "f", flag[:80])
    r = s.post(f"{api}/archive/campaigns/{arch_cid}")
    check("bulk: re-archive via entity endpoint", r.status_code == 200, r.text[:120])
    r = s.post(f"{api}/campaigns/bulk", json={"ids": [arch_cid], "action": "unarchive"})
    check("bulk: re-unarchive via bulk", r.status_code == 200, r.text[:120])
    r = s.post(f"{api}/campaigns/bulk", json={"ids": [arch_cid], "action": "resurrect"})
    check("bulk: unknown action rejected (422)", r.status_code == 422, str(r.status_code))

    r = s.post(f"{api}/archive/offers/{arch_offer}")
    check("archive: offer archived", r.status_code == 200, r.text[:150])
    flag = pg_query(f"SELECT archived FROM offers WHERE id = {arch_offer}")
    check("archive: offer flag set in DB", flag == "t", flag[:80])
    r = s.post(f"{api}/archive/offers/{arch_offer}/restore")
    check("archive: offer restored", r.status_code == 200, r.text[:150])

    r = s.post(f"{api}/archive/nope/{arch_cid}")
    check("archive: unknown entity -> 404", r.status_code == 404, str(r.status_code))

    r = s.delete(f"{api}/campaigns/{arch_cid}")
    check("archive: real delete still works", r.status_code == 200, r.text[:120])
    r = s.delete(f"{api}/offers/{arch_offer}")
    check("archive: offer delete still works", r.status_code == 200, r.text[:120])

    # ===== platform security cleanup =====
    if sec_uid:
        r = s.delete(f"{api}/users/{sec_uid}")
        check("delete 2FA test user", r.status_code == 200, r.text[:120])
    if perm_uid:
        r = s.delete(f"{api}/users/{perm_uid}")
        check("delete permissions test user", r.status_code == 200, r.text[:120])
    r = s.get(f"{api}/users/")
    leftovers = [u for u in r.json() if (u["username"] or "").startswith("smoke-2fa-")
                 or (u["username"] or "").startswith("smoke-perm-")]
    check("no security test users left", not leftovers, str(leftovers))

    print("== Regression: admin platform ==")
    ap_pid = os.getpid()

    def pg_exec(sql):
        subprocess.run(
            ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db", "-c", sql],
            capture_output=True, text=True, timeout=30)

    def pg_exec_out(sql):
        """pg_exec that returns stdout (first column, no header)."""
        out = subprocess.run(
            ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db", "-tAc", sql],
            capture_output=True, text=True, timeout=30)
        return out.stdout

    # ----- G67: bulk tags add/remove + archive -----
    r = s.post(f"{api}/campaigns/", json={
        "name": f"smoke-admin-{ap_pid}", "alias": f"smoke-admin-{ap_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "config": {"flows": [], "postbacks": [], "hide_referrer": False,
                   "fallback_url": f"https://example.com/smoke-{ap_pid}-ap-fb"}})
    check("admin: campaign created", r.status_code == 200 and "id" in r.json(), r.text[:200])
    ap_cid = r.json().get("id")

    r = s.post(f"{api}/campaigns/bulk", json={"ids": [ap_cid], "action": "tags_add",
                                              "tags": ["smoke-tag-a", "smoke-tag-b"]})
    check("bulk: tags_add ok", r.status_code == 200 and r.json().get("updated") == 1, r.text[:150])
    r = s.post(f"{api}/campaigns/bulk", json={"ids": [ap_cid], "action": "tags_remove",
                                              "tags": ["smoke-tag-b"]})
    check("bulk: tags_remove ok", r.status_code == 200 and r.json().get("updated") == 1, r.text[:150])
    r = s.get(f"{api}/campaigns/")
    camp = [c for c in r.json() if c["id"] == ap_cid]
    check("bulk: tags landed (a kept, b removed)",
          bool(camp) and camp[0]["tags"] == ["smoke-tag-a"], str(camp and camp[0]["tags"]))

    r = s.post(f"{api}/campaigns/bulk", json={"ids": [ap_cid], "action": "archive"})
    check("bulk: archive ok", r.status_code == 200, r.text[:150])
    r = s.get(f"{api}/archive/")
    check("bulk: archived id in archive set",
          r.status_code == 200 and ap_cid in r.json().get("campaigns", []), r.text[:150])
    r = s.post(f"{api}/archive/campaigns/{ap_cid}/restore")
    check("bulk: restore ok", r.status_code == 200, r.text[:150])
    r = s.get(f"{api}/archive/")
    check("bulk: id gone from archive set after restore",
          r.status_code == 200 and ap_cid not in r.json().get("campaigns", []), r.text[:150])

    # ----- G67: campaigns CSV export -> import round-trip -----
    r = s.get(f"{api}/campaigns/export")
    check("export: CSV download", r.status_code == 200
          and r.headers.get("content-type", "").startswith("text/csv")
          and r.content.startswith(b"\xef\xbb\xbf")
          and r.text.lstrip("\ufeff").startswith("id,name,alias"),
          r.headers.get("content-type", ""))
    lines = r.text.strip().split("\n")
    hdr = lines[0]
    row = [l for l in lines[1:] if l.startswith(f"{ap_cid},")]
    check("export: campaign row present with tags column", bool(row), r.text[:200])
    if row:
        cells = row[0].split(",")
        cells[1] = f"smoke-admin-renamed-{ap_pid}"
        cells[8] = "smoke-tag-a;smoke-imported"
        imp_lines = "\n".join([hdr, ",".join(cells),
                               f",smoke-admin-created-{ap_pid},smoke-admin-created-{ap_pid},"
                               f"campaign,active,position,,,smoke-new-tag,"])
        r = s.post(f"{api}/campaigns/import", json={"lines": imp_lines})
        res = (r.json().get("results") or []) if r.status_code == 200 else []
        check("import: 1 update + 1 create", r.status_code == 200
              and r.json().get("imported") == 2 and r.json().get("failed") == 0
              and any("updated" in x["detail"] for x in res)
              and any("created" in x["detail"] for x in res), r.text[:250])
    r = s.get(f"{api}/campaigns/")
    renamed = [c for c in r.json() if c["id"] == ap_cid]
    created = [c for c in r.json() if c["alias"] == f"smoke-admin-created-{ap_pid}"]
    check("import: update applied (name+tags)",
          bool(renamed) and renamed[0]["name"] == f"smoke-admin-renamed-{ap_pid}"
          and "smoke-imported" in (renamed[0]["tags"] or []), str(renamed and renamed[0]))
    check("import: new campaign created", bool(created), r.text[:200])
    ap_created_id = created[0]["id"] if created else None

    # ----- G67: offers bulk + CSV round-trip -----
    r = s.post(f"{api}/offers/", json={"name": f"Smoke AP Offer {ap_pid}",
                                       "url": "https://example.com/ap-offer?cid={click_id}",
                                       "payout": 1.5, "tags": ["smoke-o"]})
    check("offers: created", r.status_code == 200 and "id" in r.json(), r.text[:200])
    ap_oid = r.json().get("id")
    r = s.post(f"{api}/offers/bulk", json={"ids": [ap_oid], "action": "tags_add",
                                           "tags": ["smoke-o2"]})
    check("offers bulk: tags_add ok", r.status_code == 200 and r.json().get("updated") == 1, r.text[:150])
    r = s.get(f"{api}/offers/export")
    check("offers export: CSV", r.status_code == 200 and "affiliate_network_id" in r.text[:200],
          r.text[:120])
    olines = r.text.strip().split("\n")
    ohdr = olines[0]
    orow = [l for l in olines[1:] if l.startswith(f"{ap_oid},")]
    if orow:
        cells = orow[0].split(",")
        cells[ohdr.split(",").index("payout")] = "2.75"
        imp = "\n".join([ohdr, ",".join(cells),
                         f",Smoke AP Imported {ap_pid},https://example.com/ap-imp?cid={{click_id}},,US;GB,3.10,USD,active,smoke-imp,,,"])
        r = s.post(f"{api}/offers/import", json={"lines": imp})
        check("offers import: 1 update + 1 create", r.status_code == 200
              and r.json().get("imported") == 2 and r.json().get("failed") == 0, r.text[:250])
    r = s.get(f"{api}/offers/")
    upd = [o for o in r.json() if o["id"] == ap_oid]
    newo = [o for o in r.json() if o["name"] == f"Smoke AP Imported {ap_pid}"]
    check("offers import: payout updated to 2.75",
          bool(upd) and abs(float(upd[0]["payout"]) - 2.75) < 0.001, str(upd and upd[0]["payout"]))
    check("offers import: new offer created", bool(newo), r.text[:200])
    ap_new_oid = newo[0]["id"] if newo else None

    # ----- G69: monitor check-now with a dead offer URL -----
    r = s.post(f"{api}/offers/", json={"name": f"Smoke Dead Offer {ap_pid}",
                                       "url": "http://127.0.0.1:1/dead?cid={click_id}"})
    dead_oid = r.json().get("id") if r.status_code == 200 else None
    check("monitor: dead offer created", bool(dead_oid), r.text[:200])
    r = s.post(f"{api}/campaigns/", json={
        "name": f"smoke-mon-{ap_pid}", "alias": f"smoke-mon-{ap_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "config": {"flows": [{"type": "default", "name": "deadflow", "position": 1,
                              "enabled": True, "schema": "direct", "offer": dead_oid,
                              "filters": []}],
                   "postbacks": [], "hide_referrer": False,
                   "fallback_url": f"https://example.com/smoke-{ap_pid}-mon-fb"}})
    mon_cid = r.json().get("id") if r.status_code == 200 else None
    check("monitor: campaign created", bool(mon_cid), r.text[:200])

    r = s.post(f"{api}/monitor/check-now")
    check("monitor: check-now runs", r.status_code == 200 and r.json().get("checked", 0) >= 1,
          r.text[:200])
    r = s.get(f"{api}/monitor/status")
    items = [i for i in r.json().get("items", []) if i.get("campaign_id") == mon_cid]
    check("monitor: dead URL reported dead",
          bool(items) and items[0]["status"] == "dead" and items[0]["fail_count"] >= 1,
          str(items[:1]))

    # auto-disable: flip the setting on, fail 3 cycles, flow must switch off
    r = s.get(f"{api}/settings/")
    cfg = dict(r.json().get("settings") or {})
    saved_mon = cfg.get("monitoring")
    cfg["monitoring"] = {"auto_disable_flows": True}
    r = s.post(f"{api}/settings/", json={"settings": cfg})
    check("monitor: auto_disable setting saved", r.status_code == 200, r.text[:120])
    s.post(f"{api}/monitor/check-now")
    s.post(f"{api}/monitor/check-now")
    r = s.post(f"{api}/monitor/check-now")
    check("monitor: third check completes", r.status_code == 200, r.text[:150])
    r = s.get(f"{api}/campaigns/")
    mon_camp = [c for c in r.json() if c["id"] == mon_cid]
    flow = (mon_camp[0]["config"].get("flows") or [{}])[0] if mon_camp else {}
    check("monitor: flow auto-disabled after 3 failures",
          flow.get("enabled") is False and flow.get("disabled_by_monitor") is True, str(flow))
    # restore setting
    cfg["monitoring"] = saved_mon or {"auto_disable_flows": False}
    s.post(f"{api}/settings/", json={"settings": cfg})

    # ----- G70: auto rules (test-run + pause action) -----
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, country, cost, revenue) "
        f"SELECT now() - INTERVAL number HOUR, {ap_cid}, true, '', 'smoke-ap-rule-{ap_pid}-' || toString(number), "
        f"'US', 1.0, 0.0 FROM numbers(10)")
    r = s.post(f"{api}/rules/", json={
        "name": f"smoke-rule-{ap_pid}", "enabled": True, "campaign_id": ap_cid,
        "conditions": [{"metric": "roi", "period_hours": 24, "comparator": "<", "value": -10}],
        "action": "pause_campaign"})
    check("rules: created", r.status_code == 200 and r.json().get("rule", {}).get("id"),
          r.text[:200])
    rule_id = r.json().get("rule", {}).get("id") if r.status_code == 200 else None
    if rule_id:
        r = s.post(f"{api}/rules/{rule_id}/run")
        cond = (r.json().get("conditions") or [{}])[0] if r.status_code == 200 else {}
        check("rules: test-run evaluates roi on seeded data",
              r.status_code == 200 and cond.get("actual") == -100.0
              and r.json().get("would_fire") is True, r.text[:250])
        r = s.post(f"{api}/rules/{rule_id}/execute")
        check("rules: execute fires pause action",
              r.status_code == 200 and (r.json().get("action_taken") or {}).get("ok") is True,
              r.text[:250])
        r = s.get(f"{api}/campaigns/")
        paused = [c for c in r.json() if c["id"] == ap_cid]
        check("rules: campaign status flipped to paused",
              bool(paused) and paused[0]["status"] == "paused", str(paused and paused[0]["status"]))
        r = s.post(f"{api}/rules/", json={
            "name": "bad", "campaign_id": ap_cid,
            "conditions": [{"metric": "nope", "period_hours": 1, "comparator": "<", "value": 1}],
            "action": "alert_telegram"})
        check("rules: invalid metric -> 400", r.status_code == 400, r.text[:120])
        # restore + cleanup
        payload = renamed[0] if renamed else None
        if payload:
            payload = dict(payload)
            payload["status"] = "active"
            s.put(f"{api}/campaigns/{ap_cid}", json=payload)
        s.delete(f"{api}/rules/{rule_id}")
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE visitor_id LIKE 'smoke-ap-rule-{ap_pid}-%'")

    # ----- G75: global search -----
    r = s.get(f"{api}/search", params={"q": "smoke-imported"})
    check("search: finds campaign by tag",
          r.status_code == 200 and any(c["id"] == ap_cid
                                       for c in r.json().get("groups", {}).get("campaigns", [])),
          r.text[:250])
    r = s.get(f"{api}/search", params={"q": "x"})
    check("search: short query -> 400", r.status_code == 400, str(r.status_code))
    pb_click = f"smoke-ap-search-{ap_pid}"
    requests.get(f"{BASE}/pb?clickid={pb_click}&status=sale&payout=2", verify=not INSECURE)
    rec = None
    for _ in range(10):
        r2 = s.get(f"{api}/reports/", params={"click_id": pb_click})
        if r2.status_code == 200 and r2.json():
            rec = r2.json()[0]
            break
        import time
        time.sleep(0.5)
    r = s.get(f"{api}/search", params={"q": pb_click})
    check("search: finds conversion by click_id",
          r.status_code == 200 and any(c["id"] == (rec or {}).get("id")
                                       for c in r.json().get("groups", {}).get("conversions", [])),
          r.text[:250])

    # ----- D1c: campaigns:'own' list scoping -----
    own_user = f"smoke-own-{ap_pid}"
    r = s.post(f"{api}/users/", json={
        "username": own_user, "password": "smokepass1",
        "permissions": {"sections": {"campaigns": True, "dashboard": True},
                        "write": True, "campaigns": "own"}})
    check("own: user created", r.status_code == 200, r.text[:150])
    own_uid = r.json().get("id")
    ou = requests.Session()
    ou.verify = not INSECURE
    r = ou.post(f"{api}/login", json={"username": own_user, "password": "smokepass1"})
    check("own: user login", r.status_code == 200, r.text[:150])
    pg_exec(f"UPDATE campaigns SET owner_id = {own_uid} WHERE id = {ap_created_id}")
    r = ou.get(f"{api}/campaigns/")
    ids = [c["id"] for c in r.json()] if r.status_code == 200 else []
    check("own: scoped user sees only own campaign",
          r.status_code == 200 and ids == [ap_created_id], str(ids))
    r = ou.get(f"{api}/campaigns/metrics")
    check("own: metrics scoped too",
          r.status_code == 200 and (not r.json() or str(ap_created_id) in r.json()),
          r.text[:150])
    pg_exec(f"UPDATE campaigns SET owner_id = NULL WHERE id = {ap_created_id}")
    if own_uid:
        s.delete(f"{api}/users/{own_uid}")

    # ----- G76: AI auto-optimizer -----
    r = s.post(f"{api}/offers/", json={
        "name": f"Smoke Opt Offer {ap_pid}",
        "url": f"https://example.com/smoke-opt-{ap_pid}?cid={{click_id}}"})
    opt_oid = r.json().get("id") if r.status_code == 200 else None
    check("optimizer: offer created", bool(opt_oid), r.text[:200])

    # flows: two regular (a/b), one disabled forced, one disabled regular.
    # Sorted routing order = [forced, a, b, off] — forced sorts first
    # regardless of position, so CH flow_index 1 = a, 2 = b, 3 = off.
    r = s.post(f"{api}/campaigns/", json={
        "name": f"smoke-opt-{ap_pid}", "alias": f"smoke-opt-{ap_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "weight",
        "config": {"flows": [
            {"type": "regular", "name": "opt-a", "position": 1, "enabled": True,
             "schema": "direct", "offer": opt_oid, "weight": 100, "filters": []},
            {"type": "regular", "name": "opt-b", "position": 2, "enabled": True,
             "schema": "direct", "offer": opt_oid, "weight": 100, "filters": []},
            {"type": "forced", "name": "opt-forced", "position": 3, "enabled": False,
             "schema": "direct", "offer": opt_oid, "weight": 100, "filters": []},
            {"type": "regular", "name": "opt-off", "position": 4, "enabled": False,
             "schema": "direct", "offer": opt_oid, "weight": 100, "filters": []}],
            "postbacks": [], "hide_referrer": False, "stickiness": True,
            "fallback_url": f"https://example.com/smoke-{ap_pid}-opt-fb"}})
    opt_cid = r.json().get("id") if r.status_code == 200 else None
    check("optimizer: campaign created", bool(opt_cid), r.text[:200])

    r = s.get(f"{api}/optimizer/status")
    st = r.json() if r.status_code == 200 else {}
    mine = [c for c in st.get("campaigns", []) if c.get("id") == opt_cid]
    mon = [c for c in st.get("campaigns", []) if c.get("id") == mon_cid]
    check("optimizer: status shape",
          r.status_code == 200 and bool(mine)
          and "loop_last_run" in st
          and mine[0]["optimizer"]["metric"] == "epc"
          and mine[0]["optimizer"]["enabled"] is False
          and [f["name"] for f in mine[0]["flows"]] == ["opt-forced", "opt-a", "opt-b", "opt-off"],
          r.text[:250])
    check("optimizer: position-mode campaign flagged unsupported",
          bool(mon) and mon[0]["supported"] is False and mon[0]["flows"] == [],
          r.text[:250])

    r = s.get(f"{api}/optimizer/{opt_cid}")
    check("optimizer: single-campaign GET", r.status_code == 200
          and r.json().get("id") == opt_cid, r.text[:150])

    r = s.put(f"{api}/optimizer/{opt_cid}", json={"metric": "nope"})
    check("optimizer: bad metric -> 422", r.status_code == 422, r.text[:120])
    r = s.put(f"{api}/optimizer/{opt_cid}", json={"max_shift_pct": 5})
    check("optimizer: max_shift_pct below range -> 422", r.status_code == 422, r.text[:120])
    r = s.put(f"{api}/optimizer/{opt_cid}", json={"max_shift_pct": 200})
    check("optimizer: max_shift_pct above range -> 422", r.status_code == 422, r.text[:120])
    r = s.put(f"{api}/optimizer/{opt_cid}", json={"min_clicks": -1})
    check("optimizer: negative min_clicks -> 422", r.status_code == 422, r.text[:120])
    r = s.put(f"{api}/optimizer/{opt_cid}", json={
        "enabled": True, "metric": "epc", "lookback_hours": 24,
        "min_clicks": 20, "max_shift_pct": 80, "protect_clicks": 0})
    check("optimizer: settings saved", r.status_code == 200
          and r.json()["optimizer"]["enabled"] is True, r.text[:200])

    # seed: 60 human clicks on a; 60 human + 10 bot clicks on b (bots must be
    # excluded from the metric). a: 6 convs @ profit 3 -> epc 0.30;
    # b: 1 conv @ profit 0.5 -> epc ~0.0083.
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, country, cost, revenue, flow_index, is_bot) "
        f"SELECT now() - INTERVAL number MINUTE, {opt_cid}, true, '', 'smoke-opt-{ap_pid}-a-' || toString(number), 'US', 1.0, 0.0, 1, 0 FROM numbers(60)")
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, country, cost, revenue, flow_index, is_bot) "
        f"SELECT now() - INTERVAL number MINUTE, {opt_cid}, true, '', 'smoke-opt-{ap_pid}-b-' || toString(number), 'US', 1.0, 0.0, 2, 0 FROM numbers(60)")
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, country, cost, revenue, flow_index, is_bot) "
        f"SELECT now() - INTERVAL number MINUTE, {opt_cid}, true, '', 'smoke-opt-{ap_pid}-bot-' || toString(number), 'US', 1.0, 0.0, 2, 1 FROM numbers(10)")
    pg_exec(f"INSERT INTO conversions_data (click_id, campaign_id, offer_id, status, payout, revenue, profit, currency, flow_index, received_at) "
            f"SELECT 'smoke-opt-conv-a-{ap_pid}-' || g, {opt_cid}, {opt_oid}, 'sale', 5.0, 5.0, 3.0, 'USD', 1, now() - (g || ' minutes')::interval "
            f"FROM generate_series(1, 6) g")
    pg_exec(f"INSERT INTO conversions_data (click_id, campaign_id, offer_id, status, payout, revenue, profit, currency, flow_index, received_at) "
            f"SELECT 'smoke-opt-conv-b-{ap_pid}-' || g, {opt_cid}, {opt_oid}, 'sale', 0.5, 0.5, 0.5, 'USD', 2, now() - (g || ' minutes')::interval "
            f"FROM generate_series(1, 1) g")

    r = s.post(f"{api}/optimizer/{opt_cid}/run")
    run = r.json() if r.status_code == 200 else {}
    flows_by_name = {f["name"]: f for f in run.get("flows", [])}
    check("optimizer: run-now reweights the better flow up",
          r.status_code == 200 and run.get("changed") is True
          and flows_by_name.get("opt-a", {}).get("new_weight", 0)
          > flows_by_name.get("opt-a", {}).get("old_weight", 0)
          and flows_by_name.get("opt-a", {}).get("new_weight") == 180
          and flows_by_name.get("opt-b", {}).get("new_weight") == 20
          and flows_by_name.get("opt-a", {}).get("clicks") == 60
          # bot clicks excluded from b's count
          and flows_by_name.get("opt-b", {}).get("clicks") == 60,
          r.text[:400])
    check("optimizer: forced/disabled flows never analysed",
          "opt-forced" not in flows_by_name and "opt-off" not in flows_by_name,
          str(list(flows_by_name)))

    r = s.get(f"{api}/campaigns/")
    camp = [c for c in r.json() if c["id"] == opt_cid]
    cfg_flows = {f["name"]: f for f in (camp[0]["config"].get("flows") or [])} if camp else {}
    check("optimizer: weights persisted in campaign config",
          cfg_flows.get("opt-a", {}).get("weight") == 180
          and cfg_flows.get("opt-b", {}).get("weight") == 20
          and cfg_flows.get("opt-forced", {}).get("weight") == 100
          and cfg_flows.get("opt-off", {}).get("weight") == 100,
          str({k: v.get("weight") for k, v in cfg_flows.items()}))
    opt_block = (camp[0]["config"].get("optimizer") or {}) if camp else {}
    check("optimizer: last_runs recorded in optimizer block",
          len(opt_block.get("last_runs") or []) == 1
          and opt_block["last_runs"][0]["shifts"].get("opt-a") == {"old": 100, "new": 180},
          str(opt_block.get("last_runs"))[:200])

    r = s.get(f"{api}/audit/", params={"action": "optimizer_run", "entity": "campaigns"})
    check("optimizer: audit entry written",
          r.status_code == 200 and any(e.get("entity_id") == str(opt_cid)
                                       for e in r.json().get("entries", [])),
          r.text[:250])

    # guard: demand more clicks than exist -> pass skipped, weights untouched
    s.put(f"{api}/optimizer/{opt_cid}", json={"min_clicks": 100})
    r = s.post(f"{api}/optimizer/{opt_cid}/run")
    check("optimizer: insufficient-clicks guard skips",
          r.status_code == 200 and not r.json().get("changed")
          and r.json().get("reason") == "insufficient_clicks", r.text[:200])
    s.put(f"{api}/optimizer/{opt_cid}", json={"min_clicks": 20, "enabled": False})
    r = s.post(f"{api}/optimizer/{opt_cid}/run")
    check("optimizer: disabled campaign skips",
          r.status_code == 200 and r.json().get("reason") == "disabled", r.text[:150])

    # live attribution: campaign hit binds a flow via stickiness cookie, the
    # click-out (/c/...) must stamp that flow's index on the conversion row
    sess = requests.Session()
    sess.verify = not INSECURE
    r = sess.get(f"{BASE}/smoke-opt-{ap_pid}", allow_redirects=False)
    check("optimizer: campaign hit binds + redirects",
          r.status_code in (301, 302, 307, 308), f"got {r.status_code}")
    r = sess.get(f"{BASE}/c/smoke-opt-{ap_pid}/{opt_oid}", allow_redirects=False)
    loc = r.headers.get("location") or ""
    check("optimizer: click-out redirects to offer", r.status_code in (301, 302, 307, 308)
          and "click_id=" in loc, f"{r.status_code} {loc[:120]}")
    qs = dict(pair.split("=", 1) for pair in loc.split("?", 1)[1].split("&") if "=" in pair) \
        if "?" in loc else {}
    out_click = qs.get("click_id", "")
    flow_idx = ""
    for _ in range(10):
        flow_idx = pg_exec_out(
            f"SELECT flow_index FROM conversions_data WHERE click_id = '{out_click}'").strip()
        if flow_idx:
            break
        import time
        time.sleep(0.5)
    check("optimizer: conversion row carries flow_index from bind cookie",
          out_click and flow_idx in ("1", "2"), f"click={out_click} flow_index={flow_idx!r}")

    # cleanup: campaign delete must purge the seeded CH clicks
    r = s.delete(f"{api}/campaigns/{opt_cid}")
    check("optimizer: campaign deleted", r.status_code == 200, r.text[:120])
    import time
    time.sleep(1.5)
    leftover_clicks = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {opt_cid}")
    check("optimizer: CH clicks purged on delete", leftover_clicks == "0", leftover_clicks[:80])
    if opt_oid:
        r = s.delete(f"{api}/offers/{opt_oid}")
        check("optimizer: offer deleted", r.status_code == 200, r.text[:120])

    print("== Regression: admin platform cleanup ==")
    for conv in ([rec] if rec else []):
        r = s.delete(f"{api}/reports/{conv['id']}")
        check(f"delete search conversion {conv['id']}", r.status_code == 200, r.text[:120])
    for extra_oid in (dead_oid, ap_oid, ap_new_oid):
        if extra_oid:
            r = s.delete(f"{api}/offers/{extra_oid}")
            check(f"delete admin-platform offer {extra_oid}", r.status_code == 200, r.text[:120])
    for extra_cid in (ap_cid, ap_created_id, mon_cid):
        if extra_cid:
            r = s.delete(f"{api}/campaigns/{extra_cid}")
            check(f"delete admin-platform campaign {extra_cid}", r.status_code == 200, r.text[:120])
    pg_exec("DELETE FROM monitor_state WHERE url LIKE 'http://127.0.0.1:1%'")
    r = s.get(f"{api}/campaigns/")
    leftovers = [c for c in r.json() if c["alias"].startswith("smoke-admin")
                 or c["alias"].startswith("smoke-mon")]
    check("no admin-platform campaigns left", not leftovers, str(leftovers))
    r = s.get(f"{api}/rules/")
    check("no admin-platform rules left",
          all("smoke" not in (x.get("name") or "") for x in r.json().get("rules", [])),
          r.text[:150])

    print("== Regression: audit fixes ==")
    au_pid = os.getpid()
    au_cids = []
    au_oids = []
    au_conv_ids = []
    au_landing_id = None

    def au_campaign(alias, flows, **cfg_extra):
        config = {"flows": flows, "postbacks": [], "hide_referrer": False,
                  "fallback_url": f"https://example.com/smoke-au-{au_pid}-fb"}
        config.update(cfg_extra)
        payload = {"name": alias, "alias": alias, "type": "campaign", "status": "active",
                   "redirect_mode": "position", "config": config}
        r = s.post(f"{api}/campaigns/", json=payload)
        check(f"audit: create campaign {alias}", r.status_code == 200 and "id" in r.json(),
              r.text[:150])
        cid = r.json().get("id")
        if cid:
            au_cids.append(cid)
        return cid

    # -- 1. forged X-Forwarded-For cannot spoof the stored IP --
    au_alias = f"smoke-au-{au_pid}"
    au_cid = au_campaign(au_alias, [{
        "type": "default", "position": 1, "enabled": True, "schema": "redirect",
        "redirect_url": f"https://example.com/smoke-au-{au_pid}-a", "filters": []}])
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE campaign_id = {au_cid}")
    r = requests.get(f"{BASE}/{au_alias}", verify=not INSECURE, allow_redirects=False,
                     headers={"X-Forwarded-For": "6.6.6.6"})
    check("audit: forged-XFF hit still redirects",
          r.status_code in (301, 302, 307, 308), f"got {r.status_code}")
    requests.get(f"{BASE}/{au_alias}", verify=not INSECURE, allow_redirects=False)
    import time
    time.sleep(1)
    au_ips = ch_query(f"SELECT DISTINCT ip FROM clicks_data WHERE campaign_id = {au_cid}")
    check("audit: forged XFF ignored — nginx-seen IP stored",
          bool(au_ips) and not au_ips.startswith("ERROR") and "6.6.6.6" not in au_ips
          and len(au_ips.split("\n")) == 1, au_ips[:120])

    # direct-to-uvicorn traffic (no nginx) falls back to XFF[0]
    subprocess.run(["docker", "exec", "tracker_frontend", "curl", "-s", "-o", "/dev/null",
                    "-H", "Host: localhost", "-H", "X-Forwarded-For: 7.7.7.7",
                    f"http://127.0.0.1:8000/{au_alias}"], capture_output=True, timeout=30)
    time.sleep(1)
    au_direct = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {au_cid} "
                         f"AND toString(ip) LIKE '7.7.7.%'")
    check("audit: direct-to-uvicorn falls back to X-Forwarded-For",
          au_direct == "1", au_direct[:80])

    # -- 2. click-out URL carries click_id + mapped tokens only (no meta leak) --
    r = s.post(f"{BASE}/landing", files={
        "file": ("index.html", '<html><body><a href="{offer}">continue</a></body></html>',
                 "text/html")},
        data={"name": f"Smoke Audit LP {au_pid}", "site_folder": f"smoke-au-lp-{au_pid}",
              "type": 2})
    check("audit: upload landing", r.status_code == 200 and "id" in r.json(), r.text[:150])
    au_landing_id = r.json().get("id") if r.status_code == 200 else None
    r = s.post(f"{api}/offers/", json={"name": f"Smoke Audit Offer {au_pid}",
                                       "url": "https://example.com/smoke-au-offer?cid={click_id}"})
    check("audit: create offer", r.status_code == 200 and "id" in r.json(), r.text[:150])
    au_offer = r.json().get("id")
    if au_offer:
        au_oids.append(au_offer)
    lp_alias = f"smoke-au-lp-{au_pid}"
    au_campaign(lp_alias, [{
        "type": "default", "position": 1, "enabled": True, "schema": "landing_offer",
        "landing": au_landing_id, "offer": au_offer, "filters": []}],
        paramsIdMapping=[{"parameter": "sub_id_1", "token": "s1"}])
    r = requests.get(f"{BASE}/{lp_alias}", verify=not INSECURE, allow_redirects=False,
                     params={"s1": f"au-tok-{au_pid}", "click_id": f"AUCID-{au_pid}",
                             "junk": "1"},
                     headers={"User-Agent": f"AuditUA/{au_pid}", "Referer": "https://ref.example/x"})
    m = re.search(r'href="(/c/[^"]+)"', r.text) if r.status_code == 200 else None
    click_out = m.group(1) if m else ""
    check("audit: landing served with click-out link", bool(click_out), r.text[:150])
    check("audit: click-out carries click_id", f"click_id=AUCID-{au_pid}" in click_out,
          click_out[:150])
    check("audit: click-out carries mapped token", f"s1=au-tok-{au_pid}" in click_out,
          click_out[:150])
    check("audit: click-out leaks no ip/ua/referrer/junk",
          all(k not in click_out for k in
              ("ip=", "user_agent=", "referrer=", "referer=", "junk=", "language=")),
          click_out[:200])
    if click_out:
        r = requests.get(f"{BASE}{click_out}", verify=not INSECURE, allow_redirects=False)
        loc = r.headers.get("location") or ""
        check("audit: /c redirects (307)", r.status_code in (301, 302, 307, 308),
              f"got {r.status_code}")
        check("audit: /c Location leaks no ip/ua/referrer",
              bool(loc) and all(k not in loc for k in
                  ("ip=", "user_agent=", "referrer=", "referer=", "language=")),
              loc[:200])
        rec = poll_first({"click_id": f"AUCID-{au_pid}"})
        check("audit: /c click recorded", bool(rec), "")
        if rec:
            au_conv_ids.append(rec["id"])

    # -- 9/10. paused campaign does not track; /c validates the offer id --
    r = s.get(f"{api}/campaigns/")
    paused = [c for c in r.json() if c["id"] == au_cid]
    if paused:
        payload = dict(paused[0])
        payload["status"] = "paused"
        r = s.put(f"{api}/campaigns/{au_cid}", json=payload)
        check("audit: pause campaign", r.status_code == 200, r.text[:120])
    r = requests.get(f"{BASE}/{au_alias}", verify=not INSECURE, allow_redirects=False)
    check("audit: paused campaign 404s on tracking hit", r.status_code == 404, str(r.status_code))
    r = requests.get(f"{BASE}/c/{au_alias}/notanumber", verify=not INSECURE,
                     allow_redirects=False)
    check("audit: /c non-numeric offer id -> 400", r.status_code == 400, str(r.status_code))

    # -- 5. redirect_campaign A→B→A loop terminates and tracks inner campaigns --
    ra = f"smoke-au-ra-{au_pid}"
    rb = f"smoke-au-rb-{au_pid}"
    rb_cid = au_campaign(rb, [{
        "type": "default", "position": 1, "enabled": True, "schema": "redirect_campaign",
        "redirect_campaign": 0, "filters": []}])  # patched to ra below
    ra_cid = au_campaign(ra, [{
        "type": "default", "position": 1, "enabled": True, "schema": "redirect_campaign",
        "redirect_campaign": rb_cid, "filters": []}])
    for alias, target in ((rb, ra_cid),):
        r = s.get(f"{api}/campaigns/")
        camp = [c for c in r.json() if c["alias"] == alias][0]
        payload = dict(camp)
        payload["config"]["flows"][0]["redirect_campaign"] = target
        r = s.put(f"{api}/campaigns/{camp['id']}", json=payload)
        check("audit: point redirect_campaign at the other campaign",
              r.status_code == 200, r.text[:120])
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE campaign_id IN ({ra_cid}, {rb_cid})")
    r = requests.get(f"{BASE}/{ra}", verify=not INSECURE, allow_redirects=False)
    check("audit: redirect_campaign loop terminates (404, not RecursionError/500)",
          r.status_code == 404, str(r.status_code))
    time.sleep(1)
    ra_rows = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {ra_cid}")
    rb_rows = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {rb_cid}")
    check("audit: every executed level tracked (2 rows per campaign)",
          ra_rows == "2" and rb_rows == "2", f"ra={ra_rows} rb={rb_rows}")

    # -- 7. opt-out suppresses inserts on /{alias}, /t/collect and /p --
    opt2 = requests.Session()
    opt2.verify = not INSECURE
    opt2.get(f"{BASE}/optout")
    check("audit: opt-out cookie set", "aaa_optout" in opt2.cookies, str(opt2.cookies))
    # re-activate the paused campaign for the opt-out checks
    r = s.get(f"{api}/campaigns/")
    camp = [c for c in r.json() if c["id"] == au_cid][0]
    payload = dict(camp)
    payload["status"] = "active"
    s.put(f"{api}/campaigns/{au_cid}", json=payload)
    opt_before = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {au_cid}")
    r = opt2.get(f"{BASE}/{au_alias}", allow_redirects=False)
    check("audit: opt-out /{alias} still redirects",
          r.status_code in (301, 302, 307, 308), f"got {r.status_code}")
    au_opt_click = f"smoke-au-opt-{au_pid}"
    r = opt2.post(f"{BASE}/t/collect", json={"c": str(au_cid), "click_id": au_opt_click})
    check("audit: opt-out /t/collect still 200 with click_id",
          r.status_code == 200 and r.json().get("click_id") == au_opt_click, r.text[:120])
    check("audit: opt-out /t/collect sets no tracking cookie",
          "set-cookie" not in r.headers, r.headers.get("set-cookie", "")[:80])
    r = opt2.get(f"{BASE}/p/{au_alias}", params={"click_id": au_opt_click, "payout": "1"})
    check("audit: opt-out /p still returns the GIF",
          r.status_code == 200 and r.content == PIXEL_GIF, f"{r.status_code}")
    opt_after = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {au_cid}")
    check("audit: opt-out wrote no ClickHouse rows", opt_before == opt_after,
          f"{opt_before} -> {opt_after}")
    rec = poll_first({"click_id": au_opt_click})
    check("audit: opt-out /p recorded no conversion", rec is None, str(rec))

    # -- 4. concurrent identical postbacks accumulate exactly once --
    import threading
    conc_click = f"smoke-au-conc-{au_pid}"
    requests.get(f"{BASE}/pb?clickid={conc_click}&status=sale&payout=2.5", verify=not INSECURE)
    errors = []

    def fire_pb():
        try:
            requests.get(f"{BASE}/pb?clickid={conc_click}&status=sale&payout=2.5", verify=not INSECURE, timeout=30)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=fire_pb) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("audit: concurrent postbacks all accepted", not errors, str(errors[:2]))
    rec = poll_first({"click_id": conc_click})
    check("audit: concurrent identical postbacks add payout exactly once",
          rec and abs(float(rec["payout"] or 0) - 2.5) < 0.001 and rec["postback_count"] == 5,
          str(rec and (rec.get("payout"), rec.get("postback_count"))))
    if rec:
        au_conv_ids.append(rec["id"])

    # -- 15. /simulate and /_aaa_tracker_debug require an admin session --
    r = requests.post(f"{BASE}/simulate/{au_alias}", json={"count": 1}, verify=not INSECURE)
    check("audit: /simulate without admin cookie -> 401", r.status_code == 401,
          str(r.status_code))
    r = s.post(f"{BASE}/simulate/{au_alias}", json={"count": 1})
    check("audit: /simulate with admin cookie -> 200", r.status_code == 200, r.text[:120])
    r = requests.get(f"{BASE}/_aaa_tracker_debug", verify=not INSECURE)
    check("audit: /_aaa_tracker_debug without admin cookie -> 401",
          r.status_code == 401, str(r.status_code))
    r = s.get(f"{BASE}/_aaa_tracker_debug")
    check("audit: /_aaa_tracker_debug with admin cookie -> 200",
          r.status_code == 200 and isinstance(r.json(), list), r.text[:120])

    print("== Regression: audit fixes cleanup ==")
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE campaign_id IN "
             f"({', '.join(str(c) for c in au_cids)})")
    for conv in sorted(set(au_conv_ids)):
        r = s.delete(f"{api}/reports/{conv}")
        check(f"audit: delete conversion {conv}", r.status_code == 200, r.text[:120])
    for oid in au_oids:
        r = s.delete(f"{api}/offers/{oid}")
        check(f"audit: delete offer {oid}", r.status_code == 200, r.text[:120])
    if au_landing_id:
        r = s.delete(f"{BASE}/landing/{au_landing_id}")
        check("audit: delete landing", r.status_code == 200, r.text[:120])
    for cid_ in au_cids:
        r = s.delete(f"{api}/campaigns/{cid_}")
        check(f"audit: delete campaign {cid_}", r.status_code == 200, r.text[:120])
    r = s.get(f"{api}/campaigns/")
    leftovers = [c for c in r.json() if c["alias"].startswith("smoke-au-")]
    check("audit: no audit campaigns left", not leftovers, str(leftovers))

    print("== Regression: audit fixes wave 2 ==")
    af_pid = os.getpid()
    af_uids = []
    af_cids = []
    af_oids = []
    af_rule_ids = []

    def af_login(username, password="smokepass1"):
        sess = requests.Session()
        sess.verify = not INSECURE
        r = sess.post(f"{api}/login", json={"username": username, "password": password})
        assert r.status_code == 200, f"login {username}: {r.status_code} {r.text[:120]}"
        return sess

    # -- 1. PATCH user: 200 + audit row (was a guaranteed 500 post-commit) --
    af_user = f"smoke-patch-{af_pid}"
    r = s.post(f"{api}/users/", json={"username": af_user, "password": "smokepass1",
                                      "active": True})
    check("patch: user created", r.status_code == 200, r.text[:150])
    af_uid = r.json().get("id")
    if af_uid:
        af_uids.append(af_uid)
    r = s.patch(f"{api}/users/{af_uid}", json={"username": af_user,
                                               "email": f"smoke-patch-{af_pid}@example.com"})
    check("patch: user email change returns 200", r.status_code == 200, r.text[:150])
    r = s.get(f"{api}/audit/", params={"user": af_user, "action": "user_updated"})
    check("patch: audit row written",
          r.status_code == 200 and r.json().get("total", 0) >= 1, r.text[:200])

    # -- 2. write:false user: 403 on archive AND on PUT/DELETE of foreign campaigns --
    r = s.post(f"{api}/campaigns/", json={
        "name": f"smoke-af-victim-{af_pid}", "alias": f"smoke-af-victim-{af_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "config": {"flows": [], "postbacks": [], "hide_referrer": False,
                   "fallback_url": f"https://example.com/smoke-af-{af_pid}-fb"}})
    check("perm2: victim campaign created", r.status_code == 200 and "id" in r.json(),
          r.text[:150])
    victim_id = r.json().get("id")
    if victim_id:
        af_cids.append(victim_id)

    afw_user = f"smoke-afw-{af_pid}"
    r = s.post(f"{api}/users/", json={
        "username": afw_user, "password": "smokepass1",
        "permissions": {"sections": {"campaigns": True}, "write": False}})
    check("perm2: write:false user created", r.status_code == 200, r.text[:150])
    afw_uid = r.json().get("id")
    if afw_uid:
        af_uids.append(afw_uid)
    aw = af_login(afw_user)
    r = aw.post(f"{api}/archive/campaigns/{victim_id}")
    check("perm2: archive denied (403)", r.status_code == 403, str(r.status_code))
    r = aw.post(f"{api}/archive/campaigns/{victim_id}/restore")
    check("perm2: restore denied (403)", r.status_code == 403, str(r.status_code))
    victim = [c for c in (s.get(f"{api}/campaigns/").json() or []) if c["id"] == victim_id]
    if victim:
        payload = dict(victim[0])
        payload["notes"] = "perm2-attempt"
        r = aw.put(f"{api}/campaigns/{victim_id}", json=payload)
        check("perm2: PUT foreign campaign denied (403)", r.status_code == 403,
              str(r.status_code))
    r = aw.delete(f"{api}/campaigns/{victim_id}")
    check("perm2: DELETE foreign campaign denied (403)", r.status_code == 403,
          str(r.status_code))
    r = aw.post(f"{api}/campaigns/{victim_id}/clone")
    check("perm2: clone foreign campaign denied (403)", r.status_code == 403,
          str(r.status_code))

    # campaigns:'own' user CAN mutate (and archive) their own campaign
    afo_user = f"smoke-afo-{af_pid}"
    r = s.post(f"{api}/users/", json={
        "username": afo_user, "password": "smokepass1",
        "permissions": {"sections": {"campaigns": True, "dashboard": True},
                        "write": True, "campaigns": "own"}})
    check("own2: user created", r.status_code == 200, r.text[:150])
    afo_uid = r.json().get("id")
    if afo_uid:
        af_uids.append(afo_uid)
    r = s.post(f"{api}/campaigns/", json={
        "name": f"smoke-af-own-{af_pid}", "alias": f"smoke-af-own-{af_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "config": {"flows": [], "postbacks": [], "hide_referrer": False,
                   "fallback_url": f"https://example.com/smoke-af-own-{af_pid}-fb"}})
    own_cid = r.json().get("id")
    if own_cid:
        af_cids.append(own_cid)
    pg_exec(f"UPDATE campaigns SET owner_id = {afo_uid} WHERE id = {own_cid}")
    ao = af_login(afo_user)
    own_camp = [c for c in (ao.get(f"{api}/campaigns/").json() or []) if c["id"] == own_cid]
    check("own2: sees own campaign", len(own_camp) == 1, str(ao.get(f"{api}/campaigns/").json()[:120]))
    if own_camp:
        payload = dict(own_camp[0])
        payload["notes"] = "own-edit-ok"
        r = ao.put(f"{api}/campaigns/{own_cid}", json=payload)
        check("own2: PUT own campaign allowed", r.status_code == 200, r.text[:150])
    r = ao.post(f"{api}/archive/campaigns/{own_cid}")
    check("own2: archive own campaign allowed", r.status_code == 200, r.text[:150])
    flag = pg_query(f"SELECT archived FROM campaigns WHERE id = {own_cid}")
    check("own2: own campaign archived flag set", flag == "t", flag[:80])
    r = ao.post(f"{api}/archive/campaigns/{own_cid}/restore")
    check("own2: restore own campaign allowed", r.status_code == 200, r.text[:150])
    r = ao.post(f"{api}/archive/campaigns/{victim_id}")
    check("own2: archive foreign campaign denied (403)", r.status_code == 403,
          str(r.status_code))
    r = ao.delete(f"{api}/campaigns/{victim_id}")
    check("own2: DELETE foreign campaign denied (403)", r.status_code == 403,
          str(r.status_code))
    pg_exec(f"UPDATE campaigns SET owner_id = NULL WHERE id = {own_cid}")

    # -- 3. email report HTML: campaign/dimension values + report name escaped --
    email_snippet = r'''
import email_reports
email_reports.get_report_breakdown_multi = lambda *a, **k: [{
    "value": "<script>alert(1)</script>", "parent_key": "",
    "visits": 2, "clicks": 1, "conversions": 0, "cost": 0.0,
    "revenue": 0.0, "profit": 0.0, "cr": 0.0, "epc": 0.0, "roi": None}]
html_out = email_reports.build_saved_report_html(None, {
    "name": "<img src=x onerror=alert(2)>",
    "config": {"dimensions": ["url"], "date_range": ["2026-09-25", "2026-09-25"],
               "columns": ["visits", "revenue"]}})
assert "<script>alert(1)</script>" not in html_out, "dimension value not escaped"
assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_out, "escaped dim missing"
assert "<img src=x" not in html_out, "report name not escaped"
assert "&lt;img src=x" in html_out, "escaped name missing"
print("ESCAPED-OK")
'''
    out = subprocess.run(["docker", "exec", "-i", "tracker_backend", "python", "-"],
                         input=email_snippet, capture_output=True, text=True, timeout=60)
    check("email: report HTML escapes names/dimension values",
          "ESCAPED-OK" in out.stdout, (out.stdout + out.stderr)[:300])

    # -- 4. TOTP token replay rejected; backup code atomic single-use --
    if pyotp is not None:
        bfa_user = f"smoke-bfa-{af_pid}"
        r = s.post(f"{api}/users/", json={"username": bfa_user, "password": "smokepass1",
                                          "active": True})
        bfa_uid = r.json().get("id")
        if bfa_uid:
            af_uids.append(bfa_uid)
        bu = af_login(bfa_user)
        r = bu.post(f"{api}/users/me/totp/setup")
        bfa_secret = r.json().get("secret")
        r = bu.post(f"{api}/users/me/totp/enable",
                    json={"code": pyotp.TOTP(bfa_secret).now()})
        codes = r.json().get("backup_codes") or []
        check("totp2: 2FA enabled with backup codes", r.status_code == 200 and len(codes) == 10,
              r.text[:150])

        # replay: a spent totp_token cannot mint a second session
        r = requests.post(f"{api}/login", json={"username": bfa_user, "password": "smokepass1"},
                          verify=not INSECURE)
        ttok = r.json().get("totp_token")
        r = requests.post(f"{api}/login/totp",
                          json={"totp_token": ttok, "code": pyotp.TOTP(bfa_secret).now()},
                          verify=not INSECURE)
        check("totp2: first use of totp_token succeeds", r.status_code == 200, r.text[:150])
        r = requests.post(f"{api}/login/totp",
                          json={"totp_token": ttok, "code": pyotp.TOTP(bfa_secret).now()},
                          verify=not INSECURE)
        check("totp2: replayed totp_token rejected (401)", r.status_code == 401,
              str(r.status_code))

        # backup code: two concurrent redemptions (distinct tokens) → exactly one wins
        toks = []
        for _ in range(2):
            r = requests.post(f"{api}/login", json={"username": bfa_user, "password": "smokepass1"},
                              verify=not INSECURE)
            toks.append(r.json().get("totp_token"))
        results = []

        def fire_backup(i):
            results.append(requests.post(
                f"{api}/login/totp", json={"totp_token": toks[i], "code": codes[0]},
                verify=not INSECURE, timeout=30).status_code)

        threads = [threading.Thread(target=fire_backup, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("totp2: concurrent backup-code redemptions → exactly one 200",
              sorted(results) == [200, 401], str(results))

    # -- 5. search with partial permissions exposes no admin-only sections --
    r = aw.get(f"{api}/search", params={"q": "smoke"})
    groups = set((r.json().get("groups") or {}).keys()) if r.status_code == 200 else set()
    check("search: write:false user gets no admin sections",
          r.status_code == 200 and "domains" not in groups
          and groups <= {"campaigns", "offers", "landings", "sources", "affiliates",
                         "reports", "conversions"}, str(groups))

    # -- 6. CSV export: formula cells prefixed with ' --
    r = s.post(f"{api}/campaigns/", json={
        "name": "=1+1", "alias": f"smoke-csv-{af_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "config": {"flows": [], "postbacks": [], "hide_referrer": False,
                   "fallback_url": f"https://example.com/smoke-csv-{af_pid}-fb"}})
    csv_cid = r.json().get("id")
    if csv_cid:
        af_cids.append(csv_cid)
    r = s.post(f"{api}/offers/", json={"name": "@cmd|bad", "payout": -5,
                                       "url": "https://example.com/smoke-csv?cid={click_id}"})
    csv_oid = r.json().get("id")
    if csv_oid:
        af_oids.append(csv_oid)
    r = s.get(f"{api}/campaigns/export")
    line = [l for l in r.text.split("\n") if l.startswith(f"{csv_cid},")]
    check("csv: campaign export prefixes '=' cell",
          bool(line) and ",'=1+1," in "," + line[0] + ",",
          (line or [r.text[:200]])[0][:200])
    r = s.get(f"{api}/offers/export")
    line = [l for l in r.text.split("\n") if l.startswith(f"{csv_oid},")]
    check("csv: offer export prefixes '@' and '-' cells",
          bool(line) and ",'@cmd|bad," in "," + line[0] + ","
          and ",'-5.0," in "," + line[0] + ",", (line or [r.text[:200]])[0][:200])

    # -- 7. settings save merges top-level keys (no read-modify-write clobber) --
    r = s.post(f"{api}/settings/", json={"settings": {"__smoke_merge_a": 1}})
    check("merge: first key saved", r.status_code == 200, r.text[:120])
    r = s.post(f"{api}/settings/", json={"settings": {"__smoke_merge_b": 2}})
    check("merge: second key saved", r.status_code == 200, r.text[:120])
    r = s.get(f"{api}/settings/")
    merged_cfg = r.json().get("settings") or {}
    check("merge: both keys present after rapid saves",
          merged_cfg.get("__smoke_merge_a") == 1 and merged_cfg.get("__smoke_merge_b") == 2,
          str({k: merged_cfg.get(k) for k in ("__smoke_merge_a", "__smoke_merge_b")}))

    # -- 8. monitor: URL with {click_id} macro still checked (no 5xx false positive) --
    r = s.post(f"{api}/offers/", json={"name": f"Smoke Macro Offer {af_pid}",
                                       "url": "http://127.0.0.1:1/dead?cid={click_id}"})
    macro_oid = r.json().get("id")
    if macro_oid:
        af_oids.append(macro_oid)
    r = s.post(f"{api}/campaigns/", json={
        "name": f"smoke-af-macro-{af_pid}", "alias": f"smoke-af-macro-{af_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "config": {"flows": [{"type": "default", "position": 1, "enabled": True,
                              "schema": "direct", "offer": macro_oid, "filters": []}],
                   "postbacks": [], "hide_referrer": False,
                   "fallback_url": f"https://example.com/smoke-af-macro-{af_pid}-fb"}})
    macro_cid = r.json().get("id")
    if macro_cid:
        af_cids.append(macro_cid)
    r = s.post(f"{api}/monitor/check-now")
    check("monitor: macro cycle runs without crash", r.status_code == 200, r.text[:200])
    r = s.get(f"{api}/monitor/status")
    items = [i for i in r.json().get("items", []) if i.get("campaign_id") == macro_cid]
    check("monitor: macro URL detected dead",
          bool(items) and items[0]["status"] == "dead" and "{click_id}" in items[0]["url"],
          str(items[:1]))

    # -- 9. rule update coerces string campaign_id; manual execute doesn't persist matched --
    r = s.post(f"{api}/rules/", json={
        "name": f"smoke-af-rule-{af_pid}", "enabled": True, "campaign_id": victim_id,
        "conditions": [{"metric": "conversions", "period_hours": 1,
                        "comparator": ">", "value": 99999999}],
        "action": "alert_telegram"})
    af_rule = r.json().get("rule", {}).get("id") if r.status_code == 200 else None
    if af_rule:
        af_rule_ids.append(af_rule)
    check("rules2: rule created", bool(af_rule), r.text[:200])
    if af_rule:
        r = s.patch(f"{api}/rules/{af_rule}", json={"campaign_id": str(victim_id)})
        check("rules2: string campaign_id accepted + coerced",
              r.status_code == 200 and r.json().get("rule", {}).get("campaign_id") == victim_id,
              r.text[:200])
        r = s.post(f"{api}/rules/{af_rule}/execute")
        check("rules2: manual execute runs", r.status_code == 200, r.text[:200])
        r = s.get(f"{api}/rules/")
        rule = [x for x in r.json().get("rules", []) if x.get("id") == af_rule]
        check("rules2: manual execute does not persist last_result",
              bool(rule) and rule[0].get("last_result") is None, str(rule[:1]))

    print("== Regression: audit fixes wave 2 cleanup ==")
    if "__smoke_merge_a" in merged_cfg or "__smoke_merge_b" in merged_cfg:
        r = s.get(f"{api}/settings/")
        cfg_clean = r.json().get("settings") or {}
        cfg_clean["__smoke_merge_a"] = None
        cfg_clean["__smoke_merge_b"] = None
        r = s.post(f"{api}/settings/", json={"settings": cfg_clean})
        check("merge: test keys removed", r.status_code == 200, r.text[:120])
    pg_exec("DELETE FROM monitor_state WHERE url LIKE 'http://127.0.0.1:1%'")
    for rid in af_rule_ids:
        r = s.delete(f"{api}/rules/{rid}")
        check(f"rules2: delete rule {rid}", r.status_code == 200, r.text[:120])
    for oid in af_oids:
        r = s.delete(f"{api}/offers/{oid}")
        check(f"wave2: delete offer {oid}", r.status_code == 200, r.text[:120])
    for cid_ in af_cids:
        r = s.delete(f"{api}/campaigns/{cid_}")
        check(f"wave2: delete campaign {cid_}", r.status_code == 200, r.text[:120])
    for uid in af_uids:
        r = s.delete(f"{api}/users/{uid}")
        check(f"wave2: delete user {uid}", r.status_code == 200, r.text[:120])
    r = s.get(f"{api}/rules/")
    check("wave2: no smoke rules left",
          all("smoke" not in (x.get("name") or "") for x in r.json().get("rules", [])),
          r.text[:150])
    r = s.get(f"{api}/users/")
    leftovers = [u for u in r.json() if (u["username"] or "").startswith("smoke-")
                 and any(u["username"].startswith(p) for p in
                         ("smoke-patch-", "smoke-afw-", "smoke-afo-", "smoke-bfa-"))]
    check("wave2: no wave-2 users left", not leftovers, str(leftovers))

    print("== Flow actions ==")
    act_pid = os.getpid()
    act_alias = f"smoke-action-{act_pid}"
    act_payload = {
        "name": "Smoke Action Campaign", "alias": act_alias, "type": "campaign",
        "status": "active", "redirect_mode": "position",
        "config": {"flows": [{
            "type": "default", "position": 1, "enabled": True, "schema": "redirect",
            "redirect_url": "https://example.com/smoke-action-dest?a=1&b=2",
            "filters": [],
        }], "postbacks": [], "fallback_url": "", "hide_referrer": False},
    }
    r = s.post(f"{api}/campaigns/", json=act_payload)
    check("actions: create campaign", r.status_code == 200 and "id" in r.json(), r.text[:200])
    act_cid = r.json().get("id")
    act_offer_id = None
    act_landing_id = None

    base_flow = {"type": "default", "position": 1, "enabled": True, "schema": "redirect",
                 "redirect_url": "https://example.com/smoke-action-dest?a=1&b=2", "filters": []}

    def put_action_flow(flow):
        act_payload["config"]["flows"] = [flow]
        return s.put(f"{api}/campaigns/{act_cid}", json=act_payload)

    # default (absent action) stays a 302 redirect
    r = requests.get(f"{BASE}/{act_alias}", verify=not INSECURE, allow_redirects=False)
    check("actions: absent action redirects (302)", r.status_code in (301, 302, 307, 308)
          and "smoke-action-dest" in (r.headers.get("location") or ""), f"got {r.status_code}")

    # iframe — full-viewport wrapper page
    r = put_action_flow({**base_flow, "action": "iframe"})
    check("actions: save iframe flow", r.status_code == 200, r.text[:150])
    r = requests.get(f"{BASE}/{act_alias}", verify=not INSECURE, allow_redirects=False)
    check("actions: iframe serves wrapper HTML", r.status_code == 200
          and "<iframe" in r.text
          and 'src="https://example.com/smoke-action-dest?a=1&amp;b=2"' in r.text
          and "position:fixed" in r.text, f"got {r.status_code}")

    # form_post — auto-submitting form, URL query submitted as hidden fields
    r = put_action_flow({**base_flow, "action": "form_post"})
    r = requests.get(f"{BASE}/{act_alias}", verify=not INSECURE, allow_redirects=False)
    check("actions: form_post serves auto-submit form", r.status_code == 200
          and '<form method="post" action="https://example.com/smoke-action-dest?a=1&amp;b=2"' in r.text
          and 'name="a" value="1"' in r.text and "document.forms[0].submit()" in r.text,
          f"got {r.status_code}")

    # curl — server-side fetch of an operator-configured URL (internal docker
    # network address reachable from the frontend container)
    r = put_action_flow({**base_flow, "action": "curl",
                         "redirect_url": "http://tracker_nginx/backend/health"})
    r = requests.get(f"{BASE}/{act_alias}", verify=not INSECURE, allow_redirects=False)
    check("actions: curl serves fetched content", r.status_code == 200
          and "text/html" in r.headers.get("content-type", "")
          and "AAA TRACKER" in r.text, f"got {r.status_code} {r.headers.get('content-type')}")

    # curl — fetch failure falls back to a normal redirect
    r = put_action_flow({**base_flow, "action": "curl", "redirect_url": "http://127.0.0.1:1/nope"})
    r = requests.get(f"{BASE}/{act_alias}", verify=not INSECURE, allow_redirects=False)
    check("actions: curl failure falls back to redirect",
          r.status_code in (301, 302, 307, 308) and "127.0.0.1:1" in (r.headers.get("location") or ""),
          f"got {r.status_code}")

    # show_html — custom HTML served verbatim
    r = put_action_flow({**base_flow, "action": "show_html", "html": "<h1>SMOKE-ACTION-HTML</h1>"})
    r = requests.get(f"{BASE}/{act_alias}", verify=not INSECURE, allow_redirects=False)
    check("actions: show_html serves custom HTML", r.status_code == 200
          and "<h1>SMOKE-ACTION-HTML</h1>" in r.text, f"got {r.status_code}")

    # none — 200 empty body
    r = put_action_flow({**base_flow, "action": "none"})
    r = requests.get(f"{BASE}/{act_alias}", verify=not INSECURE, allow_redirects=False)
    check("actions: none serves 200 empty", r.status_code == 200 and r.text == "",
          f"got {r.status_code} {r.text[:60]!r}")

    # click-out: landing_offer flow with action=iframe — applies at /c click-out
    r = s.post(f"{BASE}/landing", files={
        "file": ("index.html", '<html><body><a href="{offer}">continue</a></body></html>',
                 "text/html")},
        data={"name": f"Smoke Action LP {act_pid}", "site_folder": f"smoke-act-lp-{act_pid}",
              "type": 2})
    check("actions: upload landing", r.status_code == 200 and "id" in r.json(), r.text[:150])
    act_landing_id = r.json().get("id")
    r = s.post(f"{api}/offers/", json={"name": f"Smoke Action Offer {act_pid}",
                                       "url": "https://example.com/smoke-act-offer?cid={click_id}"})
    check("actions: create offer", r.status_code == 200 and "id" in r.json(), r.text[:150])
    act_offer_id = r.json().get("id")
    act_payload["config"]["stickiness"] = True
    act_payload["config"]["flows"] = [{
        "type": "default", "position": 1, "enabled": True, "schema": "landing_offer",
        "landing": act_landing_id, "offer": act_offer_id, "action": "iframe", "filters": []}]
    r = s.put(f"{api}/campaigns/{act_cid}", json=act_payload)
    check("actions: save landing_offer iframe flow", r.status_code == 200, r.text[:150])
    act_sess = requests.Session()
    act_sess.verify = not INSECURE
    r = act_sess.get(f"{BASE}/{act_alias}", allow_redirects=False)
    m = re.search(r'href="(/c/[^"]+)"', r.text) if r.status_code == 200 else None
    check("actions: landing served with click-out link", bool(m), r.text[:150])
    if m:
        r = act_sess.get(f"{BASE}{m.group(1)}", allow_redirects=False)
        check("actions: click-out honors iframe action", r.status_code == 200
              and "<iframe" in r.text and "example.com/smoke-act-offer" in r.text
              and "click_id=" in r.text, f"got {r.status_code}")
    act_payload["config"].pop("stickiness", None)

    # click-api decision mirrors the action (decision-only path)
    r = put_action_flow(base_flow)
    r = requests.post(f"{BASE}/click-api/{act_alias}", verify=not INSECURE, json={
        "ip": "203.0.113.50", "user_agent": "SmokeAction/1.0 Chrome/120"})
    check("actions: click-api default action is redirect", r.status_code == 200
          and (r.json().get("decision") or {}).get("action") == "redirect", r.text[:200])
    r = put_action_flow({**base_flow, "action": "iframe"})
    r = requests.post(f"{BASE}/click-api/{act_alias}", verify=not INSECURE, json={
        "ip": "203.0.113.51", "user_agent": "SmokeAction/1.0 Chrome/120"})
    check("actions: click-api decision carries action", r.status_code == 200
          and (r.json().get("decision") or {}).get("action") == "iframe", r.text[:200])

    print("== Flow actions cleanup ==")
    if act_cid:
        r = s.delete(f"{api}/campaigns/{act_cid}")
        check("actions: delete campaign", r.status_code == 200, r.text[:120])
    if act_offer_id:
        r = s.delete(f"{api}/offers/{act_offer_id}")
        check("actions: delete offer", r.status_code == 200, r.text[:120])
    if act_landing_id:
        r = s.delete(f"{BASE}/landing/{act_landing_id}")
        check("actions: delete landing", r.status_code in (200, 204), r.text[:120])

    print("== Funnel (G10) ==")
    fn_pid = os.getpid()
    fn_alias = f"smoke-funnel-{fn_pid}"
    fn_landing_ids = []
    fn_offer_id = None
    fn_cid = None

    for marker, label in (("SMOKE-FUNNEL-0", "A"), ("SMOKE-FUNNEL-1", "B")):
        r = s.post(f"{BASE}/landing", files={
            "file": ("index.html", f'<html><body>{marker}<a href="{{offer}}">continue</a></body></html>',
                     "text/html")},
            data={"name": f"Smoke Funnel LP {label} {fn_pid}", "site_folder": f"smoke-fn-lp-{label.lower()}-{fn_pid}",
                  "type": 2})
        check(f"funnel: upload landing {label}", r.status_code == 200 and "id" in r.json(), r.text[:150])
        lid = r.json().get("id")
        if lid:
            fn_landing_ids.append(lid)
    r = s.post(f"{api}/offers/", json={"name": f"Smoke Funnel Offer {fn_pid}",
                                       "url": f"https://example.com/smoke-fn-offer-{fn_pid}?cid={{click_id}}"})
    check("funnel: create offer", r.status_code == 200 and "id" in r.json(), r.text[:150])
    fn_offer_id = r.json().get("id")

    fn_payload = None
    if len(fn_landing_ids) == 2 and fn_offer_id:
        fn_payload = {
            "name": "Smoke Funnel Campaign", "alias": fn_alias, "type": "campaign",
            "status": "active", "redirect_mode": "position",
            "config": {"flows": [], "postbacks": [], "hide_referrer": False,
                       "funnel": {"enabled": True, "steps": [
                           {"name": "Quiz 1", "landing": fn_landing_ids[0], "offers": [fn_offer_id],
                            "schema": "landing_offer"},
                           {"name": "Offer Page", "landing": fn_landing_ids[1], "offers": [fn_offer_id],
                            "schema": "landing_offer"}]}}}
        r = s.post(f"{api}/campaigns/", json=fn_payload)
        check("funnel: create campaign", r.status_code == 200 and "id" in r.json(), r.text[:200])
        fn_cid = r.json().get("id")

    fn_sess = requests.Session()
    fn_sess.verify = not INSECURE

    # (a) first hit serves step 0
    r = fn_sess.get(f"{BASE}/{fn_alias}", allow_redirects=False)
    check("funnel: first hit serves step 0 landing", r.status_code == 200
          and "SMOKE-FUNNEL-0" in r.text, f"got {r.status_code}")
    check("funnel: step bind cookie issued", fn_sess.cookies.get("aaa_bind") is not None)

    # (b) click-out → cookie step advances
    m = re.search(r'href="(/c/[^"]+)"', r.text) if r.status_code == 200 else None
    check("funnel: step 0 landing has click-out link", bool(m), r.text[:150])
    fn_click_ids = []
    if m:
        r = fn_sess.get(f"{BASE}{m.group(1)}", allow_redirects=False)
        loc = r.headers.get("location") or ""
        check("funnel: click-out redirects to offer", r.status_code in (301, 302, 307, 308)
              and f"smoke-fn-offer-{fn_pid}" in loc, f"got {r.status_code} {loc[:80]}")
        cm = re.search(r"click_id=([0-9a-fA-F-]{36})", loc)
        if cm:
            fn_click_ids.append(cm.group(1))

    # (c) second hit serves step 1
    r = fn_sess.get(f"{BASE}/{fn_alias}", allow_redirects=False)
    check("funnel: second hit serves step 1 landing", r.status_code == 200
          and "SMOKE-FUNNEL-1" in r.text, f"got {r.status_code}")

    # click-out at the last step keeps the visitor on the last step
    m = re.search(r'href="(/c/[^"]+)"', r.text) if r.status_code == 200 else None
    if m:
        r = fn_sess.get(f"{BASE}{m.group(1)}", allow_redirects=False)
        loc = r.headers.get("location") or ""
        cm = re.search(r"click_id=([0-9a-fA-F-]{36})", loc)
        if cm:
            fn_click_ids.append(cm.group(1))
    r = fn_sess.get(f"{BASE}/{fn_alias}", allow_redirects=False)
    check("funnel: repeat visit past last step re-sees last step", r.status_code == 200
          and "SMOKE-FUNNEL-1" in r.text, f"got {r.status_code}")

    # (d) ClickHouse rows carry the step in flow_index
    if fn_cid:
        out = ch_query(f"SELECT DISTINCT flow_index FROM clicks_data WHERE campaign_id = {fn_cid} ORDER BY flow_index")
        check("funnel: CH flow_index stamped with steps 0 and 1",
              set(out.split()) == {"0", "1"}, out[:80])

    # (e) conversions_data rows carry funnel_step
    out = pg_exec_out(f"SELECT string_agg(DISTINCT funnel_step::text, ',' ORDER BY funnel_step::text) "
                      f"FROM conversions_data WHERE campaign_id = {fn_cid}").strip()
    check("funnel: conversions_data carries funnel_step 0 and 1", out == "0,1", out[:80])

    # (f) click-api decision mirrors the funnel
    r = requests.post(f"{BASE}/click-api/{fn_alias}", verify=not INSECURE, json={
        "ip": "203.0.113.99", "user_agent": "SmokeFunnel/1.0 Chrome/120"})
    fn_dec = (r.json().get("decision") or {}) if r.status_code == 200 else {}
    check("funnel: click-api decision returns funnel step 0", r.status_code == 200
          and fn_dec.get("funnel") is True and fn_dec.get("step") == 0
          and fn_dec.get("landing_id") == fn_landing_ids[0]
          and fn_dec.get("landing_url") == f"/l/smoke-fn-lp-a-{fn_pid}", r.text[:200])

    # (g) postback on the step-1 click marks a conversion; funnel report aggregates per step
    if len(fn_click_ids) >= 2:
        r = requests.get(f"{BASE}/pb?clickid={fn_click_ids[1]}&status=sale&payout=9.5", verify=not INSECURE)
        check("funnel: postback on step-1 click accepted", r.status_code == 200, r.text[:150])
        r = s.get(f"{api}/reports/funnel/{fn_cid}")
        steps = (r.json().get("steps") or []) if r.status_code == 200 else []
        check("funnel: funnel report returns both steps", r.status_code == 200 and len(steps) == 2,
              r.text[:200])
        if len(steps) == 2:
            check("funnel: step 0 visits and click-outs counted",
                  steps[0]["visits"] >= 1 and steps[0]["clickouts"] >= 1, str(steps[0])[:150])
            check("funnel: step 1 conversion and revenue after postback",
                  steps[1]["conversions"] >= 1 and steps[1]["revenue"] > 0, str(steps[1])[:150])
            check("funnel: step 1 drop-off reported", steps[1]["drop_off_pct"] is not None,
                  str(steps[1])[:150])
            check("funnel: cumulative conversions accumulate",
                  steps[1]["cumulative_conversions"] >= steps[0]["cumulative_conversions"],
                  str(steps[1])[:150])
    # (i) non-funnel campaigns are rejected by the funnel report
    r = s.get(f"{api}/reports/funnel/{cid}")
    check("funnel: non-funnel campaign report 404", r.status_code == 404, str(r.status_code))
    r = s.get(f"{api}/reports/funnel/99999999")
    check("funnel: unknown campaign report 404", r.status_code == 404, str(r.status_code))

    # (h) a funnel config edit invalidates the bind cookie (routing_hash) → step resets to 0
    if fn_payload and fn_cid:
        fn_payload["config"]["funnel"]["steps"][1]["name"] = "Offer Page v2"
        r = s.put(f"{api}/campaigns/{fn_cid}", json=fn_payload)
        check("funnel: config edit saved", r.status_code == 200, r.text[:150])
        r = fn_sess.get(f"{BASE}/{fn_alias}", allow_redirects=False)
        check("funnel: config edit resets visitor to step 0", r.status_code == 200
              and "SMOKE-FUNNEL-0" in r.text, f"got {r.status_code}")

    print("== Funnel cleanup ==")
    if fn_cid:
        r = s.delete(f"{api}/campaigns/{fn_cid}")
        check("funnel: delete campaign", r.status_code == 200, r.text[:120])
        leftover = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {fn_cid}")
        check("funnel: CH clicks purged on delete", leftover == "0", leftover[:80])
    if fn_offer_id:
        r = s.delete(f"{api}/offers/{fn_offer_id}")
        check("funnel: delete offer", r.status_code == 200, r.text[:120])
    for lid in fn_landing_ids:
        r = s.delete(f"{BASE}/landing/{lid}")
        check(f"funnel: delete landing {lid}", r.status_code in (200, 204), r.text[:120])

    print("== Blacklists (G44) ==")
    # Traffic-quality blacklists: named value lists on a field, global or
    # per-campaign, action mark (tracked, is_bot) or block (untracked 404).
    bl_alias = f"smoke-bl-{os.getpid()}"
    bl_other_alias = f"smoke-bl-other-{os.getpid()}"
    bl_payload = {"name": bl_alias, "alias": bl_alias, "type": "campaign",
                  "status": "active", "redirect_mode": "position",
                  "config": {"flows": [{
                      "type": "default", "position": 1, "enabled": True, "schema": "redirect",
                      "redirect_url": f"https://example.com/smoke-bl-{os.getpid()}-a",
                      "filters": []}], "postbacks": [], "hide_referrer": False,
                      "fallback_url": ""}}
    r = s.post(f"{api}/campaigns/", json=bl_payload)
    check("bl: create campaign", r.status_code == 200 and "id" in r.json(), r.text[:200])
    bl_cid = r.json().get("id")
    bl2_payload = dict(bl_payload, name=bl_other_alias, alias=bl_other_alias)
    r = s.post(f"{api}/campaigns/", json=bl2_payload)
    bl_other_cid = r.json().get("id") if r.status_code == 200 else None

    # CRUD + validation
    r = s.get(f"{api}/fraud/blacklists")
    check("bl: list 200", r.status_code == 200
          and isinstance(r.json().get("blacklists"), list), r.text[:150])
    bl_mark = {"name": f"bl-mark-{os.getpid()}", "field": "sub_id_1",
               "values": [f"bl-hit-{os.getpid()}"], "scope": "global",
               "action": "mark", "enabled": True}
    r = s.post(f"{api}/fraud/blacklists", json=bl_mark)
    check("bl: create mark list", r.status_code == 200 and r.json().get("id"),
          r.text[:200])
    bl_mark_id = (r.json() or {}).get("id")
    bl_block = {"name": f"bl-block-{os.getpid()}", "field": "sub_id_2",
                "values": [f"bl-block-{os.getpid()}"], "scope": "campaign",
                "campaign_id": bl_cid, "action": "block", "enabled": True}
    r = s.post(f"{api}/fraud/blacklists", json=bl_block)
    check("bl: create campaign-scoped block list",
          r.status_code == 200 and r.json().get("campaign_id") == bl_cid, r.text[:200])
    bl_block_id = (r.json() or {}).get("id")
    bl_ip = {"name": f"bl-ip-{os.getpid()}", "field": "ip",
             "values": ["198.51.100.0/24"], "scope": "global", "action": "block",
             "enabled": True}
    r = s.post(f"{api}/fraud/blacklists", json=bl_ip)
    check("bl: create ip CIDR block list", r.status_code == 200, r.text[:200])
    bl_ip_id = (r.json() or {}).get("id")

    for bad, why in [
            (dict(bl_mark, id=None, field="bogus"), "bad field"),
            (dict(bl_mark, id=None, values=[]), "empty values"),
            (dict(bl_mark, id=None, action="nuke"), "bad action"),
            (dict(bl_mark, id=None, scope="campaign", campaign_id=None),
             "campaign scope without id"),
            (dict(bl_mark, id=None, values=["x"] * 5001), "too many values"),
            (dict(bl_mark, id=None, field="ip", values=["not-an-ip"]), "bad ip")]:
        bad.pop("id", None)
        r = s.post(f"{api}/fraud/blacklists", json=bad)
        check(f"bl: validation rejects {why} (400)",
              r.status_code == 400, f"{r.status_code} {r.text[:100]}")

    r = s.put(f"{api}/fraud/blacklists/{bl_mark_id}",
              json={"enabled": False})
    check("bl: toggle enabled via PUT", r.status_code == 200
          and r.json().get("enabled") is False, r.text[:150])
    r = s.put(f"{api}/fraud/blacklists/{bl_mark_id}", json={"enabled": True})
    check("bl: re-enabled", r.status_code == 200 and r.json().get("enabled") is True,
          r.text[:150])

    # Tracking plane — settings cache is 30s, wait out the TTL once for all lists
    settle_settings_cache()
    bl_ua = f"SmokeBL/{os.getpid()}"
    r = requests.get(f"{BASE}/{bl_alias}", params={"sub_id_1": f"bl-hit-{os.getpid()}"},
                     verify=not INSECURE, allow_redirects=False, headers={"User-Agent": bl_ua})
    check("bl: marked hit still redirects",
          r.status_code in (301, 302, 307, 308), str(r.status_code))
    r = requests.get(f"{BASE}/{bl_alias}", params={"sub_id_1": f"bl-miss-{os.getpid()}"},
                     verify=not INSECURE, allow_redirects=False, headers={"User-Agent": bl_ua})
    check("bl: non-matching hit redirects",
          r.status_code in (301, 302, 307, 308), str(r.status_code))
    import time as _time
    _time.sleep(1)
    rows = ch_query(f"SELECT sub_id_1, is_bot FROM clicks_data "
                    f"WHERE campaign_id = {bl_cid} ORDER BY received_at")
    lines = [l.split("\t") for l in rows.splitlines() if l]
    mark_row = [l for l in lines if l[0] == f"bl-hit-{os.getpid()}"]
    miss_row = [l for l in lines if l[0] == f"bl-miss-{os.getpid()}"]
    check("bl: matching param tracked as bot",
          bool(mark_row) and mark_row[0][1] == "true", rows[:120])
    check("bl: non-matching param unaffected",
          bool(miss_row) and miss_row[0][1] == "false", rows[:120])

    before = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {bl_cid}")
    r = requests.get(f"{BASE}/{bl_alias}", params={"sub_id_2": f"bl-block-{os.getpid()}"},
                     verify=not INSECURE, allow_redirects=False, headers={"User-Agent": bl_ua})
    check("bl: campaign-scoped block serves 404", r.status_code == 404, str(r.status_code))
    after = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {bl_cid}")
    check("bl: blocked hit is untracked", before == after, f"{before} -> {after}")

    if bl_other_cid:
        r = requests.get(f"{BASE}/{bl_other_alias}",
                         params={"sub_id_2": f"bl-block-{os.getpid()}"},
                         verify=not INSECURE, allow_redirects=False,
                         headers={"User-Agent": bl_ua})
        check("bl: campaign-scoped list does not block other campaigns",
              r.status_code in (301, 302, 307, 308), str(r.status_code))

    # IP CIDR block via direct uvicorn (nginx overwrites X-Real-IP with the
    # peer address; X-Forwarded-For reaches uvicorn unmodified)
    r = subprocess.run(["docker", "exec", "tracker_frontend", "curl", "-s", "-o", "/dev/null",
                        "-w", "%{http_code}", "-H", "Host: localhost",
                        "-H", "X-Forwarded-For: 198.51.100.77", "-A", bl_ua,
                        f"http://127.0.0.1:8000/{bl_alias}"],
                       capture_output=True, text=True, timeout=30)
    check("bl: matching CIDR IP blocked 404", r.stdout.strip() == "404", r.stdout[:50])
    r = subprocess.run(["docker", "exec", "tracker_frontend", "curl", "-s", "-o", "/dev/null",
                        "-w", "%{http_code}", "-H", "Host: localhost",
                        "-H", "X-Forwarded-For: 8.8.8.8", "-A", bl_ua,
                        f"http://127.0.0.1:8000/{bl_alias}"],
                       capture_output=True, text=True, timeout=30)
    check("bl: non-matching public IP unaffected", r.stdout.strip() in
          ("301", "302", "307", "308"), r.stdout[:50])

    print("== Blacklists cleanup ==")
    for bid, label in [(bl_mark_id, "mark"), (bl_block_id, "block"),
                       (bl_ip_id, "ip")]:
        if bid:
            r = s.delete(f"{api}/fraud/blacklists/{bid}")
            check(f"bl: delete {label} list", r.status_code == 200, r.text[:120])
    r = s.delete(f"{api}/fraud/blacklists/does-not-exist")
    check("bl: delete unknown 404", r.status_code == 404, str(r.status_code))
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE campaign_id IN ({bl_cid}, {bl_other_cid})")
    if bl_cid:
        r = s.delete(f"{api}/campaigns/{bl_cid}")
        check("bl: delete campaign", r.status_code == 200, r.text[:120])
    if bl_other_cid:
        r = s.delete(f"{api}/campaigns/{bl_other_cid}")
        check("bl: delete second campaign", r.status_code == 200, r.text[:120])

    print("== Status (G78) ==")
    r = s.get(f"{api}/status")
    body = r.json() if r.status_code == 200 else {}
    check("status: 200 with version + sections",
          r.status_code == 200 and body.get("version") == "0.9.0"
          and all(k in body for k in ("postgres", "clickhouse", "loops",
                                      "clicks_24h", "conversions_24h")), r.text[:200])
    check("status: both databases up with latency",
          (body.get("postgres") or {}).get("ok") is True
          and (body.get("clickhouse") or {}).get("ok") is True
          and isinstance(body.get("postgres", {}).get("latency_ms"), (int, float)),
          r.text[:200])
    check("status: loop timestamps section shaped",
          set((body.get("loops") or {}).keys()) == {"monitor", "rules", "optimizer",
                                                   "meta_ads"},
          str((body.get("loops") or {}).keys()))
    r = requests.get(f"{api}/status", verify=not INSECURE)
    check("status: unauthenticated 401", r.status_code == 401, str(r.status_code))
    r = s.get(f"{api}/auth-status")
    check("status: legacy auth check moved to /auth-status",
          r.status_code == 200 and r.json().get("authenticated") is True, r.text[:120])

    print("== Lander grabber (G73) ==")
    # Hermetic target: a fixture folder served by nginx at /l/<folder>/ on the
    # container network. The SSRF guard rejects internal hosts unless the
    # caller explicitly passes allow_private (own-infrastructure grabs).
    landings_dir = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "frontend", "landings"))
    grab_src_folder = "smoke-grab-src"
    grab_src_path = os.path.join(landings_dir, grab_src_folder, "index.html")
    grab_folder = f"smoke_grab_{os.getpid()}"
    grab_id = None
    os.makedirs(os.path.dirname(grab_src_path), exist_ok=True)
    with open(grab_src_path, "w", encoding="utf-8") as f:
        f.write("<!DOCTYPE html><html><head><title>Smoke Grab Fixture</title>"
                '<link rel="stylesheet" href="assets/style.css"></head><body>'
                '<img src="img/pic.png" srcset="img/pic.png 1x, /img/pic2.png 2x">'
                '<a href="https://example.com/abs">abs</a>'
                '<a href="page2.html">rel</a>'
                "</body></html>")
    try:
        r = s.post(f"{BASE}/landing/grab",
                   data={"url": f"http://tracker_nginx/l/{grab_src_folder}/",
                         "folder": grab_folder, "allow_private": "true"})
        check("grab: fixture grabbed 200",
              r.status_code == 200 and r.json().get("site") == grab_folder,
              f"{r.status_code} {r.text[:150]}")
        grab_id = (r.json() or {}).get("id")
        check("grab: name from <title>",
              bool(grab_id) and r.json().get("name") == "Smoke Grab Fixture",
              r.text[:150])

        grabbed_path = os.path.join(landings_dir, grab_folder, "index.html")
        grabbed = ""
        if os.path.exists(grabbed_path):
            with open(grabbed_path, encoding="utf-8") as f:
                grabbed = f.read()
        check("grab: relative assets rewritten absolute",
              f"http://tracker_nginx/l/{grab_src_folder}/img/pic.png" in grabbed
              and f"http://tracker_nginx/l/{grab_src_folder}/assets/style.css" in grabbed
              and "http://tracker_nginx/img/pic2.png" in grabbed, grabbed[:200])
        check("grab: absolute URL left untouched",
              'href="https://example.com/abs"' in grabbed, grabbed[:200])

        r = s.get(f"{BASE}/landings")
        rows = [l for l in (r.json() if r.status_code == 200 else [])
                if l.get("folder") == grab_folder]
        check("grab: landing row exists as local_file",
              r.status_code == 200 and len(rows) == 1
              and rows[0].get("type") == "local_file", r.text[:200])

        # SSRF guard + validation
        r = s.post(f"{BASE}/landing/grab", data={"url": "http://localhost/",
                                                 "folder": f"{grab_folder}_x1"})
        check("grab: localhost rejected 400", r.status_code == 400, f"{r.status_code}")
        r = s.post(f"{BASE}/landing/grab",
                   data={"url": f"http://tracker_nginx/l/{grab_src_folder}/",
                         "folder": f"{grab_folder}_x2"})
        check("grab: internal host rejected without opt-in", r.status_code == 400,
              f"{r.status_code}")
        r = s.post(f"{BASE}/landing/grab", data={"url": "ftp://example.com/x",
                                                 "folder": f"{grab_folder}_x3"})
        check("grab: non-http scheme rejected 400", r.status_code == 400,
              f"{r.status_code}")
        r = s.post(f"{BASE}/landing/grab",
                   data={"url": f"http://tracker_nginx/l/{grab_src_folder}/",
                         "folder": "Bad-Folder!", "allow_private": "true"})
        check("grab: invalid folder name rejected 400", r.status_code == 400,
              f"{r.status_code}")
    finally:
        if grab_id:
            r = s.delete(f"{BASE}/landing/{grab_id}")
            check("grab: delete created landing", r.status_code in (200, 204), r.text[:120])
        shutil.rmtree(os.path.join(landings_dir, grab_src_folder), ignore_errors=True)
        shutil.rmtree(os.path.join(landings_dir, grab_folder), ignore_errors=True)

    print("== Insights (G56) ==")
    ins_pid = os.getpid()
    r = s.post(f"{api}/campaigns/", json={
        "name": f"Smoke Insights {ins_pid}", "alias": f"smoke-insights-{ins_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "config": {"flows": [{
            "type": "default", "position": 1, "enabled": True, "schema": "redirect",
            "redirect_url": "https://example.com/smoke-insights", "filters": [],
        }], "postbacks": [], "fallback_url": "", "hide_referrer": False}})
    check("insights: create campaign", r.status_code == 200 and "id" in r.json(), r.text[:150])
    ins_cid = r.json().get("id")
    # baseline: 7 full days, 70 click-outs/day, $0.30 cost each
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, country, cost) "
        f"SELECT now() - toIntervalHour(24) - toIntervalDay(number % 7) - toIntervalHour(number % 20), "
        f"{ins_cid}, true, '', 'smoke-ins-base-{ins_pid}-' || toString(number), 'US', 0.3 FROM numbers(490)")
    # current 24h: 40 human click-outs + 60 bots, zero conversions today
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, country, cost, is_bot) "
        f"SELECT now() - toIntervalHour(number % 22), {ins_cid}, true, '', "
        f"'smoke-ins-cur-{ins_pid}-' || toString(number), 'US', 0.3, number >= 40 FROM numbers(100)")
    # PG conversions: 2/day over the 7 baseline days
    subprocess.run(
        ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db", "-c",
         f"INSERT INTO conversions_data (received_at, click_id, campaign_id, offer_id, status, payout, revenue, profit, visitor_id) "
         f"SELECT now() - interval '24 hours' - (d || ' days')::interval - (g || ' hours')::interval, "
         f"'smoke-ins-cv-{ins_pid}', {ins_cid}, 5, 'sale', 10, 10, 9.8, 'smoke-ins-cv-{ins_pid}-' || d || '-' || g "
         f"FROM generate_series(0,6) d, generate_series(1,2) g"],
        capture_output=True, text=True, timeout=30)

    r = s.post(f"{api}/insights/run")
    body = r.json() if r.status_code == 200 else {}
    ins_findings = [f for f in (body.get("findings") or []) if f.get("campaign_id") == ins_cid]
    check("insights: run 200 with findings", r.status_code == 200 and bool(ins_findings), r.text[:200])
    ins_by_type = {f["type"]: f for f in ins_findings}
    check("insights: ctr_drop detected critical",
          (ins_by_type.get("ctr_drop") or {}).get("severity") == "critical",
          str([f["type"] for f in ins_findings]))
    check("insights: bot_surge detected warning",
          (ins_by_type.get("bot_surge") or {}).get("severity") == "warning", "")
    check("insights: finding shape (severity/type/message/detail/detected_at)",
          all(all(k in f for k in ("severity", "type", "message", "detail", "detected_at"))
              for f in ins_findings)
          and all(all(k in f["detail"] for k in ("current", "baseline", "change_pct"))
                  for f in ins_findings), "")
    check("insights: findings sorted severity-first",
          [f["severity"] for f in body.get("findings") or []]
          == sorted([f["severity"] for f in body.get("findings") or []],
                    key=lambda s: {"critical": 0, "warning": 1, "info": 2}[s]), "")

    r = s.get(f"{api}/insights/latest")
    latest = r.json() if r.status_code == 200 else {}
    check("insights: latest returns cached run",
          r.status_code == 200 and latest.get("run_at") == body.get("run_at")
          and isinstance(latest.get("findings"), list), r.text[:200])
    r = requests.get(f"{api}/insights/latest", verify=not INSECURE)
    check("insights: latest unauthenticated 401", r.status_code == 401, str(r.status_code))
    r = requests.post(f"{api}/insights/run", verify=not INSECURE)
    check("insights: run unauthenticated 401", r.status_code == 401, str(r.status_code))

    print("== Insights cleanup ==")
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE visitor_id LIKE 'smoke-ins-%-{ins_pid}-%'")
    leftover = ch_query(
        f"SELECT count() FROM clicks_data WHERE visitor_id LIKE 'smoke-ins-%-{ins_pid}-%'")
    check("insights: seeded clicks removed", leftover == "0", leftover[:80])
    subprocess.run(
        ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db", "-c",
         f"DELETE FROM conversions_data WHERE visitor_id LIKE 'smoke-ins-cv-{ins_pid}-%'"],
        capture_output=True, text=True, timeout=30)
    if ins_cid:
        r = s.delete(f"{api}/campaigns/{ins_cid}")
        check("insights: delete campaign", r.status_code == 200, r.text[:120])
    # refresh the cached block so it no longer references the scratch campaign
    r = s.post(f"{api}/insights/run")
    check("insights: post-cleanup run 200", r.status_code == 200, r.text[:120])
    check("insights: scratch findings gone after cleanup",
          r.status_code == 200
          and not [f for f in r.json().get("findings", []) if f.get("campaign_id") == ins_cid], "")

    print("== Sources bulk (multi-select) ==")
    src_pid = os.getpid()
    bulk_src_ids = []
    for n in ("A", "B"):
        r = s.post(f"{api}/sources/", json={"name": f"Smoke Bulk Src {n} {src_pid}", "traffic_loss": 0})
        check(f"sources bulk: create {n}", r.status_code == 200 and "id" in r.json(), r.text[:150])
        bulk_src_ids.append(r.json().get("id"))
    r = s.post(f"{api}/sources/bulk", json={"ids": bulk_src_ids, "action": "delete"})
    check("sources bulk: delete both", r.status_code == 200 and r.json().get("updated") == 2,
          r.text[:150])
    r = s.get(f"{api}/sources/")
    leftover = [x["id"] for x in r.json() if x["id"] in bulk_src_ids]
    check("sources bulk: both gone from the list", leftover == [], str(leftover))
    r = s.post(f"{api}/sources/bulk", json={"ids": bulk_src_ids, "action": "delete"})
    check("sources bulk: re-delete is harmless", r.status_code == 200
          and r.json().get("updated") == 0, r.text[:150])
    r = s.post(f"{api}/sources/bulk", json={"ids": bulk_src_ids, "action": "nuke"})
    check("sources bulk: unknown action 400", r.status_code == 400, str(r.status_code))
    # a source linked to a campaign is skipped, not deleted
    r = s.post(f"{api}/sources/", json={"name": f"Smoke Linked Src {src_pid}", "traffic_loss": 0})
    check("sources bulk: linked source created", r.status_code == 200 and "id" in r.json(), r.text[:150])
    linked_src = r.json().get("id")
    r = s.post(f"{api}/campaigns/", json={
        "name": f"Smoke SrcLink {src_pid}", "alias": f"smoke-srclink-{src_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "traffic_source_id": linked_src,
        "config": {"flows": [{"type": "default", "position": 1, "enabled": True,
                "schema": "redirect", "redirect_url": "https://example.com/smoke-srclink",
                "filters": []}], "postbacks": [], "fallback_url": "", "hide_referrer": False}})
    check("sources bulk: linked campaign created", r.status_code == 200 and "id" in r.json(),
          r.text[:150])
    linked_camp = r.json().get("id")
    r = s.post(f"{api}/sources/bulk", json={"ids": [linked_src], "action": "delete"})
    check("sources bulk: linked source skipped", r.status_code == 200
          and r.json().get("updated") == 0
          and len(r.json().get("skipped") or []) == 1, r.text[:200])
    r = s.get(f"{api}/sources/")
    check("sources bulk: linked source survives", any(x["id"] == linked_src for x in r.json()), "")
    if linked_camp:
        r = s.delete(f"{api}/campaigns/{linked_camp}")
        check("sources bulk: linked campaign cleaned up", r.status_code == 200, r.text[:120])
    if linked_src:
        r = s.post(f"{api}/sources/bulk", json={"ids": [linked_src], "action": "delete"})
        check("sources bulk: linked source deleted after unlink", r.status_code == 200
              and r.json().get("updated") == 1, r.text[:150])
    # deleting a built-in preset must stick: the list endpoint used to re-seed
    # missing presets on every GET, instantly resurrecting them
    PRESET_PROBE_NAMES = ["Outbrain", "ExoClick", "PropellerAds", "Adsterra", "Taboola"]
    r = s.get(f"{api}/sources/")
    probe = next((x for x in r.json() if x["name"] in PRESET_PROBE_NAMES), None)
    created_probe = False
    if probe is None:
        # the operator may have purged the seeded catalog — recreate one
        # catalog row from the template endpoint purely for this regression
        rp = s.get(f"{api}/sources/presets")
        catalog = {p["name"]: p for p in rp.json().get("presets", [])} if rp.status_code == 200 else {}
        pname = next((n for n in PRESET_PROBE_NAMES if n in catalog), None)
        if pname:
            rc = s.post(f"{api}/sources/", json={
                "name": pname, "traffic_loss": 0,
                "settings": catalog[pname].get("params", [])})
            if rc.status_code == 200:
                probe = rc.json()
                created_probe = True
    check("sources presets: catalog preset present for probe", probe is not None,
          "probe presets unavailable in catalog")
    if probe:
        r = s.post(f"{api}/sources/bulk", json={"ids": [probe["id"]], "action": "delete"})
        check("sources presets: preset bulk-deleted", r.status_code == 200
              and r.json().get("updated") == 1, r.text[:150])
        r = s.get(f"{api}/sources/")
        check("sources presets: deleted preset stays deleted (no GET re-seed)",
              all(x["name"] != probe["name"] for x in r.json()),
              "preset was resurrected by the list endpoint")
        if not created_probe:  # leave operator-purged catalogs purged
            s.post(f"{api}/sources/", json={
                "name": probe["name"],
                "traffic_loss": probe.get("traffic_loss") or 0,
                "s2s_postback": probe.get("s2s_postback"),
                "s2s_postback_statuses": probe.get("s2s_postback_statuses") or {},
                "settings": probe.get("settings") or [],
                "additional_settings": probe.get("additional_settings") or {}})

    print("== Favicon proxy ==")
    # Ad-blockers match well-known ad-network domains anywhere in the URL, so
    # the frontend sends the domain base64url-encoded behind a "~" marker.
    # The encoded form must resolve to the same resource as the raw domain.
    def _favicon_b64(d):
        return "~" + base64.urlsafe_b64encode(d.encode()).decode().rstrip("=")

    r = s.get(f"{api}/affiliate-networks/favicon/www.google.com")
    check("favicon: raw domain 200 png", r.status_code == 200
          and "image/png" in r.headers.get("Content-Type", ""), str(r.status_code))
    raw_content = r.content
    enc = _favicon_b64("www.google.com")
    r = s.get(f"{api}/affiliate-networks/favicon/{enc}")
    check("favicon: encoded domain 200 png", r.status_code == 200
          and "image/png" in r.headers.get("Content-Type", ""), str(r.status_code))
    check("favicon: encoded resolves to same resource as raw",
          r.content == raw_content, f"{len(r.content)}B vs {len(raw_content)}B")
    r = s.get(f"{api}/sources/favicon/{enc}")
    check("favicon: sources proxy matches networks for encoded",
          r.status_code == 200 and r.content == raw_content, str(r.status_code))
    r = s.get(f"{api}/affiliate-networks/favicon/~~not-valid-base64!!")
    check("favicon: invalid segment 200 empty png, no 500", r.status_code == 200
          and "image/png" in r.headers.get("Content-Type", ""), str(r.status_code))

    print("== Audit log sorting ==")
    r = s.get(f"{api}/audit/", params={"sort_by": "id", "sort_desc": "false", "page_size": 100})
    asc_ids = [e["id"] for e in r.json().get("entries", [])]
    check("audit: sort id ascending", r.status_code == 200 and asc_ids == sorted(asc_ids),
          str(asc_ids[:6]))
    r = s.get(f"{api}/audit/", params={"sort_by": "id", "sort_desc": "true", "page_size": 100})
    desc_ids = [e["id"] for e in r.json().get("entries", [])]
    check("audit: sort id descending", desc_ids == sorted(desc_ids, reverse=True),
          str(desc_ids[:6]))
    r = s.get(f"{api}/audit/", params={"sort_by": "username", "sort_desc": "false"})
    check("audit: sort by username accepted", r.status_code == 200, r.text[:120])
    r = s.get(f"{api}/audit/", params={"sort_by": "bogus'; DROP TABLE audit_log;--"})
    check("audit: unknown sort column ignored safely", r.status_code == 200
          and [e["id"] for e in r.json().get("entries", [])][:3] == desc_ids[:3], r.text[:150])

    print("== MCP / AI-agent access (G76) ==")
    mcp_url = f"{api}/mcp"

    def mcp_call(method, params=None, mid=1, sess=None):
        payload = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            payload["params"] = params
        r = (sess or s).post(mcp_url, json=payload)
        body = r.json() if "json" in r.headers.get("content-type", "") else {}
        return r, body

    def mcp_tool(name, arguments=None, mid=1):
        r, body = mcp_call("tools/call", {"name": name,
                                          "arguments": arguments or {}}, mid=mid)
        result = (body.get("result") or {})
        parsed = None
        try:
            parsed = json.loads(result["content"][0]["text"])
        except Exception:
            pass
        return r, body, result.get("isError") is True, parsed

    r = requests.post(mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                      verify=not INSECURE)
    check("mcp: unauthenticated 401", r.status_code == 401, str(r.status_code))

    r, body = mcp_call("initialize")
    init = body.get("result") or {}
    check("mcp: initialize serverInfo",
          r.status_code == 200 and init.get("serverInfo", {}).get("name") == "aaa-tracker",
          r.text[:200])
    check("mcp: initialize protocolVersion + tools capability",
          bool(init.get("protocolVersion")) and "tools" in (init.get("capabilities") or {}), "")

    r, body = mcp_call("ping")
    check("mcp: ping", r.status_code == 200 and "result" in body, r.text[:120])

    r, body = mcp_call("tools/list")
    tools = {t["name"]: t for t in (body.get("result") or {}).get("tools") or []}
    expected_tools = {"campaigns.list", "campaigns.get", "campaigns.metrics",
                      "campaigns.set_status", "offers.list", "sources.list",
                      "reports.summary", "conversions.recent", "insights.latest"}
    check("mcp: tools/list exposes the tracker tool set",
          expected_tools <= set(tools), str(sorted(tools)))
    check("mcp: tool descriptors carry description + inputSchema",
          all(t.get("description") and isinstance(t.get("inputSchema"), dict)
              for t in tools.values()), "")

    r, body, is_err, rows = mcp_tool("campaigns.list")
    smoke_alias = f"smoke-{os.getpid()}"
    check("mcp: campaigns.list finds the smoke campaign",
          r.status_code == 200 and not is_err and isinstance(rows, list)
          and any(c.get("alias") == smoke_alias for c in rows), str(rows)[:200])

    r, body, is_err, camp = mcp_tool("campaigns.get", {"campaign_id": cid})
    check("mcp: campaigns.get returns the campaign",
          r.status_code == 200 and not is_err and (camp or {}).get("alias") == smoke_alias,
          str(camp)[:200])

    r, body, is_err, rows = mcp_tool("campaigns.metrics", {"period": "7d"})
    check("mcp: campaigns.metrics returns per-campaign rows",
          r.status_code == 200 and not is_err and isinstance(rows, list)
          and all("clicks" in m and "roi" in m for m in rows), str(rows)[:200])

    r, body, is_err, totals = mcp_tool("reports.summary", {"period": "7d"})
    check("mcp: reports.summary totals shape",
          r.status_code == 200 and not is_err
          and all(k in (totals or {}) for k in
                  ("clicks", "conversions", "cost", "revenue", "profit", "roi")),
          str(totals)[:200])

    r, body, is_err, convs = mcp_tool("conversions.recent", {"limit": 5})
    check("mcp: conversions.recent honours the limit",
          r.status_code == 200 and not is_err and isinstance(convs, list)
          and len(convs) <= 5, str(convs)[:200])

    r, body, is_err, ins = mcp_tool("insights.latest")
    check("mcp: insights.latest exposes the findings block",
          r.status_code == 200 and not is_err and "findings" in (ins or {}), str(ins)[:200])

    # set_status round-trip on a dedicated MCP campaign
    r = s.post(f"{api}/campaigns/", json={
        "name": f"Smoke MCP {os.getpid()}", "alias": f"smoke-mcp-{os.getpid()}",
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "config": {"flows": [{
            "type": "default", "position": 1, "enabled": True, "schema": "redirect",
            "redirect_url": "https://example.com/smoke-mcp", "filters": [],
        }], "postbacks": [], "fallback_url": "", "hide_referrer": False}})
    check("mcp: scratch campaign created", r.status_code == 200 and "id" in r.json(),
          r.text[:150])
    mcp_cid = r.json().get("id")

    r, body, is_err, updated = mcp_tool("campaigns.set_status",
                                        {"campaign_id": mcp_cid, "status": "paused"})
    check("mcp: set_status paused",
          r.status_code == 200 and not is_err and (updated or {}).get("status") == "paused",
          str(updated)[:200])
    r = s.get(f"{api}/campaigns/")
    check("mcp: pause visible through the REST API",
          r.status_code == 200 and any(c.get("id") == mcp_cid
                                       and c.get("status") == "paused"
                                       for c in r.json()), r.text[:150])
    r, body, is_err, updated = mcp_tool("campaigns.set_status",
                                        {"campaign_id": mcp_cid, "status": "active"})
    check("mcp: set_status active",
          r.status_code == 200 and not is_err and (updated or {}).get("status") == "active",
          str(updated)[:200])

    # protocol + tool error paths
    r, body = mcp_call("no.such.method")
    check("mcp: unknown method -> -32601",
          (body.get("error") or {}).get("code") == -32601, r.text[:150])
    r, body = mcp_call("tools/call", {"name": "no.such.tool", "arguments": {}})
    check("mcp: unknown tool -> -32602",
          (body.get("error") or {}).get("code") == -32602, r.text[:150])
    r, body = mcp_call("tools/call", {"name": "campaigns.get", "arguments": {}})
    check("mcp: missing arguments -> -32602",
          (body.get("error") or {}).get("code") == -32602, r.text[:150])
    r, body, is_err, _ = mcp_tool("campaigns.set_status",
                                  {"campaign_id": mcp_cid, "status": "bogus"})
    check("mcp: invalid status value -> tool error", is_err, r.text[:150])
    r, body, is_err, _ = mcp_tool("campaigns.get", {"campaign_id": 999999999})
    check("mcp: unknown campaign -> tool error", is_err, r.text[:150])

    r = s.post(mcp_url, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("mcp: notification accepted without a response body",
          r.status_code in (200, 202, 204), str(r.status_code))
    r = s.get(mcp_url)
    check("mcp: GET not allowed (405)", r.status_code == 405, str(r.status_code))

    print("== MCP cleanup ==")
    if mcp_cid:
        r = s.delete(f"{api}/campaigns/{mcp_cid}")
        check("mcp: delete scratch campaign", r.status_code == 200, r.text[:120])

    print("== Changelog parity: live analytics sync & platform bugs ==")
    # Bugs fixed in a competitor's changelog that this tracker shared (audit
    # 2026-09-27): click-outs/conversions never reached ClickHouse, visitor
    # params poisoned reports, comma-decimal payouts rejected, weight edits
    # reset visitor bindings, paused offers received traffic, no offer-cap on
    # click-out, Accept-Language filters never matched, duplicate names 500'd,
    # campaigns:'own' leaked conversions, CSVs garbled in Excel.

    def cp_breakdown_clicks(campaign_id):
        out = ch_query(f"SELECT countIf(click IS NULL OR click = false), countIf(click = true), "
                       f"countIf(status IN ('sale','upsale')), sumOrNull(toFloat64(revenue)) "
                       f"FROM clicks_data WHERE campaign_id = {campaign_id}")
        if not out or out.startswith("ERROR"):
            return [0, 0, 0, 0.0]
        parts = [p if p != "\\N" else "0" for p in (out.split("\t") + ["0", "0", "0", "0"])[:4]]
        return [int(float(parts[0] or 0)), int(float(parts[1] or 0)),
                int(float(parts[2] or 0)), float(parts[3] or 0)]

    cp_pid = os.getpid()
    # offer + campaign whose flow serves that offer directly
    r = s.post(f"{api}/offers/", json={
        "name": f"Smoke CP Offer {cp_pid}", "url": f"https://example.com/smoke-cp-offer-{cp_pid}?cid={{click_id}}"})
    cp_oid = r.json().get("id") if r.status_code == 200 else None
    check("cp: offer created", bool(cp_oid), r.text[:200])
    cp_alias = f"smoke-cp-{cp_pid}"
    r = s.post(f"{api}/campaigns/", json={
        "name": cp_alias, "alias": cp_alias, "type": "campaign", "status": "active",
        "redirect_mode": "position",
        "config": {"flows": [{"type": "default", "position": 1, "enabled": True,
                              "schema": "direct", "offer": cp_oid, "filters": []}],
                   "postbacks": [], "hide_referrer": False, "fallback_url": ""}})
    check("cp: campaign created", r.status_code == 200 and "id" in r.json(), r.text[:200])
    cp_cid = r.json().get("id")

    # 1. Live click-out must appear in ClickHouse as a click
    r = requests.get(f"{BASE}/{cp_alias}", verify=not INSECURE, allow_redirects=False,
                     params={"sub_id_1": f"smoke-cp-visit-{cp_pid}"})
    check("cp: campaign hit redirects", r.status_code in (301, 302, 307, 308), str(r.status_code))
    import time
    time.sleep(1.5)
    visits, clicks, convs, rev = cp_breakdown_clicks(cp_cid)
    check("cp: hit recorded as a visit row", visits == 1, f"visits={visits} clicks={clicks}")
    r = requests.get(f"{BASE}/c/{cp_alias}/{cp_oid}", verify=not INSECURE, allow_redirects=False,
                     params={"sub_id_1": f"smoke-cp-click-{cp_pid}"})
    check("cp: click-out redirects", r.status_code in (301, 302, 307, 308), str(r.status_code))
    loc = r.headers.get("location") or ""
    m = re.search(r"[?&]click_id=([^&]+)", loc)
    cp_click_id = m.group(1) if m else None
    check("cp: click-out location carries click_id", bool(cp_click_id), loc[:120])
    time.sleep(1.5)
    visits, clicks, convs, rev = cp_breakdown_clicks(cp_cid)
    check("cp: click-out recorded as CH click (not a second visit)",
          visits == 1 and clicks == 1, f"visits={visits} clicks={clicks}")

    # 2. Live postback must update the CH click row (conversions + revenue)
    if cp_click_id:
        r = requests.get(f"{BASE}/pb?clickid={cp_click_id}&status=sale&payout=10", verify=not INSECURE)
        check("cp: postback 200", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
        ok = False
        for _ in range(10):
            time.sleep(1)
            _, _, convs2, rev2 = cp_breakdown_clicks(cp_cid)
            if convs2 >= 1 and rev2 >= 10:
                ok = True
                break
        check("cp: conversion visible in CH reports (clicks/conversions/revenue)",
              ok, f"convs={convs2} rev={rev2}")

    # 3. Visitor params must not poison CH rows
    poison_alias = f"smoke-cpp-{cp_pid}"
    r = s.post(f"{api}/campaigns/", json={
        "name": poison_alias, "alias": poison_alias, "type": "campaign", "status": "active",
        "redirect_mode": "position",
        "config": {"flows": [{"type": "default", "position": 1, "enabled": True,
                              "schema": "redirect",
                              "redirect_url": f"https://example.com/smoke-cpp-{cp_pid}",
                              "filters": []}],
                   "postbacks": [], "hide_referrer": False, "fallback_url": ""}})
    poison_cid = r.json().get("id") if r.status_code == 200 else None
    requests.get(f"{BASE}/{poison_alias}", verify=not INSECURE, allow_redirects=False,
                 params={"sub_id_1": f"smoke-cpp-{cp_pid}", "status": "sale",
                         "revenue": "999", "cost": "0,5"})
    time.sleep(1.5)
    row = ch_query(f"SELECT empty(status), isNull(revenue), cost FROM clicks_data "
                   f"WHERE campaign_id = {poison_cid} AND sub_id_1 = 'smoke-cpp-{cp_pid}' LIMIT 1")
    parts = (row.split("\t") + ["ERR", "ERR", "ERR"])[:3] if row else ["ERR", "ERR", "ERR"]
    check("cp: poisoned status/revenue ignored, row kept, bad cost sanitized",
          parts[0] == "1" and parts[1] == "1" and parts[2] == "0.5", str(parts))

    # 4. Comma-decimal payout accepted on the postback
    if cp_click_id:
        r = requests.get(f"{BASE}/pb?clickid={cp_click_id}&status=sale&payout=2,25", verify=not INSECURE)
        check("cp: comma-decimal postback accepted", r.status_code == 200, f"{r.status_code} {r.text[:80]}")

    # 5. Accept-Language filter matches primary subtag
    al_alias = f"smoke-cpal-{cp_pid}"
    r = s.post(f"{api}/campaigns/", json={
        "name": al_alias, "alias": al_alias, "type": "campaign", "status": "active",
        "redirect_mode": "position",
        "config": {"flows": [
            {"type": "default", "position": 1, "enabled": True, "schema": "redirect",
             "redirect_url": f"https://example.com/smoke-cpal-en-{cp_pid}",
             "filters": [{"logic": "and", "conditions":
                          [{"field": "language", "operator": "equals", "value": "en"}]}]},
            {"type": "default", "position": 2, "enabled": True, "schema": "redirect",
             "redirect_url": f"https://example.com/smoke-cpal-any-{cp_pid}", "filters": []}],
            "postbacks": [], "hide_referrer": False, "fallback_url": ""}})
    al_cid = r.json().get("id") if r.status_code == 200 else None
    r = requests.get(f"{BASE}/{al_alias}", verify=not INSECURE, allow_redirects=False,
                     headers={"Accept-Language": "en-US,en;q=0.9"})
    check("cp: en-US Accept-Language matches 'en' filter",
          f"smoke-cpal-en-{cp_pid}" in (r.headers.get("location") or ""), r.headers.get("location", "")[:100])
    r = requests.get(f"{BASE}/{al_alias}", verify=not INSECURE, allow_redirects=False,
                     headers={"Accept-Language": "fr-FR,fr;q=0.9"})
    check("cp: fr Accept-Language falls through to next flow",
          f"smoke-cpal-any-{cp_pid}" in (r.headers.get("location") or ""), r.headers.get("location", "")[:100])

    # 6. Weight-only edit must not reset visitor bindings
    wb_alias = f"smoke-cpwb-{cp_pid}"
    r = s.post(f"{api}/campaigns/", json={
        "name": wb_alias, "alias": wb_alias, "type": "campaign", "status": "active",
        "redirect_mode": "weight",
        "config": {"stickiness": True, "flows": [
            {"type": "regular", "position": 1, "enabled": True, "schema": "redirect",
             "redirect_url": f"https://example.com/smoke-cpwb-a-{cp_pid}", "weight": 50, "filters": []},
            {"type": "regular", "position": 2, "enabled": True, "schema": "redirect",
             "redirect_url": f"https://example.com/smoke-cpwb-b-{cp_pid}", "weight": 50, "filters": []}],
            "postbacks": [], "hide_referrer": False, "fallback_url": ""}})
    wb_cid = r.json().get("id") if r.status_code == 200 else None
    wb_cfg = None
    if wb_cid:
        rg = s.get(f"{api}/campaigns/{wb_cid}")
        wb_cfg = rg.json().get("config") if rg.status_code == 200 else None
    if not wb_cfg:  # fall back to the payload shape the API accepted
        wb_cfg = {"stickiness": True, "flows": [
            {"type": "regular", "position": 1, "enabled": True, "schema": "redirect",
             "redirect_url": f"https://example.com/smoke-cpwb-a-{cp_pid}", "weight": 50, "filters": []},
            {"type": "regular", "position": 2, "enabled": True, "schema": "redirect",
             "redirect_url": f"https://example.com/smoke-cpwb-b-{cp_pid}", "weight": 50, "filters": []}],
            "postbacks": [], "hide_referrer": False, "fallback_url": ""}
    r = requests.get(f"{BASE}/{wb_alias}", verify=not INSECURE, allow_redirects=False)
    first_loc = r.headers.get("location") or ""
    bind_cookie = r.cookies.get("aaa_bind")
    # change ONLY the weights, keep everything else byte-identical
    for f in wb_cfg["flows"]:
        f["weight"] = 80 if f["position"] == 1 else 20
    if wb_cid:
        s.put(f"{api}/campaigns/{wb_cid}", json={
            "name": wb_alias, "alias": wb_alias, "type": "campaign", "status": "active",
            "redirect_mode": "weight", "config": wb_cfg})
        r2 = requests.get(f"{BASE}/{wb_alias}", verify=not INSECURE, allow_redirects=False,
                          cookies={"aaa_bind": bind_cookie} if bind_cookie else {})
        second_loc = r2.headers.get("location") or ""
        check("cp: weight-only edit preserves visitor binding",
              bool(bind_cookie) and first_loc == second_loc and first_loc != "",
              f"{first_loc[:60]} vs {second_loc[:60]}")

    # 7. Paused / archived offers must not receive click-outs
    r = s.post(f"{api}/offers/", json={
        "name": f"Smoke CP Paused {cp_pid}",
        "url": f"https://example.com/smoke-cp-paused-{cp_pid}", "status": "paused"})
    paused_oid = r.json().get("id") if r.status_code == 200 else None
    if paused_oid:
        r = requests.get(f"{BASE}/c/{cp_alias}/{paused_oid}", verify=not INSECURE, allow_redirects=False)
        check("cp: paused offer click-out refused", r.status_code == 404, str(r.status_code))
        s.patch(f"{api}/offers/{paused_oid}", json={"archived": True})
        r = requests.get(f"{BASE}/c/{cp_alias}/{paused_oid}", verify=not INSECURE, allow_redirects=False)
        check("cp: archived offer click-out refused", r.status_code == 404, str(r.status_code))
        s.delete(f"{api}/offers/{paused_oid}")

    # 8. Offer daily cap enforced on the click-out route
    r = s.post(f"{api}/offers/", json={
        "name": f"Smoke CP Cap {cp_pid}", "url": f"https://example.com/smoke-cp-cap-{cp_pid}?cid={{click_id}}",
        "daily_conversions_cap": 1})
    cap_oid = r.json().get("id") if r.status_code == 200 else None
    if cap_oid:
        r = requests.get(f"{BASE}/c/{cp_alias}/{cap_oid}", verify=not INSECURE, allow_redirects=False)
        m = re.search(r"[?&]click_id=([^&]+)", r.headers.get("location") or "")
        if m:
            requests.get(f"{BASE}/pb?clickid={m.group(1)}&status=sale&payout=5", verify=not INSECURE)
        r = requests.get(f"{BASE}/c/{cp_alias}/{cap_oid}", verify=not INSECURE, allow_redirects=False)
        check("cp: capped offer click-out refused after cap reached", r.status_code == 404, str(r.status_code))
        s.delete(f"{api}/offers/{cap_oid}")

    # 9. Duplicate campaign name -> 400, not 500; clone twice works
    r = s.post(f"{api}/campaigns/", json={
        "name": cp_alias, "alias": f"smoke-cp-dup-{cp_pid}", "type": "campaign",
        "status": "active", "redirect_mode": "position", "config": {"flows": []}})
    check("cp: duplicate campaign name -> 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")
    if cp_cid:
        r1 = s.post(f"{api}/campaigns/{cp_cid}/clone")
        r2 = s.post(f"{api}/campaigns/{cp_cid}/clone")
        names = []
        for rr in (r1, r2):
            if rr.status_code == 200:
                names.append(rr.json().get("name"))
                s.delete(f"{api}/campaigns/{rr.json().get('id')}")
        check("cp: cloning twice succeeds with distinct names",
              r1.status_code == 200 and r2.status_code == 200 and len(set(names)) == 2,
              f"{r1.status_code}/{r2.status_code} {names}")

    # 10. campaigns:'own' must scope the conversions log + mutations
    own2 = f"smoke-own2-{cp_pid}"
    r = s.post(f"{api}/users/", json={
        "username": own2, "password": "smokepass1",
        "permissions": {"sections": {"campaigns": True, "reports": True, "dashboard": True},
                        "write": True, "campaigns": "own"}})
    own2_uid = r.json().get("id") if r.status_code == 200 else None
    check("cp: scoped user created", bool(own2_uid), r.text[:150])
    ou2 = requests.Session()
    ou2.verify = not INSECURE
    ou2.post(f"{api}/login", json={"username": own2, "password": "smokepass1"})
    own_alias = f"smoke-cpown-{cp_pid}"
    r = s.post(f"{api}/campaigns/", json={
        "name": own_alias, "alias": own_alias, "type": "campaign", "status": "active",
        "redirect_mode": "position",
        "config": {"flows": [{"type": "default", "position": 1, "enabled": True,
                              "schema": "redirect",
                              "redirect_url": f"https://example.com/smoke-cpown-{cp_pid}",
                              "filters": []}],
                   "postbacks": [], "hide_referrer": False, "fallback_url": ""}})
    own_cid = r.json().get("id") if r.status_code == 200 else None
    if own2_uid and own_cid:
        pg_exec(f"UPDATE campaigns SET owner_id = {own2_uid} WHERE id = {own_cid}")
        # a conversion on the ADMIN-owned campaign (cp_cid) must be invisible
        requests.get(f"{BASE}/pb?clickid=smoke-cpadmin-conv-{cp_pid}&status=sale&payout=7", verify=not INSECURE)
        r = ou2.get(f"{api}/reports/", params={"click_id": f"smoke-cpadmin-conv-{cp_pid}"})
        check("cp: scoped user cannot read others' conversions",
              r.status_code in (200, 403) and
              (r.status_code == 403 or all(c.get("campaign_id") == own_cid for c in r.json())),
              f"{r.status_code} {r.text[:120]}")
        r = ou2.get(f"{api}/reports/")
        check("cp: scoped user conversions list excludes others' rows",
              r.status_code == 200 and all(c.get("campaign_id") == own_cid for c in r.json()),
              r.text[:150])
    if own2_uid:
        s.delete(f"{api}/users/{own2_uid}")
    if own_cid:
        s.delete(f"{api}/campaigns/{own_cid}")

    # 11. CSV export starts with a UTF-8 BOM (Excel-safe non-ASCII)
    r = s.get(f"{api}/campaigns/export")
    check("cp: campaigns CSV has UTF-8 BOM",
          r.status_code == 200 and r.content.startswith(b"\xef\xbb\xbf"), str(r.status_code))
    r = s.get(f"{api}/offers/export")
    check("cp: offers CSV has UTF-8 BOM",
          r.status_code == 200 and r.content.startswith(b"\xef\xbb\xbf"), str(r.status_code))

    # 12. Landing link whitespace stripped on create (admin session required —
    # the landing router is admin-gated per the security audit)
    r = requests.get(f"{BASE}/landings", verify=not INSECURE)
    check("cp: unauthenticated /landings -> 401 (admin-gated)",
          r.status_code == 401, f"{r.status_code} {r.text[:80]}")
    r = s.post(f"{BASE}/landing", verify=not INSECURE, data={
        "name": f"Smoke CP Landing {cp_pid}", "site_folder": f"smoke_cp_l_{cp_pid}",
        "type": 0, "link": f"  https://example.com/smoke-cp-landing-{cp_pid}  "})
    check("cp: landing created", r.status_code in (200, 201), f"{r.status_code} {r.text[:100]}")
    r = s.get(f"{BASE}/landings", verify=not INSECURE)
    landing_rows = r.json() if r.status_code == 200 else []
    cp_landing = next((x for x in landing_rows
                       if x.get("folder") == f"smoke_cp_l_{cp_pid}"), None)
    check("cp: landing link stripped",
          cp_landing is not None and cp_landing.get("link") == f"https://example.com/smoke-cp-landing-{cp_pid}",
          str((cp_landing or {}).get("link")))
    if cp_landing:
        s.delete(f"{BASE}/landing/{cp_landing.get('id')}", verify=not INSECURE)

    print("== Changelog parity cleanup ==")
    if poison_cid:
        ch_query(f"ALTER TABLE clicks_data DELETE WHERE campaign_id = {poison_cid}")
        s.delete(f"{api}/campaigns/{poison_cid}")
    if al_cid:
        ch_query(f"ALTER TABLE clicks_data DELETE WHERE campaign_id = {al_cid}")
        s.delete(f"{api}/campaigns/{al_cid}")
    if wb_cid:
        ch_query(f"ALTER TABLE clicks_data DELETE WHERE campaign_id = {wb_cid}")
        s.delete(f"{api}/campaigns/{wb_cid}")
    if cp_cid:
        ch_query(f"ALTER TABLE clicks_data DELETE WHERE campaign_id = {cp_cid}")
        fraud_pg(f"DELETE FROM conversions_data WHERE click_id = '{cp_click_id}'") if cp_click_id else None
        s.delete(f"{api}/campaigns/{cp_cid}")
    if cp_oid:
        s.delete(f"{api}/offers/{cp_oid}")

    print("== Sources single delete (row action) ==")
    sng_pid = os.getpid()
    r = s.post(f"{api}/sources/", json={"name": f"Smoke Single Src {sng_pid}", "traffic_loss": 0})
    check("sources single: source created", r.status_code == 200 and "id" in r.json(), r.text[:150])
    single_src = r.json().get("id")
    # a source linked to a campaign must be refused with 409, not a 500 FK error
    r = s.post(f"{api}/campaigns/", json={
        "name": f"Smoke SingleLink {sng_pid}", "alias": f"smoke-singlelink-{sng_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "traffic_source_id": single_src,
        "config": {"flows": [{"type": "default", "position": 1, "enabled": True,
                "schema": "redirect", "redirect_url": "https://example.com/smoke-singlelink",
                "filters": []}], "postbacks": [], "fallback_url": "", "hide_referrer": False}})
    check("sources single: linked campaign created", r.status_code == 200 and "id" in r.json(),
          r.text[:150])
    single_camp = r.json().get("id")
    r = s.delete(f"{api}/sources/{single_src}")
    check("sources single: linked source delete -> 409", r.status_code == 409, str(r.status_code))
    check("sources single: 409 detail mentions campaigns",
          "campaign" in (r.json().get("detail") or "").lower(), r.text[:200])
    r = s.get(f"{api}/sources/")
    check("sources single: linked source survives", any(x["id"] == single_src for x in r.json()), "")
    if single_camp:
        r = s.delete(f"{api}/campaigns/{single_camp}")
        check("sources single: linked campaign cleaned up", r.status_code == 200, r.text[:120])
    r = s.delete(f"{api}/sources/{single_src}")
    check("sources single: unlinked source deleted", r.status_code == 200, r.text[:150])
    r = s.get(f"{api}/sources/")
    check("sources single: row gone from the list",
          all(x["id"] != single_src for x in r.json()), "")
    r = s.delete(f"{api}/sources/{single_src}")
    check("sources single: re-delete -> 404", r.status_code == 404, str(r.status_code))
    r = s.delete(f"{api}/sources/999999999")
    check("sources single: unknown id -> 404", r.status_code == 404, str(r.status_code))

    print("== Pagination & CSV export ==")
    # Self-seeded, pid-isolated data: 12 CH click rows + 7 PG conversions.
    pg_cid = 880000 + (os.getpid() % 10000)
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, country, cost) "
        f"SELECT now(), {pg_cid}, number >= 10, '', 'seed-pg-{os.getpid()}-' || toString(number), 'US', 0.01 "
        f"FROM numbers(12)")
    fraud_pg(
        f"INSERT INTO conversions_data (received_at, click_id, campaign_id, status, revenue, profit) "
        f"SELECT now(), 'seed-pgc-{os.getpid()}-' || g, {pg_cid}, 'sale', 1.5, 1.5 "
        f"FROM generate_series(1, 7) g")

    # click-log: offset opts into the {items, total} pager shape; total is a
    # real count() over the same filters (isolated campaign → exactly 12)
    r = s.post(f"{api}/dashboard/click-log", json={"campaigns": [pg_cid], "limit": 5, "offset": 0})
    page1 = r.json() if r.status_code == 200 else {}
    check("click-log paged shape {items,total}",
          isinstance(page1, dict) and "items" in page1 and "total" in page1, r.text[:150])
    check("click-log limit=5 returns 5 items", len(page1.get("items") or []) == 5,
          str(len(page1.get("items") or [])))
    check("click-log total counts all filtered rows", page1.get("total") == 12, str(page1.get("total")))
    r = s.post(f"{api}/dashboard/click-log", json={"campaigns": [pg_cid], "limit": 5, "offset": 5})
    page2 = r.json() if r.status_code == 200 else {}
    ids1 = {x.get("visitor_id") for x in page1.get("items") or []}
    ids2 = {x.get("visitor_id") for x in page2.get("items") or []}
    check("click-log offset=5 skips the first 5 (stable order)",
          len(ids2) == 5 and not (ids1 & ids2), f"{sorted(ids1 & ids2)[:3]}")
    check("click-log page 2 keeps the same total", page2.get("total") == 12, str(page2.get("total")))
    # no offset → legacy bare-list shape (back-compat for existing callers)
    r = s.post(f"{api}/dashboard/click-log", json={"campaigns": [pg_cid], "limit": 5})
    check("click-log without offset keeps list shape",
          r.status_code == 200 and isinstance(r.json(), list), r.text[:120])

    # click-log CSV export: BOM, text/csv, filename, filters honored
    r = s.post(f"{api}/dashboard/click-log/export", json={"campaigns": [pg_cid]})
    csv_body = r.content.decode("utf-8-sig") if r.status_code == 200 else ""
    check("click-log CSV has UTF-8 BOM",
          r.status_code == 200 and r.content.startswith(b"\xef\xbb\xbf"), str(r.status_code))
    check("click-log CSV content-type text/csv",
          "text/csv" in r.headers.get("content-type", ""), r.headers.get("content-type", ""))
    check("click-log CSV filename click_log.csv",
          "click_log.csv" in r.headers.get("content-disposition", ""), r.headers.get("content-disposition", ""))
    check("click-log CSV exports all filtered rows",
          len(csv_body.splitlines()) == 13 and csv_body.count(f"seed-pg-{os.getpid()}-") == 12,
          f"{len(csv_body.splitlines())} lines")
    r = s.post(f"{api}/dashboard/click-log/export", json={"campaigns": [pg_cid + 1]})
    check("click-log CSV respects filters",
          r.status_code == 200 and len(r.content.decode("utf-8-sig").splitlines()) == 1, r.text[:100])

    # conversions: same contract on the Postgres list
    r = s.get(f"{api}/reports/", params={"search": f"seed-pgc-{os.getpid()}", "limit": 5, "offset": 0})
    page1 = r.json() if r.status_code == 200 else {}
    check("conversions paged shape {items,total}",
          isinstance(page1, dict) and "items" in page1 and "total" in page1, r.text[:150])
    check("conversions limit=5 returns 5 items", len(page1.get("items") or []) == 5,
          str(len(page1.get("items") or [])))
    check("conversions total counts all filtered rows", page1.get("total") == 7, str(page1.get("total")))
    r = s.get(f"{api}/reports/", params={"search": f"seed-pgc-{os.getpid()}", "limit": 5, "offset": 5})
    page2 = r.json() if r.status_code == 200 else {}
    cids1 = {x.get("click_id") for x in page1.get("items") or []}
    cids2 = {x.get("click_id") for x in page2.get("items") or []}
    check("conversions offset=5 skips the first 5 (stable order)",
          len(cids2) == 2 and not (cids1 & cids2), f"{sorted(cids1 & cids2)[:3]}")
    r = s.get(f"{api}/reports/", params={"limit": 5})
    check("conversions without offset keeps list shape",
          r.status_code == 200 and isinstance(r.json(), list), r.text[:120])

    # conversions CSV export
    r = s.get(f"{api}/reports/export", params={"search": f"seed-pgc-{os.getpid()}"})
    csv_body = r.content.decode("utf-8-sig") if r.status_code == 200 else ""
    check("conversions CSV has UTF-8 BOM",
          r.status_code == 200 and r.content.startswith(b"\xef\xbb\xbf"), str(r.status_code))
    check("conversions CSV content-type text/csv",
          "text/csv" in r.headers.get("content-type", ""), r.headers.get("content-type", ""))
    check("conversions CSV filename conversions.csv",
          "conversions.csv" in r.headers.get("content-disposition", ""), r.headers.get("content-disposition", ""))
    check("conversions CSV exports all filtered rows",
          len(csv_body.splitlines()) == 8 and csv_body.count(f"seed-pgc-{os.getpid()}-") == 7,
          f"{len(csv_body.splitlines())} lines")

    # cleanup what we seeded
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE visitor_id LIKE 'seed-pg-{os.getpid()}-%'")
    fraud_pg(f"DELETE FROM conversions_data WHERE click_id LIKE 'seed-pgc-{os.getpid()}-%'")
    leftover_ch = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {pg_cid}")
    check("pagination seed CH rows removed", leftover_ch == "0", leftover_ch[:80])
    leftover_pg = fraud_pg(f"SELECT count(*) FROM conversions_data WHERE click_id LIKE 'seed-pgc-{os.getpid()}-%'")
    check("pagination seed PG rows removed", leftover_pg == "0", leftover_pg[:80])

    print("== Report dimensions + retroactive cost update ==")
    # New report dimensions (domain / user_agent / week / os_version) plus the
    # admin retroactive cost-update tool, verified on a pid-suffixed campaign.
    dim_pid = os.getpid()
    dim_alias = f"smoke-dim-{dim_pid}"
    r = s.post(f"{api}/campaigns/", json={
        "name": dim_alias, "alias": dim_alias, "type": "campaign", "status": "active",
        "redirect_mode": "position",
        "config": {"flows": [{"type": "default", "position": 1, "enabled": True,
                              "schema": "redirect",
                              "redirect_url": f"https://example.com/smoke-dim-{dim_pid}",
                              "filters": []}],
                   "postbacks": [], "hide_referrer": False, "fallback_url": ""}})
    check("dims: campaign created", r.status_code == 200 and "id" in r.json(), r.text[:150])
    dim_cid = r.json().get("id")

    # 6 rows: two domains, two os_versions, one user_agent; row 5 is a click.
    ch_query(
        "INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, country, url, user_agent, os_version, cost) "
        f"SELECT now(), {dim_cid}, number >= 5, '', 'seed-dim-{dim_pid}-' || toString(number), 'US', "
        f"if(number % 2 = 0, 'https://landing-a.example/lp', 'https://landing-b.example/lp'), "
        f"'SmokeUA/{dim_pid} Chrome/120.0', if(number % 3 = 0, '10', '14.5'), 0 FROM numbers(6)")
    dim_today = datetime.now(timezone.utc).date()
    dim_today_s = str(dim_today)
    dim_filters = {"date_from": dim_today_s, "date_to": dim_today_s, "campaigns": [dim_cid]}

    def dim_breakdown(dims):
        return s.post(f"{api}/dashboard/breakdown", json={"dimensions": dims, "filters": dim_filters})

    # -- week: ISO-week keys derived from received_at --
    r = dim_breakdown(["week"])
    week_rows = r.json().get("rows") if r.status_code == 200 else []
    iso = dim_today.isocalendar()
    expect_week = f"{iso[0]}-W{iso[1]:02d}"
    check("dims: week breakdown returns ISO week keys",
          r.status_code == 200 and any(x["value"] == expect_week for x in week_rows),
          str([x.get("value") for x in week_rows[:5]]))

    # -- user_agent / os_version / domain read the (migrated) columns --
    r = dim_breakdown(["user_agent"])
    ua_rows = r.json().get("rows") if r.status_code == 200 else []
    check("dims: user_agent breakdown groups the seeded UA",
          r.status_code == 200 and any(
              x["value"] == f"SmokeUA/{dim_pid} Chrome/120.0" and x["visits"] == 5 and x["clicks"] == 1
              for x in ua_rows), str([(x.get("value"), x.get("visits")) for x in ua_rows[:3]]))
    r = dim_breakdown(["os_version"])
    osv_rows = r.json().get("rows") if r.status_code == 200 else []
    check("dims: os_version breakdown splits 10 / 14.5",
          r.status_code == 200 and {x["value"] for x in osv_rows} == {"10", "14.5"},
          str([x.get("value") for x in osv_rows]))
    r = dim_breakdown(["domain"])
    dom_rows = r.json().get("rows") if r.status_code == 200 else []
    check("dims: domain breakdown extracts host from url",
          r.status_code == 200 and {x["value"] for x in dom_rows} == {"landing-a.example", "landing-b.example"},
          str([x.get("value") for x in dom_rows]))

    # -- retroactive cost update: auth + payload validation --
    r = requests.post(f"{api}/costs/update", json={
        "campaign_id": dim_cid, "period": {"from": dim_today_s, "to": dim_today_s}, "cost": 0.07},
        verify=not INSECURE)
    check("costs: unauthenticated -> 401", r.status_code == 401, str(r.status_code))
    bad_payloads = (
        {"campaign_id": dim_cid, "cost": 0.07},                                                  # no period
        {"campaign_id": dim_cid, "period": {"from": dim_today_s, "to": "2020-01-01"}, "cost": 0.07},  # from > to
        {"campaign_id": dim_cid, "period": {"from": "nope", "to": dim_today_s}, "cost": 0.07},  # bad date
        {"campaign_id": dim_cid, "period": {"from": dim_today_s, "to": dim_today_s}, "cost": -1},  # negative
    )
    bad_codes = [s.post(f"{api}/costs/update", json=p).status_code for p in bad_payloads]
    check("costs: bad payloads -> 400", bad_codes == [400] * len(bad_payloads), str(bad_codes))
    r = s.post(f"{api}/costs/update", json={
        "campaign_id": dim_cid, "period": {"from": dim_today_s, "to": dim_today_s}, "cost": "x"})
    check("costs: non-numeric cost -> 4xx", r.status_code in (400, 422), str(r.status_code))

    # -- retroactive cost update: applies per-click cost, report reflects it --
    r = s.post(f"{api}/costs/update", json={
        "campaign_id": dim_cid, "period": {"from": dim_today_s, "to": dim_today_s}, "cost": 0.07})
    check("costs: update sets cost on all 6 rows",
          r.status_code == 200 and r.json().get("updated_rows") == 6, r.text[:200])
    r = dim_breakdown(["campaign_id"])
    mine = {x["value"]: x for x in (r.json().get("rows") or [])}.get(str(dim_cid)) \
        if r.status_code == 200 else None
    check("costs: breakdown reflects new cost (6 x 0.07 = 0.42)",
          bool(mine) and abs(float(mine.get("cost") or 0) - 0.42) < 0.001,
          str(mine and mine.get("cost")))
    ch_cost = ch_query(f"SELECT DISTINCT toString(cost) FROM clicks_data WHERE campaign_id = {dim_cid}")
    check("costs: CH rows carry the per-click cost", ch_cost == "0.07", ch_cost[:80])

    print("== Report dimensions + cost update cleanup ==")
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE campaign_id = {dim_cid}")
    leftover = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {dim_cid}")
    check("dims: seeded CH rows removed", leftover == "0", leftover[:80])
    if dim_cid:
        r = s.delete(f"{api}/campaigns/{dim_cid}")
        check("dims: campaign deleted", r.status_code == 200, r.text[:120])

    print("== Bulk owner/network + domain groups ==")
    bk_pid = os.getpid()

    # -- bulk owner: only the selected campaigns change --
    bk_camps = []
    for i in (1, 2):
        r = s.post(f"{api}/campaigns/", json={
            "name": f"Smoke BulkOwner {bk_pid}-{i}", "alias": f"smoke-bulkown-{bk_pid}-{i}",
            "type": "campaign", "status": "active", "redirect_mode": "position",
            "config": {"flows": []}})
        check(f"bulk: campaign {i} created", r.status_code == 200 and "id" in r.json(),
              r.text[:150])
        bk_camps.append(r.json().get("id"))
    r = s.post(f"{api}/users/", json={"username": f"smoke-bulkown-{bk_pid}",
                                      "password": "smokepass1"})
    check("bulk: owner user created", r.status_code == 200, r.text[:150])
    bk_owner = r.json().get("id")
    r = s.get(f"{api}/campaigns/users")
    check("bulk: owner choices listed", r.status_code == 200
          and any(u.get("id") == bk_owner for u in r.json()), r.text[:150])
    r = s.post(f"{api}/campaigns/bulk/owner", json={"ids": bk_camps, "owner_id": bk_owner})
    check("bulk-owner applied to 2", r.status_code == 200 and r.json().get("updated") == 2,
          r.text[:150])
    r = s.get(f"{api}/campaigns/")
    owners = {c["id"]: c.get("owner_id") for c in r.json() if c["id"] in bk_camps}
    check("bulk-owner set on exactly the selected campaigns",
          owners == {bk_camps[0]: bk_owner, bk_camps[1]: bk_owner}, str(owners))
    r = s.post(f"{api}/campaigns/bulk/owner", json={"ids": [bk_camps[0]], "owner_id": None})
    check("bulk-owner clear one", r.status_code == 200 and r.json().get("updated") == 1,
          r.text[:150])
    r = s.get(f"{api}/campaigns/")
    cleared = {c["id"]: c.get("owner_id") for c in r.json() if c["id"] in bk_camps}
    check("bulk-owner clear took effect",
          cleared.get(bk_camps[0]) is None and cleared.get(bk_camps[1]) == bk_owner,
          str(cleared))
    r = s.post(f"{api}/campaigns/bulk/owner", json={"ids": bk_camps, "owner_id": 999999999})
    check("bulk-owner unknown owner -> 404", r.status_code == 404, str(r.status_code))

    # -- bulk network --
    r = s.post(f"{api}/affiliate-networks/", json={"name": f"Smoke Bulk Net {bk_pid}"})
    check("bulk: network created", r.status_code == 200 and "id" in r.json(), r.text[:150])
    bk_net = r.json().get("id")
    bk_offers = []
    for i in (1, 2):
        r = s.post(f"{api}/offers/", json={"name": f"Smoke BulkNet {bk_pid}-{i}",
                                           "url": "https://example.com/bulk"})
        check(f"bulk: offer {i} created", r.status_code == 200 and "id" in r.json(),
              r.text[:150])
        bk_offers.append(r.json().get("id"))
    r = s.post(f"{api}/offers/bulk-network",
               json={"ids": bk_offers, "affiliate_network_id": bk_net})
    check("bulk-network applied to 2",
          r.status_code == 200 and r.json().get("updated") == 2, r.text[:150])
    r = s.get(f"{api}/offers/")
    nets = {o["id"]: o.get("affiliate_network_id") for o in r.json() if o["id"] in bk_offers}
    check("bulk-network set on exactly the selected offers",
          nets == {bk_offers[0]: bk_net, bk_offers[1]: bk_net}, str(nets))
    r = s.post(f"{api}/offers/bulk-network",
               json={"ids": bk_offers, "affiliate_network_id": 999999999})
    check("bulk-network unknown network -> 404", r.status_code == 404, str(r.status_code))

    # -- domain groups: grants gate the campaign-binding domain list --
    grp_doms = []
    for suffix in ("a", "b"):
        r = s.post(f"{api}/domains/",
                   json={"domain": f"smoke-grp-{bk_pid}-{suffix}.example.com"})
        check(f"bulk: domain {suffix} created", r.status_code == 200 and "id" in r.json(),
              r.text[:150])
        grp_doms.append(r.json().get("id"))
    r = s.post(f"{api}/domains/groups", json={"name": f"Smoke Group {bk_pid}"})
    check("bulk: group created", r.status_code == 200 and "id" in r.json(), r.text[:150])
    grp_id = r.json().get("id")
    r = s.post(f"{api}/domains/groups/{grp_id}/domains", json={"ids": [grp_doms[0]]})
    check("bulk: domain assigned to group",
          r.status_code == 200 and r.json().get("updated") == 1, r.text[:150])
    r = s.get(f"{api}/domains/groups")
    mine = [g for g in r.json() if g.get("id") == grp_id] if r.status_code == 200 else []
    check("bulk: group lists its domain",
          bool(mine) and grp_doms[0] in (mine[0].get("domain_ids") or []), r.text[:200])

    grant_users = {}
    for tag in ("grant", "nogrant"):
        r = s.post(f"{api}/users/", json={
            "username": f"smoke-{tag}-{bk_pid}", "password": "smokepass1",
            "permissions": {"sections": {"domains": True}}})
        check(f"bulk: {tag} user created", r.status_code == 200, r.text[:150])
        grant_users[tag] = r.json().get("id")
    gsess = {}
    for tag in ("grant", "nogrant"):
        gsess[tag] = requests.Session()
        gsess[tag].verify = not INSECURE
        r = gsess[tag].post(f"{api}/login",
                            json={"username": f"smoke-{tag}-{bk_pid}",
                                  "password": "smokepass1"})
        check(f"bulk: {tag} user login", r.status_code == 200, r.text[:150])

    r = gsess["nogrant"].get(f"{api}/domains/")
    visible = {d["id"] for d in r.json()} if r.status_code == 200 else set()
    check("groups: non-granted user does not see grouped domain",
          grp_doms[0] not in visible and grp_doms[1] in visible, str(sorted(visible))[:120])
    r = gsess["grant"].get(f"{api}/domains/")
    visible = {d["id"] for d in r.json()} if r.status_code == 200 else set()
    check("groups: user without grant does not see grouped domain yet",
          grp_doms[0] not in visible, str(sorted(visible))[:120])
    r = s.get(f"{api}/domains/")
    check("groups: admin always sees grouped domain",
          r.status_code == 200 and grp_doms[0] in {d["id"] for d in r.json()}, r.text[:120])

    r = s.post(f"{api}/domains/groups/{grp_id}/users",
               json={"ids": [grant_users["grant"]]})
    check("groups: user granted", r.status_code == 200 and r.json().get("updated") == 1,
          r.text[:150])
    r = gsess["grant"].get(f"{api}/domains/")
    visible = {d["id"] for d in r.json()} if r.status_code == 200 else set()
    check("groups: granted user now sees grouped domain", grp_doms[0] in visible,
          str(sorted(visible))[:120])
    r = gsess["nogrant"].get(f"{api}/domains/")
    visible = {d["id"] for d in r.json()} if r.status_code == 200 else set()
    check("groups: grant changed nothing for others", grp_doms[0] not in visible, "")

    r = s.delete(f"{api}/domains/groups/{grp_id}/users/{grant_users['grant']}")
    check("groups: grant revoked", r.status_code == 200, r.text[:150])
    r = gsess["grant"].get(f"{api}/domains/")
    visible = {d["id"] for d in r.json()} if r.status_code == 200 else set()
    check("groups: revoked user loses grouped domain again", grp_doms[0] not in visible, "")
    r = s.put(f"{api}/domains/groups/{grp_id}", json={"name": f"Smoke Group {bk_pid} R"})
    check("groups: rename works", r.status_code == 200, r.text[:150])
    r = s.delete(f"{api}/domains/groups/{grp_id}/domains/{grp_doms[0]}")
    check("groups: domain unassigned", r.status_code == 200, r.text[:150])
    r = s.get(f"{api}/domains/")
    check("groups: ungrouped domain visible to everyone again",
          r.status_code == 200 and grp_doms[0] in {d["id"] for d in r.json()}, r.text[:120])

    # cleanup everything this block created
    r = s.delete(f"{api}/domains/groups/{grp_id}")
    check("groups: group deleted", r.status_code == 200, r.text[:150])
    r = s.get(f"{api}/domains/groups")
    check("groups: gone from list",
          r.status_code == 200 and all(g.get("id") != grp_id for g in r.json()), r.text[:150])
    r = s.delete(f"{api}/domains/groups/999999999")
    check("groups: unknown group -> 404", r.status_code == 404, str(r.status_code))
    for d in grp_doms:
        s.delete(f"{api}/domains/{d}")
    for o in bk_offers:
        s.delete(f"{api}/offers/{o}")
    s.delete(f"{api}/affiliate-networks/{bk_net}")
    for c in bk_camps:
        s.delete(f"{api}/campaigns/{c}")
    for u in [bk_owner] + list(grant_users.values()):
        s.delete(f"{api}/users/{u}")

    # -- IPv6 visitors: the click-api accepts a v6 client address; the CH row
    # keeps 0.0.0.0 in the IPv4 `ip` column and the full address in ip_full --
    # (own throwaway campaign: the shared extras campaign is deleted long
    # before this point, and main() rebinds `alias` late, so neither shared
    # variable is usable here)
    print("== IPv6 support ==")
    v6_alias = f"smoke-v6c-{os.getpid()}"
    v6_payload = {"name": v6_alias, "alias": v6_alias, "type": "campaign",
                  "status": "active", "redirect_mode": "position",
                  "config": {"flows": [{
                      "type": "default", "position": 1, "enabled": True, "schema": "redirect",
                      "redirect_url": f"https://example.com/smoke-v6-{os.getpid()}",
                      "filters": []}], "postbacks": [], "hide_referrer": False,
                      "fallback_url": ""}}
    r = s.post(f"{api}/campaigns/", json=v6_payload)
    check("v6: create campaign", r.status_code == 200 and "id" in r.json(), r.text[:200])
    v6_cid = r.json().get("id")

    v6_addr = "2001:db8:85a3::8a2e:370:7334"
    r = requests.post(f"{BASE}/click-api/{v6_alias}", verify=not INSECURE, json={
        "ip": v6_addr, "user_agent": "SmokeIPv6/1.0 Chrome/120",
        "referrer": "https://ads.example/v6",
        "sub_id_2": f"smoke-v6-{os.getpid()}"})
    v6_click = r.json().get("click_id") if r.status_code == 200 else None
    check("v6: click-api accepts an IPv6 address",
          r.status_code == 200 and bool(v6_click), r.text[:200])
    v6_row = ch_query(
        f"SELECT toString(ip), ip_full, is_bot, fraud_score "
        f"FROM clicks_data WHERE visitor_id = '{v6_click}' LIMIT 1") if v6_click else ""
    v6_parts = v6_row.split("\t") if v6_row and not v6_row.startswith("ERROR") else []
    check("v6: CH row keeps 0.0.0.0 in ip and the full address in ip_full",
          len(v6_parts) == 4 and v6_parts[0] == "0.0.0.0" and v6_parts[1] == v6_addr,
          v6_row[:200])
    check("v6: bot/fraud fields populated without crashing",
          len(v6_parts) == 4 and v6_parts[2] in ("true", "false")
          and v6_parts[3].isdigit(), v6_row[:200])

    # v4 regression: same endpoint, IPv4 address — `ip` unchanged, ip_full mirrors it.
    r = requests.post(f"{BASE}/click-api/{v6_alias}", verify=not INSECURE, json={
        "ip": "203.0.113.55", "user_agent": "SmokeIPv6/1.0 Chrome/120",
        "sub_id_2": f"smoke-v6-{os.getpid()}"})
    v4_click = r.json().get("click_id") if r.status_code == 200 else None
    v4_row = ch_query(
        f"SELECT toString(ip), ip_full FROM clicks_data "
        f"WHERE visitor_id = '{v4_click}' LIMIT 1") if v4_click else ""
    v4_parts = v4_row.split("\t") if v4_row and not v4_row.startswith("ERROR") else []
    check("v4: ip column unchanged and ip_full mirrors the v4 address",
          len(v4_parts) == 2 and v4_parts[0] == "203.0.113.55"
          and v4_parts[1] == "203.0.113.55", v4_row[:200])

    # The click log surfaces the full v6 address (falling back to `ip` for
    # rows written before ip_full existed).
    r = s.post(f"{api}/dashboard/click-log", json={"campaigns": [v6_cid], "search": v6_addr})
    v6_log = r.json() if r.status_code == 200 else []
    check("v6: click-log search finds the row by its full address",
          isinstance(v6_log, list)
          and any(row.get("ip") == v6_addr and row.get("visitor_id") == v6_click
                  for row in v6_log), r.text[:200])

    if v6_click:
        ch_query(f"ALTER TABLE clicks_data DELETE WHERE visitor_id = '{v6_click}'")
    if v4_click:
        ch_query(f"ALTER TABLE clicks_data DELETE WHERE visitor_id = '{v4_click}'")
    if v6_cid:
        r = s.delete(f"{api}/campaigns/{v6_cid}")
        check("v6: campaign deleted", r.status_code == 200, r.text[:120])

    # ----- Ops polish: SSL expiry / settings backup / URL filter / delete guard / click-log scoping -----
    op_pid = os.getpid()

    print("== Ops polish: SSL expiry ==")
    r = s.post(f"{api}/domains/", json={"domain": f"smoke-ssl-{op_pid}.example.com"})
    check("ops-ssl: domain created for ssl check", r.status_code == 200, r.text[:150])
    ssl_dom_id = r.json().get("id")
    r = s.get(f"{api}/domains/ssl-expiry")
    check("ops-ssl: endpoint returns list shape",
          r.status_code == 200 and isinstance(r.json().get("domains"), list), r.text[:200])
    ssl_rows = {d.get("domain"): d for d in (r.json().get("domains") or [])} if r.status_code == 200 else {}
    ssl_row = ssl_rows.get(f"smoke-ssl-{op_pid}.example.com", {})
    check("ops-ssl: missing cert reported as unknown (not an error)",
          ssl_row.get("status") == "unknown" and ssl_row.get("days_to_expiry") is None,
          str(ssl_row)[:150])
    if ssl_dom_id:
        r = s.delete(f"{api}/domains/{ssl_dom_id}")
        check("ops-ssl: check domain cleaned up", r.status_code == 200, r.text[:150])

    print("== Ops polish: settings backup ==")
    r = s.get(f"{api}/settings/")
    op_settings_backup = (r.json().get("settings") or {}) if r.status_code == 200 else {}
    op_tg_backup = op_settings_backup.get("telegram") or {}
    op_cur_backup = op_settings_backup.get("currency")
    r = s.post(f"{api}/settings/", json={"settings": {"telegram": {
        "bot_token": f"smoke-tok-{op_pid}", "chat_id": f"smoke-chat-{op_pid}"}}})
    check("ops-backup: token seeded", r.status_code == 200, r.text[:150])
    r = s.get(f"{api}/settings/export")
    check("ops-backup: export downloads a JSON document",
          r.status_code == 200
          and "attachment" in r.headers.get("Content-Disposition", "")
          and isinstance(r.json().get("data"), dict), r.text[:200])
    exp_tg = ((r.json().get("data") or {}).get("settings") or {}).get("telegram") or {}
    check("ops-backup: export nulls secrets but keeps the keys",
          "bot_token" in exp_tg and exp_tg.get("bot_token") is None, str(exp_tg)[:150])
    check("ops-backup: export keeps non-secret values",
          exp_tg.get("chat_id") == f"smoke-chat-{op_pid}", str(exp_tg)[:150])
    r = s.post(f"{api}/settings/import", json={
        "data": {"settings": {"currency": "EUR", "telegram": {"bot_token": None}}}})
    check("ops-backup: import accepted", r.status_code == 200, r.text[:200])
    r = s.get(f"{api}/settings/")
    imp_settings = (r.json().get("settings") or {}) if r.status_code == 200 else {}
    check("ops-backup: import applied the change",
          imp_settings.get("currency") == "EUR", str(imp_settings.get("currency"))[:60])
    check("ops-backup: import kept the live secret for nulled keys",
          (imp_settings.get("telegram") or {}).get("bot_token") == f"smoke-tok-{op_pid}",
          str((imp_settings.get("telegram") or {}).get("bot_token"))[:80])
    r = s.post(f"{api}/settings/import", json="not-a-settings-document")
    check("ops-backup: import rejects garbage (400)", r.status_code == 400, str(r.status_code))
    r = s.post(f"{api}/settings/", json={
        "settings": {"currency": op_cur_backup, "telegram": op_tg_backup}})
    check("ops-backup: original settings restored", r.status_code == 200, r.text[:150])
    if "chat_id" not in op_tg_backup:
        s.post(f"{api}/settings/", json={"settings": {"telegram": {"chat_id": None}}})
    r = s.get(f"{api}/settings/")
    check("ops-backup: live secret back to original after restore",
          ((r.json().get("settings") or {}).get("telegram") or {}).get("bot_token")
          == op_tg_backup.get("bot_token"), r.text[:150])

    print("== Ops polish: conversion URL filter ==")
    r = s.post(f"{api}/offers/", json={
        "name": f"Smoke Url Offer {op_pid}",
        "url": f"https://example.com/smoke-url-{op_pid}/landing"})
    check("ops-url: offer created", r.status_code == 200 and bool(r.json().get("id")), r.text[:200])
    url_offer_id = r.json().get("id")
    url_conv_id = ""
    if url_offer_id and cid:
        url_conv_id = ((pg_exec_out(
            "INSERT INTO conversions_data (received_at, click_id, campaign_id, offer_id, status,"
            " payout, revenue, profit) VALUES (now(), 'none', %d, %d, 'sale', 1, 1, 1) RETURNING id"
            % (cid, url_offer_id)) or "").strip().splitlines() or [""])[0]
    check("ops-url: conversion seeded", bool(url_conv_id), str(url_conv_id)[:80])
    r = s.get(f"{api}/reports/", params={"url": f"smoke-url-{op_pid}"})
    matched = [c for c in (r.json() if r.status_code == 200 else [])
               if str(c.get("id")) == url_conv_id]
    check("ops-url: url filter matches conversion",
          r.status_code == 200 and bool(matched), r.text[:200])
    r = s.get(f"{api}/reports/", params={"url": f"no-such-marker-{op_pid}"})
    check("ops-url: url filter excludes non-matching",
          r.status_code == 200
          and all(str(c.get("id")) != url_conv_id for c in r.json()), r.text[:200])
    r = s.get(f"{api}/reports/", params={"url": f"smoke-url-{op_pid}", "limit": 10, "offset": 0})
    check("ops-url: url filter works in paginated mode",
          r.status_code == 200 and any(str(c.get("id")) == url_conv_id
                                      for c in (r.json().get("items") or []))
          and r.json().get("total") is not None, r.text[:200])
    if url_conv_id:
        r = s.delete(f"{api}/reports/{url_conv_id}")
        check("ops-url: seeded conversion cleaned up", r.status_code == 200, r.text[:150])
    if url_offer_id:
        s.delete(f"{api}/offers/{url_offer_id}")

    print("== Ops polish: domain delete guard ==")
    r = s.post(f"{api}/domains/", json={"domain": f"smoke-guard-{op_pid}.example.com"})
    check("ops-guard: domain created", r.status_code == 200, r.text[:150])
    guard_dom_id = r.json().get("id")
    guard_cid = None
    if guard_dom_id:
        r = s.post(f"{api}/campaigns/", json={
            "name": f"Smoke Guard {op_pid}", "alias": f"smoke-guard-{op_pid}",
            "type": "campaign", "status": "active", "redirect_mode": "weight",
            "domain_id": guard_dom_id,
            "config": {"flows": [{"type": "default", "position": 1, "enabled": True,
                                  "schema": "redirect",
                                  "redirect_url": "https://example.com/smoke-guard",
                                  "weight": 100, "filters": []}],
                       "postbacks": [], "hide_referrer": False}})
        guard_cid = r.json().get("id")
        check("ops-guard: campaign bound to domain", bool(guard_cid), r.text[:200])
    if guard_dom_id and guard_cid:
        r = s.delete(f"{api}/domains/{guard_dom_id}")
        check("ops-guard: bound domain delete -> 409",
              r.status_code == 409 and "campaign" in (r.json().get("detail") or "").lower(),
              r.text[:200])
        pg_exec(f"UPDATE campaigns SET domain_id = NULL WHERE id = {guard_cid}")
        r = s.delete(f"{api}/domains/{guard_dom_id}")
        check("ops-guard: unbound domain delete -> 200", r.status_code == 200, r.text[:200])
    if guard_cid:
        r = s.delete(f"{api}/campaigns/{guard_cid}")
        check("ops-guard: campaign cleaned up", r.status_code == 200, r.text[:150])

    print("== Ops polish: click-log campaigns:'own' scoping ==")
    import time as _time
    own_cid = other_cid = scope_uid = None
    r = s.post(f"{api}/campaigns/", json={
        "name": f"Smoke Scope Own {op_pid}", "alias": f"smoke-scope-own-{op_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "weight",
        "config": {"flows": [{"type": "default", "position": 1, "enabled": True,
                              "schema": "redirect",
                              "redirect_url": "https://example.com/smoke-scope-own",
                              "weight": 100, "filters": []}],
                   "postbacks": [], "hide_referrer": False}})
    own_cid = r.json().get("id")
    r = s.post(f"{api}/campaigns/", json={
        "name": f"Smoke Scope Other {op_pid}", "alias": f"smoke-scope-other-{op_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "weight",
        "config": {"flows": [{"type": "default", "position": 1, "enabled": True,
                              "schema": "redirect",
                              "redirect_url": "https://example.com/smoke-scope-other",
                              "weight": 100, "filters": []}],
                   "postbacks": [], "hide_referrer": False}})
    other_cid = r.json().get("id")
    check("ops-scope: two campaigns created", bool(own_cid) and bool(other_cid), r.text[:200])
    for _al in (f"smoke-scope-own-{op_pid}", f"smoke-scope-other-{op_pid}"):
        requests.get(f"{BASE}/{_al}", verify=not INSECURE, allow_redirects=False)
    scope_user = f"smoke-scope-{op_pid}"
    r = s.post(f"{api}/users/", json={
        "username": scope_user, "password": "smokepass1",
        "permissions": {"sections": {"campaigns": True, "dashboard": True, "reports": True},
                        "write": True, "campaigns": "own"}})
    scope_uid = r.json().get("id")
    check("ops-scope: limited user created", r.status_code == 200 and bool(scope_uid), r.text[:150])
    ss = requests.Session()
    ss.verify = not INSECURE
    if scope_uid:
        r = ss.post(f"{api}/login", json={"username": scope_user, "password": "smokepass1"})
        check("ops-scope: limited user login", r.status_code == 200, r.text[:150])
        pg_exec(f"UPDATE campaigns SET owner_id = {scope_uid} WHERE id = {own_cid}")
        scope_ids = set()
        for _ in range(20):
            r = ss.post(f"{api}/dashboard/click-log", json={"limit": 500})
            rows = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
            scope_ids = {row.get("campaign_id") for row in rows}
            if own_cid in scope_ids:
                break
            _time.sleep(0.5)
        check("ops-scope: click-log shows only own campaigns",
              r.status_code == 200 and scope_ids and scope_ids <= {own_cid} and own_cid in scope_ids,
              f"status={r.status_code} ids={sorted(i for i in scope_ids if i is not None)[:10]}")
        r = ss.post(f"{api}/dashboard/click-log", json={"limit": 50, "offset": 0})
        paged = r.json() if r.status_code == 200 else {}
        paged_ids = {row.get("campaign_id") for row in (paged.get("items") or [])}
        check("ops-scope: paginated click-log scoped too",
              r.status_code == 200 and paged.get("total") is not None
              and paged_ids <= {own_cid} and own_cid in paged_ids,
              r.text[:200])
        feed_ids = set()
        for _ in range(20):
            r = ss.get(f"{api}/dashboard/live-clicks", params={"limit": 100})
            feed = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
            feed_ids = {row.get("campaign_id") for row in feed}
            if own_cid in feed_ids:
                break
            _time.sleep(0.5)
        check("ops-scope: live feed shows only own campaigns",
              r.status_code == 200 and feed_ids <= {own_cid} and own_cid in feed_ids,
              f"ids={sorted(i for i in feed_ids if i is not None)[:10]}")
    if own_cid and other_cid:
        r = s.post(f"{api}/dashboard/click-log", json={"limit": 500})
        admin_ids = {row.get("campaign_id") for row in r.json()} if r.status_code == 200 else set()
        check("ops-scope: admin click-log still sees both campaigns",
              own_cid in admin_ids and other_cid in admin_ids,
              f"own={own_cid in admin_ids} other={other_cid in admin_ids}")
        r = s.get(f"{api}/dashboard/live-clicks", params={"limit": 100})
        admin_feed = {row.get("campaign_id") for row in r.json()} if r.status_code == 200 else set()
        check("ops-scope: admin live feed still sees both campaigns",
              own_cid in admin_feed and other_cid in admin_feed,
              f"own={own_cid in admin_feed} other={other_cid in admin_feed}")
    if own_cid:
        pg_exec(f"UPDATE campaigns SET owner_id = NULL WHERE id = {own_cid}")
    if scope_uid:
        r = s.delete(f"{api}/users/{scope_uid}")
        check("ops-scope: limited user cleaned up", r.status_code == 200, r.text[:150])
    for _c in (own_cid, other_cid):
        if _c:
            r = s.delete(f"{api}/campaigns/{_c}")
            check("ops-scope: campaign cleaned up", r.status_code == 200, r.text[:150])

    print("== Security audit fixes (tracking plane) ==")
    fx_pid = os.getpid()
    fx_anon = requests.Session()
    fx_anon.verify = not INSECURE

    # --- P1: landings management requires an admin session ---
    r = fx_anon.get(f"{BASE}/landings")
    check("sec: GET /landings unauthenticated -> 401", r.status_code == 401, str(r.status_code))
    r = s.get(f"{BASE}/landings")
    check("sec: GET /landings with admin session -> 200", r.status_code == 200, str(r.status_code))
    r = fx_anon.post(f"{BASE}/landing/grab",
                     data={"url": "https://example.com/", "folder": f"smoke_sec_{fx_pid}"})
    check("sec: POST /landing/grab unauthenticated -> 401", r.status_code == 401, str(r.status_code))
    r = fx_anon.delete(f"{BASE}/landing/99999999")
    check("sec: DELETE /landing/{id} unauthenticated -> 401 (no rmtree)", r.status_code == 401,
          str(r.status_code))
    r = fx_anon.get(f"{BASE}/landings_editor/1/file", params={"filename": "index.html"})
    check("sec: editor file read unauthenticated -> 401", r.status_code == 401, str(r.status_code))

    # --- fixtures: offer + campaigns for the click-out / config checks ---
    r = s.post(f"{api}/offers/", json={"name": f"Smoke Sec Offer {fx_pid}",
                                       "url": "https://example.com/smoke-sec-offer?cid={click_id}"})
    check("sec: offer created", r.status_code == 200 and "id" in r.json(), r.text[:150])
    fx_offer_id = r.json().get("id")

    fx_alias = f"smoke-sec-{fx_pid}"
    r = s.post(f"{api}/campaigns/", json={
        "name": f"Smoke Sec {fx_pid}", "alias": fx_alias,
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "config": {"flows": [{"type": "default", "position": 1, "enabled": True,
                              "schema": "redirect",
                              "redirect_url": "https://example.com/smoke-sec",
                              "filters": []}],
                   "postbacks": [], "fallback_url": "", "hide_referrer": False}})
    check("sec: campaign created", r.status_code == 200 and "id" in r.json(), r.text[:150])
    fx_cid = r.json().get("id")

    # --- P1: NULL / garbage campaign config must 404, never 500 ---
    fx_null_alias = f"smoke-sec-null-{fx_pid}"
    r = s.post(f"{api}/campaigns/", json={
        "name": fx_null_alias, "alias": fx_null_alias, "type": "campaign",
        "status": "active", "redirect_mode": "position",
        "config": {"flows": [], "postbacks": []}})
    fx_null_id = r.json().get("id")
    pg_exec(f"UPDATE campaigns SET config = NULL WHERE id = {fx_null_id}")
    r = fx_anon.get(f"{BASE}/{fx_null_alias}", allow_redirects=False)
    check("sec: NULL config campaign 404s (no 500)", r.status_code == 404, str(r.status_code))

    # --- P1: non-dict JSON bodies must not 500 the tracking plane ---
    r = fx_anon.post(f"{BASE}/t/collect", json=[1, 2, 3])
    check("sec: /t/collect JSON array body -> <500", r.status_code < 500, str(r.status_code))
    r = fx_anon.post(f"{BASE}/t/collect", json=42)
    check("sec: /t/collect JSON scalar body -> <500", r.status_code < 500, str(r.status_code))
    r = fx_anon.post(f"{BASE}/{fx_alias}", json=[{"junk": "x"}])
    check("sec: POST /{alias} JSON array body -> <500", r.status_code < 500, str(r.status_code))

    # --- P1: superscript-digit campaign ref -> 404 (not 500) ---
    r = fx_anon.get(f"{BASE}/p/²")
    check("sec: /p/² (isdigit trap) -> 404", r.status_code == 404, str(r.status_code))

    # --- P1: direct-flow conversion reaches ClickHouse with click_id + status ---
    fx_ch_click = f"smoke-sec-ch-{fx_pid}"
    r = fx_anon.post(f"{BASE}/t/collect", json={"c": fx_alias, "click_id": fx_ch_click,
                                                "url": "https://landing.example/sec"})
    check("sec: /t/collect accepted with explicit click_id",
          r.status_code == 200 and r.json().get("click_id") == fx_ch_click, r.text[:120])
    r = fx_anon.get(f"{BASE}/pb?clickid={fx_ch_click}&status=sale&payout=3.5")
    check("sec: /pb conversion for direct click accepted",
          r.status_code == 200 and r.json().get("status") == "ok", r.text[:120])
    fx_ch_row = ""
    for _ in range(15):
        fx_ch_row = ch_query(
            f"SELECT click_id, status FROM clicks_data WHERE click_id = '{fx_ch_click}' "
            f"ORDER BY received_at DESC LIMIT 1")
        if fx_ch_row and "sale" in fx_ch_row:
            break
        _time.sleep(1)
    check("sec: CH row carries the direct click_id",
          bool(fx_ch_row) and not fx_ch_row.startswith("ERROR")
          and fx_ch_row.split("\t")[0] == fx_ch_click, fx_ch_row[:150])
    check("sec: CH row synced to the conversion status", "sale" in fx_ch_row, fx_ch_row[:150])

    # --- P2: NaN / Infinity payouts rejected 400 ---
    r = fx_anon.get(f"{BASE}/pb?clickid=smoke-sec-nan-{fx_pid}&status=sale&payout=nan")
    check("sec: /pb NaN payout -> 400", r.status_code == 400, str(r.status_code))
    r = fx_anon.get(f"{BASE}/pb?clickid=smoke-sec-inf-{fx_pid}&status=sale&payout=Infinity")
    check("sec: /pb Infinity payout -> 400", r.status_code == 400, str(r.status_code))
    r = fx_anon.get(f"{BASE}/p/{fx_alias}",
                    params={"click_id": f"smoke-sec-pnan-{fx_pid}", "payout": "nan", "fmt": "json"})
    check("sec: /p NaN payout -> 400", r.status_code == 400, str(r.status_code))

    # --- P2: over-long click_id / sub_id values accepted and truncated ---
    fx_long = "L" * 300
    r = fx_anon.get(f"{BASE}/pb?clickid={fx_long}&status=sale&payout=1", params={"sub_id_1": "S" * 200})
    check("sec: over-long click_id/sub_id postback accepted (no 500)",
          r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    fx_long_rec = poll_first({"click_id": fx_long[:100]})
    check("sec: over-long click_id truncated to 100 chars", fx_long_rec is not None, str(fx_long_rec))

    # --- P2: non-ASCII postback key -> 403 (TypeError/500 before the fix) ---
    r = s.get(f"{api}/settings/")
    fx_settings_all = r.json() if r.status_code == 200 else {}
    fx_cfg = dict(fx_settings_all.get("settings") or {})
    fx_saved_sec = dict(fx_cfg.get("postback_security") or {})
    fx_cfg["postback_security"] = dict(fx_saved_sec, secret_key="smoke-sec-key")
    r = s.post(f"{api}/settings/", json={"settings": fx_cfg})
    check("sec: postback secret saved", r.status_code == 200, r.text[:120])
    settle_settings_cache()
    r = fx_anon.get(f"{BASE}/pb?clickid=smoke-sec-key1-{fx_pid}&status=sale&payout=1", params={"key": "é"})
    check("sec: non-ASCII postback key -> 403 (not 500)", r.status_code == 403, str(r.status_code))
    r = fx_anon.get(f"{BASE}/pb?clickid=smoke-sec-key2-{fx_pid}&status=sale&payout=1", params={"key": "smoke-sec-key"})
    check("sec: correct postback key accepted", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    fx_cfg["postback_security"] = fx_saved_sec
    r = s.post(f"{api}/settings/", json={"settings": fx_cfg})
    check("sec: postback secret restored", r.status_code == 200, r.text[:120])
    settle_settings_cache()

    # --- P1: explicit ?click_id= beats a stale aaa_cid cookie on click-out ---
    fx_stale = requests.Session()
    fx_stale.verify = not INSECURE
    fx_stale.cookies.set("aaa_cid", f"smoke-sec-stale-{fx_pid}")
    r = fx_stale.get(f"{BASE}/c/{fx_alias}/{fx_offer_id}",
                     params={"click_id": f"smoke-sec-explicit-{fx_pid}"}, allow_redirects=False)
    check("sec: click-out honors explicit ?click_id over stale cookie",
          r.status_code in (301, 302, 307, 308)
          and f"click_id=smoke-sec-explicit-{fx_pid}" in (r.headers.get("location") or ""),
          f"{r.status_code} {(r.headers.get('location') or '')[:150]}")

    # --- P1: opted-out visitor click-out redirects but stores nothing ---
    fx_opt = requests.Session()
    fx_opt.verify = not INSECURE
    fx_opt.get(f"{BASE}/optout")
    fx_opt_click = f"smoke-sec-opt-{fx_pid}"
    fx_ch_before = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {fx_cid}")
    r = fx_opt.get(f"{BASE}/c/{fx_alias}/{fx_offer_id}", params={"click_id": fx_opt_click},
                   allow_redirects=False)
    check("sec: opted-out click-out still redirects (302)",
          r.status_code in (301, 302, 307, 308), str(r.status_code))
    fx_ch_after = ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {fx_cid}")
    check("sec: opted-out click-out wrote no CH row", fx_ch_after == fx_ch_before,
          f"{fx_ch_before} -> {fx_ch_after}")
    fx_opt_rec = poll_first({"click_id": fx_opt_click})
    check("sec: opted-out click-out wrote no conversion", fx_opt_rec is None, str(fx_opt_rec))

    # --- P1: paused campaign refuses click-outs ---
    pg_exec(f"UPDATE campaigns SET status = 'paused' WHERE id = {fx_cid}")
    r = fx_anon.get(f"{BASE}/c/{fx_alias}/{fx_offer_id}", allow_redirects=False)
    check("sec: paused campaign click-out -> 404", r.status_code == 404, str(r.status_code))
    pg_exec(f"UPDATE campaigns SET status = 'active' WHERE id = {fx_cid}")

    # --- P2: stickiness-bound visitor whose offer gets paused falls back ---
    fx_sticky_alias = f"smoke-sec-sticky-{fx_pid}"
    r = s.post(f"{api}/campaigns/", json={
        "name": f"Smoke Sec Sticky {fx_pid}", "alias": fx_sticky_alias,
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "config": {"stickiness": True,
                   "flows": [{"type": "default", "position": 1, "enabled": True,
                              "schema": "direct", "offer": fx_offer_id, "filters": []}],
                   "postbacks": [],
                   "fallback_url": "https://example.com/smoke-sec-fb",
                   "hide_referrer": False}})
    check("sec: sticky campaign created", r.status_code == 200 and "id" in r.json(), r.text[:150])
    fx_sticky_id = r.json().get("id")
    fx_sticky = requests.Session()
    fx_sticky.verify = not INSECURE
    r = fx_sticky.get(f"{BASE}/{fx_sticky_alias}", allow_redirects=False)
    check("sec: sticky campaign serves the offer", r.status_code in (301, 302, 307, 308)
          and "smoke-sec-offer" in (r.headers.get("location") or ""),
          f"{r.status_code} {(r.headers.get('location') or '')[:150]}")
    check("sec: stickiness cookie issued", bool(fx_sticky.cookies.get("aaa_bind")),
          str(fx_sticky.cookies))
    pg_exec(f"UPDATE offers SET status = 'paused' WHERE id = {fx_offer_id}")
    r = fx_sticky.get(f"{BASE}/{fx_sticky_alias}", allow_redirects=False)
    check("sec: bound flow with paused offer falls back (serves fallback, not paused offer)",
          "smoke-sec-fb" in (r.headers.get("location") or ""),
          f"{r.status_code} {(r.headers.get('location') or '')[:150]}")
    pg_exec(f"UPDATE offers SET status = 'active' WHERE id = {fx_offer_id}")

    # --- self-cleaning ---
    for fx_del_click in (fx_ch_click, fx_long[:100], f"smoke-sec-key1-{fx_pid}",
                         f"smoke-sec-key2-{fx_pid}", f"smoke-sec-pnan-{fx_pid}"):
        r = s.get(f"{api}/reports/", params={"click_id": fx_del_click})
        for fx_rec in (r.json() if r.status_code == 200 and isinstance(r.json(), list) else []):
            if fx_rec.get("click_id") == fx_del_click:
                s.delete(f"{api}/reports/{fx_rec['id']}")
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE click_id LIKE 'smoke-sec-%-{fx_pid}' "
             f"OR visitor_id LIKE 'smoke-sec-%-{fx_pid}'")
    for fx_del_id in (fx_sticky_id, fx_null_id, fx_cid):
        if fx_del_id:
            r = s.delete(f"{api}/campaigns/{fx_del_id}")
            check("sec: campaign cleaned up", r.status_code == 200, r.text[:120])
    if fx_offer_id:
        r = s.delete(f"{api}/offers/{fx_offer_id}")
        check("sec: offer cleaned up", r.status_code == 200, r.text[:120])

    print("== Ops hardening: scoping, validators, admin guards ==")
    hd_pid = os.getpid()
    hd_today = datetime.utcnow().strftime("%Y-%m-%d")

    # ----- P1-7: PATCH must not wipe fields the caller didn't send -----
    r = s.post(f"{api}/offers/", json={
        "name": f"Smoke Patch Offer {hd_pid}", "url": f"https://example.com/patch-{hd_pid}",
        "tags": [f"smoke-{hd_pid}"], "tokens": {"cid": "{click_id}"}})
    check("hd-patch: offer created", r.status_code == 200 and bool(r.json().get("id")), r.text[:200])
    patch_offer_id = r.json().get("id")
    if patch_offer_id:
        r = s.patch(f"{api}/offers/{patch_offer_id}",
                    json={"name": f"Smoke Patch Offer 2 {hd_pid}",
                          "url": f"https://example.com/patch-{hd_pid}"})
        check("hd-patch: partial PATCH accepted", r.status_code == 200, r.text[:200])
        r = s.get(f"{api}/offers/")
        got = next((o for o in r.json() if o["id"] == patch_offer_id), {}) if r.status_code == 200 else {}
        check("hd-patch: PATCH keeps tags/tokens",
              got.get("tags") == [f"smoke-{hd_pid}"] and got.get("tokens") == {"cid": "{click_id}"},
              f"tags={got.get('tags')} tokens={got.get('tokens')}")
    r = s.post(f"{api}/affiliate-networks/", json={
        "name": f"Smoke Patch Net {hd_pid}", "s2s_postback": f"https://pb.example/{hd_pid}"})
    patch_net_id = r.json().get("id")
    check("hd-patch: network created", r.status_code == 200 and bool(patch_net_id), r.text[:200])
    if patch_net_id:
        r = s.patch(f"{api}/affiliate-networks/{patch_net_id}", json={"name": f"Smoke Patch Net 2 {hd_pid}"})
        check("hd-patch: network partial PATCH accepted", r.status_code == 200, r.text[:200])
        r = s.get(f"{api}/affiliate-networks/")
        got = next((n for n in r.json() if n["id"] == patch_net_id), {}) if r.status_code == 200 else {}
        check("hd-patch: network PATCH keeps s2s_postback",
              got.get("s2s_postback") == f"https://pb.example/{hd_pid}", str(got)[:150])

    # ----- P2-12: 'archived' is not a creatable status (PG enum has no such value) -----
    r = s.post(f"{api}/campaigns/", json={
        "name": f"Smoke HD Archived {hd_pid}", "alias": f"smoke-hd-arch-{hd_pid}", "status": "archived"})
    check("hd-arch: 'archived' status rejected (422)", r.status_code == 422, str(r.status_code))

    # ----- P2-13: duplicate-name source PATCH -> 400 -----
    r = s.post(f"{api}/sources/", json={"name": f"Smoke Src A {hd_pid}"})
    src_a = r.json().get("id")
    r = s.post(f"{api}/sources/", json={"name": f"Smoke Src B {hd_pid}"})
    src_b = r.json().get("id")
    check("hd-source: sources created", r.status_code == 200 and bool(src_a) and bool(src_b), r.text[:200])
    if src_a and src_b:
        r = s.patch(f"{api}/sources/{src_b}", json={"name": f"Smoke Src A {hd_pid}"})
        check("hd-source: duplicate-name PATCH -> 400", r.status_code == 400, str(r.status_code))

    # ----- P1-1: campaigns:'own' scope on dashboard metrics/visits/breakdown -----
    hd_funnel_cfg = {"enabled": True,
                     "steps": [{"name": "Step 1", "landing": None, "offers": [],
                                "schema": "landing_offer"}]}
    r = s.post(f"{api}/campaigns/", json={
        "name": f"Smoke HD Own {hd_pid}", "alias": f"smoke-hd-own-{hd_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "weight",
        "config": {"flows": [], "postbacks": [], "hide_referrer": False,
                   "funnel": hd_funnel_cfg}})
    hd_own_cid = r.json().get("id")
    r = s.post(f"{api}/campaigns/", json={
        "name": f"Smoke HD Other {hd_pid}", "alias": f"smoke-hd-other-{hd_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "weight",
        "config": {"flows": [], "postbacks": [], "hide_referrer": False,
                   "funnel": hd_funnel_cfg}})
    hd_other_cid = r.json().get("id")
    check("hd-scope: two campaigns created", bool(hd_own_cid) and bool(hd_other_cid), r.text[:200])
    for _c, _tag in ((hd_own_cid, "own"), (hd_other_cid, "other")):
        requests.post(f"{BASE}/t/collect",
                      json={"c": str(_c), "url": f"https://hd-{_tag}-{hd_pid}.example/lp"},
                      verify=not INSECURE)
    pg_exec("INSERT INTO conversions_data (received_at, click_id, campaign_id, status, revenue, profit) VALUES "
            f"(now(), 'smoke-hd-conv-own-{hd_pid}', {hd_own_cid}, 'sale', 1, 1), "
            f"(now(), 'smoke-hd-conv-other-{hd_pid}', {hd_other_cid}, 'sale', 1, 1)")
    hd_user = f"smoke-hd-{hd_pid}"
    r = s.post(f"{api}/users/", json={
        "username": hd_user, "password": "smokepass1",
        "permissions": {"sections": {"campaigns": True, "dashboard": True, "reports": True},
                        "write": True, "campaigns": "own"}})
    hd_uid = r.json().get("id")
    hs = requests.Session()
    hs.verify = not INSECURE
    if hd_uid:
        r = hs.post(f"{api}/login", json={"username": hd_user, "password": "smokepass1"})
        check("hd-scope: limited user login", r.status_code == 200, r.text[:150])
        pg_exec(f"UPDATE campaigns SET owner_id = {hd_uid} WHERE id = {hd_own_cid}")
        hd_ids = set()
        for _ in range(20):
            r = hs.post(f"{api}/dashboard/breakdown", json={"dimensions": ["campaign_id"], "limit": 500})
            hd_ids = {row.get("value") for row in (r.json().get("rows") or [])} if r.status_code == 200 else set()
            if str(hd_own_cid) in hd_ids:
                break
            _time.sleep(0.5)
        check("hd-scope: breakdown shows only own campaigns",
              r.status_code == 200 and hd_ids and hd_ids <= {str(hd_own_cid)} and str(hd_own_cid) in hd_ids,
              f"status={r.status_code} ids={sorted(hd_ids)[:8]}")
        r = hs.post(f"{api}/dashboard/metrics", json={})
        check("hd-scope: scoped metrics 200 with own visits",
              r.status_code == 200 and ((r.json().get("metrics") or {}).get("visits") or 0) > 0, r.text[:200])
        r = hs.post(f"{api}/dashboard/visits", json={"limit": 100})
        vis_rows = r.json() if r.status_code == 200 else []
        check("hd-scope: visits show own and not other",
              r.status_code == 200
              and any(f"hd-own-{hd_pid}" in (row.get("url") or "") for row in vis_rows)
              and not any(f"hd-other-{hd_pid}" in (row.get("url") or "") for row in vis_rows),
              f"status={r.status_code} n={len(vis_rows)}")
        # conversion_date aggregates carry the same scope
        r = hs.post(f"{api}/dashboard/breakdown",
                    json={"dimensions": ["campaign_id"], "date_basis": "conversion_date"})
        conv_vals = {row.get("value") for row in (r.json().get("rows") or [])} if r.status_code == 200 else set()
        check("hd-scope: conversion_date breakdown hides other campaigns",
              r.status_code == 200 and str(hd_other_cid) not in conv_vals
              and str(hd_own_cid) in conv_vals, f"vals={sorted(conv_vals)[:8]}")
        r = s.post(f"{api}/dashboard/breakdown",
                   json={"dimensions": ["campaign_id"], "date_basis": "conversion_date", "limit": 2000})
        admin_vals = {row.get("value") for row in (r.json().get("rows") or [])} if r.status_code == 200 else set()
        check("hd-scope: admin conversion breakdown sees both campaigns",
              r.status_code == 200 and str(hd_own_cid) in admin_vals and str(hd_other_cid) in admin_vals,
              f"vals={sorted(admin_vals)[:8]}")
        # P1-5: funnel + global search respect the same scope
        r = hs.get(f"{api}/reports/funnel/{hd_own_cid}")
        check("hd-funnel: own campaign funnel readable", r.status_code == 200, r.text[:150])
        r = hs.get(f"{api}/reports/funnel/{hd_other_cid}")
        check("hd-funnel: other campaign funnel -> 404", r.status_code == 404, str(r.status_code))
        r = s.get(f"{api}/reports/funnel/{hd_other_cid}")
        check("hd-funnel: admin still reads funnel", r.status_code == 200, r.text[:150])
        r = hs.get(f"{api}/search", params={"q": "smoke-hd-conv"})
        hd_conv_names = [x.get("name") for x in (r.json().get("groups") or {}).get("conversions", [])]
        check("hd-search: scoped search hides other campaign conversions",
              r.status_code == 200 and hd_conv_names
              and all(f"conv-other-{hd_pid}" not in n for n in hd_conv_names)
              and any(f"conv-own-{hd_pid}" in n for n in hd_conv_names), str(hd_conv_names)[:150])
    # campaigns:'own' with NO owned campaigns -> empty results, never 'no filter'
    hd_user2 = f"smoke-hd-none-{hd_pid}"
    r = s.post(f"{api}/users/", json={
        "username": hd_user2, "password": "smokepass1",
        "permissions": {"sections": {"dashboard": True, "reports": True},
                        "write": True, "campaigns": "own"}})
    hd_uid2 = r.json().get("id")
    if hd_uid2:
        s2 = requests.Session()
        s2.verify = not INSECURE
        s2.post(f"{api}/login", json={"username": hd_user2, "password": "smokepass1"})
        r = s2.post(f"{api}/dashboard/breakdown", json={"dimensions": ["campaign_id"]})
        check("hd-scope: empty scope -> empty breakdown",
              r.status_code == 200 and r.json().get("rows") == [], r.text[:150])
        r = s2.post(f"{api}/dashboard/metrics", json={})
        check("hd-scope: empty scope -> all-zero metrics",
              r.status_code == 200 and ((r.json().get("metrics") or {}).get("visits") or 0) == 0, r.text[:150])
        r = s2.post(f"{api}/dashboard/visits", json={"limit": 50})
        check("hd-scope: empty scope -> empty visits", r.status_code == 200 and r.json() == [], r.text[:150])
        r = s.delete(f"{api}/users/{hd_uid2}")
        check("hd-scope: empty-scope user cleaned up", r.status_code == 200, r.text[:150])

    # ----- P1-2/P1-3: bad bodies and bad dates -> 4xx, never 500 -----
    r = s.post(f"{api}/dashboard/metrics", data="not-json", headers={"Content-Type": "application/json"})
    check("hd-validate: non-JSON metrics body -> 422", r.status_code == 422, str(r.status_code))
    r = s.post(f"{api}/dashboard/metrics", json=[1, 2])
    check("hd-validate: array metrics body -> 422", r.status_code == 422, str(r.status_code))
    r = s.post(f"{api}/dashboard/metrics", json={"date_from": "nonsense", "date_to": "2026-01-01"})
    check("hd-validate: bad metrics dates -> 400", r.status_code == 400, str(r.status_code))
    r = s.post(f"{api}/dashboard/breakdown", json={
        "dimensions": ["campaign_id"], "filters": {"date_from": "nonsense", "date_to": "2026-01-01"}})
    check("hd-validate: bad breakdown dates -> 400", r.status_code == 400, str(r.status_code))
    r = s.get(f"{api}/reports/", params={"date_from": "nonsense", "date_to": "2026-01-01"})
    check("hd-validate: bad conversion dates -> 400", r.status_code == 400, str(r.status_code))

    # ----- P1-4: LIKE wildcards stay literal -----
    r = s.get(f"{api}/reports/", params={"search": f"smoke-hd-conv_other-{hd_pid}"})
    hd_like_hits = [c for c in (r.json() if r.status_code == 200 else [])
                    if f"smoke-hd-conv-{hd_pid}" in (c.get("click_id") or "")]
    check("hd-like: underscore is literal in conversion search",
          r.status_code == 200 and not hd_like_hits, str([c.get("click_id") for c in hd_like_hits])[:150])
    r = s.get(f"{api}/reports/", params={"search": f"smoke-hd-conv-own-{hd_pid}"})
    check("hd-like: literal conversion search still matches",
          r.status_code == 200
          and any(c.get("click_id") == f"smoke-hd-conv-own-{hd_pid}" for c in r.json()), r.text[:200])
    r = s.get(f"{api}/search", params={"q": f"smoke-hd-conv_other-{hd_pid}"})
    grp = (r.json().get("groups") or {}).get("conversions", [])
    check("hd-like: underscore is literal in global search", r.status_code == 200 and not grp, str(grp)[:120])
    r = s.get(f"{api}/search", params={"q": f"smoke-hd-conv-own-{hd_pid}"})
    grp = (r.json().get("groups") or {}).get("conversions", [])
    check("hd-like: global search still matches literally", r.status_code == 200 and bool(grp), str(grp)[:120])

    # ----- P1-9: tracker_admin / last-active-admin guards -----
    r = s.get(f"{api}/users/")
    hd_ta = next((u for u in (r.json() or []) if (u.get("username") or "").lower() == "tracker_admin"), None)
    if hd_ta:
        r = s.patch(f"{api}/users/{hd_ta['id']}", json={"username": hd_ta["username"], "active": False})
        check("hd-admin: cannot deactivate tracker_admin", r.status_code == 400, r.text[:120])
        r = s.patch(f"{api}/users/{hd_ta['id']}", json={"username": hd_ta["username"], "is_admin": False})
        check("hd-admin: cannot demote tracker_admin", r.status_code == 400, r.text[:120])
        r = s.delete(f"{api}/users/{hd_ta['id']}")
        check("hd-admin: cannot delete tracker_admin", r.status_code == 400, r.text[:120])
    hd_admin2 = f"smoke-hd-admin2-{hd_pid}"
    r = s.post(f"{api}/users/", json={"username": hd_admin2, "password": "smokepass1",
                                      "is_admin": True, "active": True})
    hd_admin2_id = r.json().get("id")
    check("hd-admin: second admin created", r.status_code == 200 and bool(hd_admin2_id), r.text[:150])
    if hd_admin2_id:
        r = s.patch(f"{api}/users/{hd_admin2_id}", json={"username": hd_admin2, "active": False})
        check("hd-admin: non-last admin can be deactivated", r.status_code == 200, r.text[:150])
        r = s.patch(f"{api}/users/{hd_admin2_id}", json={"username": hd_admin2, "active": True})
        check("hd-admin: admin reactivated", r.status_code == 200, r.text[:150])
        r = s.delete(f"{api}/users/{hd_admin2_id}")
        check("hd-admin: second admin cleaned up", r.status_code == 200, r.text[:150])

    # ----- P1-10: settings import must keep saved-report share tokens -----
    r = s.post(f"{api}/settings/saved-reports", json={
        "name": f"smoke-hd-share-{hd_pid}", "config": {"dimensions": ["campaign_id"]}})
    hd_rid = (r.json().get("report") or {}).get("id")
    check("hd-share: saved report created", r.status_code == 200 and bool(hd_rid), r.text[:200])
    hd_token = None
    if hd_rid:
        r = s.post(f"{api}/settings/saved-reports/{hd_rid}/share")
        hd_token = (r.json().get("share") or {}).get("token")
        check("hd-share: token minted", bool(hd_token), r.text[:150])
        r = s.get(f"{api}/settings/export")
        hd_export = (r.json().get("data") or {}) if r.status_code == 200 else {}
        r = s.post(f"{api}/settings/import", json={"data": {"saved_reports": hd_export.get("saved_reports")}})
        check("hd-share: backup with saved_reports re-imported", r.status_code == 200, r.text[:200])
        r = s.get(f"{api}/settings/saved-reports")
        rep = next((x for x in r.json().get("reports", []) if x.get("id") == hd_rid), {})
        check("hd-share: token survives import",
              (rep.get("share") or {}).get("token") == hd_token, str(rep.get("share"))[:120])
        r = requests.post(f"{api}/dashboard/public/report/{hd_token}", verify=not INSECURE)
        check("hd-share: public link still serves after import", r.status_code == 200, r.text[:150])

    # ----- P1-6: MCP write gate + argument validation -----
    r = s.post(f"{api}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "campaigns.set_status", "arguments": [1, 2]}})
    err = (r.json() or {}).get("error") or {}
    check("hd-mcp: non-dict arguments -> -32602",
          r.status_code == 200 and err.get("code") == -32602, r.text[:200])
    r = s.post(f"{api}/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "ping"})
    check("hd-mcp: ping works for admin", r.status_code == 200, r.text[:120])
    if hd_uid:
        r = hs.post(f"{api}/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                        "params": {"name": "campaigns.set_status",
                                                   "arguments": {"campaign_id": hd_own_cid or 0,
                                                                 "status": "paused"}}})
        check("hd-mcp: mutating tool blocked without settings write", r.status_code == 403, str(r.status_code))

    # ----- P2-11: costs target must exist + mutation is audited -----
    r = s.post(f"{api}/costs/update", json={
        "campaign_id": 99999999, "period": {"from": hd_today, "to": hd_today}, "cost": 0})
    check("hd-costs: unknown campaign -> 404", r.status_code == 404, str(r.status_code))
    if hd_own_cid:
        r = s.post(f"{api}/costs/update", json={
            "campaign_id": hd_own_cid, "period": {"from": hd_today, "to": hd_today}, "cost": 0})
        check("hd-costs: real update ok", r.status_code == 200 and "updated_rows" in r.json(), r.text[:150])
        hd_cnt = pg_query("SELECT count(*) FROM audit_log WHERE action='cost_update'")
        check("hd-costs: cost update audited", (hd_cnt or "0").isdigit() and int(hd_cnt) > 0, hd_cnt[:40])

    # ----- P2-14: destructive/money ops are audited -----
    r = s.post(f"{api}/settings/", json={"settings": {"currency": (s.get(f"{api}/settings/").json().get("settings") or {}).get("currency")}})
    check("hd-audit: settings save accepted", r.status_code == 200, r.text[:150])
    hd_cnt = pg_query("SELECT count(*) FROM audit_log WHERE action='save' AND entity='settings'")
    check("hd-audit: settings save audited", (hd_cnt or "0").isdigit() and int(hd_cnt) > 0, hd_cnt[:40])
    r = s.post(f"{api}/reports/import", json={"lines": f"smoke-hd-imp-{hd_pid},0,tid-hd-{hd_pid},sale"})
    check("hd-audit: conversion import accepted", r.status_code == 200, r.text[:150])
    hd_cnt = pg_query("SELECT count(*) FROM audit_log WHERE action='import' AND entity='conversions'")
    check("hd-audit: conversion import audited", (hd_cnt or "0").isdigit() and int(hd_cnt) > 0, hd_cnt[:40])
    hd_imp_id = (pg_query(f"SELECT id FROM conversions_data WHERE external_id='tid-hd-{hd_pid}' "
                          "ORDER BY id DESC LIMIT 1").splitlines() or [""])[0].strip()
    if hd_imp_id:
        r = s.delete(f"{api}/reports/{hd_imp_id}")
        check("hd-audit: imported conversion cleaned up", r.status_code == 200, r.text[:150])

    # ----- P2-15: optimizer PUT keeps run history -----
    if hd_other_cid:
        pg_exec("UPDATE campaigns SET config = jsonb_set(COALESCE(config, '{}'::jsonb), '{optimizer}', "
                "'{\"enabled\": true, \"last_runs\": [{\"at\": \"2026-01-01T00:00:00\", \"reason\": \"optimized\"}]}'::jsonb) "
                f"WHERE id = {hd_other_cid}")
        r = s.put(f"{api}/optimizer/{hd_other_cid}", json={"metric": "cr"})
        check("hd-opt: PUT accepted", r.status_code == 200, r.text[:200])
        hd_runs = pg_query(f"SELECT config->'optimizer'->'last_runs' FROM campaigns WHERE id = {hd_other_cid}")
        check("hd-opt: last_runs survives PUT", "2026-01-01T00:00:00" in (hd_runs or ""), hd_runs[:120])
        r = s.get(f"{api}/optimizer/{hd_other_cid}")
        check("hd-opt: GET exposes updated optimizer block",
              r.status_code == 200 and (r.json().get("optimizer") or {}).get("metric") == "cr", r.text[:200])

    # ----- P1-8: a refreshed TOTP token does not reset the failure budget -----
    if pyotp is not None:
        hd_totp_user = f"smoke-hd-totp-{hd_pid}"
        r = s.post(f"{api}/users/", json={"username": hd_totp_user, "password": "smokepass1", "active": True})
        hd_totp_uid = r.json().get("id")
        ts = requests.Session()
        ts.verify = not INSECURE
        ts.post(f"{api}/login", json={"username": hd_totp_user, "password": "smokepass1"})
        r = ts.post(f"{api}/users/me/totp/setup")
        hd_totp_secret = r.json().get("secret")
        r = ts.post(f"{api}/users/me/totp/enable", json={"code": pyotp.TOTP(hd_totp_secret).now()})
        check("hd-totp: 2FA enabled", r.status_code == 200, r.text[:150])
        r = requests.post(f"{api}/login", json={"username": hd_totp_user, "password": "smokepass1"},
                          verify=not INSECURE)
        hd_t1 = r.json().get("totp_token")
        hd_codes = [requests.post(f"{api}/login/totp", json={"totp_token": hd_t1, "code": "000000"},
                                  verify=not INSECURE).status_code for _ in range(3)]
        check("hd-totp: 3 wrong codes -> 401x3", hd_codes == [401, 401, 401], str(hd_codes))
        r = requests.post(f"{api}/login", json={"username": hd_totp_user, "password": "smokepass1"},
                          verify=not INSECURE)
        hd_t2 = r.json().get("totp_token")
        r = requests.post(f"{api}/login/totp",
                          json={"totp_token": hd_t2, "code": pyotp.TOTP(hd_totp_secret).now()},
                          verify=not INSECURE)
        check("hd-totp: fresh token does not reset the failure budget", r.status_code == 429, r.text[:150])
        if hd_totp_uid:
            r = s.delete(f"{api}/users/{hd_totp_uid}")
            check("hd-totp: user cleaned up", r.status_code == 200, r.text[:150])

    # ----- P2-17: audit rows carry the real client IP, not the nginx proxy IP -----
    try:
        hd_ng_ip = subprocess.run(
            ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
             "tracker_nginx"], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        hd_ng_ip = ""
    requests.post(f"{api}/login", json={"username": USER, "password": PASS}, verify=not INSECURE)
    hd_ip_row = pg_query("SELECT ip FROM audit_log WHERE action='login_success' ORDER BY id DESC LIMIT 1")
    check("hd-ip: login audit IP is not the nginx proxy IP",
          bool(hd_ip_row) and bool(hd_ng_ip) and hd_ip_row != hd_ng_ip,
          f"audit={hd_ip_row} nginx={hd_ng_ip}")

    # ----- hardening block cleanup -----
    if patch_offer_id:
        r = s.delete(f"{api}/offers/{patch_offer_id}")
        check("hd-cleanup: patch offer deleted", r.status_code == 200, r.text[:120])
    if patch_net_id:
        r = s.delete(f"{api}/affiliate-networks/{patch_net_id}")
        check("hd-cleanup: patch network deleted", r.status_code == 200, r.text[:120])
    for _sid in (src_a, src_b):
        if _sid:
            r = s.delete(f"{api}/sources/{_sid}")
            check("hd-cleanup: source deleted", r.status_code == 200, r.text[:120])
    if hd_rid:
        r = s.delete(f"{api}/settings/saved-reports/{hd_rid}")
        check("hd-cleanup: saved report deleted", r.status_code == 200, r.text[:120])
    pg_exec(f"DELETE FROM conversions_data WHERE click_id LIKE 'smoke-hd-conv-{hd_pid}-%'")
    if hd_uid:
        r = s.delete(f"{api}/users/{hd_uid}")
        check("hd-cleanup: limited user deleted", r.status_code == 200, r.text[:150])
    for _c in (hd_own_cid, hd_other_cid):
        if _c:
            r = s.delete(f"{api}/campaigns/{_c}")
            check("hd-cleanup: campaign deleted", r.status_code == 200, r.text[:150])

    # ----- Reports depth: click-log tag filter, pagination tiebreak, offer pause, click_date round-trip -----
    rt_pid = os.getpid()
    rt_tag_a = f"smoke-rt-tag-a-{rt_pid}"
    rt_tag_b = f"smoke-rt-tag-b-{rt_pid}"
    rt_today = str(datetime.now(timezone.utc).date())

    def _rt_campaign(suffix):
        rt_alias = f"smoke-rt-{suffix}-{rt_pid}"
        rr = s.post(f"{api}/campaigns/", json={
            "name": rt_alias, "alias": rt_alias, "type": "campaign", "status": "active",
            "redirect_mode": "position",
            "config": {"flows": [], "postbacks": [], "hide_referrer": False,
                       "fallback_url": f"https://example.com/smoke-rt-{suffix}-{rt_pid}-fb"}})
        return rr.json().get("id") if rr.status_code == 200 else None

    rt_cid_a = _rt_campaign("a")
    rt_cid_b = _rt_campaign("b")
    rt_cid_page = _rt_campaign("page")  # noqa: F841
    check("rt-tags: campaigns created", all([rt_cid_a, rt_cid_b, rt_cid_page]),
          f"{rt_cid_a},{rt_cid_b},{rt_cid_page}")
    if rt_cid_a:
        s.patch(f"{api}/campaigns/{rt_cid_a}/tags", json={"tags": [rt_tag_a]})
    if rt_cid_b:
        s.patch(f"{api}/campaigns/{rt_cid_b}/tags", json={"tags": [rt_tag_b]})

    if rt_cid_a and rt_cid_b:
        ch_query(
            f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, click_id, url, country, cost) "
            f"SELECT now(), {rt_cid_a}, NULL, '', 'smoke-rt-{rt_pid}-a-' || toString(number), "
            f"'smoke-rt-{rt_pid}-a-' || toString(number), "
            f"'https://example.com/smoke-rt-{rt_pid}-a-' || toString(number), 'US', 0.01 FROM numbers(3)")
        ch_query(
            f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, click_id, url, country, cost) "
            f"SELECT now(), {rt_cid_b}, NULL, '', 'smoke-rt-{rt_pid}-b-' || toString(number), "
            f"'smoke-rt-{rt_pid}-b-' || toString(number), "
            f"'https://example.com/smoke-rt-{rt_pid}-b-' || toString(number), 'US', 0.01 FROM numbers(2)")

        r_tag = s.post(f"{api}/dashboard/click-log", json={
            "campaign_tags": [rt_tag_a], "limit": 50, "offset": 0})
        tagged = r_tag.json().get("items") if r_tag.status_code == 200 else []
        check("rt-tags: click-log tag filter returns only campaigns with the tag",
              r_tag.status_code == 200 and len(tagged) == 3
              and all(x.get("campaign_id") == rt_cid_a for x in tagged),
              r_tag.text[:200])
        check("rt-tags: tag-filtered count matches rows",
              r_tag.status_code == 200 and r_tag.json().get("total") == len(tagged),
              r_tag.text[:200])
        r_notag = s.post(f"{api}/dashboard/click-log", json={
            "campaigns": [rt_cid_a, rt_cid_b], "limit": 50, "offset": 0})
        notag = r_notag.json().get("items") if r_notag.status_code == 200 else []
        check("rt-tags: untagged query sees both campaigns' clicks",
              r_notag.status_code == 200 and {x.get("campaign_id") for x in notag} == {rt_cid_a, rt_cid_b},
              r_notag.text[:200])
        r_exp = s.post(f"{api}/dashboard/click-log/export", json={"campaign_tags": [rt_tag_a]})
        exported = (sum(1 for ln in r_exp.text.splitlines() if f"smoke-rt-{rt_pid}-a-" in ln)
                    if r_exp.status_code == 200 else -1)
        check("rt-tags: export honours the tag filter", r_exp.status_code == 200 and exported == 3,
              f"{r_exp.status_code} {exported}")

    # Pagination tiebreak: 25 rows sharing received_at/visitor_id, unique click_id.
    if rt_cid_page:
        ch_query(
            f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, click_id, url, country, cost) "
            f"SELECT toDateTime('2026-01-01 00:00:00'), {rt_cid_page}, NULL, '', '', "
            f"'smoke-rt-{rt_pid}-page-' || leftPad(toString(number), 2, '0'), "
            f"'https://example.com/smoke-rt-{rt_pid}-page-' || leftPad(toString(number), 2, '0'), 'US', 0.01 "
            f"FROM numbers(25)")
        rt_seen = []
        rt_total = None
        rt_ok_pages = True
        for rt_off in (0, 10, 20):
            rr = s.post(f"{api}/dashboard/click-log", json={
                "campaigns": [rt_cid_page], "limit": 10, "offset": rt_off})
            if rr.status_code != 200:
                rt_ok_pages = False
                break
            body = rr.json()
            rt_total = body.get("total")
            rt_seen.extend(x.get("url") for x in body.get("items", []))
        check("rt-page: identical-timestamp rows page cleanly (total 25, no dups/gaps)",
              rt_ok_pages and rt_total == 25 and len(rt_seen) == 25 and len(set(rt_seen)) == 25
              and all(u for u in rt_seen),
              f"total={rt_total} seen={len(rt_seen)} uniq={len(set(rt_seen))}")

    # Pause/resume offer endpoint behaviour.
    r = s.post(f"{api}/offers/", json={
        "name": f"Smoke RT Offer {rt_pid}", "url": f"https://example.com/smoke-rt-offer-{rt_pid}",
        "status": "active"})
    rt_oid = r.json().get("id") if r.status_code == 200 else None
    check("rt-pause: offer created", r.status_code == 200 and bool(rt_oid), r.text[:150])
    if rt_oid:
        r = s.post(f"{api}/offers/{rt_oid}/status", json={"status": "paused"})
        check("rt-pause: pause accepted", r.status_code == 200 and r.json().get("status") == "paused",
              r.text[:150])
        check("rt-pause: DB reflects paused",
              pg_query(f"SELECT status FROM offers WHERE id = {rt_oid}") == "paused",
              pg_query(f"SELECT status FROM offers WHERE id = {rt_oid}"))
        r = s.post(f"{api}/offers/{rt_oid}/status", json={"status": "active"})
        check("rt-pause: resume accepted", r.status_code == 200 and r.json().get("status") == "active",
              r.text[:150])
        r = s.post(f"{api}/offers/{rt_oid}/status", json={"status": "bogus"})
        check("rt-pause: invalid status -> 400", r.status_code == 400, str(r.status_code))
        r = s.post(f"{api}/offers/999999999/status", json={"status": "paused"})
        check("rt-pause: unknown offer -> 404", r.status_code == 404, str(r.status_code))

    # click_date basis still attributes by the CLICK date after the bounded round-trip.
    if rt_cid_a:
        ch_query(
            f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, click_id, country, cost) "
            f"VALUES (now(), {rt_cid_a}, true, '', 'smoke-rt-{rt_pid}-conv-today', "
            f"'smoke-rt-{rt_pid}-conv-today', 'US', 0.1)")
        ch_query(
            f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, click_id, country, cost) "
            f"VALUES (toDateTime(now()) - INTERVAL 10 DAY, {rt_cid_a}, true, '', 'smoke-rt-{rt_pid}-conv-old', "
            f"'smoke-rt-{rt_pid}-conv-old', 'US', 0.1)")
        pg_exec(
            f"INSERT INTO conversions_data (received_at, click_id, campaign_id, offer_id, status, payout, revenue, profit, visitor_id) "
            f"VALUES (now() - interval '1 day', 'smoke-rt-{rt_pid}-conv-today', {rt_cid_a}, 5, 'sale', 4, 4, 4, "
            f"'smoke-rt-{rt_pid}-conv-today')")
        pg_exec(
            f"INSERT INTO conversions_data (received_at, click_id, campaign_id, offer_id, status, payout, revenue, profit, visitor_id) "
            f"VALUES (now(), 'smoke-rt-{rt_pid}-conv-old', {rt_cid_a}, 5, 'sale', 4, 4, 4, "
            f"'smoke-rt-{rt_pid}-conv-old')")
        r_tk = s.get(f"{api}/reports/", params={
            "date_from": rt_today, "date_to": rt_today, "date_basis": "click_date",
            "click_id": f"smoke-rt-{rt_pid}-conv-today"})
        r_old = s.get(f"{api}/reports/", params={
            "date_from": rt_today, "date_to": rt_today, "date_basis": "click_date",
            "click_id": f"smoke-rt-{rt_pid}-conv-old"})
        check("rt-conv: click_date keeps a conversion that landed after its today click",
              r_tk.status_code == 200
              and any(c["click_id"] == f"smoke-rt-{rt_pid}-conv-today" for c in r_tk.json()),
              r_tk.text[:200])
        check("rt-conv: click_date hides a conversion whose click is outside the window",
              r_old.status_code == 200
              and not any(c["click_id"] == f"smoke-rt-{rt_pid}-conv-old" for c in r_old.json()),
              r_old.text[:200])

    # ----- rt cleanup -----
    ch_query(f"ALTER TABLE clicks_data DELETE WHERE click_id LIKE 'smoke-rt-{rt_pid}-%'")
    pg_exec(f"DELETE FROM conversions_data WHERE click_id LIKE 'smoke-rt-{rt_pid}-%'")
    if rt_oid:
        s.delete(f"{api}/offers/{rt_oid}")
    for _c in (rt_cid_a, rt_cid_b, rt_cid_page):
        if _c:
            s.delete(f"{api}/campaigns/{_c}")

    # ===== G85/G86/G87: postback rules, fanout controls, /pb hardening =====
    import time as _g8time
    import threading as _g8threading
    import http.server as _g8httpserver
    import socketserver as _g8socketserver
    import urllib.parse as _g8urlparse
    import hashlib as _g8hashlib

    g8_pid = os.getpid()

    def g8_db(sql):
        return subprocess.run(
            ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db", "-tAc", sql],
            capture_output=True, text=True, timeout=30).stdout.strip()

    # Local HTTP receiver the frontend container reaches via host.docker.internal —
    # lets these checks observe the outgoing postback payload the tracking plane fires.
    g8_port = 18000 + (g8_pid % 1000)
    g8_captured = []
    _g8socketserver.TCPServer.allow_reuse_address = True

    class _G8Receiver(_g8httpserver.BaseHTTPRequestHandler):
        def _ok(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
            g8_captured.append({"method": self.command, "path": self.path, "body": body})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        do_GET = _ok
        do_POST = _ok

        def log_message(self, *args):
            pass

    g8_srv = _g8socketserver.TCPServer(("0.0.0.0", g8_port), _G8Receiver)
    g8_srv.daemon_threads = True
    _g8threading.Thread(target=g8_srv.serve_forever, daemon=True).start()
    g8_recv = f"http://host.docker.internal:{g8_port}"

    g8_probe = ""
    try:
        g8_probe = subprocess.run(
            ["docker", "exec", "tracker_frontend", "curl", "-s", "-m", "3", f"{g8_recv}/ready"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        pass
    check("g87: postback receiver reachable from the frontend container", "ok" in g8_probe,
          g8_probe[:80])

    def g8_paths(marker):
        return [c["path"] for c in g8_captured if marker in c["path"]]

    def g8_wait(marker, n, timeout=20):
        end = _g8time.time() + timeout
        while _g8time.time() < end:
            if len(g8_paths(marker)) >= n:
                break
            _g8time.sleep(0.5)
        return g8_paths(marker)

    g8_campaigns, g8_sources = [], []

    def g8_new_campaign(tag, **kw):
        config = {"flows": [], "postbacks": [], "hide_referrer": False}
        config.update(kw.pop("config", {}))
        r = s.post(f"{api}/campaigns/", json={
            "name": f"{tag}-{g8_pid}", "alias": f"{tag}-{g8_pid}", "type": "campaign",
            "status": "active", "redirect_mode": "position", "config": config, **kw})
        cid_ = r.json().get("id")
        if cid_:
            g8_campaigns.append(cid_)
        return cid_

    def g8_seed(click, cid):
        g8_db("INSERT INTO conversions_data (received_at, click_id, campaign_id, status, payout, "
              f"revenue, profit) VALUES (now(), '{click}', {cid}, 'lead', 0, 0, 0)")

    # --- G85: global postback-processing rules (settings.postback_rules) ---
    g8_saved_pr = (s.get(f"{api}/settings/").json().get("settings") or {}).get("postback_rules")
    g8_rules = [
        {"enabled": True, "name": "g8 reject", "conditions": [
            {"field": "click_id", "operator": "starts_with", "value": f"g85rej-{g8_pid}"}],
         "action": {"type": "reject"}},
        {"enabled": True, "name": "g8 remap", "conditions": [
            {"field": "click_id", "operator": "equals", "value": f"g85status-{g8_pid}"}],
         "action": {"type": "set_status", "value": "rejected"}},
        {"enabled": True, "name": "g8 abs", "conditions": [
            {"field": "click_id", "operator": "equals", "value": f"g85payout-{g8_pid}"}],
         "action": {"type": "set_payout", "value": 12.5}},
        {"enabled": True, "name": "g8 mult", "conditions": [
            {"field": "click_id", "operator": "equals", "value": f"g85mult-{g8_pid}"}],
         "action": {"type": "set_payout", "mode": "multiplier", "value": 3}},
        {"enabled": False, "name": "g8 disabled", "conditions": [
            {"field": "click_id", "operator": "equals", "value": f"g85dis-{g8_pid}"}],
         "action": {"type": "reject"}},
    ]
    r = s.post(f"{api}/settings/", json={"settings": {"postback_rules": g8_rules}})
    check("g85: postback rules saved", r.status_code == 200, r.text[:120])
    # the tracking plane caches settings blocks for 30s — wait out the TTL
    settle_settings_cache()

    g8_cid_tok = g8_new_campaign("smoke-g8tok", config={"postbacks": [{
        "url": f"{g8_recv}/tok?cid={{click_id}}&md5={{_md5}}&u={{unixconversiontime}}"
               "&s2={status2}&e1={event_1}&payout=auto",
        "method": "GET", "status": {}}]})
    check("g85/g87: token campaign created", bool(g8_cid_tok), str(g8_cid_tok))

    # reject: fresh click -> no row written; seeded click -> no fanout
    g8_rej = f"g85rej-{g8_pid}"
    r = requests.get(f"{BASE}/pb?clickid={g8_rej}&status=sale&payout=5", verify=not INSECURE)
    g8_rej_body = r.json() if "json" in r.headers.get("content-type", "") else {}
    check("g85: reject returns 200 with a clear reason",
          r.status_code == 200 and g8_rej_body.get("status") == "rejected"
          and bool(g8_rej_body.get("reason")), r.text[:160])
    check("g85: reject writes no conversion row",
          g8_db(f"SELECT count(*) FROM conversions_data WHERE click_id='{g8_rej}'") == "0",
          g8_db(f"SELECT count(*) FROM conversions_data WHERE click_id='{g8_rej}'"))
    if g8_cid_tok:
        g8_rej2 = f"g85rej-{g8_pid}-fan"
        g8_seed(g8_rej2, g8_cid_tok)
        g8_captured.clear()
        requests.get(f"{BASE}/pb?clickid={g8_rej2}&status=sale&payout=5", verify=not INSECURE)
        _g8time.sleep(3)
        check("g85: reject suppresses fanout", g8_paths(g8_rej2) == [], str(g8_paths(g8_rej2)))

    g8_ss = f"g85status-{g8_pid}"
    requests.get(f"{BASE}/pb?clickid={g8_ss}&status=sale&payout=5", verify=not INSECURE)
    check("g85: set_status remaps the written status",
          g8_db(f"SELECT status FROM conversions_data WHERE click_id='{g8_ss}'") == "rejected",
          g8_db(f"SELECT status FROM conversions_data WHERE click_id='{g8_ss}'"))

    g8_pa = f"g85payout-{g8_pid}"
    requests.get(f"{BASE}/pb?clickid={g8_pa}&status=sale&payout=2", verify=not INSECURE)
    check("g85: set_payout absolute value applied",
          g8_db(f"SELECT payout FROM conversions_data WHERE click_id='{g8_pa}'") == "12.5",
          g8_db(f"SELECT payout FROM conversions_data WHERE click_id='{g8_pa}'"))
    g8_pm = f"g85mult-{g8_pid}"
    requests.get(f"{BASE}/pb?clickid={g8_pm}&status=sale&payout=2", verify=not INSECURE)
    check("g85: set_payout multiplier applied",
          g8_db(f"SELECT payout FROM conversions_data WHERE click_id='{g8_pm}'") == "6",
          g8_db(f"SELECT payout FROM conversions_data WHERE click_id='{g8_pm}'"))

    g8_dis = f"g85dis-{g8_pid}"
    r = requests.get(f"{BASE}/pb?clickid={g8_dis}&status=sale&payout=5", verify=not INSECURE)
    check("g85: disabled rule ignored (postback processed normally)",
          r.status_code == 200 and r.json().get("duplicate") is False
          and g8_db(f"SELECT count(*) FROM conversions_data WHERE click_id='{g8_dis}'") == "1",
          r.text[:160])

    # --- G86: traffic-source conversion fanout controls (additional_settings) ---
    r = s.post(f"{api}/sources/", json={
        "name": f"smoke-g8sample-{g8_pid}", "s2s_postback": f"{g8_recv}/sample",
        "additional_settings": {"sample_percent": 50}})
    g8_src_sample = r.json().get("id")
    if g8_src_sample:
        g8_sources.append(g8_src_sample)
    r = s.post(f"{api}/sources/", json={
        "name": f"smoke-g8upsell-{g8_pid}", "s2s_postback": f"{g8_recv}/upsell",
        "additional_settings": {"disable_upsell": True, "sample_percent": 100}})
    g8_src_upsell = r.json().get("id")
    if g8_src_upsell:
        g8_sources.append(g8_src_upsell)
    check("g86: sources created", bool(g8_src_sample) and bool(g8_src_upsell), r.text[:150])

    g8_cid_sample = g8_new_campaign("smoke-g8sample", traffic_source_id=g8_src_sample) \
        if g8_src_sample else None
    g8_cid_upsell = g8_new_campaign("smoke-g8upsell", traffic_source_id=g8_src_upsell) \
        if g8_src_upsell else None
    check("g86: source-linked campaigns created", bool(g8_cid_sample) and bool(g8_cid_upsell),
          f"{g8_cid_sample}/{g8_cid_upsell}")

    if g8_cid_sample:
        # determinism: the same click_id must yield the same decision twice
        g8_captured.clear()
        det_clicks = [f"g86det-{g8_pid}-a", f"g86det-{g8_pid}-b"]
        for cl in det_clicks:
            g8_seed(cl, g8_cid_sample)
            requests.get(f"{BASE}/pb?clickid={cl}&status=lead&payout=1", verify=not INSECURE)
            requests.get(f"{BASE}/pb?clickid={cl}&status=sale&payout=2", verify=not INSECURE)
        _g8time.sleep(4)
        det_counts = {cl: len(g8_paths(cl)) for cl in det_clicks}
        check("g86: sampling decision is deterministic per click_id",
              all(n in (0, 2) for n in det_counts.values()), str(det_counts))

        # distribution roughly matches the configured share over many ids
        dist_ids = [f"g86dist-{g8_pid}-{i}" for i in range(40)]
        g8_db("INSERT INTO conversions_data (received_at, click_id, campaign_id, status, payout, "
              "revenue, profit) VALUES " + ",".join(
                  f"(now(), '{i}', {g8_cid_sample}, 'lead', 0, 0, 0)" for i in dist_ids))
        g8_captured.clear()
        for cl in dist_ids:
            requests.get(f"{BASE}/pb?clickid={cl}&status=sale&payout=1", verify=not INSECURE)
        end = _g8time.time() + 25
        while _g8time.time() < end:
            if sum(1 for c in g8_captured if "g86dist-" in c["path"]) >= 40:
                break
            _g8time.sleep(0.5)
        dist_n = sum(1 for c in g8_captured if "g86dist-" in c["path"])
        check("g86: sample_percent ~50% distribution over many ids", 8 <= dist_n <= 32,
              f"{dist_n}/40 forwarded")

    if g8_cid_upsell:
        g8_captured.clear()
        su = f"g86up-sale-{g8_pid}"
        g8_seed(su, g8_cid_upsell)
        requests.get(f"{BASE}/pb?clickid={su}&status=sale&payout=1", verify=not INSECURE)
        uu = f"g86up-ups-{g8_pid}"
        g8_seed(uu, g8_cid_upsell)
        requests.get(f"{BASE}/pb?clickid={uu}&status=upsale&payout=1", verify=not INSECURE)
        g8_wait(su, 1, timeout=8)
        _g8time.sleep(1)
        check("g86: disable_upsell keeps the sale forward", len(g8_paths(su)) == 1,
              str(g8_paths(su)))
        check("g86: disable_upsell skips the upsell forward", g8_paths(uu) == [],
              str(g8_paths(uu)))

    # --- G87: /pb hardening + extra tokens ---
    if g8_cid_tok:
        g8_tok = f"g87tok-{g8_pid}"
        g8_seed(g8_tok, g8_cid_tok)
        g8_captured.clear()
        requests.get(f"{BASE}/pb?clickid={g8_tok}&status=sale&payout=7.5", verify=not INSECURE)
        tok_paths = g8_wait(g8_tok, 1, timeout=8)
        g8_q = _g8urlparse.parse_qs(_g8urlparse.urlparse(tok_paths[-1]).query) if tok_paths else {}
        check("g87: {_md5} token substituted in the fired postback",
              g8_q.get("md5", [""])[0] == _g8hashlib.md5(g8_tok.encode()).hexdigest(),
              str(g8_q.get("md5")))
        check("g87: {unixconversiontime} token substituted",
              g8_q.get("u", [""])[0].isdigit(), str(g8_q.get("u")))
        check("g87: {status2}/{event_1} resolve to empty, not literal",
              g8_q.get("s2", [""])[0] == "" and g8_q.get("e1", [""])[0] == "", str(g8_q))
        check("g87: payout=auto resolves to the fired payout",
              g8_q.get("payout", [""])[0] == "7.5", str(g8_q.get("payout")))

    g8_head = f"g87head-{g8_pid}"
    r = requests.head(f"{BASE}/pb?clickid={g8_head}&status=sale&payout=1.5", verify=not INSECURE, allow_redirects=False)
    check("g87: HEAD /pb returns 200", r.status_code == 200, str(r.status_code))
    check("g87: HEAD /pb writes nothing",
          g8_db(f"SELECT count(*) FROM conversions_data WHERE click_id='{g8_head}'") == "0",
          g8_db(f"SELECT count(*) FROM conversions_data WHERE click_id='{g8_head}'"))
    r = requests.head(f"{BASE}/pb?clickid={g8_head}&status=definitely_not_a_status&payout=1.5", verify=not INSECURE)
    check("g87: HEAD /pb keeps status validation (400)", r.status_code == 400, str(r.status_code))

    g8_postj = f"g87postj-{g8_pid}"
    r = requests.post(f"{BASE}/pb?clickid={g8_postj}&status=sale&payout=2.25",
                      json={"sub_id_1": f"json-{g8_pid}"}, verify=not INSECURE)
    check("g87: POST /pb with JSON body accepted",
          r.status_code == 200 and r.json().get("updated_status") == "sale", r.text[:160])
    check("g87: POST JSON extra field recorded",
          g8_db(f"SELECT sub_id_1 FROM conversions_data WHERE click_id='{g8_postj}'")
          == f"json-{g8_pid}",
          g8_db(f"SELECT sub_id_1 FROM conversions_data WHERE click_id='{g8_postj}'"))
    g8_postf = f"g87postf-{g8_pid}"
    r = requests.post(f"{BASE}/pb?clickid={g8_postf}&status=lead&payout=1.25",
                      data={"sub_id_1": f"form-{g8_pid}"}, verify=not INSECURE)
    check("g87: POST /pb with form body accepted",
          r.status_code == 200 and r.json().get("updated_status") == "lead", r.text[:160])
    check("g87: POST form extra field recorded",
          g8_db(f"SELECT sub_id_1 FROM conversions_data WHERE click_id='{g8_postf}'")
          == f"form-{g8_pid}",
          g8_db(f"SELECT sub_id_1 FROM conversions_data WHERE click_id='{g8_postf}'"))

    # --- g8 cleanup (campaigns before sources: a linked source can't be deleted) ---
    try:
        g8_srv.shutdown()
        g8_srv.server_close()
    except Exception:
        pass
    try:
        if g8_saved_pr:
            s.post(f"{api}/settings/", json={"settings": {"postback_rules": g8_saved_pr}})
        else:
            s.post(f"{api}/settings/", json={"settings": {"postback_rules": None}})
    except Exception:
        pass
    g8_db(f"DELETE FROM conversions_data WHERE click_id LIKE 'g85rej-{g8_pid}%' "
          f"OR click_id LIKE 'g85status-{g8_pid}%' OR click_id LIKE 'g85payout-{g8_pid}%' "
          f"OR click_id LIKE 'g85mult-{g8_pid}%' OR click_id LIKE 'g85dis-{g8_pid}%' "
          f"OR click_id LIKE 'g86%{g8_pid}%' OR click_id LIKE 'g87%{g8_pid}%'")
    for _c in g8_campaigns:
        s.delete(f"{api}/campaigns/{_c}")
    for _sr in g8_sources:
        s.delete(f"{api}/sources/{_sr}")

    # ===== Routing criteria, prefetch/bot params, login whitelist, hiding
    # domain, source status map =====
    import time as _wtime
    import threading as _wthreading
    import http.server as _whttpserver
    import socketserver as _wsocketserver

    w_pid = os.getpid()
    w_offers, w_campaigns, w_sources = [], [], []
    w_offer_n = [0]

    def w_offer(url):
        # Offer names are unique — suffix each with a counter so multiple
        # offers in this block don't collide.
        w_offer_n[0] += 1
        r = s.post(f"{api}/offers/", json={
            "name": f"smoke-wave-offer-{w_pid}-{w_offer_n[0]}", "url": url})
        oid = r.json().get("id")
        if oid:
            w_offers.append(oid)
        return oid

    def w_campaign(tag, flows, **kw):
        cfg = {"flows": flows, "postbacks": [], "fallback_url": "", "hide_referrer": False}
        cfg.update(kw.pop("config", {}))
        r = s.post(f"{api}/campaigns/", json={
            "name": f"smoke-wave-{tag}-{w_pid}", "alias": f"smoke-wave-{tag}-{w_pid}",
            "type": "campaign", "status": "active", "redirect_mode": "position",
            "config": cfg, **kw})
        cid = r.json().get("id")
        if cid:
            w_campaigns.append(cid)
        return cid

    def w_group(field, op, value):
        return {"combinator": "and", "groups": [{"logic": "and", "conditions": [
            {"field": field, "operator": op, "value": value}]}]}

    def w_curl(path, xff, headers=None, cookies=None):
        # Direct to uvicorn (nginx rewrites X-Real-IP): X-Forwarded-For is the
        # trusted client-IP fallback, so these hits control the resolved IP.
        cmd = ["docker", "exec", "tracker_frontend", "curl", "-s", "-D", "-",
               "-H", "Host: localhost", "-H", f"X-Forwarded-For: {xff}"]
        for h in headers or []:
            cmd += ["-H", h]
        if cookies:
            cmd += ["-b", cookies]
        cmd.append(f"http://127.0.0.1:8000{path}")
        return subprocess.run(cmd, capture_output=True, text=True, timeout=40).stdout

    w_saved_tracking = (s.get(f"{api}/settings/").json().get("settings") or {}).get("tracking")
    w_saved_login = (s.get(f"{api}/settings/").json().get("settings") or {}).get("login_security")

    # -- rDNS (reverse DNS / PTR) routing criterion --
    w_oa = w_offer("https://example.com/wave-a?cid={click_id}")
    w_ob = w_offer("https://example.com/wave-b?cid={click_id}")
    w_rdns = w_campaign("rdns", [
        {"type": "default", "position": 1, "enabled": True, "schema": "direct",
         "offer": w_oa, "filters": w_group("rdns", "contains", "local")},
        {"type": "default", "position": 2, "enabled": True, "schema": "direct",
         "offer": w_ob, "filters": []}])
    check("wave: rdns campaign created", bool(w_rdns) and bool(w_oa) and bool(w_ob),
          f"{w_rdns}/{w_oa}/{w_ob}")
    out = w_curl(f"/smoke-wave-rdns-{w_pid}", "127.0.0.1")
    check("wave: rDNS filter matches the localhost PTR", "wave-a" in out, out[:200])
    _w_t0 = _wtime.time()
    out = w_curl(f"/smoke-wave-rdns-{w_pid}", "192.0.2.1")
    check("wave: rDNS non-resolving IP falls through, bounded",
          "wave-b" in out and (_wtime.time() - _w_t0) < 6,
          f"{out[:120]} dt={_wtime.time() - _w_t0:.1f}")

    # -- prefetch filtering --
    def w_rdns_rows():
        return ch_query(f"SELECT count() FROM clicks_data WHERE campaign_id = {w_rdns}")

    w_before = w_rdns_rows()
    w_curl(f"/smoke-wave-rdns-{w_pid}", "127.0.0.1", headers=["Sec-Purpose: prefetch"])
    w_curl(f"/smoke-wave-rdns-{w_pid}", "127.0.0.1", headers=["X-Purpose: prefetch"])
    _wtime.sleep(1)
    w_after = w_rdns_rows()
    check("wave: prefetch hits are not counted (no CH rows)", w_before == w_after,
          f"{w_before} -> {w_after}")

    # -- source-declared bot param (opt-in, spoof-safe) --
    r = s.post(f"{api}/sources/", json={
        "name": f"smoke-wave-src-{w_pid}", "additional_settings": {"is_bot_param": "is_bot"}})
    w_src = r.json().get("id")
    if w_src:
        w_sources.append(w_src)
    w_srccamp = w_campaign("srcbot", [
        {"type": "default", "position": 1, "enabled": True, "schema": "direct",
         "offer": w_oa, "filters": []}], traffic_source_id=w_src)
    check("wave: source-bot campaign created", bool(w_src) and bool(w_srccamp),
          f"{w_src}/{w_srccamp}")
    w_curl(f"/smoke-wave-srcbot-{w_pid}?is_bot=1", "203.0.113.9")
    _wtime.sleep(1)
    w_row = ch_query(f"SELECT is_bot FROM clicks_data WHERE campaign_id = {w_srccamp} "
                     f"ORDER BY received_at DESC LIMIT 1")
    check("wave: configured is_bot param marks the row is_bot", w_row == "true", w_row[:80])
    w_curl(f"/smoke-wave-srcbot-{w_pid}", "203.0.113.9")
    _wtime.sleep(1)
    w_row = ch_query(f"SELECT is_bot FROM clicks_data WHERE campaign_id = {w_srccamp} "
                     f"ORDER BY received_at DESC LIMIT 1")
    check("wave: absent is_bot param stays human", w_row == "false", w_row[:80])
    # spoof guard: a source with NO is_bot_param configured ignores ?is_bot=1
    r = s.post(f"{api}/sources/", json={
        "name": f"smoke-wave-src2-{w_pid}", "additional_settings": {}})
    w_src2 = r.json().get("id")
    if w_src2:
        w_sources.append(w_src2)
    w_srccamp2 = w_campaign("srcbot2", [
        {"type": "default", "position": 1, "enabled": True, "schema": "direct",
         "offer": w_oa, "filters": []}], traffic_source_id=w_src2)
    w_curl(f"/smoke-wave-srcbot2-{w_pid}?is_bot=1", "203.0.113.9")
    _wtime.sleep(1)
    w_row = ch_query(f"SELECT is_bot FROM clicks_data WHERE campaign_id = {w_srccamp2} "
                     f"ORDER BY received_at DESC LIMIT 1")
    check("wave: unconfigured source ignores ?is_bot=1 (no spoof)", w_row == "false", w_row[:80])

    # -- do not assign costs for bot clicks --
    s.post(f"{api}/settings/", json={"settings": {"tracking": {"skip_bot_costs": True}}})
    settle_settings_cache()
    w_curl(f"/smoke-wave-srcbot-{w_pid}?is_bot=1&cost=7.5", "203.0.113.9")
    _wtime.sleep(1)
    w_row = ch_query(f"SELECT is_bot, toString(cost) FROM clicks_data "
                     f"WHERE campaign_id = {w_srccamp} ORDER BY received_at DESC LIMIT 1")
    check("wave: skip_bot_costs zeroes a bot row's cost", w_row.split("\t") == ["true", "0"],
          w_row[:80])
    w_curl(f"/smoke-wave-srcbot-{w_pid}?cost=7.5", "203.0.113.9")
    _wtime.sleep(1)
    w_row = ch_query(f"SELECT is_bot, toString(cost) FROM clicks_data "
                     f"WHERE campaign_id = {w_srccamp} ORDER BY received_at DESC LIMIT 1")
    check("wave: skip_bot_costs keeps a human row's cost",
          w_row.split("\t")[0] == "false" and abs(float(w_row.split("\t")[1]) - 7.5) < 0.01,
          w_row[:80])

    # -- conversion-status routing criterion --
    w_oc = w_offer("https://example.com/wave-ca?cid={click_id}")
    w_od = w_offer("https://example.com/wave-cb?cid={click_id}")
    w_convcamp = w_campaign("convst", [
        {"type": "default", "position": 1, "enabled": True, "schema": "direct",
         "offer": w_oc, "filters": w_group("conversion_status", "equals", "rejected")},
        {"type": "default", "position": 2, "enabled": True, "schema": "direct",
         "offer": w_od, "filters": []}])
    w_vid = f"smoke-wave-vid-{w_pid}"
    pg_exec("INSERT INTO conversions_data (received_at, click_id, campaign_id, status, "
            f"payout, revenue, profit, visitor_id) VALUES (now(), "
            f"'smoke-wave-convclick-{w_pid}', {w_convcamp}, 'rejected', 0, 0, 0, '{w_vid}')")
    out = w_curl(f"/smoke-wave-convst-{w_pid}", "203.0.113.9", cookies=f"aaa_vid={w_vid}")
    check("wave: conversion_status filter matches the prior status", "wave-ca" in out, out[:200])
    out = w_curl(f"/smoke-wave-convst-{w_pid}", "203.0.113.9", cookies="aaa_vid=nobody-here")
    check("wave: unknown visitor conversion_status falls through", "wave-cb" in out, out[:200])

    # -- login IP whitelist (G90) --
    r = s.post(f"{api}/settings/", json={"settings": {"login_security": {
        "ip_whitelist": "127.0.0.1/32,172.16.0.0/12"}}})
    check("wave: login whitelist saved", r.status_code == 200, r.text[:120])
    r = requests.post(f"{api}/login", json={"username": USER, "password": PASS},
                      verify=not INSECURE)
    check("wave: whitelisted client can still log in", r.status_code == 200,
          f"{r.status_code} {r.text[:80]}")
    s.post(f"{api}/settings/", json={"settings": {"login_security": {
        "ip_whitelist": "10.0.0.0/8"}}})
    r = requests.post(f"{api}/login", json={"username": USER, "password": PASS},
                      verify=not INSECURE)
    check("wave: non-whitelisted login refused 403", r.status_code == 403,
          f"{r.status_code} {r.text[:100]}")

    # -- hide-referrer secondary domain (G91) --
    s.post(f"{api}/settings/", json={"settings": {"tracking": {
        "referrer_hiding_domain": "hide.smoke.test"}}})
    settle_settings_cache()
    w_hidecamp = w_campaign("hide", [
        {"type": "default", "position": 1, "enabled": True, "schema": "direct",
         "offer": w_oa, "filters": []}], config={"hide_referrer": True})
    check("wave: hide-referrer campaign created", bool(w_hidecamp), str(w_hidecamp))
    r = s.get(f"{BASE}/smoke-wave-hide-{w_pid}", allow_redirects=False)
    loc = r.headers.get("location") or ""
    check("wave: hide-referrer hops through the secondary domain",
          r.status_code in (301, 302, 307, 308)
          and "hide.smoke.test/__hide_referrer" in loc and "u=" in loc,
          f"{r.status_code} {loc[:140]}")
    r = requests.get(f"{BASE}/__hide_referrer", params={"u": "https://example.com/dest"},
                     verify=not INSECURE)
    check("wave: hop route serves the meta refresh",
          r.status_code == 200 and 'content="no-referrer"' in r.text
          and "example.com/dest" in r.text, r.text[:120])
    r = requests.get(f"{BASE}/__hide_referrer", params={"u": "javascript:alert(1)"},
                     verify=not INSECURE)
    check("wave: hop route rejects non-http destinations", r.status_code == 404, str(r.status_code))

    # Restore the global hiding domain NOW (and wait out the frontend's 30s
    # settings cache) so a rapid re-run's earlier hide-referrer check can never
    # see a stale configuration.
    if w_saved_tracking is None:
        s.post(f"{api}/settings/", json={"settings": {"tracking": None}})
    else:
        s.post(f"{api}/settings/", json={"settings": {"tracking": w_saved_tracking}})
    settle_settings_cache()

    # -- traffic-source status map accepts the stored spellings --
    w_port = 19000 + (w_pid % 1000)
    w_captured = []
    _wsocketserver.TCPServer.allow_reuse_address = True

    class _WRecv(_whttpserver.BaseHTTPRequestHandler):
        def _ok(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            w_captured.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        do_GET = _ok
        do_POST = _ok

        def log_message(self, *args):
            pass

    w_srv = _wsocketserver.TCPServer(("0.0.0.0", w_port), _WRecv)
    w_srv.daemon_threads = True
    _wthreading.Thread(target=w_srv.serve_forever, daemon=True).start()
    w_recv = f"http://host.docker.internal:{w_port}"
    r = s.post(f"{api}/sources/", json={
        "name": f"smoke-wave-fan-{w_pid}", "s2s_postback": f"{w_recv}/fan",
        "s2s_postback_statuses": {"sale": False, "lead": False,
                                  "rejected": True, "upsale": True}})
    w_fsrc = r.json().get("id")
    if w_fsrc:
        w_sources.append(w_fsrc)
    w_fcamp = w_campaign("fan", [], traffic_source_id=w_fsrc)
    check("wave: fanout source + campaign created", bool(w_fsrc) and bool(w_fcamp),
          f"{w_fsrc}/{w_fcamp}")

    def w_seed(click):
        pg_exec("INSERT INTO conversions_data (received_at, click_id, campaign_id, status, "
                f"payout, revenue, profit) VALUES (now(), '{click}', {w_fcamp}, 'lead', 0, 0, 0)")

    w_clicks = {
        "rejected": f"smoke-wave-fan-rej-{w_pid}",
        "upsale": f"smoke-wave-fan-ups-{w_pid}",
        "sale": f"smoke-wave-fan-sale-{w_pid}",
    }
    for _st, _ck in w_clicks.items():
        w_seed(_ck)
        requests.get(f"{BASE}/pb?clickid={_ck}&status={_st}&payout=1", verify=not INSECURE)
    _w_end = _wtime.time() + 15
    while _wtime.time() < _w_end and len(w_captured) < 2:
        _wtime.sleep(0.5)
    _wtime.sleep(1)
    check("wave: status map forwards 'rejected' (stored spelling)",
          any("rej-" in p for p in w_captured), str(w_captured))
    check("wave: status map forwards 'upsale' (stored spelling)",
          any("ups-" in p for p in w_captured), str(w_captured))
    check("wave: status map gates a disabled 'sale'",
          not any("fan-sale-" in p for p in w_captured), str(w_captured))
    try:
        w_srv.shutdown()
        w_srv.server_close()
    except Exception:
        pass

    # ---- wave cleanup ----
    try:
        if w_saved_tracking is None:
            s.post(f"{api}/settings/", json={"settings": {"tracking": None}})
        else:
            s.post(f"{api}/settings/", json={"settings": {"tracking": w_saved_tracking}})
        if w_saved_login is None:
            s.post(f"{api}/settings/", json={"settings": {"login_security": None}})
        else:
            s.post(f"{api}/settings/", json={"settings": {"login_security": w_saved_login}})
    except Exception:
        pass
    pg_exec(f"DELETE FROM conversions_data WHERE click_id LIKE 'smoke-wave-%{w_pid}%' "
            f"OR visitor_id = 'smoke-wave-vid-{w_pid}'")
    if w_campaigns:
        ch_query("ALTER TABLE clicks_data DELETE WHERE campaign_id IN (%s)"
                 % ",".join(str(c) for c in w_campaigns if c))
    for _c in w_campaigns:
        if _c:
            s.delete(f"{api}/campaigns/{_c}")
    for _o in w_offers:
        if _o:
            s.delete(f"{api}/offers/{_o}")
    for _sr in w_sources:
        if _sr:
            s.delete(f"{api}/sources/{_sr}")

    # ===== Meta Conversions API (CAPI): mock receiver end-to-end =====
    import time as _capi_time
    import threading as _capi_threading
    import http.server as _capi_httpserver
    import socketserver as _capi_socketserver
    import hashlib as _capi_hashlib

    capi_pid = os.getpid()
    capi_port = 20000 + (capi_pid % 1000)
    capi_captured = []
    _capi_socketserver.TCPServer.allow_reuse_address = True

    class _CapiReceiver(_capi_httpserver.BaseHTTPRequestHandler):
        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {"_raw": raw}
            capi_captured.append({"path": self.path, "body": body})
            # dataset ids containing "fail500" simulate a transient Graph outage
            if "fail500" in self.path:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"server error")
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"events_received":1}')

        do_GET = _handle
        do_POST = _handle

        def log_message(self, *args):
            pass

    capi_srv = _capi_socketserver.TCPServer(("0.0.0.0", capi_port), _CapiReceiver)
    capi_srv.daemon_threads = True
    _capi_threading.Thread(target=capi_srv.serve_forever, daemon=True).start()
    capi_base = f"http://host.docker.internal:{capi_port}"

    capi_probe = ""
    try:
        capi_probe = subprocess.run(
            ["docker", "exec", "tracker_frontend", "curl", "-s", "-m", "3", f"{capi_base}/ready"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        pass
    check("meta-capi: mock receiver reachable from the frontend container",
          "events_received" in capi_probe, capi_probe[:80])

    def capi_paths(marker):
        return [c["path"] for c in capi_captured if marker in c["path"]]

    def capi_click_count(click):
        return sum(1 for c in capi_captured for ev in (c["body"].get("data") or [])
                   if ev.get("event_id") == click)

    def capi_wait_click(click, n, timeout=20):
        end = _capi_time.time() + timeout
        while _capi_time.time() < end:
            if capi_click_count(click) >= n:
                break
            _capi_time.sleep(0.3)
        return capi_click_count(click)

    def capi_body_for(click):
        for c in capi_captured:
            for ev in (c["body"].get("data") or []):
                if ev.get("event_id") == click:
                    return c["body"]
        return None

    def capi_seed(click, cid=None, fbc="", fbp=""):
        fbc_sql = f"'{fbc}'" if fbc else "NULL"
        fbp_sql = f"'{fbp}'" if fbp else "NULL"
        pg_exec("INSERT INTO conversions_data (received_at, click_id, campaign_id, status, "
                f"payout, revenue, profit, fbc, fbp) VALUES (now(), '{click}', "
                f"{cid if cid else 'NULL'}, 'lead', 0, 0, 0, {fbc_sql}, {fbp_sql})")

    def capi_set(**over):
        cfg = {
            "enabled": True,
            "dataset_id": f"smoke-meta-live-{capi_pid}",
            "access_token": f"tok-{capi_pid}",
            "test_event_code": f"smoke-test-code-{capi_pid}",
            "default_currency": "USD",
            "api_version": "v21.0",
            "graph_base_url": capi_base,
            "dry_run": False,
            "status_events": {"sale": "Purchase", "lead": "Lead", "upsale": "Subscribe"},
            "send_statuses": ["lead", "sale", "upsale"],
            "include_customer_match": True,
            "pixel_overrides": {},
        }
        cfg.update(over)
        return s.post(f"{api}/settings/", json={"settings": {"meta_capi": cfg}})

    capi_saved = (s.get(f"{api}/settings/").json().get("settings") or {}).get("meta_capi")

    # -- disabled -> nothing is ever sent (cache already holds the default) --
    capi_dis_click = f"smoke-meta-dis-{capi_pid}"
    capi_seed(capi_dis_click)
    r = capi_set(enabled=False, dry_run=True, dataset_id=f"smoke-meta-dis-{capi_pid}")
    check("meta-capi: disabled config saved", r.status_code == 200, r.text[:120])
    requests.get(f"{BASE}/pb?clickid={capi_dis_click}&status=sale&payout=5", verify=not INSECURE)
    _capi_time.sleep(2.5)
    check("meta-capi: disabled sends nothing",
          capi_paths(f"smoke-meta-dis-{capi_pid}") == [], str(capi_paths(""))[:150])

    # -- dry_run -> payload built + logged, no HTTP call --
    capi_dry_click = f"smoke-meta-dry-{capi_pid}"
    capi_seed(capi_dry_click, fbc="fb.1.1.smokedryfbc", fbp="fb.1.2.smokedryfbp")
    r = capi_set(enabled=True, dry_run=True, dataset_id=f"smoke-meta-dry-{capi_pid}")
    check("meta-capi: dry-run config saved", r.status_code == 200, r.text[:120])
    settle_settings_cache()
    requests.get(f"{BASE}/pb?clickid={capi_dry_click}&status=sale&payout=5", verify=not INSECURE)
    _capi_time.sleep(2.5)
    check("meta-capi: dry-run makes no HTTP call",
          capi_paths(f"smoke-meta-dry-{capi_pid}") == [], str(capi_paths(""))[:150])
    dry_rows = pg_exec_out(f"SELECT count(*) FROM meta_capi_log WHERE click_id='{capi_dry_click}' "
                           f"AND outcome='dry_run'")
    check("meta-capi: dry-run records the built payload", dry_rows.strip() == "1", dry_rows)
    dbg = s.get(f"{BASE}/_aaa_tracker_debug")
    check("meta-capi: dry-run visible in the tracking log",
          "Meta CAPI dry-run" in dbg.text, dbg.text[:120])

    # -- enabled + live -> full payload shape on the mock receiver --
    capi_live_ds = f"smoke-meta-live-{capi_pid}"
    r = capi_set(enabled=True, dry_run=False, dataset_id=capi_live_ds)
    check("meta-capi: live config saved", r.status_code == 200, r.text[:120])
    settle_settings_cache()

    capi_live_click = f"smoke-meta-live-{capi_pid}"
    capi_seed(capi_live_click, fbc="fb.1.111.smokefbc", fbp="fb.1.222.smokefbp")
    r = requests.get(f"{BASE}/pb?clickid={capi_live_click}&status=sale&payout=5",
                     params={"email": "User@Example.com", "phone": "+15551230000"},
                     verify=not INSECURE)
    check("meta-capi: /pb still returns 200 while CAPI fires", r.status_code == 200, r.text[:120])
    capi_wait_click(capi_live_click, 1)
    body = capi_body_for(capi_live_click) or {}
    ev = (body.get("data") or [{}])[0]
    ud = ev.get("user_data") or {}
    cd = ev.get("custom_data") or {}
    live_paths = capi_paths(capi_live_ds)
    check("meta-capi: live send reached the receiver", len(live_paths) >= 1, str(live_paths)[:150])
    check("meta-capi: sale maps to Purchase", ev.get("event_name") == "Purchase", str(ev)[:150])
    check("meta-capi: event_id equals click_id", ev.get("event_id") == capi_live_click,
          str(ev.get("event_id")))
    check("meta-capi: action_source is website", ev.get("action_source") == "website",
          str(ev.get("action_source")))
    check("meta-capi: fbc passthrough", ud.get("fbc") == "fb.1.111.smokefbc", str(ud.get("fbc")))
    check("meta-capi: fbp passthrough", ud.get("fbp") == "fb.1.222.smokefbp", str(ud.get("fbp")))
    check("meta-capi: email hashed (trim+lower)",
          ud.get("em") == [_capi_hashlib.sha256(b"user@example.com").hexdigest()],
          str(ud.get("em")))
    check("meta-capi: phone hashed (digits only)",
          ud.get("ph") == [_capi_hashlib.sha256(b"15551230000").hexdigest()],
          str(ud.get("ph")))
    check("meta-capi: client ip + user agent included",
          bool(ud.get("client_ip_address")) and bool(ud.get("client_user_agent")), str(ud)[:150])
    check("meta-capi: value + currency", cd.get("value") == 5.0 and cd.get("currency") == "USD",
          str(cd))
    check("meta-capi: test_event_code included",
          body.get("test_event_code") == f"smoke-test-code-{capi_pid}",
          str(body.get("test_event_code")))
    check("meta-capi: access token passed to Graph",
          len(live_paths) >= 1 and f"tok-{capi_pid}" in live_paths[0], str(live_paths[:1])[:150])

    # -- status outside send_statuses -> no send --
    capi_rej_click = f"smoke-meta-rej-{capi_pid}"
    capi_seed(capi_rej_click)
    before_rej = len(capi_paths(capi_live_ds))
    requests.get(f"{BASE}/pb?clickid={capi_rej_click}&status=rejected&payout=1", verify=not INSECURE)
    _capi_time.sleep(2.5)
    check("meta-capi: status outside send_statuses is not sent",
          len(capi_paths(capi_live_ds)) == before_rej, str(capi_paths(capi_live_ds))[:150])

    # -- duplicate click_id+status -> exactly one send --
    capi_dup_click = f"smoke-meta-dup-{capi_pid}"
    capi_seed(capi_dup_click)
    requests.get(f"{BASE}/pb?clickid={capi_dup_click}&status=sale&payout=5", verify=not INSECURE)
    capi_wait_click(capi_dup_click, 1)
    # different payout => not a row-level duplicate, but the CAPI (click,status)
    # dedupe must still suppress the second send
    requests.get(f"{BASE}/pb?clickid={capi_dup_click}&status=sale&payout=9", verify=not INSECURE)
    _capi_time.sleep(2.5)
    check("meta-capi: duplicate click_id+status sends exactly once",
          capi_click_count(capi_dup_click) == 1,
          f"count={capi_click_count(capi_dup_click)}")

    # -- campaign-level meta_pixel_id override + receiver 500 -> bounded retry --
    capi_fail_ds = f"fail500-{capi_pid}"
    r = s.post(f"{api}/campaigns/", json={
        "name": f"smoke-meta-fail-{capi_pid}", "alias": f"smoke-meta-fail-{capi_pid}",
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "config": {"flows": [], "postbacks": [], "hide_referrer": False,
                   "meta_pixel_id": capi_fail_ds}})
    capi_fail_cid = r.json().get("id")
    check("meta-capi: campaign pixel override created", bool(capi_fail_cid), r.text[:150])
    capi_fail_click = f"smoke-meta-fail-{capi_pid}"
    capi_seed(capi_fail_click, cid=capi_fail_cid)
    r = requests.get(f"{BASE}/pb?clickid={capi_fail_click}&status=sale&payout=3", verify=not INSECURE)
    check("meta-capi: Graph 500 does not change the /pb response", r.status_code == 200,
          r.text[:150])
    capi_wait_click(capi_fail_click, 1)
    _capi_time.sleep(3)
    fail_attempts = capi_click_count(capi_fail_click)
    check("meta-capi: transient 500 retried then given up (2-3 attempts)",
          2 <= fail_attempts <= 3, f"attempts={fail_attempts}")
    check("meta-capi: campaign override used the fail500 dataset",
          capi_paths(capi_fail_ds) != [], str(capi_paths(capi_fail_ds))[:150])
    fail_log = pg_exec_out(f"SELECT count(*) FROM meta_capi_log WHERE click_id='{capi_fail_click}' "
                           f"AND outcome='failed'")
    check("meta-capi: failed send recorded in meta_capi_log", fail_log.strip() != "0", fail_log)

    # -- access token: real for admin, masked for a settings-reading non-admin --
    r = s.get(f"{api}/settings/")
    capi_admin_cfg = (r.json().get("settings") or {}).get("meta_capi") or {}
    check("meta-capi: admin sees the real token",
          capi_admin_cfg.get("access_token") == f"tok-{capi_pid}",
          str(capi_admin_cfg.get("access_token"))[:40])
    capi_user = f"smoke-meta-user-{capi_pid}"
    r = s.post(f"{api}/users/", json={
        "username": capi_user, "password": "smokepass1",
        "permissions": {"sections": {"settings": True}, "write": False}})
    capi_uid = r.json().get("id")
    check("meta-capi: restricted settings-reader created", r.status_code == 200, r.text[:150])
    capi_user_sess = requests.Session()
    capi_user_sess.verify = not INSECURE
    r = capi_user_sess.post(f"{api}/login", json={"username": capi_user, "password": "smokepass1"})
    check("meta-capi: restricted user login", r.status_code == 200, r.text[:120])
    r = capi_user_sess.get(f"{api}/settings/")
    check("meta-capi: non-admin GET does not leak the token",
          f"tok-{capi_pid}" not in r.text, r.text[:150])
    capi_masked = ((r.json().get("settings") or {}).get("meta_capi") or {}).get("access_token")
    check("meta-capi: non-admin sees a masked token",
          bool(capi_masked) and capi_masked != f"tok-{capi_pid}" and "\u2022" in capi_masked,
          str(capi_masked)[:40])

    # -- meta-capi cleanup --
    if capi_saved is None:
        s.post(f"{api}/settings/", json={"settings": {"meta_capi": None}})
    else:
        s.post(f"{api}/settings/", json={"settings": {"meta_capi": capi_saved}})
    pg_exec(f"DELETE FROM conversions_data WHERE click_id LIKE 'smoke-meta-%-{capi_pid}%' "
            f"OR click_id LIKE 'smoke-meta-{capi_pid}%'")
    pg_exec(f"DELETE FROM meta_capi_sent WHERE click_id LIKE 'smoke-meta-%-{capi_pid}%'")
    pg_exec(f"DELETE FROM meta_capi_log WHERE click_id LIKE 'smoke-meta-%-{capi_pid}%'")
    if capi_fail_cid:
        s.delete(f"{api}/campaigns/{capi_fail_cid}")
    if capi_uid:
        s.delete(f"{api}/users/{capi_uid}")
    try:
        capi_srv.shutdown()
        capi_srv.server_close()
    except Exception:
        pass

    # ===== CAPI pixel records: CRUD, bindings, per-pixel resolution =====
    # Self-contained: the block above restores/tears down its own receiver, so
    # start a fresh mock receiver on the next port and reuse its capture list.
    _px_pid = os.getpid()
    _px_port = capi_port + 1
    _px_srv = _capi_socketserver.TCPServer(("0.0.0.0", _px_port), _CapiReceiver)
    _px_srv.daemon_threads = True
    _capi_threading.Thread(target=_px_srv.serve_forever, daemon=True).start()
    _px_base = f"http://host.docker.internal:{_px_port}"

    _px_probe = ""
    try:
        _px_probe = subprocess.run(
            ["docker", "exec", "tracker_frontend", "curl", "-s", "-m", "3", f"{_px_base}/ready"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        pass
    check("capi-pixels: pixel receiver reachable from the frontend container",
          "events_received" in _px_probe, _px_probe[:80])

    px_tag = f"smoke-px-{_px_pid}"

    def px_seed(click, cid=None, offer_id=None, status="sale", payout=5):
        pg_exec("INSERT INTO conversions_data (received_at, click_id, campaign_id, offer_id, "
                f"status, payout, revenue, profit) VALUES (now(), '{click}', "
                f"{cid if cid else 'NULL'}, {offer_id if offer_id else 'NULL'}, "
                f"'{status}', {payout}, {payout}, {payout})")

    def px_mk(title, dataset, **over):
        body = {"title": title, "platform": "meta", "pixel_id": dataset,
                "access_token": f"{px_tag}-tok-{dataset}", "default_event_name": "Lead",
                "action_source": "website", "status": "active",
                "custom_matching": False, "conversion_matching": [],
                "payout_customisations": []}
        body.update(over)
        return s.post(f"{api}/settings/capi-pixels", json=body)

    def px_bind(scope, scope_id, ids, **over):
        body = {"scope": scope, "scope_id": scope_id, "pixel_ids": ids}
        body.update(over)
        return s.put(f"{api}/settings/capi-bindings", json=body)

    # -- fixtures: traffic channel + two campaigns (one channel-bound) + offer --
    r = s.post(f"{api}/sources/", json={"name": f"{px_tag}-src"})
    px_src = r.json().get("id")
    check("capi-pixels: channel fixture created", bool(px_src), r.text[:120])
    r = s.post(f"{api}/sources/", json={"name": f"{px_tag}-src2"})
    px_src2 = r.json().get("id")
    r = s.post(f"{api}/campaigns/", json={
        "name": f"{px_tag}-chan", "alias": f"{px_tag}-chan", "type": "campaign",
        "status": "active", "redirect_mode": "position", "traffic_source_id": px_src,
        "config": {"flows": [], "postbacks": [], "hide_referrer": False}})
    px_chan_cid = r.json().get("id")
    r = s.post(f"{api}/campaigns/", json={
        "name": f"{px_tag}-plain", "alias": f"{px_tag}-plain", "type": "campaign",
        "status": "active", "redirect_mode": "position",
        "config": {"flows": [], "postbacks": [], "hide_referrer": False}})
    px_plain_cid = r.json().get("id")
    r = s.post(f"{api}/campaigns/", json={
        "name": f"{px_tag}-off", "alias": f"{px_tag}-off", "type": "campaign",
        "status": "active", "redirect_mode": "position", "traffic_source_id": px_src2,
        "config": {"flows": [], "postbacks": [], "hide_referrer": False}})
    px_off_cid = r.json().get("id")
    r = s.post(f"{api}/offers/", json={"name": f"{px_tag}-offer",
                                       "url": "https://example.com/px", "payout": 10})
    px_offer = r.json().get("id")
    check("capi-pixels: offer fixture created", bool(px_offer), r.text[:120])

    # Global CAPI config gates the send; point it at the pixel receiver.
    px_saved = (s.get(f"{api}/settings/").json().get("settings") or {}).get("meta_capi")
    r = s.post(f"{api}/settings/", json={"settings": {"meta_capi": {
        "enabled": True, "dry_run": False, "graph_base_url": _px_base,
        "dataset_id": "", "access_token": "", "test_event_code": "",
        "default_currency": "USD", "send_statuses": ["lead", "sale", "upsale"],
        "include_customer_match": True, "status_events": {}, "pixel_overrides": {}}}})
    check("capi-pixels: global CAPI config saved", r.status_code == 200, r.text[:120])
    settle_settings_cache()

    # -- create: per-pixel mapping + payout customisation ride along --
    r = px_mk("A", f"{px_tag}-dsA", default_event_name="Lead", custom_matching=True,
              conversion_matching=[{"conversion_type": "sale", "event_name": "Purchase"}],
              payout_customisations=[{"conversion_type": "sale", "value": 42.5,
                                      "currency": "EUR"}])
    pxa = r.json().get("pixel") or {}
    check("capi-pixels: create returns the record", r.status_code == 200 and pxa.get("id"),
          r.text[:200])
    check("capi-pixels: create keeps action_source/mapping/payout",
          pxa.get("custom_matching") is True
          and pxa.get("conversion_matching", [{}])[0].get("event_name") == "Purchase"
          and pxa.get("payout_customisations", [{}])[0].get("value") == 42.5,
          str(pxa)[:250])
    r = px_mk("B", f"{px_tag}-dsB", default_event_name="Lead")
    pxb = r.json().get("pixel") or {}
    r = px_mk("C", f"{px_tag}-dsC", default_event_name="Lead", status="inactive")
    pxc = r.json().get("pixel") or {}

    # -- list shows title + pixel id + status --
    r = s.get(f"{api}/settings/capi-pixels")
    listed = {p["title"]: p for p in (r.json().get("pixels") or []) if p["title"] in ("A", "B", "C")}
    check("capi-pixels: list returns title, pixel_id and status",
          listed.get("A", {}).get("pixel_id") == f"{px_tag}-dsA"
          and listed.get("A", {}).get("status") == "active"
          and listed.get("C", {}).get("status") == "inactive",
          str(listed)[:250])

    # -- update / delete round-trip --
    r = s.put(f"{api}/settings/capi-pixels/{pxa['id']}", json={
        "title": "A2", "platform": "meta", "pixel_id": f"{px_tag}-dsA",
        "default_event_name": "Lead", "custom_matching": False})
    check("capi-pixels: update changes the title",
          r.status_code == 200 and (r.json().get("pixel") or {}).get("title") == "A2",
          r.text[:150])
    # restore A's mapping for the send assertions
    s.put(f"{api}/settings/capi-pixels/{pxa['id']}", json={
        "title": "A", "platform": "meta", "pixel_id": f"{px_tag}-dsA",
        "default_event_name": "Lead", "custom_matching": True,
        "conversion_matching": [{"conversion_type": "sale", "event_name": "Purchase"}],
        "payout_customisations": [{"conversion_type": "sale", "value": 42.5,
                                   "currency": "EUR"}]})

    # -- channel binding: only that channel's conversions use the pixel --
    r = px_bind("channel", px_src, [pxa["id"]], active=True, impression_cost_sync=True)
    check("capi-pixels: channel binding saved",
          r.status_code == 200 and r.json().get("pixel_ids") == [pxa["id"]], r.text[:150])
    r = s.get(f"{api}/settings/capi-bindings", params={"scope": "channel", "scope_id": px_src})
    check("capi-pixels: channel binding + toggles round-trip",
          r.json().get("pixel_ids") == [pxa["id"]]
          and r.json().get("active") is True
          and r.json().get("impression_cost_sync") is True, r.text[:150])

    px_chan_click = f"{px_tag}-chan-click"
    px_seed(px_chan_click, cid=px_chan_cid, status="sale", payout=5)
    requests.get(f"{BASE}/pb?clickid={px_chan_click}&status=sale&payout=5", verify=not INSECURE)
    capi_wait_click(px_chan_click, 1)
    check("capi-pixels: channel-bound conversion sent to its pixel",
          capi_click_count(px_chan_click) == 1, f"count={capi_click_count(px_chan_click)}")
    _chan_body = capi_body_for(px_chan_click) or {}
    _chan_ev = (_chan_body.get("data") or [{}])[0]
    check("capi-pixels: per-pixel conversion-type→event mapping honored",
          _chan_ev.get("event_name") == "Purchase", str(_chan_ev.get("event_name")))
    check("capi-pixels: per-pixel payout customisation applied",
          (_chan_ev.get("custom_data") or {}).get("value") == 42.5
          and (_chan_ev.get("custom_data") or {}).get("currency") == "EUR",
          str(_chan_ev.get("custom_data")))
    check("capi-pixels: sent dataset is the bound pixel's",
          capi_paths(f"{px_tag}-dsA") != [], str(capi_paths(px_tag))[:150])

    # A conversion with no channel/offer binding sends nothing (no global fallback
    # once any pixel record exists).
    px_none_click = f"{px_tag}-none-click"
    px_seed(px_none_click, cid=px_plain_cid, status="sale", payout=5)
    before_none = len(capi_captured)
    requests.get(f"{BASE}/pb?clickid={px_none_click}&status=sale&payout=5", verify=not INSECURE)
    _capi_time.sleep(2.0)
    check("capi-pixels: unbound conversion sends nothing",
          capi_click_count(px_none_click) == 0, f"count={capi_click_count(px_none_click)}")

    # -- offer binding: all of that offer's conversions use the pixel --
    r = px_bind("offer", px_offer, [pxb["id"]])
    check("capi-pixels: offer binding saved", r.status_code == 200, r.text[:150])
    px_offer_click = f"{px_tag}-offer-click"
    px_seed(px_offer_click, cid=px_plain_cid, offer_id=px_offer, status="sale", payout=5)
    requests.get(f"{BASE}/pb?clickid={px_offer_click}&status=sale&payout=5", verify=not INSECURE)
    capi_wait_click(px_offer_click, 1)
    check("capi-pixels: offer-bound conversion sent to its pixel",
          capi_click_count(px_offer_click) == 1
          and capi_paths(f"{px_tag}-dsB") != [],
          f"count={capi_click_count(px_offer_click)} paths={capi_paths(px_tag)}")

    # -- bound at both levels -> exactly one send (channel resolves first) --
    px_both_click = f"{px_tag}-both-click"
    px_seed(px_both_click, cid=px_chan_cid, offer_id=px_offer, status="sale", payout=5)
    requests.get(f"{BASE}/pb?clickid={px_both_click}&status=sale&payout=5", verify=not INSECURE)
    capi_wait_click(px_both_click, 1)
    _capi_time.sleep(1.5)
    check("capi-pixels: pixel bound at both levels sends exactly once",
          capi_click_count(px_both_click) == 1,
          f"count={capi_click_count(px_both_click)}")

    # -- inactive pixel skipped --
    r = px_bind("channel", px_src2, [pxc["id"]], active=True)
    px_off_click = f"{px_tag}-off-click"
    px_seed(px_off_click, cid=px_off_cid, status="sale", payout=5)
    requests.get(f"{BASE}/pb?clickid={px_off_click}&status=sale&payout=5", verify=not INSECURE)
    _capi_time.sleep(2.0)
    check("capi-pixels: inactive pixel skipped",
          capi_click_count(px_off_click) == 0, f"count={capi_click_count(px_off_click)}")

    # -- dry-run sends nothing even with a bound pixel --
    s.post(f"{api}/settings/", json={"settings": {"meta_capi": {
        "enabled": True, "dry_run": True, "graph_base_url": _px_base,
        "send_statuses": ["lead", "sale", "upsale"], "include_customer_match": True,
        "default_currency": "USD", "dataset_id": "", "access_token": ""}}})
    settle_settings_cache()
    px_dry_click = f"{px_tag}-dry-click"
    px_seed(px_dry_click, cid=px_chan_cid, status="sale", payout=5)
    requests.get(f"{BASE}/pb?clickid={px_dry_click}&status=sale&payout=5", verify=not INSECURE)
    _capi_time.sleep(2.0)
    check("capi-pixels: dry-run sends nothing",
          capi_click_count(px_dry_click) == 0, f"count={capi_click_count(px_dry_click)}")
    s.post(f"{api}/settings/", json={"settings": {"meta_capi": {"dry_run": False}}})

    # -- secret not leaked to a settings-reading non-admin --
    r = s.get(f"{api}/settings/capi-pixels")
    check("capi-pixels: admin sees the real token",
          any(p["id"] == pxa["id"] and p.get("access_token") == f"{px_tag}-tok-{px_tag}-dsA"
              for p in (r.json().get("pixels") or [])), r.text[:150])
    px_user = f"{px_tag}-user"
    r = s.post(f"{api}/users/", json={
        "username": px_user, "password": "smokepass1",
        "permissions": {"sections": {"settings": True}, "write": False}})
    px_uid = r.json().get("id")
    check("capi-pixels: restricted settings-reader created", r.status_code == 200, r.text[:150])
    px_sess = requests.Session()
    px_sess.verify = not INSECURE
    px_sess.post(f"{api}/login", json={"username": px_user, "password": "smokepass1"})
    r = px_sess.get(f"{api}/settings/capi-pixels")
    check("capi-pixels: non-admin GET does not leak the token",
          f"{px_tag}-tok-{px_tag}-dsA" not in r.text, r.text[:150])
    check("capi-pixels: non-admin sees a masked token",
          any("\u2022" in (p.get("access_token") or "")
              for p in (r.json().get("pixels") or [])), r.text[:150])

    # -- the CAPI page serves the pixel card + dialog markers --
    rp = s.get(f"{BASE}/backend/capi-integrations")
    check("capi-pixels: CAPI integrations page serves the pixel card",
          rp.status_code == 200 and "capiPixels" in rp.text
          and "openPixelDialog" in rp.text and "Add new pixel" in rp.text,
          f"{rp.status_code}")
    check("capi-pixels: CAPI integrations page has no unreplaced jinja tags",
          "{%" not in rp.text, "unreplaced jinja tag")

    # -- delete --
    r = s.delete(f"{api}/settings/capi-pixels/{pxc['id']}")
    check("capi-pixels: delete removes the record", r.status_code == 200, r.text[:150])

    # -- pixel block cleanup --
    for _pid in (pxa.get("id"), pxb.get("id")):
        if _pid:
            s.delete(f"{api}/settings/capi-pixels/{_pid}")
    s.post(f"{api}/settings/", json={"settings": {"meta_capi": px_saved}})
    pg_exec(f"DELETE FROM capi_channel_settings WHERE source_id IN ({px_src}, {px_src2})")
    pg_exec(f"DELETE FROM capi_pixel_sent WHERE click_id LIKE '{px_tag}%'")
    pg_exec(f"DELETE FROM meta_capi_log WHERE click_id LIKE '{px_tag}%'")
    pg_exec(f"DELETE FROM conversions_data WHERE click_id LIKE '{px_tag}%'")
    for _cid in (px_chan_cid, px_plain_cid, px_off_cid):
        if _cid:
            s.delete(f"{api}/campaigns/{_cid}")
    if px_offer:
        s.delete(f"{api}/offers/{px_offer}")
    for _sid in (px_src, px_src2):
        if _sid:
            s.delete(f"{api}/sources/{_sid}")
    if px_uid:
        s.delete(f"{api}/users/{px_uid}")
    try:
        _px_srv.shutdown()
        _px_srv.server_close()
    except Exception:
        pass

    # ----- session cookie must not be Secure over plain HTTP (login would silently fail) -----
    http_base = "http://" + BASE.split("://", 1)[-1].split("/")[0]
    r = requests.post(f"{http_base}/backend/api/login",
                      json={"username": USER, "password": PASS}, verify=not INSECURE)
    sc_http = r.headers.get("set-cookie") or ""
    check("cookie: http login issues a session cookie",
          r.status_code == 200 and "session_token=" in sc_http, f"{r.status_code} {sc_http[:100]}")
    check("cookie: http session cookie is not Secure",
          "secure" not in sc_http.lower(), sc_http[:120])
    r = requests.post(f"{BASE}/backend/api/login",
                      json={"username": USER, "password": PASS}, verify=not INSECURE)
    sc_https = r.headers.get("set-cookie") or ""
    check("cookie: https session cookie is Secure",
          r.status_code == 200 and "secure" in sc_https.lower(), f"{r.status_code} {sc_https[:120]}")

    # ===== Query-style postbacks (/pb?…) + per-status Mode =====
    qp_pid = os.getpid()
    qp_tag = f"smoke-qp-{qp_pid}"

    def qp_cell(click, cols="status || '|' || payout || '|' || COALESCE(transaction_id, '')"):
        return pg_query(f"SELECT {cols} FROM conversions_data WHERE click_id = '{click}'")

    def qp_count(click):
        return pg_query(f"SELECT count(*) FROM conversions_data WHERE click_id = '{click}'")

    print("== Query-style postbacks ==")
    qp1 = f"{qp_tag}-q1"
    r = requests.get(f"{BASE}/pb", params={"clickid": qp1, "status": "sale", "payout": "1.5"},
                     verify=not INSECURE)
    check("qp: query-style /pb GET accepted",
          r.status_code == 200 and r.json().get("click_id") == qp1, r.text[:150])
    check("qp: query-style stores status and payout", qp_cell(qp1) == "sale|1.5|", qp_cell(qp1))

    for qp_alias in ("click_id", "clickid", "click", "subid", "sub_id", "cid"):
        qp_cl = f"{qp_tag}-ck-{qp_alias}"
        r = requests.get(f"{BASE}/pb", params={qp_alias: qp_cl, "status": "lead", "payout": "1"},
                         verify=not INSECURE)
        check(f"qp: click-id alias '{qp_alias}' maps",
              r.status_code == 200 and r.json().get("click_id") == qp_cl and qp_cell(qp_cl) == "lead|1|",
              r.text[:120])

    for qp_alias in ("status", "type", "event", "conversion_type"):
        qp_cl = f"{qp_tag}-st-{qp_alias}"
        r = requests.get(f"{BASE}/pb", params={"clickid": qp_cl, qp_alias: "sale", "payout": "2"},
                         verify=not INSECURE)
        check(f"qp: status alias '{qp_alias}' maps",
              r.status_code == 200 and qp_cell(qp_cl) == "sale|2|", r.text[:120])

    for qp_alias in ("payout", "sum", "amount", "revenue", "price"):
        qp_cl = f"{qp_tag}-po-{qp_alias}"
        r = requests.get(f"{BASE}/pb", params={"clickid": qp_cl, "status": "lead", qp_alias: "3.5"},
                         verify=not INSECURE)
        check(f"qp: payout alias '{qp_alias}' maps",
              r.status_code == 200 and qp_cell(qp_cl) == "lead|3.5|", r.text[:120])

    for qp_alias in ("tid", "transaction_id", "external_id", "txn", "transactionid"):
        qp_cl = f"{qp_tag}-tx-{qp_alias}"
        qp_tid = f"{qp_tag}-tid-{qp_alias}"
        r = requests.get(f"{BASE}/pb",
                         params={"clickid": qp_cl, "status": "lead", "payout": "1", qp_alias: qp_tid},
                         verify=not INSECURE)
        # external_id keeps its own column; the shorter aliases land on transaction_id
        check(f"qp: transaction alias '{qp_alias}' stored for dedupe",
              r.status_code == 200
              and qp_cell(qp_cl, "status || '|' || payout || '|' || "
                                 "COALESCE(transaction_id, external_id)") == f"lead|1|{qp_tid}",
              r.text[:120])

    qp_pf = f"{qp_tag}-postform"
    r = requests.post(f"{BASE}/pb",
                      data={"clickid": qp_pf, "conversion_type": "sale", "sum": "4.5",
                            "sub_id_1": f"pf-{qp_pid}"}, verify=not INSECURE)
    check("qp: POST form aliases accepted",
          r.status_code == 200 and r.json().get("click_id") == qp_pf, r.text[:150])
    check("qp: POST form stores status/payout and the sub_id passthrough",
          qp_cell(qp_pf) == "sale|4.5|"
          and pg_query(f"SELECT sub_id_1 FROM conversions_data WHERE click_id='{qp_pf}'") == f"pf-{qp_pid}",
          qp_cell(qp_pf))

    qp_pj = f"{qp_tag}-postjson"
    r = requests.post(f"{BASE}/pb",
                      json={"cid": qp_pj, "event": "lead", "revenue": "5.5",
                            "txn": f"{qp_tag}-tjson"}, verify=not INSECURE)
    check("qp: POST JSON aliases accepted",
          r.status_code == 200 and r.json().get("click_id") == qp_pj, r.text[:150])
    check("qp: POST JSON stores status/payout/transaction",
          qp_cell(qp_pj) == f"lead|5.5|{qp_tag}-tjson", qp_cell(qp_pj))

    qp_ms = f"{qp_tag}-missstatus"
    r = requests.get(f"{BASE}/pb", params={"clickid": qp_ms, "payout": "2"}, verify=not INSECURE)
    check("qp: missing status defaults to lead",
          r.status_code == 200 and qp_cell(qp_ms) == "lead|2|", r.text[:120])
    qp_mp = f"{qp_tag}-misspayout"
    r = requests.get(f"{BASE}/pb", params={"clickid": qp_mp, "status": "sale"}, verify=not INSECURE)
    check("qp: missing payout defaults to 0",
          r.status_code == 200 and qp_cell(qp_mp) == "sale|0|", r.text[:120])

    qp_none_tid = f"{qp_tag}-nonetid"
    r = requests.get(f"{BASE}/pb", params={"status": "sale", "payout": "6",
                                           "transaction_id": qp_none_tid}, verify=not INSECURE)
    check("qp: missing click id takes the clickless path",
          r.status_code == 200 and r.json().get("clickless") is True
          and r.json().get("click_id") == "none", r.text[:150])

    r = requests.get(f"{BASE}/pb", params={"clickid": f"{qp_tag}-bad", "status": "not_a_status",
                                           "payout": "1"}, verify=not INSECURE)
    check("qp: invalid status still 400", r.status_code == 400, str(r.status_code))
    r = requests.get(f"{BASE}/pb", params={"clickid": f"{qp_tag}-bad2", "status": "sale",
                                           "payout": "not_a_number"}, verify=not INSECURE)
    check("qp: invalid payout still 400", r.status_code == 400, str(r.status_code))

    qp_path = f"{qp_tag}-path"
    r = requests.get(f"{BASE}/pb?clickid={qp_path}&status=sale&payout=1.25", verify=not INSECURE)
    check("qp: canonical /pb works",
          r.status_code == 200 and r.json().get("duplicate") is False, r.text[:150])
    r = requests.get(f"{BASE}/pb?clickid={qp_path}&status=sale&payout=1.25", verify=not INSECURE)
    check("qp: canonical identical refire still dedupes",
          r.status_code == 200 and r.json().get("duplicate") is True, r.text[:150])

    # the legacy path form is retired: it now redirects to the canonical query form so
    # networks that were already configured with it keep working
    r = requests.get(f"{BASE}/pb/{qp_tag}-gone/sale/1", verify=not INSECURE, allow_redirects=False)
    check("qp: legacy path-style /pb redirects (301) to the query form",
          r.status_code == 301 and f"clickid={qp_tag}-gone" in (r.headers.get("location") or ""),
          f"{r.status_code} {r.headers.get('location')}")
    r = requests.get(f"{BASE}/pb/{qp_path}/sale/1.25", params={"transaction_id": "legacy-redirect"},
                     verify=not INSECURE, allow_redirects=False)
    loc = r.headers.get("location") or ""
    check("qp: legacy redirect carries the click id, status, payout and extra params",
          r.status_code == 301 and "clickid=" in loc and "status=sale" in loc
          and "payout=1.25" in loc and "transaction_id=legacy-redirect" in loc, loc)
    r = requests.get(f"{BASE}/pb/{qp_path}/sale/1.25", verify=not INSECURE,
                     params={"transaction_id": "legacy-redirect"})
    check("qp: following the legacy redirect records the conversion",
          r.status_code == 200 and r.json().get("click_id") == qp_path, r.text[:150])
    r = requests.post(f"{BASE}/pb/{qp_path}/sale/3", verify=not INSECURE, allow_redirects=False)
    check("qp: legacy path-style POST redirects with 308 (method preserved)",
          r.status_code == 308, str(r.status_code))

    # -- per-status Mode: 'new' inserts an extra row, 'repeated' accumulates --
    print("== Postback Mode (new vs repeated) ==")
    qp_saved = (s.get(f"{api}/settings/").json().get("settings") or {}).get("custom_statuses") or []
    qp_new_name = f"qp new {qp_pid}"
    qp_rep_name = f"qp rep {qp_pid}"
    r = s.post(f"{api}/settings/", json={"settings": {"custom_statuses": list(qp_saved) + [
        {"name": qp_new_name, "mode": "new"},
        {"name": qp_rep_name, "mode": "repeated"}]}})
    check("qp: mode custom statuses saved", r.status_code == 200, r.text[:120])
    settle_settings_cache()
    qp_new_status = qp_new_name.replace(" ", "_")
    qp_rep_status = qp_rep_name.replace(" ", "_")

    qp_new_click = f"{qp_tag}-new"
    requests.get(f"{BASE}/pb?clickid={qp_new_click}&status={qp_new_status}&payout=2",
                 params={"transaction_id": f"{qp_tag}-n1"}, verify=not INSECURE)
    r = requests.get(f"{BASE}/pb?clickid={qp_new_click}&status={qp_new_status}&payout=3",
                     params={"transaction_id": f"{qp_tag}-n2"}, verify=not INSECURE)
    check("qp: mode=new writes a second conversion row",
          r.status_code == 200 and r.json().get("duplicate") is False and qp_count(qp_new_click) == "2",
          f"{r.text[:100]} rows={qp_count(qp_new_click)}")
    qp_payouts = pg_query(f"SELECT string_agg(payout::text, ',' ORDER BY received_at) "
                          f"FROM conversions_data WHERE click_id='{qp_new_click}'")
    check("qp: mode=new keeps both payouts on separate rows", qp_payouts == "2,3", qp_payouts)

    r = requests.get(f"{BASE}/pb?clickid={qp_new_click}&status={qp_new_status}&payout=3",
                     params={"transaction_id": f"{qp_tag}-n2"}, verify=not INSECURE)
    check("qp: mode=new still dedupes a repeated transaction id",
          r.status_code == 200 and r.json().get("duplicate") is True and qp_count(qp_new_click) == "2",
          f"{r.text[:100]} rows={qp_count(qp_new_click)}")

    r = requests.get(f"{BASE}/pb?clickid={qp_new_click}&status={qp_new_status}&payout=3",
                     params={"transaction_id": f"{qp_tag}-n3"}, verify=not INSECURE)
    check("qp: mode=new fresh transaction id not swallowed by the 60s dedupe",
          r.status_code == 200 and r.json().get("duplicate") is False and qp_count(qp_new_click) == "3",
          f"{r.text[:100]} rows={qp_count(qp_new_click)}")

    qp_rep_click = f"{qp_tag}-rep"
    requests.get(f"{BASE}/pb?clickid={qp_rep_click}&status={qp_rep_status}&payout=2",
                 params={"transaction_id": f"{qp_tag}-r1"}, verify=not INSECURE)
    r = requests.get(f"{BASE}/pb?clickid={qp_rep_click}&status={qp_rep_status}&payout=3",
                     params={"transaction_id": f"{qp_tag}-r2"}, verify=not INSECURE)
    check("qp: mode=repeated updates in place (one row, accumulated)",
          r.status_code == 200 and qp_count(qp_rep_click) == "1"
          and qp_cell(qp_rep_click, "payout::text") == "5"
          and qp_cell(qp_rep_click, "postback_count::text") == "2",
          f"{r.text[:100]} rows={qp_count(qp_rep_click)} payout={qp_cell(qp_rep_click, 'payout::text')}")

    r = s.post(f"{api}/settings/", json={"settings": {"custom_statuses": qp_saved}})
    check("qp: custom statuses restored", r.status_code == 200, r.text[:120])
    pg_exec(f"DELETE FROM conversions_data WHERE click_id LIKE '{qp_tag}%' "
            f"OR transaction_id LIKE '{qp_tag}%'")
    check("qp: verification rows cleaned",
          pg_query(f"SELECT count(*) FROM conversions_data WHERE click_id LIKE '{qp_tag}%'") == "0"
          and pg_query(f"SELECT count(*) FROM conversions_data WHERE transaction_id LIKE '{qp_tag}%'") == "0",
          pg_query(f"SELECT count(*) FROM conversions_data WHERE click_id LIKE '{qp_tag}%'"))

    # ===== Active sessions + audit log filters =====
    print("== Active sessions & audit filters ==")
    sess_pid = os.getpid()
    sess_user = f"smoke-sess-{sess_pid}"
    r = s.post(f"{api}/users/", json={"username": sess_user, "password": "smokepass1",
                                      "active": True})
    check("sessions: test user created", r.status_code == 200, r.text[:150])
    sess_uid = r.json().get("id")

    chrome_ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    iphone_ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                 "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                 "Mobile/15E148 Safari/604.1")

    s1 = requests.Session()
    s1.verify = not INSECURE
    r = s1.post(f"{api}/login", json={"username": sess_user, "password": "smokepass1"},
                headers={"User-Agent": chrome_ua})
    check("sessions: first login ok", r.status_code == 200, r.text[:150])

    r = s1.get(f"{api}/users/me/sessions")
    sess_list = r.json().get("sessions", []) if r.status_code == 200 else []
    current = [x for x in sess_list if x.get("current")]
    check("sessions: own session listed + marked current",
          r.status_code == 200 and len(sess_list) == 1 and len(current) == 1, r.text[:250])
    cur_row = current[0] if current else {}
    check("sessions: login records ip + user agent",
          bool(cur_row.get("ip")) and "Chrome/120" in (cur_row.get("user_agent") or ""),
          json.dumps(cur_row)[:250])
    check("sessions: UA parsed to device/browser/os",
          cur_row.get("device") == "Desktop" and cur_row.get("browser") == "Chrome"
          and cur_row.get("os") == "Windows", json.dumps(cur_row)[:250])

    s2 = requests.Session()
    s2.verify = not INSECURE
    r = s2.post(f"{api}/login", json={"username": sess_user, "password": "smokepass1"},
                headers={"User-Agent": iphone_ua})
    check("sessions: second login (fresh client) ok", r.status_code == 200, r.text[:150])

    r = s.get(f"{api}/users/{sess_uid}/sessions")
    admin_sessions = r.json().get("sessions", []) if r.status_code == 200 else []
    check("sessions: admin sees both devices",
          r.status_code == 200 and len(admin_sessions) == 2, r.text[:250])

    r = s1.get(f"{api}/users/me/sessions")
    others = [x for x in r.json().get("sessions", []) if not x.get("current")]
    check("sessions: second device visible to owner", len(others) == 1, r.text[:200])
    other_id = others[0]["id"] if others else ""
    r = s1.delete(f"{api}/users/me/sessions/{other_id}")
    check("sessions: revoke one other session", r.status_code == 200, r.text[:150])
    r = s2.get(f"{api}/users/me")
    check("sessions: revoked cookie rejected (401)", r.status_code == 401, str(r.status_code))
    r = s1.get(f"{api}/users/me")
    check("sessions: current session unaffected by revoke", r.status_code == 200, r.text[:120])

    s3 = requests.Session()
    s3.verify = not INSECURE
    s3.post(f"{api}/login", json={"username": sess_user, "password": "smokepass1"},
            headers={"User-Agent": iphone_ua})
    r = s1.delete(f"{api}/users/me/sessions", params={"others": "true"})
    check("sessions: log out all other devices",
          r.status_code == 200 and r.json().get("revoked", 0) >= 1, r.text[:150])
    r = s3.get(f"{api}/users/me")
    check("sessions: other cookie rejected after logout-all (401)",
          r.status_code == 401, str(r.status_code))
    r = s1.get(f"{api}/users/me")
    check("sessions: current session survives logout-all", r.status_code == 200, r.text[:120])
    r = s1.get(f"{api}/users/me/sessions")
    remaining = r.json().get("sessions", [])
    check("sessions: only the current session remains",
          len(remaining) == 1 and remaining[0].get("current") is True, r.text[:200])

    # -- audit log filters + CSV export --
    r = s.get(f"{api}/audit/", params={"user": sess_user})
    d = r.json() if r.status_code == 200 else {}
    check("audit: user filter returns only that user",
          r.status_code == 200 and d.get("total", 0) >= 1
          and all(e["username"] == sess_user for e in d.get("entries", [])), r.text[:200])
    r = s.get(f"{api}/audit/", params={"entity": "user", "user": sess_user, "q": sess_user})
    d = r.json() if r.status_code == 200 else {}
    check("audit: entity + substring filters match",
          r.status_code == 200 and d.get("total", 0) >= 1
          and all(e["entity"] == "user" for e in d.get("entries", [])), r.text[:200])
    r = s.get(f"{api}/audit/", params={"date_from": "2099-01-01", "date_to": "2099-01-02"})
    check("audit: future date range returns nothing",
          r.status_code == 200 and r.json().get("total") == 0, r.text[:200])
    r = s.get(f"{api}/audit/", params={"user": sess_user,
                                       "date_from": "2000-01-01", "date_to": "2100-01-01"})
    check("audit: wide date range includes rows",
          r.status_code == 200 and r.json().get("total", 0) >= 1, r.text[:200])
    r = s.get(f"{api}/audit/export", params={"user": sess_user})
    body = r.content if r.status_code == 200 else b""
    check("audit: CSV export has UTF-8 BOM + header",
          r.status_code == 200 and body.startswith(b"\xef\xbb\xbf")
          and body.lstrip(b"\xef\xbb\xbf").startswith(b"id,at,username"), repr(body[:60]))
    check("audit: CSV export is filtered to the requested user",
          bool(body) and sess_user.encode() in body
          and b"tracker_admin" not in body, repr(body[:200]))

    # cleanup this section
    s1.post(f"{api}/logout")
    if sess_uid:
        r = s.delete(f"{api}/users/{sess_uid}")
        check("sessions: test user cleaned up", r.status_code == 200, r.text[:120])

    # ===== Logs area =====
    print("== Logs area ==")
    lg_pid = os.getpid()
    lg_base = f"logs-{lg_pid}"
    lg_dest = f"https://example.com/lg-dest-{lg_pid}"
    lg_fb = f"https://example.com/lg-fb-{lg_pid}"
    lg_campaign = lg_fb_campaign = None
    lg_saved_pr = (s.get(f"{api}/settings/").json().get("settings") or {}).get("postback_rules")

    r = s.post(f"{api}/campaigns/", json={
        "name": f"{lg_base}-redirect", "alias": f"{lg_base}-redirect",
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "config": {"flows": [{"type": "default", "position": 1, "enabled": True,
                              "schema": "redirect", "redirect_url": lg_dest,
                              "weight": 100, "filters": []}],
                   "postbacks": [], "fallback_url": lg_fb, "hide_referrer": False}})
    check("logs: redirect campaign created", r.status_code == 200 and "id" in r.json(),
          r.text[:200])
    lg_campaign = r.json().get("id") if r.status_code == 200 else None

    r = s.post(f"{api}/campaigns/", json={
        "name": f"{lg_base}-nomatch", "alias": f"{lg_base}-nomatch",
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "config": {"flows": [], "postbacks": [], "fallback_url": lg_fb, "hide_referrer": False}})
    check("logs: no-match campaign created", r.status_code == 200 and "id" in r.json(),
          r.text[:200])
    lg_fb_campaign = r.json().get("id") if r.status_code == 200 else None

    def lg_wait(fn, timeout=15):
        import time as _t
        deadline = _t.time() + timeout
        while _t.time() < deadline:
            if fn():
                return True
            _t.sleep(1)
        return False

    def lg_fwd(campaign_id, status):
        return s.get(f"{api}/logs/click-forwarding",
                     params={"campaign_id": campaign_id, "status": status,
                             "offset": 0, "limit": 10}).json()

    # -- click forwarding: normal redirect records the destination; no-match
    #    flow ends in a fallback --
    r = requests.get(f"{BASE}/{lg_base}-redirect", verify=not INSECURE, allow_redirects=False)
    check("logs: redirect campaign serves a redirect",
          r.status_code in (301, 302, 307, 308) and lg_dest in (r.headers.get("location") or ""),
          r.headers.get("location", ""))
    r = requests.get(f"{BASE}/{lg_base}-nomatch", verify=not INSECURE, allow_redirects=False)
    check("logs: no-match campaign falls back",
          r.status_code in (301, 302, 307, 308) and lg_fb in (r.headers.get("location") or ""),
          r.headers.get("location", ""))
    lg_fwd_resp = lg_fwd(lg_campaign, "redirected")
    check("logs: click-forwarding paged shape",
          isinstance(lg_fwd_resp.get("items"), list) and isinstance(lg_fwd_resp.get("total"), int),
          str(lg_fwd_resp)[:160])
    check("logs: successful redirect logged with destination",
          lg_wait(lambda: any(lg_dest in (it.get("destination_url") or "")
                              for it in lg_fwd(lg_campaign, "redirected").get("items", []))),
          str(lg_fwd(lg_campaign, "redirected"))[:200])
    check("logs: no-match flow logged as fallback",
          lg_wait(lambda: any(it.get("status") == "fallback"
                              for it in lg_fwd(lg_fb_campaign, "fallback").get("items", []))),
          str(lg_fwd(lg_fb_campaign, "fallback"))[:200])

    # -- S2S postbacks: accepted then duplicate --
    lg_acc = f"{lg_base}-accept"
    r = requests.get(f"{BASE}/pb", params={"clickid": lg_acc, "status": "sale", "payout": "5"},
                     verify=not INSECURE)
    check("logs: accepted postback processed", r.status_code == 200, r.text[:120])
    r = requests.get(f"{BASE}/pb", params={"clickid": lg_acc, "status": "sale", "payout": "5"},
                     verify=not INSECURE)
    check("logs: duplicate postback flagged", r.status_code == 200 and r.json().get("duplicate") is True,
          r.text[:120])

    def lg_postbacks(click_id, **kw):
        params = {"click_id": click_id, "offset": 0, "limit": 20}
        params.update(kw)
        return s.get(f"{api}/logs/postbacks", params=params).json()

    lg_pb = lg_postbacks(lg_acc)
    check("logs: postbacks paged shape",
          isinstance(lg_pb.get("items"), list) and isinstance(lg_pb.get("total"), int),
          str(lg_pb)[:160])
    check("logs: accepted postback logged",
          lg_wait(lambda: any(it.get("result") == "accepted"
                              for it in lg_postbacks(lg_acc).get("items", []))),
          str(lg_postbacks(lg_acc))[:200])
    check("logs: duplicate postback logged",
          lg_wait(lambda: any(it.get("result") == "duplicate"
                              for it in lg_postbacks(lg_acc).get("items", []))),
          str(lg_postbacks(lg_acc))[:200])
    lg_dup = lg_postbacks(lg_acc, result="duplicate")
    check("logs: postbacks respects the result filter",
          lg_dup.get("total") >= 1 and all(it.get("result") == "duplicate" for it in lg_dup["items"]),
          str(lg_dup)[:160])

    # -- CSV export carries the UTF-8 BOM --
    rc = s.get(f"{api}/logs/postbacks", params={"click_id": lg_acc, "format": "csv"})
    check("logs: postbacks CSV has UTF-8 BOM and header",
          rc.status_code == 200 and rc.content[:3] == b"\xef\xbb\xbf"
          and b"click_id" in rc.content[:120], repr(rc.content[:60]))

    # -- rule-rejected postback --
    lg_rej = f"{lg_base}-rule-rej"
    r = s.post(f"{api}/settings/", json={"settings": {"postback_rules": [{
        "enabled": True, "name": f"{lg_base} reject",
        "conditions": [{"field": "click_id", "operator": "starts_with", "value": lg_rej}],
        "action": {"type": "reject"}}]}})
    check("logs: reject rule saved", r.status_code == 200, r.text[:120])
    settle_settings_cache()
    r = requests.get(f"{BASE}/pb", params={"clickid": lg_rej, "status": "sale", "payout": "3"},
                     verify=not INSECURE)
    check("logs: rule-rejected postback returns rejected",
          r.status_code == 200 and r.json().get("status") == "rejected", r.text[:160])
    check("logs: rule-rejected postback logged",
          lg_wait(lambda: any(it.get("result") == "rule_rejected"
                              for it in lg_postbacks(lg_rej).get("items", []))),
          str(lg_postbacks(lg_rej))[:200])

    # -- outbound API (CAPI) postbacks reuse meta_capi_log --
    lg_api = s.get(f"{api}/logs/api-postbacks", params={"offset": 0, "limit": 5}).json()
    check("logs: api-postbacks paged shape",
          isinstance(lg_api.get("items"), list) and isinstance(lg_api.get("total"), int),
          str(lg_api)[:160])

    # -- cost updates write cost_update_logs --
    if lg_campaign:
        r = s.post(f"{api}/costs/update", json={
            "campaign_id": lg_campaign,
            "period": {"from": "2000-01-01", "to": "2000-01-02"}, "cost": 1})
        check("logs: cost update applied", r.status_code == 200, r.text[:150])
        cu = s.get(f"{api}/logs/cost-updates",
                   params={"campaign_id": lg_campaign, "offset": 0}).json()
        check("logs: cost-updates paged shape",
              isinstance(cu.get("items"), list) and isinstance(cu.get("total"), int),
              str(cu)[:160])
        check("logs: cost update logged for the campaign",
              any(it.get("campaign_id") == lg_campaign and it.get("username")
                  for it in cu.get("items", [])), str(cu)[:200])

    # -- logs cleanup --
    s.post(f"{api}/settings/", json={"settings": {"postback_rules": lg_saved_pr}})
    pg_exec(f"DELETE FROM postback_logs WHERE click_id LIKE '{lg_base}%'")
    pg_exec(f"DELETE FROM conversions_data WHERE click_id LIKE '{lg_base}%'")
    if lg_campaign:
        pg_exec(f"DELETE FROM click_forward_logs WHERE campaign_id = {lg_campaign}")
        pg_exec(f"DELETE FROM cost_update_logs WHERE campaign_id = {lg_campaign}")
    if lg_fb_campaign:
        pg_exec(f"DELETE FROM click_forward_logs WHERE campaign_id = {lg_fb_campaign}")
    if lg_fb_campaign:
        s.delete(f"{api}/campaigns/{lg_fb_campaign}")
    if lg_campaign:
        s.delete(f"{api}/campaigns/{lg_campaign}")
    check("logs: verification rows cleaned",
          pg_exec_out(f"SELECT count(*) FROM postback_logs WHERE click_id LIKE '{lg_base}%'").strip() == "0",
          pg_exec_out(f"SELECT count(*) FROM postback_logs WHERE click_id LIKE '{lg_base}%'"))

    # ===== Wave 18: sidebar grouping, saved presets, scripts, funnel
    # templates and the global fallback URL =====
    print("== Wave 18: IA + tools ==")
    w18 = os.getpid()

    # -- sidebar still renders every section (served-HTML check) --
    r = s.get(f"{BASE}/backend/dashboard")
    w18_nav_keys = ["dashboard", "campaigns", "landings", "affiliates", "offers",
                    "sources", "reports", "conversion-tracking", "logs", "domains",
                    "fraud", "optimizer", "settings", "users", "documentation", "scripts"]
    w18_html = r.text if r.status_code == 200 else ""
    w18_missing = [k for k in w18_nav_keys if f"key: '{k}'" not in w18_html]
    check("w18: sidebar renders every nav section",
          r.status_code == 200 and not w18_missing, str(w18_missing))
    check("w18: sidebar groups are present",
          "group: 'Performance'" in w18_html and "group: 'Tools'" in w18_html
          and "group: 'Account'" in w18_html,
          "group markers missing")

    # -- filter presets: CRUD + scope isolation --
    w18_p1 = s.post(f"{api}/filter-presets/", json={
        "name": f"w18-preset-logs-{w18}", "scope": "logs-postbacks",
        "filters": {"result": "accepted", "status": "sale"}})
    check("w18: create preset (logs)", w18_p1.status_code == 200, w18_p1.text[:150])
    w18_p1_id = w18_p1.json().get("preset", {}).get("id")
    w18_p2 = s.post(f"{api}/filter-presets/", json={
        "name": f"w18-preset-reports-{w18}", "scope": "reports",
        "filters": {"dimensions": ["country"]}})
    check("w18: create preset (reports)", w18_p2.status_code == 200, w18_p2.text[:150])
    w18_p2_id = w18_p2.json().get("preset", {}).get("id")

    w18_logs = s.get(f"{api}/filter-presets/", params={"scope": "logs-postbacks"}).json()
    w18_logs_ids = {p["id"] for p in w18_logs.get("presets", [])}
    check("w18: presets scoped — logs view sees only its own",
          w18_p1_id in w18_logs_ids and w18_p2_id not in w18_logs_ids,
          str(w18_logs)[:200])
    w18_reps = s.get(f"{api}/filter-presets/", params={"scope": "reports"}).json()
    w18_reps_ids = {p["id"] for p in w18_reps.get("presets", [])}
    check("w18: presets scoped — reports view sees only its own",
          w18_p2_id in w18_reps_ids and w18_p1_id not in w18_reps_ids,
          str(w18_reps)[:200])

    r = s.put(f"{api}/filter-presets/{w18_p1_id}",
              json={"name": f"w18-preset-logs-{w18}-renamed", "filters": {"result": "duplicate"}})
    check("w18: update preset",
          r.status_code == 200 and r.json()["preset"]["name"].endswith("-renamed")
          and r.json()["preset"]["filters"].get("result") == "duplicate", r.text[:200])

    for pid_, label in ((w18_p1_id, "logs"), (w18_p2_id, "reports")):
        r = s.delete(f"{api}/filter-presets/{pid_}")
        check(f"w18: delete preset ({label})", r.status_code == 200, r.text[:120])

    # -- script library: CRUD + list --
    w18_code = f"<script>/* w18-{w18} */</script>"
    r = s.post(f"{api}/scripts/", json={
        "title": f"w18-script-{w18}", "description": "smoke", "code": w18_code})
    check("w18: create script", r.status_code == 200, r.text[:150])
    w18_script_id = r.json().get("script", {}).get("id")
    r = s.get(f"{api}/scripts/")
    check("w18: script appears in list",
          r.status_code == 200 and any(x["id"] == w18_script_id for x in r.json().get("scripts", [])),
          r.text[:200])
    r = s.put(f"{api}/scripts/{w18_script_id}", json={"code": w18_code + "//v2"})
    check("w18: update script",
          r.status_code == 200 and r.json()["script"]["code"].endswith("//v2"), r.text[:200])
    r = s.delete(f"{api}/scripts/{w18_script_id}")
    check("w18: delete script", r.status_code == 200, r.text[:120])

    # -- funnel templates: save/apply round-trip --
    w18_steps = [
        {"name": "Step One", "landing": 1, "offers": [2, 3], "schema": "landing_offer"},
        {"name": "Step Two", "landing": None, "offers": [4], "schema": "landing_offer"},
    ]
    r = s.post(f"{api}/funnel-templates/", json={
        "name": f"w18-funnel-{w18}", "steps": w18_steps})
    check("w18: save funnel template", r.status_code == 200, r.text[:150])
    w18_tpl_id = r.json().get("template", {}).get("id")
    check("w18: template stores the steps verbatim",
          r.json()["template"]["steps"] == w18_steps, r.text[:250])

    r = s.get(f"{api}/funnel-templates/")
    w18_tpl = next((t for t in r.json().get("templates", []) if t["id"] == w18_tpl_id), None)
    check("w18: template listed with its steps", w18_tpl is not None
          and w18_tpl["steps"] == w18_steps, str(r.json())[:200])

    # Apply: a new campaign payload carries the template's steps verbatim.
    w18_funnel_alias = f"w18-funnel-camp-{w18}"
    r = s.post(f"{api}/campaigns/", json={
        "name": f"w18 funnel camp {w18}", "alias": w18_funnel_alias,
        "type": "campaign", "status": "active", "redirect_mode": "position",
        "config": {"flows": [], "postbacks": [], "hide_referrer": False, "fallback_url": "",
                   "funnel": {"enabled": True, "steps": w18_tpl["steps"]}}})
    check("w18: apply template to a new campaign", r.status_code == 200, r.text[:200])
    w18_funnel_cid = r.json().get("id")
    r = s.get(f"{api}/campaigns/")
    w18_saved = next((c for c in r.json() if c["id"] == w18_funnel_cid), None)
    check("w18: applied steps round-trip through the campaign",
          w18_saved is not None and w18_saved["config"]["funnel"]["steps"] == w18_steps,
          str(w18_saved)[:300])
    s.delete(f"{api}/campaigns/{w18_funnel_cid}")
    s.delete(f"{api}/funnel-templates/{w18_tpl_id}")

    # -- global fallback URL (with macro substitution) --
    w18_prev_settings = s.get(f"{api}/settings/").json().get("settings", {})
    w18_prev_fallback = w18_prev_settings.get("fallback_url", "")
    r = s.post(f"{api}/settings/", json={"settings": {
        "fallback_url": "https://example.com/w18-global-{click_id}?c={campaign_name}&s={sub_id_1}&m={_md5}"}})
    check("w18: global fallback saved", r.status_code == 200, r.text[:120])
    settle_settings_cache()

    w18_alias = f"w18-global-fb-{w18}"
    w18_name = f"w18 global campaign {w18}"
    w18_cfg = {"flows": [{"type": "default", "position": 1, "enabled": True,
                          "schema": "redirect", "redirect_url": "https://example.com/w18-a",
                          "weight": 100,
                          "filters": [{"parameter": "country", "condition": "equals", "value": "ZZ"}]}],
               "postbacks": [], "fallback_url": "", "hide_referrer": False}
    r = s.post(f"{api}/campaigns/", json={
        "name": w18_name, "alias": w18_alias, "type": "campaign", "status": "active",
        "redirect_mode": "position", "config": w18_cfg})
    check("w18: no-fallback campaign created", r.status_code == 200, r.text[:150])
    w18_cid = r.json().get("id")

    r = requests.get(f"{BASE}/{w18_alias}",
                     params={"click_id": f"w18clk{w18}", "sub_id_1": f"w18sub{w18}"},
                     verify=not INSECURE, allow_redirects=False)
    w18_loc = r.headers.get("location") or ""
    w18_md5 = __import__("hashlib").md5(f"w18clk{w18}".encode()).hexdigest()
    check("w18: no-fallback campaign hits the global fallback",
          "example.com/w18-global-w18clk" in w18_loc, f"{r.status_code} -> {w18_loc}")
    check("w18: global fallback macros substituted",
          f"s=w18sub{w18}" in w18_loc and f"m={w18_md5}" in w18_loc
          and "c=w18%20global%20campaign" in w18_loc, w18_loc)

    # A campaign-level fallback still wins over the global one.
    w18_cfg["fallback_url"] = "https://example.com/w18-own-{click_id}"
    r = s.put(f"{api}/campaigns/{w18_cid}", json={
        "name": w18_name, "alias": w18_alias, "type": "campaign", "status": "active",
        "redirect_mode": "position", "config": w18_cfg})
    r = requests.get(f"{BASE}/{w18_alias}", params={"click_id": f"w18own{w18}"},
                     verify=not INSECURE, allow_redirects=False)
    check("w18: campaign-level fallback wins over global",
          f"example.com/w18-own-w18own{w18}" in (r.headers.get("location") or ""),
          f"{r.status_code} -> {r.headers.get('location', '')}")

    s.delete(f"{api}/campaigns/{w18_cid}")
    r = s.post(f"{api}/settings/", json={"settings": {"fallback_url": w18_prev_fallback}})
    check("w18: global fallback restored", r.status_code == 200, r.text[:120])
    settle_settings_cache()

    # ===== Wave 19A: workspace display settings + configurable columns =====
    print("== Wave 19A: workspace display settings ==")
    w19 = os.getpid()

    r = s.get(f"{api}/workspace/")
    check("w19: workspace reader returns the display block",
          r.status_code == 200 and isinstance(r.json().get("workspace"), dict)
          and "decimals" in r.json()["workspace"], r.text[:160])
    w19_saved_ws = ((s.get(f"{api}/settings/").json().get("settings")) or {}).get("workspace")

    w19_decimals = 3 if (w19_saved_ws or {}).get("decimals") != 3 else 4
    w19_ws = {
        "decimals": w19_decimals,
        "divider": "space",
        "row_coloring": [
            {"column": "roi", "operator": "<", "value": 0, "color": "red"},
            {"column": "roi", "operator": ">=", "value": 100, "color": "green"},
        ],
        "default_columns": {"logs-clicks": ["received_at", "ip", "status"]},
    }

    # A sibling key in the shared settings document must survive the save.
    r = s.post(f"{api}/settings/", json={"settings": {"__smoke_w19_keep": {"n": 1}}})
    check("w19: sentinel settings key saved", r.status_code == 200, r.text[:120])
    r = s.post(f"{api}/settings/", json={"settings": {"workspace": w19_ws}})
    check("w19: workspace block saved via settings API", r.status_code == 200, r.text[:120])

    w19_back = (((s.get(f"{api}/settings/").json().get("settings")) or {}).get("workspace") or {})
    check("w19: workspace decimals round-trips", w19_back.get("decimals") == w19_decimals,
          str(w19_back)[:160])
    check("w19: workspace divider round-trips", w19_back.get("divider") == "space",
          str(w19_back)[:160])
    check("w19: workspace row_coloring round-trips",
          isinstance(w19_back.get("row_coloring"), list) and len(w19_back["row_coloring"]) == 2
          and w19_back["row_coloring"][0].get("color") == "red",
          str(w19_back.get("row_coloring"))[:160])
    check("w19: workspace default_columns round-trips",
          (w19_back.get("default_columns") or {}).get("logs-clicks")
          == ["received_at", "ip", "status"], str(w19_back.get("default_columns"))[:160])
    w19_keep = (((s.get(f"{api}/settings/").json().get("settings")) or {}).get("__smoke_w19_keep") or {})
    check("w19: saving workspace does not clobber sibling settings keys",
          w19_keep.get("n") == 1, str(w19_keep)[:120])
    r = s.get(f"{api}/workspace/")
    check("w19: workspace reader reflects saved decimals",
          (r.json().get("workspace") or {}).get("decimals") == w19_decimals, r.text[:160])

    # -- column templates: save/load/delete per scope, isolated between scopes --
    w19_scope_a = "logs-clicks"
    w19_scope_b = "reports"
    w19_tname = f"w19-{w19}"
    r = s.put(f"{api}/settings/column-templates", json={
        "scope": w19_scope_a, "name": w19_tname, "columns": ["received_at", "ip"]})
    check("w19: column template saved (scope A)", r.status_code == 200
          and r.json().get("template", {}).get("name") == w19_tname, r.text[:160])
    r = s.put(f"{api}/settings/column-templates", json={
        "scope": w19_scope_b, "name": w19_tname, "columns": ["visits", "revenue"]})
    check("w19: column template saved (scope B)", r.status_code == 200, r.text[:160])

    w19_ta = s.get(f"{api}/settings/column-templates",
                   params={"scope": w19_scope_a}).json().get("templates", [])
    w19_tb = s.get(f"{api}/settings/column-templates",
                   params={"scope": w19_scope_b}).json().get("templates", [])
    w19_a_cols = next((t.get("columns") for t in w19_ta if t.get("name") == w19_tname), None)
    w19_b_cols = next((t.get("columns") for t in w19_tb if t.get("name") == w19_tname), None)
    check("w19: column templates load per scope",
          w19_a_cols == ["received_at", "ip"] and w19_b_cols == ["visits", "revenue"],
          f"A={w19_a_cols} B={w19_b_cols}")
    check("w19: column templates isolated between scopes",
          len([t for t in w19_ta if t.get("name") == w19_tname]) == 1
          and len([t for t in w19_tb if t.get("name") == w19_tname]) == 1,
          f"A={w19_ta} B={w19_tb}")

    check("w19: empty template columns rejected",
          s.put(f"{api}/settings/column-templates",
                json={"scope": w19_scope_a, "name": "w19-bad", "columns": []}).status_code == 400)
    check("w19: deleting a missing template 404s",
          s.delete(f"{api}/settings/column-templates",
                   params={"scope": w19_scope_a, "name": "w19-missing"}).status_code == 404)
    check("w19: column template deleted (scope A)",
          s.delete(f"{api}/settings/column-templates",
                   params={"scope": w19_scope_a, "name": w19_tname}).status_code == 200)
    check("w19: column template deleted (scope B)",
          s.delete(f"{api}/settings/column-templates",
                   params={"scope": w19_scope_b, "name": w19_tname}).status_code == 200)
    w19_ta2 = s.get(f"{api}/settings/column-templates",
                    params={"scope": w19_scope_a}).json().get("templates", [])
    check("w19: deleted template no longer listed",
          not any(t.get("name") == w19_tname for t in w19_ta2), str(w19_ta2)[:160])

    # -- the served pages carry the new markers --
    for w19_pg, w19_marker in (("settings", "settings.workspace.decimals"),
                               ("logs", "loadColumnTemplates"),
                               ("reports", "column-templates?scope=reports")):
        rp = s.get(f"{BASE}/backend/{w19_pg}")
        check(f"w19: served /backend/{w19_pg} has the workspace marker",
              rp.status_code == 200 and w19_marker in rp.text,
              f"{rp.status_code} missing {w19_marker}")
        check(f"w19: /backend/{w19_pg} has no unreplaced jinja tags",
              "{%" not in rp.text, "unreplaced jinja tag")

    # cleanup: restore the original workspace and drop the sentinel key
    _ = s.post(f"{api}/settings/", json={"settings": {"workspace": w19_saved_ws}})
    _ = s.post(f"{api}/settings/", json={"settings": {"__smoke_w19_keep": None}})
    w19_ws_final = (((s.get(f"{api}/settings/").json().get("settings")) or {}).get("workspace") or {})
    check("w19: workspace restored after the run",
          (w19_saved_ws or {}) == w19_ws_final if isinstance(w19_saved_ws, dict) else True,
          str(w19_ws_final)[:160])

    # ===== Wave 19B: report templates, IP report, conversion reconciliation =====
    print("== Wave 19B: IP report + approval lifecycle + conversions log ==")
    w19b = os.getpid()
    w19b_conv_ids = []

    # -- approval column exists with the pending default after the startup migration
    w19b_col = pg_exec_out(
        "SELECT column_default || '|' || is_nullable FROM information_schema.columns "
        "WHERE table_name='conversions_data' AND column_name='approval'").strip()
    check("w19b: approval column exists with pending default after migration",
          "'pending'::character varying" in w19b_col and w19b_col.endswith("|NO"), w19b_col)

    # -- a normal conversion starts pending and is not a duplicate
    w19b_norm = f"w19b-normal-{w19b}"
    w19b_tid_a = f"w19bsum-{w19b}-a"
    requests.get(f"{BASE}/pb?clickid={w19b_norm}&status=sale&payout=4",
                 params={"transaction_id": w19b_tid_a}, verify=not INSECURE)
    w19b_rec_a = poll_first({"click_id": w19b_norm})
    check("w19b: normal conversion defaults to approval=pending",
          w19b_rec_a and w19b_rec_a.get("approval") == "pending", str(w19b_rec_a and w19b_rec_a.get("approval")))
    check("w19b: normal conversion is not a duplicate and exposes its dedupe token",
          w19b_rec_a and w19b_rec_a.get("is_duplicate") is False
          and w19b_rec_a.get("dedupe_token") == w19b_tid_a,
          str(w19b_rec_a and (w19b_rec_a.get("is_duplicate"), w19b_rec_a.get("dedupe_token"))))
    if w19b_rec_a:
        w19b_conv_ids.append(w19b_rec_a["id"])

    # -- single-conversion approval set + invalid value rejected
    if w19b_rec_a:
        r = s.patch(f"{api}/reports/{w19b_rec_a['id']}", json={"approval": "approved"})
        check("w19b: single conversion approval set accepted", r.status_code == 200, r.text[:150])
        r = s.get(f"{api}/reports/", params={"click_id": w19b_norm})
        got = r.json()[0] if r.status_code == 200 and r.json() else {}
        check("w19b: approval reflected in the conversion list",
              got.get("approval") == "approved", str(got.get("approval")))
        r = s.patch(f"{api}/reports/{w19b_rec_a['id']}", json={"approval": "maybe"})
        check("w19b: invalid approval on single update -> 400", r.status_code == 400, str(r.status_code))

    # -- invalid approval on the bulk endpoint -> 400
    r = s.post(f"{api}/reports/bulk-approval", json={"ids": [w19b_rec_a["id"] if w19b_rec_a else 0],
                                                     "approval": "maybe"})
    check("w19b: bulk approval rejects an invalid value (400)", r.status_code == 400, str(r.status_code))

    # -- manual add conversion (dedupe path), then bulk approval + bulk status
    w19b_man = f"w19b-man-{w19b}"
    w19b_tid_b = f"w19bsum-{w19b}-b"
    r = s.post(f"{api}/reports/conversion", json={
        "status": "sale", "approval": "pending", "payout": 3.0,
        "click_id": w19b_man, "transaction_id": w19b_tid_b,
        "sub_ids": {"sub_id_1": f"w19b-sub-{w19b}"}})
    w19b_man_id = (r.json() or {}).get("id")
    check("w19b: manual add conversion created", r.status_code == 200
          and (r.json() or {}).get("created") is True and bool(w19b_man_id), r.text[:200])
    if w19b_man_id:
        w19b_conv_ids.append(w19b_man_id)
    check("w19b: manual add without any id -> 400",
          s.post(f"{api}/reports/conversion", json={"payout": 1}).status_code == 400)

    if w19b_man_id:
        r = s.post(f"{api}/reports/bulk-approval",
                   json={"ids": [w19b_man_id], "approval": "declined"})
        check("w19b: bulk approval accepted", r.status_code == 200
              and (r.json() or {}).get("updated") == 1, r.text[:180])
        r = s.get(f"{api}/reports/", params={"click_id": w19b_man})
        got = r.json()[0] if r.status_code == 200 and r.json() else {}
        check("w19b: bulk approval reflected in the list", got.get("approval") == "declined",
              str(got.get("approval")))

        r = s.post(f"{api}/reports/bulk-status",
                   json={"ids": [w19b_man_id], "status": "rejected"})
        check("w19b: bulk status change accepted", r.status_code == 200
              and (r.json() or {}).get("updated") == 1, r.text[:180])
        r = s.get(f"{api}/reports/", params={"click_id": w19b_man})
        got = r.json()[0] if r.status_code == 200 and r.json() else {}
        check("w19b: bulk status reflected in the list", got.get("status") == "rejected",
              str(got.get("status")))
    check("w19b: bulk status rejects an unknown status (400)",
          s.post(f"{api}/reports/bulk-status",
                 json={"ids": [w19b_man_id or 0], "status": "definitely_not_a_status"}).status_code == 400)

    # -- approval filter
    r = s.get(f"{api}/reports/", params={"approval": "approved", "click_id": w19b_norm})
    check("w19b: approval filter returns the approved row",
          r.status_code == 200 and r.json() and r.json()[0].get("approval") == "approved",
          r.text[:200])

    # -- the dedupe path marks the row duplicate on a repeated write
    if w19b_man_id:
        r = s.post(f"{api}/reports/conversion", json={"click_id": w19b_man, "payout": 1.0})
        check("w19b: repeat write dedupes onto the existing conversion",
              r.status_code == 200 and (r.json() or {}).get("created") is False, r.text[:180])
        r = s.get(f"{api}/reports/", params={"click_id": w19b_man})
        got = r.json()[0] if r.status_code == 200 and r.json() else {}
        check("w19b: duplicate conversion flagged and keeps its dedupe token",
              got.get("is_duplicate") is True and got.get("dedupe_token") == w19b_tid_b,
              str(got and (got.get("is_duplicate"), got.get("dedupe_token"))))

    # -- approval / decline rates over the two pid-scoped conversions
    r = s.get(f"{api}/reports/summary", params={"search": f"w19bsum-{w19b}"})
    summ = r.json() if r.status_code == 200 else {}
    check("w19b: conversions summary returns approval/decline rates",
          r.status_code == 200 and summ.get("total", 0) >= 2
          and summ.get("approval_rate") == round(summ.get("approved", 0) / summ["total"] * 100, 2)
          and summ.get("decline_rate") == round(summ.get("declined", 0) / summ["total"] * 100, 2),
          str(summ))
    check("w19b: summary approval counts match the seeded rows",
          summ.get("approved", 0) >= 1 and summ.get("declined", 0) >= 1, str(summ))

    # -- CSV export carries the approval column
    r = s.get(f"{api}/reports/export", params={"click_id": w19b_man})
    check("w19b: conversions CSV export includes the approval column",
          r.status_code == 200 and "approval" in r.text.splitlines()[0], r.text[:120])

    # -- IP report: single day returns rows, multi-day is rejected with an explanation
    requests.get(f"{BASE}/{alias}", verify=not INSECURE, allow_redirects=False)
    w19b_day = ch_query("SELECT toString(toDate(now()))")
    r = s.post(f"{api}/dashboard/breakdown", json={
        "dimensions": ["ip"], "filters": {"date_from": w19b_day, "date_to": w19b_day}})
    check("w19b: IP report single day returns grouped rows",
          r.status_code == 200 and isinstance((r.json() or {}).get("rows"), list)
          and len(r.json()["rows"]) >= 1, f"{r.status_code} {r.text[:160]}")
    r = s.post(f"{api}/dashboard/breakdown", json={
        "dimensions": ["ip"], "filters": {"date_from": "2026-01-01", "date_to": "2026-01-05"}})
    check("w19b: IP report multi-day range rejected with an explanation",
          r.status_code == 400 and "single day" in (r.json() or {}).get("detail", "").lower(),
          f"{r.status_code} {r.text[:160]}")

    # -- served page carries the new markers, no unreplaced jinja tags
    rp = s.get(f"{BASE}/backend/reports")
    check("w19b: served /backend/reports 200 with gallery + approval markers",
          rp.status_code == 200 and "reportTemplateGallery" in rp.text
          and "bulk-approval" in rp.text and "loadConversionsSummary" in rp.text,
          f"{rp.status_code}")
    check("w19b: /backend/reports has no unreplaced jinja tags",
          "{%" not in rp.text, "unreplaced jinja tag")

    # cleanup the conversions this block created
    for w19b_cid_conv in w19b_conv_ids:
        s.delete(f"{api}/reports/{w19b_cid_conv}")

    # ===== Meta Ads cost auto-sync: mock Graph endpoint + end-to-end =====
    import time as _meta_time
    import threading as _meta_threading
    import http.server as _meta_httpserver
    import socketserver as _meta_socketserver

    meta_pid = os.getpid()
    meta_port = 22000 + (meta_pid % 1000)
    meta_base = f"http://host.docker.internal:{meta_port}"
    meta_acct = f"primary-{meta_pid}"
    meta_token = f"tok-meta-{meta_pid}"
    meta_captured = []
    _meta_socketserver.TCPServer.allow_reuse_address = True

    meta_day = ch_query("SELECT toString(toDate(now()))")

    def _meta_row(pcid, cname, spend, impressions, clicks):
        return {"campaign_id": pcid, "campaign_name": cname, "date_start": meta_day,
                "spend": str(spend), "impressions": str(impressions), "clicks": str(clicks)}

    class _MetaGraph(_meta_httpserver.BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            meta_captured.append({"method": "GET", "path": self.path,
                                  "path_only": parsed.path, "params": params})

            def _send(code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            if "/ready" in self.path:
                _send(200, {"ready": True})
                return
            if "fail500" in self.path:
                _send(500, {"error": "server error"})
                return
            if "fail429" in self.path:
                _send(429, {"error": "rate limited"})
                return
            if parsed.path.endswith("/adsets"):
                # Ad-platform control: a campaign's ad sets. The parent status is
                # included so the endpoint can surface it when it is returned.
                _send(200, {
                    "campaign_status": "ACTIVE",
                    "data": [
                        {"id": f"as1-{meta_pid}", "name": f"Ad set one {meta_pid}",
                         "status": "ACTIVE"},
                        {"id": f"as2-{meta_pid}", "name": f"Ad set two {meta_pid}",
                         "status": "PAUSED"},
                    ],
                })
                return
            if "page2" in self.path:
                # second page: name-matched, tracking-matched, zero-click and unmatched rows
                _send(200, {"data": [
                    _meta_row(f"mvn-{meta_pid}", f"MV Name {meta_pid}", 6, 60, 3),
                    _meta_row(f"mvtrk-{meta_pid}", f"mv-utm-{meta_pid}", 8, 80, 4),
                    _meta_row(f"mvz-{meta_pid}", f"MV Zero {meta_pid}", 3, 30, 5),
                    _meta_row(f"mvnone-{meta_pid}", f"MV Nobody {meta_pid}", 4, 40, 1),
                ]})
                return
            _send(200, {
                "data": [_meta_row(f"mvid-{meta_pid}", f"MV ID {meta_pid}", 10, 100, 2)],
                "paging": {"next": f"{meta_base}/page2-{meta_pid}"},
            })

        def do_POST(self):
            # Ad-platform control writes: campaign / ad-set status POSTs. The
            # body is form-encoded (status + access_token), as Meta expects.
            from urllib.parse import urlparse, parse_qs
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode() if length else ""
            body = {k: v[0] for k, v in parse_qs(raw).items()}
            parsed = urlparse(self.path)
            meta_captured.append({"method": "POST", "path": self.path,
                                  "path_only": parsed.path, "body": body})

            def _send(code, obj):
                out = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out)

            if "fail500" in self.path:
                _send(500, {"error": "server error"})
                return
            _send(200, {"id": parsed.path.rsplit("/", 1)[-1],
                        "status": body.get("status", "")})

        def log_message(self, *args):
            pass

    meta_srv = _meta_socketserver.TCPServer(("0.0.0.0", meta_port), _MetaGraph)
    meta_srv.daemon_threads = True
    _meta_threading.Thread(target=meta_srv.serve_forever, daemon=True).start()

    # Reachability from the backend container (where the sync runs).
    meta_probe = ""
    try:
        meta_probe = subprocess.run(
            ["docker", "exec", "tracker_backend", "python", "-c",
             f"import urllib.request;print(urllib.request.urlopen('{meta_base}/ready',timeout=5).read().decode())"],
            capture_output=True, text=True, timeout=15).stdout
    except Exception:
        pass
    if "ready" not in meta_probe:
        try:
            meta_probe = requests.get(f"http://127.0.0.1:{meta_port}/ready", timeout=5).text
        except Exception:
            pass
    check("meta-ads: mock Graph receiver reachable from the backend container",
          "ready" in meta_probe, meta_probe[:80])

    def meta_mk(name, alias, platform_id=None, config=None):
        body = {"name": name, "alias": alias, "type": "campaign", "status": "active",
                "redirect_mode": "position",
                "config": config or {"flows": [], "postbacks": [], "hide_referrer": False}}
        if platform_id is not None:
            body["ad_platform_campaign_id"] = platform_id
        return s.post(f"{api}/campaigns/", json=body)

    r = meta_mk(f"MV ID {meta_pid}", f"smoke-meta-id-{meta_pid}", f"mvid-{meta_pid}")
    meta_id_cid = r.json().get("id")
    r = meta_mk(f"MV Name {meta_pid}", f"smoke-meta-name-{meta_pid}")
    meta_name_cid = r.json().get("id")
    r = meta_mk(f"MV Track {meta_pid}", f"smoke-meta-track-{meta_pid}", None,
                {"flows": [], "postbacks": [], "hide_referrer": False,
                 "utm_campaign": f"mv-utm-{meta_pid}"})
    meta_track_cid = r.json().get("id")
    r = meta_mk(f"MV Zero {meta_pid}", f"smoke-meta-zero-{meta_pid}")
    meta_zero_cid = r.json().get("id")
    check("meta-ads: four test campaigns created",
          all([meta_id_cid, meta_name_cid, meta_track_cid, meta_zero_cid]),
          f"{meta_id_cid},{meta_name_cid},{meta_track_cid},{meta_zero_cid}")

    r = s.get(f"{api}/campaigns/")
    listed = next((c for c in r.json() if c["alias"] == f"smoke-meta-id-{meta_pid}"), {})
    check("meta-ads: campaign API exposes ad_platform_campaign_id",
          listed.get("ad_platform_campaign_id") == f"mvid-{meta_pid}",
          str(listed.get("ad_platform_campaign_id")))

    def meta_set(**over):
        cfg = {"enabled": True, "ad_account_ids": [meta_acct], "access_token": meta_token,
               "api_version": "v21.0", "dry_run": False, "cadence": "hourly",
               "backfill_days": 7, "graph_base_url": meta_base, "match_preference": "auto"}
        cfg.update(over)
        return s.post(f"{api}/settings/", json={"settings": {"meta_ads": cfg}})

    def meta_sync():
        return s.post(f"{api}/meta-ads/sync")

    def meta_rows():
        return pg_exec_out(
            f"SELECT count(*) FROM ad_cost_daily WHERE ad_account_id = '{meta_acct}'").strip()

    meta_saved = ((s.get(f"{api}/settings/").json().get("settings") or {})
                  .get("meta_ads"))
    meta_uid = None
    try:
        # -- dry-run: builds + logs the request and matching, writes nothing --
        r = meta_set(dry_run=True)
        check("meta-ads: dry-run config saved", r.status_code == 200, r.text[:120])
        r = meta_sync()
        dry = r.json() if r.status_code == 200 else {}
        check("meta-ads: dry-run sync returns dry_run status",
              r.status_code == 200 and dry.get("status") == "dry_run", r.text[:200])
        check("meta-ads: dry-run reported would-allocate targets",
              len(dry.get("would_allocate") or []) == 4, str(dry.get("would_allocate"))[:200])
        check("meta-ads: dry-run reports matching (4 matched, 1 unmatched)",
              dry.get("matched") == 4 and dry.get("unmatched") == 1,
              f"matched={dry.get('matched')} unmatched={dry.get('unmatched')}")
        check("meta-ads: dry-run masks the token in the logged request",
              bool(dry.get("requests")) and meta_token not in json.dumps(dry.get("requests")),
              str(dry.get("requests"))[:150])
        check("meta-ads: dry-run writes no ad_cost_daily rows", meta_rows() == "0", meta_rows())

        # -- seed tracker clicks for the matched campaigns --
        def meta_seed(cid, clicks, visits=0):
            total = int(clicks) + int(visits)
            if total <= 0:
                return
            ch_query(
                "INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, cost) "
                f"SELECT now(), {cid}, number < {int(clicks)}, '', "
                f"'meta-seed-{meta_pid}-{cid}-' || toString(number), 0 FROM numbers({total})")

        meta_seed(meta_id_cid, 2, visits=1)   # spend 10 / 2 clicks = 5 each
        meta_seed(meta_name_cid, 3)           # spend 6 / 3 = 2 each
        meta_seed(meta_track_cid, 4)          # spend 8 / 4 = 2 each
        # meta_zero_cid deliberately has no clicks -> stored but not allocated

        # -- live sync: request shape, matching, storage + allocation --
        r = meta_set(dry_run=False)
        check("meta-ads: live config saved", r.status_code == 200, r.text[:120])
        r = meta_sync()
        live = r.json() if r.status_code == 200 else {}
        check("meta-ads: live sync returns ok",
              r.status_code == 200 and live.get("status") == "ok", r.text[:250])

        insights = next((c for c in meta_captured if c["path_only"].endswith("/insights")), None)
        check("meta-ads: insights path is /{api_version}/act_{account}/insights",
              bool(insights) and insights["path_only"] == f"/v21.0/act_{meta_acct}/insights",
              str(insights and insights["path_only"]))
        ip = (insights or {}).get("params", {})
        check("meta-ads: insights level=campaign + time_increment=1",
              ip.get("level") == "campaign" and ip.get("time_increment") == "1", str(ip)[:200])
        check("meta-ads: insights fields are the contract",
              ip.get("fields") == "campaign_id,campaign_name,spend,impressions,clicks",
              str(ip.get("fields")))
        check("meta-ads: insights use an explicit time_range for the backfill",
              "time_range" in ip and meta_day in ip.get("time_range", ""), str(ip.get("time_range")))
        check("meta-ads: access token passed to Graph",
              ip.get("access_token") == meta_token, str(ip.get("access_token"))[:40])
        check("meta-ads: paging.next is followed verbatim",
              any(c["path_only"].endswith(f"/page2-{meta_pid}") for c in meta_captured),
              str([c["path_only"] for c in meta_captured])[:200])

        strat = {(m["platform_campaign_id"]): m["strategy"] for m in (live.get("matches") or [])}
        check("meta-ads: matched by ad_platform_campaign_id",
              strat.get(f"mvid-{meta_pid}") == "ad_platform_campaign_id", str(strat))
        check("meta-ads: fallback match by campaign name",
              strat.get(f"mvn-{meta_pid}") == "name", str(strat))
        check("meta-ads: fallback match by utm_campaign tracking id",
              strat.get(f"mvtrk-{meta_pid}") == "tracking_id", str(strat))
        check("meta-ads: unmatched campaign reported as unmatched",
              live.get("matched") == 4 and live.get("unmatched") == 1
              and any(u["platform_campaign_id"] == f"mvnone-{meta_pid}"
                      for u in (live.get("unmatched_rows") or [])),
              f"m={live.get('matched')} u={live.get('unmatched')}")

        check("meta-ads: one audit row per campaign+day (5 rows incl. unmatched/zero-click)",
              meta_rows() == "5", meta_rows())
        matched_ids = pg_exec_out(
            f"SELECT matched_campaign_id FROM ad_cost_daily "
            f"WHERE ad_account_id='{meta_acct}' AND platform_campaign_id='mvnone-{meta_pid}'").strip()
        check("meta-ads: unmatched row stores a NULL matched_campaign_id", matched_ids == "",
              matched_ids)
        check("meta-ads: zero-click matched day is stored but allocates nothing",
              live.get("zero_click_days") == 1, str(live.get("zero_click_days")))

        # allocation: per-click cost = spend / that day's clicks
        a_cost = ch_query(
            f"SELECT DISTINCT toString(cost) FROM clicks_data WHERE campaign_id={meta_id_cid} AND click=true")
        a_total = ch_query(
            f"SELECT toString(sum(cost)) FROM clicks_data WHERE campaign_id={meta_id_cid} AND click=true")
        b_total = ch_query(
            f"SELECT toString(sum(cost)) FROM clicks_data WHERE campaign_id={meta_name_cid} AND click=true")
        c_total = ch_query(
            f"SELECT toString(sum(cost)) FROM clicks_data WHERE campaign_id={meta_track_cid} AND click=true")
        z_total = ch_query(
            f"SELECT toString(ifNull(sum(cost), 0)) FROM clicks_data WHERE campaign_id={meta_zero_cid}")
        a_visit = ch_query(
            f"SELECT DISTINCT toString(cost) FROM clicks_data WHERE campaign_id={meta_id_cid} AND click=false")
        check("meta-ads: per-click cost = spend / clicks (10/2 = 5)",
              a_cost in ("5", "5.0"), a_cost)
        check("meta-ads: campaign day total cost equals platform spend",
              a_total in ("10", "10.0") and b_total in ("6", "6.0") and c_total in ("8", "8.0"),
              f"a={a_total} b={b_total} c={c_total}")
        check("meta-ads: visit rows are not allocated",
              a_visit in ("0", "0.0"), a_visit)
        check("meta-ads: zero-click campaign got no cost", z_total in ("0", "", "0.0"), z_total)

        # -- idempotency: same payload twice -> still one row per campaign+day --
        r = meta_sync()
        check("meta-ads: re-sync stays ok", r.status_code == 200, r.text[:150])
        check("meta-ads: re-sync does not duplicate ad_cost_daily rows", meta_rows() == "5",
              meta_rows())
        a_cost2 = ch_query(
            f"SELECT DISTINCT toString(cost) FROM clicks_data WHERE campaign_id={meta_id_cid} AND click=true")
        check("meta-ads: re-sync does not double the allocated cost", a_cost2 == a_cost,
              f"{a_cost} -> {a_cost2}")

        # -- re-run REPLACES rather than adds: clobber, then re-sync restores --
        s.post(f"{api}/costs/update", json={
            "campaign_id": meta_id_cid,
            "period": {"from": meta_day, "to": meta_day}, "cost": 99})
        r = meta_sync()
        a_cost3 = ch_query(
            f"SELECT DISTINCT toString(cost) FROM clicks_data WHERE campaign_id={meta_id_cid} AND click=true")
        check("meta-ads: re-sync replaces a clobbered day (99 -> 5, never adds)",
              r.status_code == 200 and a_cost3 in ("5", "5.0"), f"after={a_cost3}")

        # -- dry-run after live still writes nothing --
        meta_set(dry_run=True)
        before = meta_rows()
        r = meta_sync()
        check("meta-ads: dry-run after live writes nothing",
              r.status_code == 200 and (r.json() or {}).get("status") == "dry_run"
              and meta_rows() == before, r.text[:150])
        meta_set(dry_run=False)

        # -- 500 / 429 are retried and leave the endpoint healthy --
        meta_set(dry_run=False, ad_account_ids=[f"fail500-{meta_pid}"])
        r = meta_sync()
        j500 = r.json() if r.status_code == 200 else {}
        check("meta-ads: Graph 500 returns a status (not an exception)",
              r.status_code == 200 and j500.get("status") == "ok"
              and j500.get("errors"), r.text[:200])
        check("meta-ads: Graph 500 is retried (bounded)",
              int(j500.get("attempts") or 0) >= 3, str(j500.get("attempts")))
        meta_set(dry_run=False, ad_account_ids=[f"fail429-{meta_pid}"])
        r = meta_sync()
        j429 = r.json() if r.status_code == 200 else {}
        check("meta-ads: Graph 429 is retried and the endpoint stays healthy",
              r.status_code == 200 and j429.get("errors")
              and int(j429.get("attempts") or 0) >= 3, r.text[:200])

        # -- status endpoint shape (after restoring the good account) --
        meta_set(dry_run=False)
        meta_sync()
        r = s.get(f"{api}/meta-ads/status")
        st = r.json() if r.status_code == 200 else {}
        check("meta-ads: status endpoint shape",
              r.status_code == 200
              and st.get("enabled") is True and st.get("dry_run") is False
              and st.get("cadence") == "hourly" and st.get("api_version") == "v21.0"
              and st.get("last_sync_at") and isinstance(st.get("last_result"), dict)
              and st.get("matched") == 4 and st.get("unmatched") == 1
              and "last_error" in st, str(st)[:250])

        # -- token masked for a settings-reading non-admin, nulled on export --
        r = s.get(f"{api}/settings/")
        admin_token = ((r.json().get("settings") or {}).get("meta_ads") or {}).get("access_token")
        check("meta-ads: admin sees the real token", admin_token == meta_token,
              str(admin_token)[:40])
        r = s.get(f"{api}/settings/export")
        export_token = (((r.json().get("data") or {}).get("settings") or {})
                        .get("meta_ads") or {}).get("access_token")
        check("meta-ads: settings export nulls the token", export_token is None,
              str(export_token)[:40])

        meta_user = f"smoke-meta-user-{meta_pid}"
        r = s.post(f"{api}/users/", json={
            "username": meta_user, "password": "smokepass1",
            "permissions": {"sections": {"settings": True}, "write": False}})
        meta_uid = (r.json() or {}).get("id")
        meta_user_sess = requests.Session()
        meta_user_sess.verify = not INSECURE
        meta_user_sess.post(f"{api}/login", json={"username": meta_user, "password": "smokepass1"})
        r = meta_user_sess.get(f"{api}/settings/")
        masked = ((r.json().get("settings") or {}).get("meta_ads") or {}).get("access_token")
        check("meta-ads: non-admin GET masks the token",
              meta_token not in r.text and bool(masked) and "\u2022" in masked,
              str(masked)[:40])

        # -- served pages carry the new UI markers --
        rp = s.get(f"{BASE}/backend/settings")
        check("meta-ads: Settings page serves the cost-sync card",
              rp.status_code == 200 and "Meta Ads cost sync" in rp.text
              and "syncMetaAdsNow" in rp.text, f"{rp.status_code}")
        rc = s.get(f"{BASE}/backend/campaigns")
        check("meta-ads: campaign editor serves the ad-platform campaign id field",
              rc.status_code == 200 and "Ad-platform campaign ID" in rc.text
              and "ad_platform_campaign_id" in rc.text, f"{rc.status_code}")
    finally:
        # -- restore settings + tear down everything this block created --
        if meta_saved is None:
            s.post(f"{api}/settings/", json={"settings": {"meta_ads": None}})
        else:
            s.post(f"{api}/settings/", json={"settings": {"meta_ads": meta_saved}})
        for _mc in (meta_id_cid, meta_name_cid, meta_track_cid, meta_zero_cid):
            if _mc:
                s.delete(f"{api}/campaigns/{_mc}")
        pg_exec(f"DELETE FROM ad_cost_daily WHERE ad_account_id LIKE '%-{meta_pid}'")
        if meta_uid:
            s.delete(f"{api}/users/{meta_uid}")
        try:
            meta_srv.shutdown()
            meta_srv.server_close()
        except Exception:
            pass

    # ===== Meta ad-platform controls: pause/resume campaign + ad sets =====
    # Reuses the _MetaGraph receiver above (extended with do_POST + /adsets).
    # The cost-sync block shut its server down and restored settings, so this
    # block binds a fresh instance on its own port and re-configures meta_ads.
    meta_captured.clear()
    ctl_port = 23000 + (meta_pid % 1000)
    ctl_base = f"http://host.docker.internal:{ctl_port}"
    _meta_socketserver.TCPServer.allow_reuse_address = True
    ctl_srv = None
    for _attempt in range(8):
        try:
            ctl_srv = _meta_socketserver.TCPServer(("0.0.0.0", ctl_port), _MetaGraph)
            ctl_base = f"http://host.docker.internal:{ctl_port}"
            break
        except OSError:
            ctl_port += 1
    if ctl_srv is not None:
        ctl_srv.daemon_threads = True
        _meta_threading.Thread(target=ctl_srv.serve_forever, daemon=True).start()

    def ctl_set(**over):
        cfg = {"enabled": True, "ad_account_ids": [meta_acct], "access_token": meta_token,
               "api_version": "v21.0", "dry_run": True, "cadence": "hourly",
               "backfill_days": 7, "graph_base_url": ctl_base, "match_preference": "auto"}
        cfg.update(over)
        return s.post(f"{api}/settings/", json={"settings": {"meta_ads": cfg}})

    ctl_cid = ctl_unmapped_cid = ctl_fail_cid = None
    ctl_texts = []
    try:
        r = meta_mk(f"MV Control {meta_pid}", f"smoke-meta-ctl-{meta_pid}",
                    f"mvctl-{meta_pid}")
        ctl_cid = (r.json() or {}).get("id")
        r = meta_mk(f"MV Unmapped {meta_pid}", f"smoke-meta-unmapped-{meta_pid}")
        ctl_unmapped_cid = (r.json() or {}).get("id")
        r = meta_mk(f"MV Fail {meta_pid}", f"smoke-meta-fail-{meta_pid}",
                    f"fail500camp-{meta_pid}")
        ctl_fail_cid = (r.json() or {}).get("id")
        check("meta-controls: control test campaigns created",
              all([ctl_cid, ctl_unmapped_cid, ctl_fail_cid]),
              f"{ctl_cid},{ctl_unmapped_cid},{ctl_fail_cid}")

        # -- listing ad sets (a read: runs in dry-run too) --
        ctl_set(dry_run=True)
        meta_captured.clear()
        r = s.get(f"{api}/meta-ads/campaigns/{ctl_cid}/adsets")
        ctl_texts.append(r.text)
        jl = r.json() if r.status_code == 200 else {}
        ads = {a.get("id"): a for a in (jl.get("adsets") or [])}
        check("meta-controls: ad-set listing returns the platform ad sets",
              r.status_code == 200 and len(ads) == 2
              and ads.get(f"as1-{meta_pid}", {}).get("status") == "ACTIVE"
              and ads.get(f"as2-{meta_pid}", {}).get("paused") is True, r.text[:220])
        check("meta-controls: listing surfaces the parent campaign status when returned",
              jl.get("campaign_status") == "ACTIVE", str(jl.get("campaign_status")))
        getcap = next((c for c in meta_captured
                       if c.get("method") == "GET" and c.get("path_only", "").endswith("/adsets")),
                      None)
        check("meta-controls: ad-set listing request shape",
              bool(getcap) and getcap["path_only"] == f"/v21.0/mvctl-{meta_pid}/adsets"
              and getcap["params"].get("fields") == "id,name,status"
              and getcap["params"].get("limit") == "100"
              and getcap["params"].get("access_token") == meta_token, str(getcap)[:220])
        check("meta-controls: dry-run listing performs no write",
              not [c for c in meta_captured if c.get("method") == "POST"],
              str([c.get("method") for c in meta_captured]))

        # -- dry-run pause: intended change echoed, zero HTTP writes --
        meta_captured.clear()
        r = s.post(f"{api}/meta-ads/campaigns/{ctl_cid}/status",
                   json={"status": "PAUSED"})
        ctl_texts.append(r.text)
        jd = r.json() if r.status_code == 200 else {}
        check("meta-controls: dry-run pause returns dry_run + intended change",
              r.status_code == 200 and jd.get("dry_run") is True and jd.get("ok") is True
              and (jd.get("campaign") or {}).get("status") == "PAUSED"
              and ((jd.get("campaign") or {}).get("would_send") or {})
                  .get("body", {}).get("status") == "PAUSED", r.text[:250])
        check("meta-controls: dry-run pause performs no HTTP write at all",
              not [c for c in meta_captured if c.get("method") == "POST"],
              str([c.get("method") for c in meta_captured]))

        # -- live pause + resume of the campaign --
        ctl_set(dry_run=False)
        meta_captured.clear()
        r = s.post(f"{api}/meta-ads/campaigns/{ctl_cid}/status",
                   json={"status": "PAUSED"})
        ctl_texts.append(r.text)
        jlive = r.json() if r.status_code == 200 else {}
        check("meta-controls: live campaign pause returns a non-dry result",
              r.status_code == 200 and jlive.get("dry_run") is False
              and jlive.get("ok") is True, r.text[:220])
        pc = next((c for c in meta_captured
                   if c.get("method") == "POST"
                   and c.get("path_only") == f"/v21.0/mvctl-{meta_pid}"), None)
        check("meta-controls: campaign pause sends the correct method/URL/body",
              bool(pc) and pc["body"].get("status") == "PAUSED"
              and pc["body"].get("access_token") == meta_token, str(pc)[:200])

        meta_captured.clear()
        r = s.post(f"{api}/meta-ads/campaigns/{ctl_cid}/status",
                   json={"status": "ACTIVE"})
        ctl_texts.append(r.text)
        pc2 = next((c for c in meta_captured
                    if c.get("method") == "POST"
                    and c.get("path_only") == f"/v21.0/mvctl-{meta_pid}"), None)
        check("meta-controls: campaign resume sends status=ACTIVE",
              bool(pc2) and pc2["body"].get("status") == "ACTIVE", str(pc2)[:200])

        # -- one ad set --
        meta_captured.clear()
        r = s.post(f"{api}/meta-ads/adsets/as1-{meta_pid}/status",
                   json={"status": "PAUSED"})
        ctl_texts.append(r.text)
        ac = next((c for c in meta_captured
                   if c.get("method") == "POST"
                   and c.get("path_only") == f"/v21.0/as1-{meta_pid}"), None)
        check("meta-controls: ad-set pause sends the correct method/URL/body",
              r.status_code == 200 and (r.json() or {}).get("ok") is True
              and bool(ac) and ac["body"].get("status") == "PAUSED", str(ac)[:200])

        # -- bulk: campaign + its active ad sets in one request --
        meta_captured.clear()
        r = s.post(f"{api}/meta-ads/campaigns/{ctl_cid}/status",
                   json={"status": "PAUSED", "include_adsets": True})
        ctl_texts.append(r.text)
        jb = r.json() if r.status_code == 200 else {}
        posts = [c for c in meta_captured if c.get("method") == "POST"]
        check("meta-controls: bulk pause posts the campaign and its active ad set",
              r.status_code == 200 and jb.get("ok") is True
              and any(c["path_only"] == f"/v21.0/mvctl-{meta_pid}" for c in posts)
              and any(c["path_only"] == f"/v21.0/as1-{meta_pid}" for c in posts),
              str([c["path_only"] for c in posts])[:220])
        check("meta-controls: bulk pause skips the already-paused ad set",
              not any(c["path_only"] == f"/v21.0/as2-{meta_pid}" for c in posts),
              str([c["path_only"] for c in posts])[:200])

        # -- a Graph 5xx on a write is retried and surfaced, never raised --
        meta_captured.clear()
        r = s.post(f"{api}/meta-ads/campaigns/{ctl_fail_cid}/status",
                   json={"status": "PAUSED"})
        ctl_texts.append(r.text)
        jf = r.json() if r.status_code == 200 else {}
        check("meta-controls: Graph 5xx on a write returns a clean error result",
              r.status_code == 200 and jf.get("ok") is False and jf.get("error"),
              r.text[:220])
        check("meta-controls: Graph 5xx on a write is retried (bounded)",
              len([c for c in meta_captured if c.get("method") == "POST"]) >= 3,
              str(len([c for c in meta_captured if c.get("method") == "POST"])))

        # -- no platform mapping -> clear rejection, never a guessed campaign --
        r = s.post(f"{api}/meta-ads/campaigns/{ctl_unmapped_cid}/status",
                   json={"status": "PAUSED"})
        ctl_texts.append(r.text)
        ju = r.json() if r.status_code else {}
        check("meta-controls: campaign with no platform mapping is rejected clearly",
              r.status_code == 400 and "platform" in (ju.get("detail") or "").lower(),
              f"{r.status_code} {r.text[:200]}")
        r = s.get(f"{api}/meta-ads/campaigns/{ctl_unmapped_cid}/adsets")
        ctl_texts.append(r.text)
        check("meta-controls: unmapped campaign ad-set listing rejected too",
              r.status_code == 400
              and "platform" in ((r.json() or {}).get("detail") or "").lower(),
              f"{r.status_code} {r.text[:160]}")

        # -- token never leaks into a control response --
        check("meta-controls: token never appears in a control response",
              all(meta_token not in t for t in ctl_texts),
              str([t[:60] for t in ctl_texts if meta_token in t]))

        # -- the status endpoint reflects the last control action --
        st = s.get(f"{api}/meta-ads/status").json()
        lc = st.get("last_control") or {}
        check("meta-controls: status endpoint reflects the last control action",
              bool(lc.get("action")) and "dry_run" in lc
              and lc.get("platform_campaign_id") is not None, str(lc)[:220])

        # -- served pages carry the new controls + markers --
        rc = s.get(f"{BASE}/backend/campaigns")
        check("meta-controls: campaign editor serves the Ad platform controls",
              rc.status_code == 200 and "meta-ad-platform-section" in rc.text
              and "controlPlatformCampaign" in rc.text
              and "meta-ad-control-dryrun" in rc.text, f"{rc.status_code}")
        check("meta-controls: campaign list row serves the pause/resume action",
              rc.status_code == 200 and "rowPlatformToggle" in rc.text
              and "platformStatusOf" in rc.text, f"{rc.status_code}")
    finally:
        if meta_saved is None:
            s.post(f"{api}/settings/", json={"settings": {"meta_ads": None}})
        else:
            s.post(f"{api}/settings/", json={"settings": {"meta_ads": meta_saved}})
        for _cc in (ctl_cid, ctl_unmapped_cid, ctl_fail_cid):
            if _cc:
                s.delete(f"{api}/campaigns/{_cc}")
        if ctl_srv is not None:
            try:
                ctl_srv.shutdown()
                ctl_srv.server_close()
            except Exception:
                pass

    # ===== Integrations: Meta OAuth "Connect" flow (mock provider) =====
    # App credentials come from the ENVIRONMENT (META_APP_ID / META_APP_SECRET /
    # INTEGRATIONS_ENCRYPTION_KEY) — never settings. The mock provider is reached
    # through the test-only graph_base_url / oauth_base_url settings overrides. If
    # the app env vars are absent the flow checks are replaced by the "not
    # configured" degradation checks, so the suite stays green either way.
    import threading as _int_threading
    import http.server as _int_httpserver
    import socketserver as _int_socketserver
    from urllib.parse import (urlparse as _int_urlparse, parse_qs as _int_parse_qs,
                              unquote as _int_unquote)

    int_pid = os.getpid()
    int_port = 24000 + (int_pid % 1000)
    int_base = f"http://host.docker.internal:{int_port}"
    int_short = f"shorttoken-{int_pid}"
    int_long = f"longtoken-{int_pid}"
    int_secret = os.environ.get("META_APP_SECRET", "")
    int_flow = bool(os.environ.get("META_APP_ID") and int_secret)
    int_captured = []
    _int_socketserver.TCPServer.allow_reuse_address = True

    class _IntegrGraph(_int_httpserver.BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = _int_urlparse(self.path)
            params = {k: v[0] for k, v in _int_parse_qs(parsed.query).items()}
            int_captured.append({"method": "GET", "path_only": parsed.path,
                                 "params": params})
            if parsed.path.endswith("/oauth/access_token"):
                if params.get("grant_type") == "fb_exchange_token":
                    if params.get("fb_exchange_token") == f"fail-{int_pid}":
                        self._send(500, {"error": {"message": "upgrade boom"}})
                        return
                    self._send(200, {"access_token": int_long, "token_type": "bearer",
                                     "expires_in": 5184000})
                    return
                if params.get("code") == f"retry500-{int_pid}":
                    self._send(500, {"error": {"message": "exchange boom"}})
                    return
                if params.get("code") == f"badcode-{int_pid}":
                    self._send(400, {"error": {"message": "Invalid verification code",
                                               "code": 100}})
                    return
                self._send(200, {"access_token": int_short, "token_type": "bearer",
                                 "expires_in": 3600})
                return
            if parsed.path.endswith("/me/adaccounts2"):
                self._send(200, {"data": [{"id": f"act_b{int_pid}", "name": "Int Account B",
                                           "account_id": "22", "currency": "EUR",
                                           "account_status": 1}]})
                return
            if parsed.path.endswith("/me/adaccounts"):
                self._send(200, {
                    "data": [{"id": f"act_a{int_pid}", "name": "Int Account A",
                              "account_id": "11", "currency": "USD",
                              "account_status": 1}],
                    "paging": {"next": f"{int_base}/v21.0/me/adaccounts2?after=x-{int_pid}"},
                })
                return
            if parsed.path.endswith("/adspixels"):
                self._send(200, {"data": [{"id": f"px-{int_pid}",
                                           "name": f"Int Pixel {int_pid}"}]})
                return
            if parsed.path.endswith("/campaigns"):
                self._send(200, {"data": [{"id": f"camp-{int_pid}",
                                           "name": f"Int Campaign {int_pid}",
                                           "status": "ACTIVE"}]})
                return
            if parsed.path.endswith("/me"):
                self._send(200, {"id": f"u-{int_pid}", "name": f"Int User {int_pid}"})
                return
            self._send(200, {"data": []})

        def do_DELETE(self):
            parsed = _int_urlparse(self.path)
            int_captured.append({"method": "DELETE", "path_only": parsed.path,
                                 "params": {k: v[0] for k, v in
                                            _int_parse_qs(parsed.query).items()}})
            self._send(200, {"success": True})

        def log_message(self, *args):
            pass

    int_srv = None
    for _attempt in range(8):
        try:
            int_srv = _int_socketserver.TCPServer(("0.0.0.0", int_port), _IntegrGraph)
            int_base = f"http://host.docker.internal:{int_port}"
            break
        except OSError:
            int_port += 1
    if int_srv is not None:
        int_srv.daemon_threads = True
        _int_threading.Thread(target=int_srv.serve_forever, daemon=True).start()

    int_probe = ""
    if int_srv is not None:
        try:
            int_probe = subprocess.run(
                ["docker", "exec", "tracker_backend", "python", "-c",
                 f"import urllib.request;print(urllib.request.urlopen('{int_base}/v21.0/me',timeout=5).read().decode())"],
                capture_output=True, text=True, timeout=15).stdout
        except Exception:
            pass
    check("integrations: mock provider reachable from the backend container",
          "Int User" in int_probe, int_probe[:80])

    def int_set(**over):
        cfg = {"graph_base_url": int_base, "oauth_base_url": int_base}
        cfg.update(over)
        return s.post(f"{api}/settings/", json={"settings": {"integrations": cfg}})

    def int_start(platform="meta"):
        r = s.get(f"{api}/integrations/{platform}/oauth/start", allow_redirects=False)
        loc = r.headers.get("location") or ""
        q = {k: v[0] for k, v in _int_parse_qs(_int_urlparse(loc).query).items()}
        return r, q.get("state"), loc, _int_unquote(q.get("redirect_uri") or "")

    def int_callback(state, code=None, error=None, extra=""):
        url = f"{api}/integrations/meta/oauth/callback?state={state}{extra}"
        if code is not None:
            url += f"&code={code}"
        if error is not None:
            url += f"&error={error}"
        return s.get(url, allow_redirects=False)

    def int_meta():
        j = s.get(f"{api}/integrations").json()
        return (j.get("platforms") or {}).get("meta") or {}

    int_saved = ((s.get(f"{api}/settings/").json().get("settings") or {})
                 .get("integrations"))
    int_texts = []
    try:
        s.delete(f"{api}/integrations/meta/connection")
        r = int_set()
        check("integrations: endpoint host overrides saved", r.status_code == 200, r.text[:120])

        # -- listing: no credentials returned; callback URL derived + https --
        r = s.get(f"{api}/integrations")
        int_texts.append(r.text)
        jl = r.json() if r.status_code == 200 else {}
        meta = (jl.get("platforms") or {}).get("meta") or {}
        # The advertised callback URL depends on the deployment (PUBLIC_BASE_URL or the
        # request host), so assert the shape and then require the flow to use exactly the
        # advertised URI — that consistency is the thing that breaks in production.
        _cb_suffix = "/backend/api/integrations/meta/callback"
        check("integrations: GET lists every platform with a callback URL",
              r.status_code == 200 and len(jl.get("platforms") or {}) == 5,
              str(list((jl.get("platforms") or {}).keys())))
        _advertised_cb = meta.get("callback_url") or ""
        check("integrations: callback URL is https with the /backend prefix",
              _advertised_cb.startswith("https://") and _advertised_cb.endswith(_cb_suffix),
              str(_advertised_cb))
        _env_public = (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
        if _env_public:
            check("integrations: callback URL honours PUBLIC_BASE_URL when the suite sets it",
                  _advertised_cb == f"{_env_public}{_cb_suffix}", _advertised_cb)
        check("integrations: no client id/secret keys in the response",
              "client_secret" not in r.text and "client_id" not in r.text,
              r.text[:160])
        check("integrations: no app secret appears in the response",
              not int_secret or int_secret not in r.text,
              "secret leaked")
        check("integrations: token encryption key status is reported",
              isinstance(jl.get("encryption_configured"), bool)
              and (not int_flow or jl.get("encryption_configured") is True),
              str(jl.get("encryption_configured")))
        check("integrations: unimplemented platforms are flagged",
              (jl.get("platforms", {}).get("snapchat") or {}).get("implemented") is False,
              str((jl.get("platforms", {}).get("snapchat") or {}).get("implemented")))

        # -- env absent -> "not configured by this deployment", clean degradation --
        unconfigured = [p for p in ("snapchat", "tiktok", "pinterest", "google")
                        if not (jl.get("platforms", {}).get(p) or {}).get("configured")]
        check("integrations: platforms without env vars report configured=false",
              len(unconfigured) == 4, str(unconfigured))
        check("integrations: such platforms disable Connect (connect_available=false)",
              all((jl.get("platforms", {}).get(p) or {}).get("connect_available") is False
                  for p in unconfigured), str(unconfigured))
        rs = s.get(f"{api}/integrations/snapchat/oauth/start", allow_redirects=False)
        check("integrations: Connect for an unconfigured platform degrades cleanly (no 5xx)",
              rs.status_code < 500 and rs.status_code in (400, 302, 307), str(rs.status_code))
        ra = s.get(f"{api}/integrations/snapchat/assets")
        check("integrations: assets for an unconfigured platform degrade cleanly",
              ra.status_code == 200 and ra.json().get("connected") is False,
              f"{ra.status_code} {ra.text[:120]}")

        # -- PUBLIC_BASE_URL derivation: set vs request-host fallback (in-process) --
        snippet = (
            "from app_pages.integrations import derive_callback_url\n"
            "from starlette.requests import Request\n"
            "scope={'type':'http','method':'GET','path':'/x','root_path':'/backend',"
            "'scheme':'http','headers':[],'query_string':b'',"
            "'server':('localhost',80),'client':('127.0.0.1',1)}\n"
            "print(derive_callback_url(Request(scope),'meta'))")

        def _run_in_backend(code, env=None):
            cmd = ["docker", "exec"]
            for k, v in (env or {}).items():
                cmd += ["-e", f"{k}={v}"]
            cmd += ["tracker_backend", "python", "-c", code]
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=20).stdout.strip()

        fb = _run_in_backend(snippet, {"PUBLIC_BASE_URL": ""})
        pb = _run_in_backend(snippet, {"PUBLIC_BASE_URL": "https://aaatracker.website"})
        check("integrations: callback URL falls back to the request host",
              fb == "http://localhost/backend/api/integrations/meta/callback", fb[:120])
        check("integrations: PUBLIC_BASE_URL drives the callback URL when set",
              pb == "https://aaatracker.website/backend/api/integrations/meta/callback",
              pb[:140])

        # -- encryption key absent -> refuse to store plaintext (in-process) --
        refuse = _run_in_backend(
            "import os; os.environ.pop('INTEGRATIONS_ENCRYPTION_KEY', None)\n"
            "import app_pages.integrations as i\n"
            "print(i.encryption_configured(), i.encrypt_token('plain'))")
        check("integrations: with no encryption key, storing is refused (no plaintext)",
              refuse.endswith("None") and refuse.startswith("False"), refuse[:80])

        if not int_flow:
            # Deployment has no Meta app env vars: assert the degradation only.
            check("integrations: meta without env vars reports configured=false",
                  meta.get("configured") is False
                  and meta.get("connect_available") is False, str(meta.get("configured")))
            r = s.get(f"{api}/integrations/meta/oauth/start", allow_redirects=False)
            check("integrations: meta Connect without env vars degrades cleanly",
                  r.status_code in (400, 302, 307) and r.status_code < 500,
                  str(r.status_code))
        else:
            check("integrations: meta env vars are configured (flow enabled)",
                  meta.get("configured") is True and meta.get("connect_available") is True,
                  str(meta.get("configured")))

            # -- oauth start: state issued + consent URL carries the params --
            rs, st1, loc, redirect_uri = int_start()
            int_texts.append(loc)
            loc_q = {k: v[0] for k, v in _int_parse_qs(_int_urlparse(loc).query).items()}
            check("integrations: start 302s to the provider consent URL",
                  rs.status_code in (302, 307) and loc.startswith(int_base)
                  and _int_urlparse(loc).path == "/v21.0/dialog/oauth",
                  f"{rs.status_code} {loc[:100]}")
            check("integrations: consent URL carries client_id + scope + response_type",
                  bool(loc_q.get("client_id")) and loc_q.get("scope") == "ads_read,ads_management"
                  and loc_q.get("response_type") == "code", str(loc_q)[:200])
            check("integrations: consent URL carries the advertised redirect_uri + state",
                  redirect_uri == _advertised_cb and bool(st1),
                  f"{redirect_uri} vs {_advertised_cb} state={bool(st1)}")
            check("integrations: start stored a single-use state row",
                  pg_query(f"SELECT count(*) FROM oauth_states WHERE state = '{st1}'") == "1",
                  pg_query(f"SELECT count(*) FROM oauth_states WHERE state = '{st1}'"))

            # -- unknown / replayed / expired states rejected, nothing stored --
            r = s.get(f"{api}/integrations/meta/oauth/callback?code=x&state=nope-"
                      + str(int_pid), allow_redirects=False)
            check("integrations: unknown state is rejected with a readable error",
                  r.status_code in (302, 307) and "status=error" in (r.headers.get("location") or "")
                  and "invalid" in _int_unquote(r.headers.get("location") or "").lower(),
                  str(r.headers.get("location"))[:160])

            r = int_callback(st1, code=f"badcode-{int_pid}")
            loc_err = _int_unquote(r.headers.get("location") or "")
            int_texts.append(loc_err)
            check("integrations: a failed exchange is surfaced, not stored",
                  r.status_code in (302, 307) and "status=error" in loc_err
                  and "exchange" in loc_err.lower(), loc_err[:200])
            r = int_callback(st1, code=f"badcode-{int_pid}")
            check("integrations: replayed state is rejected (single-use)",
                  r.status_code in (302, 307) and "status=error" in (r.headers.get("location") or ""),
                  str(r.headers.get("location"))[:160])
            check("integrations: replay stored no connection",
                  int_meta().get("connected") is False, str(int_meta().get("connected")))

            _, st2, _, _ = int_start()
            pg_exec(f"UPDATE oauth_states SET created_at = now() - interval '11 minutes' "
                    f"WHERE state = '{st2}'")
            r = int_callback(st2, code="good")
            check("integrations: expired state is rejected",
                  r.status_code in (302, 307)
                  and "expired" in _int_unquote(r.headers.get("location") or "").lower(),
                  str(r.headers.get("location"))[:180])

            # -- provider error=access_denied is readable and stores nothing --
            _, st3, _, _ = int_start()
            r = int_callback(st3, error="access_denied",
                             extra=f"&error_description=The+user+denied+the+request-{int_pid}")
            loc_deny = _int_unquote(r.headers.get("location") or "")
            int_texts.append(loc_deny)
            check("integrations: access_denied produces a readable error, no connection",
                  r.status_code in (302, 307) and "status=error" in loc_deny
                  and int_meta().get("connected") is False, loc_deny[:200])

            # -- token-exchange 5xx retried (bounded) and surfaced cleanly --
            _, st4, _, _ = int_start()
            int_captured.clear()
            r = int_callback(st4, code=f"retry500-{int_pid}")
            loc_500 = _int_unquote(r.headers.get("location") or "")
            exch = [c for c in int_captured
                    if c["path_only"].endswith("/oauth/access_token")
                    and c["params"].get("code") == f"retry500-{int_pid}"]
            check("integrations: token-exchange 5xx is retried (bounded)",
                  len(exch) >= 3, str(len(exch)))
            check("integrations: token-exchange 5xx is surfaced cleanly",
                  r.status_code in (302, 307) and "status=error" in loc_500
                  and int_meta().get("connected") is False, loc_500[:200])

            # -- happy path: exchange -> long-lived upgrade -> store -> assets --
            _, st5, _, _ = int_start()
            int_captured.clear()
            r = int_callback(st5, code="goodcode")
            loc_ok = _int_unquote(r.headers.get("location") or "")
            check("integrations: callback 302s back to the Integrations page (not JSON)",
                  r.status_code in (302, 307) and loc_ok.startswith("/backend/integrations")
                  and "status=ok" in loc_ok, f"{r.status_code} {loc_ok[:120]}")
            check("integrations: callback reports the connection + asset counts",
                  "Connected" in loc_ok and "accounts=" in loc_ok, loc_ok[:200])

            exch = next((c for c in int_captured
                         if c["path_only"].endswith("/oauth/access_token")
                         and c["params"].get("code") == "goodcode"), None)
            check("integrations: code exchange uses the identical redirect_uri + env secret",
                  bool(exch) and exch["params"].get("redirect_uri") == _advertised_cb
                  and exch["params"].get("client_secret") == int_secret,
                  str(exch and exch["params"])[:200])
            upg = next((c for c in int_captured
                        if c["path_only"].endswith("/oauth/access_token")
                        and c["params"].get("grant_type") == "fb_exchange_token"), None)
            check("integrations: short-lived token is upgraded to a long-lived one",
                  bool(upg) and upg["params"].get("fb_exchange_token") == int_short,
                  str(upg and upg["params"].get("grant_type")))
            check("integrations: paging.next is followed verbatim",
                  any(c["path_only"].endswith("/adaccounts2") for c in int_captured),
                  str([c["path_only"] for c in int_captured])[:200])

            r = s.get(f"{api}/integrations")
            int_texts.append(r.text)
            meta = (r.json().get("platforms") or {}).get("meta") or {}
            conn = meta.get("connection") or {}
            check("integrations: GET reports the connection as connected",
                  meta.get("connected") is True and conn.get("has_token") is True
                  and conn.get("readable") is True, str(meta.get("connected")))
            check("integrations: long-lived token masked, not the short one",
                  conn.get("access_token") and "\u2022" in conn.get("access_token", ""),
                  str(conn.get("access_token"))[:40])
            check("integrations: 'Connected as <label>' comes from the token owner",
                  conn.get("account_label") == f"Int User {int_pid}", str(conn.get("account_label")))
            check("integrations: scopes + expiry are surfaced",
                  "ads_read" in (conn.get("scopes") or [])
                  and bool(conn.get("expires_at")) and conn.get("expired") is False,
                  str(conn)[:200])

            r = s.get(f"{api}/integrations/meta/assets")
            int_texts.append(r.text)
            ja = r.json() if r.status_code == 200 else {}
            acct_ids = {a.get("id") for a in (ja.get("ad_accounts") or [])}
            check("integrations: assets return the mock's ad accounts (paging followed)",
                  r.status_code == 200 and acct_ids == {f"act_a{int_pid}", f"act_b{int_pid}"},
                  str(acct_ids))
            check("integrations: assets return datasets + campaigns for the pickers",
                  bool(ja.get("datasets")) and bool(ja.get("campaigns"))
                  and ja.get("counts", {}).get("ad_accounts") == 2, str(ja.get("counts")))
            acct_cap = next((c for c in int_captured
                             if c["path_only"].endswith("/me/adaccounts")), None)
            check("integrations: asset calls carry the stored long-lived token",
                  bool(acct_cap) and acct_cap["params"].get("access_token") == int_long,
                  str(acct_cap and acct_cap["params"].get("access_token"))[:40])

            # -- token stored as ciphertext, not plaintext --
            raw_stored = pg_query("SELECT access_token FROM integration_connections "
                                  "WHERE platform = 'meta'")
            check("integrations: stored access_token is ciphertext (not the plaintext)",
                  bool(raw_stored) and raw_stored != int_long and int_long not in raw_stored
                  and not raw_stored.startswith("short"), raw_stored[:60])

            # -- the app secret + tokens never appear in a response body --
            check("integrations: app secret never appears in a response body",
                  not int_secret or all(int_secret not in t for t in int_texts),
                  str([t[:60] for t in int_texts if int_secret and int_secret in t]))
            check("integrations: tokens never appear in a response body",
                  all(int_long not in t and int_short not in t for t in int_texts),
                  str([t[:60] for t in int_texts if int_long in t or int_short in t]))

            # -- settings export nulls the connection token --
            r = s.get(f"{api}/settings/export")
            exp_data = r.json().get("data") or {}
            exp_conn = next((c for c in (exp_data.get("integration_connections") or [])
                             if c.get("platform") == "meta"), {})
            check("integrations: settings export nulls the connection access token",
                  bool(exp_conn) and exp_conn.get("access_token") is None
                  and exp_conn.get("account_label"), str(exp_conn)[:160])
            check("integrations: settings export carries no integrations secret block",
                  "client_secret" not in json.dumps(
                      (exp_data.get("settings") or {}).get("integrations") or {}),
                  "secret block present")

            # -- disconnect clears + best-effort revokes --
            r = s.delete(f"{api}/integrations/meta/connection")
            jd = r.json() if r.status_code == 200 else {}
            check("integrations: disconnect clears the stored connection",
                  r.status_code == 200 and jd.get("cleared") is True, r.text[:160])
            check("integrations: disconnect best-effort revokes at the provider",
                  bool([c for c in int_captured if c["method"] == "DELETE"
                        and c["path_only"].endswith("/me/permissions")]),
                  str([c["method"] for c in int_captured])[:120])
            check("integrations: disconnect leaves no stored token",
                  int_meta().get("connected") is False
                  and pg_query("SELECT count(*) FROM integration_connections "
                               "WHERE platform = 'meta'") == "0",
                  pg_query("SELECT count(*) FROM integration_connections WHERE platform = 'meta'"))

        # -- decrypt failure degrades to "needs reconnect" (wrong key) --
        marker = f"secret-unreadable-{int_pid}"
        wrong = subprocess.run(
            ["docker", "exec", "tracker_backend", "python", "-c",
             "from cryptography.fernet import Fernet;"
             "print(Fernet(Fernet.generate_key()).encrypt(b'" + marker + "').decode())"],
            capture_output=True, text=True, timeout=20).stdout.strip()
        if not int_flow:
            pass  # no Meta connection on an unconfigured deployment
        elif wrong:
            pg_exec("INSERT INTO integration_connections (platform, access_token, "
                    "token_type, scopes, account_label) VALUES ('meta', '" + wrong + "', "
                    "'bearer', 'ads_read', 'Wrong Key') "
                    "ON CONFLICT (platform) DO UPDATE SET access_token = EXCLUDED.access_token")
            r = s.get(f"{api}/integrations")
            int_texts.append(r.text)
            conn = ((r.json().get("platforms") or {}).get("meta") or {}).get("connection") or {}
            check("integrations: an unreadable token reports needs-reconnect (no 500)",
                  r.status_code == 200 and conn.get("has_token") is True
                  and conn.get("readable") is False
                  and conn.get("reconnect_needed") is True, str(conn)[:180])
            r = s.get(f"{api}/integrations/meta/assets")
            int_texts.append(r.text)
            check("integrations: assets degrade cleanly when the token can't be decrypted",
                  r.status_code == 200 and r.json().get("connected") is False
                  and r.json().get("needs_reconnect") is True
                  and marker not in r.text, f"{r.status_code} {r.text[:160]}")
            check("integrations: the unreadable ciphertext is never returned",
                  marker not in r.text and wrong not in r.text, "ciphertext leaked")
            s.delete(f"{api}/integrations/meta/connection")
        else:
            check("integrations: wrong-key ciphertext generated for the degrade test",
                  False, "docker exec failed")

        # -- served pages carry the markers (no credential fields) --
        rp = s.get(f"{BASE}/backend/integrations")
        check("integrations: Integrations page serves the Integrations card",
              rp.status_code == 200 and "integrations-card" in rp.text
              and "connectIntegration" in rp.text
              and "Ad accounts for cost sync" in rp.text, f"{rp.status_code}")
        # The callback URL and the deployment's env-var names are operator plumbing,
        # not product surface: the card must not show either.
        check("integrations: the callback URL is not exposed in the UI",
              "copyIntegrationCallback" not in rp.text
              and "integration-callback" not in rp.text, "callback UI still present")
        check("integrations: the card does not name deployment env vars",
              "env_vars" not in rp.text, "env var list still rendered")
        check("integrations: Integrations page has no client id/secret fields",
              "integration-client-secret" not in rp.text
              and "integration-client-id" not in rp.text, "credential field present")
        check("integrations: Integrations page has no unreplaced jinja tags",
              "{%" not in rp.text, "unreplaced jinja tag")
        rc = s.get(f"{BASE}/backend/campaigns")
        check("integrations: campaign editor offers discovered platform campaigns",
              rc.status_code == 200 and "platformCampaignOptions" in rc.text
              and "loadPlatformAssets" in rc.text, f"{rc.status_code}")
    finally:
        restore = {"graph_base_url": "", "oauth_base_url": ""}
        if isinstance(int_saved, dict):
            restore.update(int_saved)
        restore["graph_base_url"] = ""
        restore["oauth_base_url"] = ""
        s.post(f"{api}/settings/", json={"settings": {"integrations": restore}})
        pg_exec("DELETE FROM integration_connections WHERE platform = 'meta'")
        if int_srv is not None:
            try:
                int_srv.shutdown()
                int_srv.server_close()
            except Exception:
                pass

    print("== Multi-tenancy: tenant isolation (phase 1) ==")
    # Acceptance gate for phase 1: a second tenant must be able to reuse the
    # same alias/names, and must be unable to see or touch tenant 1's rows
    # through any list, detail, update or delete endpoint — and vice versa.
    mt_pid = os.getpid()

    def pg_scalar(sql):
        out = subprocess.run(
            ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db", "-tAc", sql],
            capture_output=True, text=True, timeout=30)
        return out.stdout.strip()

    def pg_exec(sql):
        return subprocess.run(
            ["docker", "exec", "tracker_postgres", "psql", "-U", "user", "-d", "db", "-c", sql],
            capture_output=True, text=True, timeout=30).stdout

    def ch_exec(sql):
        out = subprocess.run(
            ["docker", "exec", CH_CONTAINER, "clickhouse-client", "-u", CH_USER,
             "--password", CH_PASS, "-q", sql],
            capture_output=True, text=True, timeout=60)
        return out.returncode, out.stdout.strip(), out.stderr.strip()

    mt_alias = f"mt-alias-{mt_pid}"
    mt_b_name = f"MT Campaign B {mt_pid}"
    mt_offer = f"MT Offer {mt_pid}"
    mt_source = f"MT Source {mt_pid}"
    mt_network = f"MT Network {mt_pid}"
    mt_b_domain = f"mt-b-{mt_pid}.example.com"
    mt_a_domain = f"mt-a-{mt_pid}.example.com"
    mt_b_user = f"mt-b-user-{mt_pid}"
    mt_b_tenant = None
    mt_created = {"campaigns": [], "offers": [], "sources": [],
                  "affiliate_networks": [], "domains": []}
    sb = requests.Session()
    sb.verify = not INSECURE
    # The same-alias case needs a campaign with a live flow on both sides.
    mt_config = {"flows": [{"type": "default", "position": 1, "enabled": True,
                            "schema": "redirect", "weight": 100, "filters": [],
                            "redirect_url": "https://example.com/mt-A"}],
                 "postbacks": [], "fallback_url": "https://example.com/mt-A-fb"}

    r = s.post(f"{api}/tenants/", json={"name": f"MT Tenant {mt_pid}",
                                        "slug": f"mt-{mt_pid}",
                                        "username": mt_b_user, "password": "smokepass1",
                                        "email": f"mt-{mt_pid}@example.com",
                                        "role": "owner"})
    check("isolation: tenant B provisioned", r.status_code == 200 and r.json().get("tenant_id"),
          r.text[:200])
    mt_b_tenant = (r.json() or {}).get("tenant_id")

    # Phase 1 keeps `is_admin` install-global (per-tenant users/roles are phase
    # 2), so B's user needs the admin flag to reach the admin-only sections.
    r = s.get(f"{api}/users/")
    mt_b_uid = next((u["id"] for u in (r.json() or []) if u["username"] == mt_b_user), None)
    check("isolation: tenant B user is listed (users stay install-global in phase 1)",
          mt_b_uid is not None, r.text[:150])
    if mt_b_uid:
        r = s.patch(f"{api}/users/{mt_b_uid}",
                    json={"username": mt_b_user, "is_admin": True})
        check("isolation: tenant B user promoted (global admin flag, phase 1)",
              r.status_code == 200, r.text[:150])

    r = sb.post(f"{api}/login", json={"username": mt_b_user, "password": "smokepass1"})
    check("isolation: tenant B login", r.status_code == 200, r.text[:150])

    r = sb.get(f"{api}/tenants/")
    mt_b_list = r.json() if r.status_code == 200 else {}
    check("isolation: B's session resolves to tenant B only",
          mt_b_list.get("current_tenant_id") == mt_b_tenant
          and [t["tenant_id"] for t in mt_b_list.get("tenants", [])] == [mt_b_tenant],
          r.text[:200])
    check("isolation: B cannot switch into tenant 1 (403)",
          sb.post(f"{api}/tenants/switch", json={"tenant_id": 1}).status_code == 403,
          "switch into a non-member tenant must be refused")

    # ---- tenant A (tenant 1) entities ----
    r = s.post(f"{api}/campaigns/", json={
        "name": f"MT Campaign A {mt_pid}", "alias": mt_alias, "type": "campaign",
        "status": "active", "redirect_mode": "position", "config": mt_config})
    mt_a_campaign = (r.json() or {}).get("id")
    check("isolation: A campaign created", r.status_code == 200 and mt_a_campaign, r.text[:200])
    mt_created["campaigns"].append(mt_a_campaign)

    r = s.post(f"{api}/offers/", json={"name": mt_offer, "url": "https://example.com/mt-a"})
    mt_a_offer = (r.json() or {}).get("id")
    check("isolation: A offer created", r.status_code == 200 and mt_a_offer, r.text[:200])
    mt_created["offers"].append(mt_a_offer)

    r = s.post(f"{api}/sources/", json={"name": mt_source, "traffic_loss": 0})
    mt_a_source = (r.json() or {}).get("id")
    check("isolation: A source created", r.status_code == 200 and mt_a_source, r.text[:200])
    mt_created["sources"].append(mt_a_source)

    r = s.post(f"{api}/affiliate-networks/", json={"name": mt_network})
    mt_a_network = (r.json() or {}).get("id")
    check("isolation: A network created", r.status_code == 200 and mt_a_network, r.text[:200])
    mt_created["affiliate_networks"].append(mt_a_network)

    r = s.post(f"{api}/domains/", json={"domain": mt_a_domain})
    mt_a_domain_id = (r.json() or {}).get("id")
    check("isolation: A domain created", r.status_code == 200 and mt_a_domain_id, r.text[:200])
    mt_created["domains"].append(mt_a_domain_id)

    # ---- tenant B: the SAME alias / names (per-tenant uniques) ----
    r = sb.post(f"{api}/campaigns/", json={
        "name": mt_b_name, "alias": mt_alias, "type": "campaign",
        "status": "active", "redirect_mode": "position",
        "domain_id": None,
        "config": dict(mt_config, flows=[dict(mt_config["flows"][0],
                                              redirect_url="https://example.com/mt-B")])})
    mt_b_campaign = (r.json() or {}).get("id")
    check("isolation: B reuses A's campaign alias (per-tenant unique)",
          r.status_code == 200 and mt_b_campaign, r.text[:200])
    mt_created["campaigns"].append(mt_b_campaign)

    r = sb.post(f"{api}/offers/", json={"name": mt_offer, "url": "https://example.com/mt-b"})
    mt_b_offer = (r.json() or {}).get("id")
    check("isolation: B reuses A's offer name", r.status_code == 200 and mt_b_offer, r.text[:200])
    mt_created["offers"].append(mt_b_offer)

    r = sb.post(f"{api}/sources/", json={"name": mt_source, "traffic_loss": 0})
    mt_b_source = (r.json() or {}).get("id")
    check("isolation: B reuses A's source name", r.status_code == 200 and mt_b_source, r.text[:200])
    mt_created["sources"].append(mt_b_source)

    r = sb.post(f"{api}/affiliate-networks/", json={"name": mt_network})
    mt_b_network = (r.json() or {}).get("id")
    check("isolation: B reuses A's network name", r.status_code == 200 and mt_b_network, r.text[:200])
    mt_created["affiliate_networks"].append(mt_b_network)

    r = sb.post(f"{api}/domains/", json={"domain": mt_b_domain})
    mt_b_domain_id = (r.json() or {}).get("id")
    check("isolation: B domain created", r.status_code == 200 and mt_b_domain_id, r.text[:200])
    mt_created["domains"].append(mt_b_domain_id)

    # ---- lists never cross tenants ----
    def ids_of(session, path, key="id"):
        rr = session.get(f"{api}/{path}")
        return {row.get(key) for row in (rr.json() if rr.status_code == 200 else [])}

    for path, a_id, b_id, label in [
        ("campaigns/", mt_a_campaign, mt_b_campaign, "campaigns"),
        ("offers/", mt_a_offer, mt_b_offer, "offers"),
        ("sources/", mt_a_source, mt_b_source, "sources"),
        ("affiliate-networks/", mt_a_network, mt_b_network, "networks"),
    ]:
        a_ids = ids_of(s, path)
        b_ids = ids_of(sb, path)
        check(f"isolation: A's {label} list excludes B's row", b_id not in a_ids, str(sorted(a_ids))[:120])
        check(f"isolation: B's {label} list excludes A's row", a_id not in b_ids, str(sorted(b_ids))[:120])

    r = s.get(f"{api}/domains/")
    a_domains = {d.get("domain") for d in (r.json() if r.status_code == 200 else [])}
    r = sb.get(f"{api}/domains/")
    b_domains = {d.get("domain") for d in (r.json() if r.status_code == 200 else [])}
    check("isolation: A's domain list excludes B's domain", mt_b_domain not in a_domains, str(a_domains)[:120])
    check("isolation: B's domain list excludes A's domain", mt_a_domain not in b_domains, str(b_domains)[:120])

    # ---- detail / update / delete of the other tenant's rows ----
    def cross_tenant_blocked(session, label, method, path, json_body=None):
        fn = getattr(session, method)
        rr = fn(f"{api}/{path}", json=json_body) if json_body is not None else fn(f"{api}/{path}")
        check(f"isolation: {label}", rr.status_code in (403, 404),
              f"got {rr.status_code} {rr.text[:120]}")

    # Detail reads that exist for a campaign id (there is no plain
    # GET /campaigns/{id}); both must 404 across tenants.
    cross_tenant_blocked(sb, "B cannot read A's campaign detail", "get",
                         f"optimizer/{mt_a_campaign}")
    cross_tenant_blocked(sb, "B cannot read A's campaign funnel report", "get",
                         f"reports/funnel/{mt_a_campaign}")
    cross_tenant_blocked(s, "A cannot read B's campaign detail", "get",
                         f"optimizer/{mt_b_campaign}")
    cross_tenant_blocked(s, "A cannot read B's campaign funnel report", "get",
                         f"reports/funnel/{mt_b_campaign}")

    # Complete, valid payloads so validation passes and the request genuinely
    # attempts a cross-tenant write.
    mt_campaign_put = {"name": "hijacked", "alias": mt_alias, "type": "campaign",
                       "status": "active", "redirect_mode": "position", "config": mt_config}
    mt_offer_patch = {"name": mt_offer, "url": "https://example.com/hijacked"}
    mt_source_patch = {"name": mt_source, "traffic_loss": 0}
    mt_network_patch = {"name": mt_network}

    cross_tenant_blocked(sb, "B cannot update A's campaign by id", "put",
                         f"campaigns/{mt_a_campaign}", mt_campaign_put)
    cross_tenant_blocked(sb, "B cannot delete A's campaign by id", "delete",
                         f"campaigns/{mt_a_campaign}")
    cross_tenant_blocked(sb, "B cannot update A's offer by id", "patch",
                         f"offers/{mt_a_offer}", mt_offer_patch)
    cross_tenant_blocked(sb, "B cannot delete A's offer by id", "delete", f"offers/{mt_a_offer}")
    cross_tenant_blocked(sb, "B cannot update A's source by id", "patch",
                         f"sources/{mt_a_source}", mt_source_patch)
    cross_tenant_blocked(sb, "B cannot delete A's source by id", "delete", f"sources/{mt_a_source}")
    cross_tenant_blocked(sb, "B cannot update A's domain by id", "put",
                         f"domains/{mt_a_domain_id}",
                         {"domain": mt_a_domain, "group_name": "hijacked"})
    cross_tenant_blocked(sb, "B cannot delete A's domain by id", "delete", f"domains/{mt_a_domain_id}")
    cross_tenant_blocked(sb, "B cannot update A's network by id", "patch",
                         f"affiliate-networks/{mt_a_network}", mt_network_patch)
    cross_tenant_blocked(sb, "B cannot delete A's network by id", "delete",
                         f"affiliate-networks/{mt_a_network}")

    cross_tenant_blocked(s, "A cannot update B's campaign by id", "put",
                         f"campaigns/{mt_b_campaign}", mt_campaign_put)
    cross_tenant_blocked(s, "A cannot delete B's campaign by id", "delete",
                         f"campaigns/{mt_b_campaign}")
    cross_tenant_blocked(s, "A cannot update B's offer by id", "patch",
                         f"offers/{mt_b_offer}", mt_offer_patch)
    cross_tenant_blocked(s, "A cannot delete B's offer by id", "delete", f"offers/{mt_b_offer}")
    cross_tenant_blocked(s, "A cannot update B's source by id", "patch",
                         f"sources/{mt_b_source}", mt_source_patch)
    cross_tenant_blocked(s, "A cannot delete B's source by id", "delete", f"sources/{mt_b_source}")
    cross_tenant_blocked(s, "A cannot update B's network by id", "patch",
                         f"affiliate-networks/{mt_b_network}", mt_network_patch)
    cross_tenant_blocked(s, "A cannot delete B's network by id", "delete",
                         f"affiliate-networks/{mt_b_network}")
    if mt_b_domain_id:
        cross_tenant_blocked(s, "A cannot update B's domain by id", "put",
                             f"domains/{mt_b_domain_id}",
                             {"domain": mt_b_domain, "group_name": "hijacked"})
        cross_tenant_blocked(s, "A cannot delete B's domain by id", "delete",
                             f"domains/{mt_b_domain_id}")

    # The blocked deletes must not have deleted anything.
    check("isolation: A's campaign survives B's delete attempts",
          str(mt_a_campaign) in pg_scalar(
              f"SELECT id FROM campaigns WHERE id = {int(mt_a_campaign)}"))
    check("isolation: B's campaign survives A's delete attempts",
          str(mt_b_campaign) in pg_scalar(
              f"SELECT id FROM campaigns WHERE id = {int(mt_b_campaign)}"))

    # ---- conversions ----
    r = sb.post(f"{api}/reports/conversion", json={
        "click_id": f"mt-b-conv-{mt_pid}", "status": "sale", "payout": 7, "revenue": 9})
    mt_b_conv = (r.json() or {}).get("id")
    check("isolation: B conversion created", r.status_code == 200 and mt_b_conv, r.text[:200])
    r = sb.get(f"{api}/reports/?offset=0&limit=200")
    b_conv_ids = {c.get("id") for c in ((r.json() or {}).get("items") or [])}
    check("isolation: B's conversion log contains B's conversion", mt_b_conv in b_conv_ids,
          f"missing {mt_b_conv}")
    r = s.get(f"{api}/reports/?offset=0&limit=200")
    a_conv = r.json() if r.status_code == 200 else {}
    a_conv_ids = {c.get("id") for c in (a_conv.get("items") or [])}
    check("isolation: A's conversion log excludes B's conversion", mt_b_conv not in a_conv_ids,
          f"leaked {mt_b_conv}")
    r = s.patch(f"{api}/reports/{mt_b_conv}", json={"status": "trash"})
    check("isolation: A cannot update B's conversion", r.status_code in (403, 404),
          f"got {r.status_code}")
    r = s.delete(f"{api}/reports/{mt_b_conv}")
    check("isolation: A cannot delete B's conversion", r.status_code in (403, 404),
          f"got {r.status_code}")
    check("isolation: B's conversion survived A's delete", str(mt_b_conv) in pg_scalar(
        f"SELECT id FROM conversions_data WHERE id = {int(mt_b_conv)}"))

    # ---- tracking plane: Host -> tenant, same alias on both sides ----
    r = requests.get(f"{BASE}/{mt_alias}", verify=not INSECURE, allow_redirects=False)
    check("isolation: unknown host resolves the lowest tenant's alias (tenant 1)",
          r.status_code in (301, 302, 307, 308)
          and "example.com/mt-A" in (r.headers.get("location") or ""),
          f"{r.status_code} {r.headers.get('location', '')}")
    # An explicit click id (the plain redirect records an empty one) so the
    # postback below can reference exactly this click.
    mt_click_id = f"mt-b-click-{mt_pid}"
    r = requests.get(f"{BASE}/{mt_alias}?click_id={mt_click_id}", verify=not INSECURE,
                     allow_redirects=False, headers={"Host": mt_b_domain})
    check("isolation: B's domain host resolves B's campaign, not A's",
          r.status_code in (301, 302, 307, 308)
          and "example.com/mt-B" in (r.headers.get("location") or ""),
          f"{r.status_code} {r.headers.get('location', '')}")
    r = requests.get(f"{BASE}/{mt_alias}", verify=not INSECURE, allow_redirects=False,
                     headers={"Host": mt_a_domain})
    check("isolation: A's domain host resolves A's campaign",
          r.status_code in (301, 302, 307, 308)
          and "example.com/mt-A" in (r.headers.get("location") or ""),
          f"{r.status_code} {r.headers.get('location', '')}")

    # ---- ClickHouse: the click row carries the campaign's tenant ----
    _rc, chips, ch_err = ch_exec(
        f"SELECT count() FROM clicks_data WHERE tenant_id = {int(mt_b_tenant)} "
        f"AND campaign_id = {int(mt_b_campaign)}")
    check("isolation: B's click landed in clicks_data with tenant_id = B",
          chips == "1", f"{chips!r} {ch_err[:120]}")
    _rc, chips, _e = ch_exec(
        f"SELECT count() FROM clicks_data WHERE tenant_id = 1 "
        f"AND campaign_id = {int(mt_b_campaign)}")
    check("isolation: no clicks_data row mislabels B's campaign as tenant 1", chips == "0", chips)

    # A's click log must not contain B's campaign; B's must.
    r = s.post(f"{api}/dashboard/click-log", json={"campaigns": [mt_b_campaign], "limit": 5})
    check("isolation: A's click log excludes B's campaign clicks",
          r.status_code == 200 and r.json() == [], r.text[:150])
    r = sb.post(f"{api}/dashboard/click-log", json={"campaigns": [mt_b_campaign], "limit": 5})
    check("isolation: B's click log includes B's campaign clicks",
          r.status_code == 200 and len(r.json()) >= 1, r.text[:150])

    # ---- postback log ----
    # The click row exists (asserted above); its click_id is the one we sent.
    _rc, mt_click_check, _e = ch_exec(
        f"SELECT click_id FROM clicks_data WHERE tenant_id = {int(mt_b_tenant)} "
        f"AND campaign_id = {int(mt_b_campaign)} ORDER BY received_at DESC LIMIT 1")
    check("isolation: B's click row carries the click id the request sent",
          mt_click_check == mt_click_id, f"{mt_click_check!r}")
    if mt_click_check:
        requests.get(f"{BASE}/pb?clickid={mt_click_id}&status=sale&payout=3",
                     verify=not INSECURE)
        r = sb.get(f"{api}/logs/postbacks?offset=0&limit=200")
        b_log = r.json() if r.status_code == 200 else {}
        check("isolation: B's postback log contains B's click",
              any(mt_click_id in json.dumps(i) for i in (b_log.get("items") or [])),
              r.text[:150])
        r = s.get(f"{api}/logs/postbacks?offset=0&limit=200")
        a_log = r.json() if r.status_code == 200 else {}
        check("isolation: A's postback log excludes B's click",
              not any(mt_click_id in json.dumps(i) for i in (a_log.get("items") or [])),
              "cross-tenant postback log leak")
        r = sb.get(f"{api}/logs/click-forwarding?offset=0&limit=200")
        b_fwd = r.json() if r.status_code == 200 else {}
        r = s.get(f"{api}/logs/click-forwarding?offset=0&limit=200")
        a_fwd = r.json() if r.status_code == 200 else {}
        b_fwd_ids = {i.get("id") for i in (b_fwd.get("items") or [])}
        a_fwd_ids = {i.get("id") for i in (a_fwd.get("items") or [])}
        check("isolation: B's click-forwarding log is non-empty and shares no row id with A's",
              bool(b_fwd_ids) and not (b_fwd_ids & a_fwd_ids),
              f"a={len(a_fwd_ids)} b={len(b_fwd_ids)} shared={len(b_fwd_ids & a_fwd_ids)}")
    else:
        check("isolation: pending-scope: postback + forward log checks (no click id found)",
              False, "no clicks_data row to drive the postback")

    # ---- audit log: B's actions are written into tenant B's trail ----
    check("isolation: B's audit rows carry tenant B",
          pg_scalar(f"SELECT count(*) FROM audit_log WHERE tenant_id = "
                    f"{int(mt_b_tenant)}") != "0", "no tenant-2 audit rows")
    r = s.get(f"{api}/audit/", params={"q": mt_b_name, "page_size": 100})
    check("isolation: A's audit search cannot reach B's entity names",
          r.status_code == 200 and not (r.json() or {}).get("entries"),
          r.text[:150])
    r = sb.get(f"{api}/audit/", params={"q": mt_b_name, "page_size": 100})
    check("isolation: B's audit search finds B's own entity names",
          r.status_code == 200 and bool((r.json() or {}).get("entries")), r.text[:150])

    # ---- self-cleanup: tenant B and everything it owns ----
    for table in ("campaigns", "offers", "sources", "affiliate_networks", "domains",
                  "landings", "capi_pixels", "capi_pixel_bindings",
                  "capi_channel_settings", "capi_pixel_sent", "meta_capi_sent",
                  "meta_capi_log", "ad_cost_daily", "integration_connections",
                  "scripts", "filter_presets", "funnel_templates", "domain_groups",
                  "auto_rules", "monitor_state", "honeypot_hits", "postback_logs",
                  "click_forward_logs", "cost_update_logs", "conversions_data",
                  "audit_log", "settings"):
        pg_exec(f"DELETE FROM {table} WHERE tenant_id = {int(mt_b_tenant)}")
    ch_exec(f"ALTER TABLE clicks_data DELETE WHERE tenant_id = {int(mt_b_tenant)}")
    pg_exec(f"DELETE FROM auth_sessions WHERE username = '{mt_b_user}'")
    pg_exec(f"DELETE FROM users WHERE username = '{mt_b_user}'")
    pg_exec(f"DELETE FROM tenants WHERE id = {int(mt_b_tenant)}")
    # tenant 1's own MT rows
    for table in ("campaigns", "offers", "sources", "affiliate_networks", "domains"):
        pg_exec(f"DELETE FROM {table} WHERE id IN ({', '.join(str(i) for i in mt_created[table] if i)})")
    ch_exec(f"ALTER TABLE clicks_data DELETE WHERE campaign_id IN "
            f"({int(mt_a_campaign)}, {int(mt_b_campaign)})")
    pg_exec("DELETE FROM conversions_data WHERE click_id LIKE 'mt-b-conv-%'")
    check("isolation: tenant B cleaned up",
          pg_scalar(f"SELECT count(*) FROM tenants WHERE id = {int(mt_b_tenant)}") == "0")
    check("isolation: tenant B rows cleaned up",
          pg_scalar(f"SELECT count(*) FROM campaigns WHERE tenant_id = {int(mt_b_tenant)}") == "0")

    # =====================================================================
    print("== Multi-tenancy phase 2A: roles, permissions, members ==")
    # Authority comes from the *membership in the current tenant*, not the
    # users row: a viewer cannot write while the same user as editor can, and
    # the same user can differ per tenant. A tenant's members are managed from
    # inside that tenant; users.is_admin is only the platform-operator flag.
    rp_pid = os.getpid()

    def rp_login(username, password="smokepass1"):
        sess = requests.Session()
        sess.verify = not INSECURE
        rr = sess.post(f"{api}/login", json={"username": username, "password": password})
        check(f"phase2: login {username}", rr.status_code == 200, rr.text[:120])
        return sess

    def rp_campaign_payload(alias, url="https://example.com/rp"):
        return {"name": alias, "alias": alias, "type": "campaign", "status": "active",
                "redirect_mode": "weight",
                "config": {"flows": [{"type": "default", "position": 1, "enabled": True,
                                      "schema": "redirect", "redirect_url": url,
                                      "weight": 100, "filters": []}],
                           "postbacks": [], "fallback_url": url}}

    # --- a workspace T with one member per role ---
    r = s.post(f"{api}/tenants/", json={"name": f"RP Tenant {rp_pid}",
                                        "slug": f"rp-{rp_pid}"})
    rp_t = (r.json() or {}).get("tenant_id")
    check("phase2: workspace T provisioned", r.status_code == 200 and bool(rp_t), r.text[:150])

    rp_owner = f"rp-owner-{rp_pid}"
    rp_admin = f"rp-admin-{rp_pid}"
    rp_editor = f"rp-editor-{rp_pid}"
    rp_viewer = f"rp-viewer-{rp_pid}"
    rp_pa = f"rp-pa-{rp_pid}"           # platform operator, but only a viewer in T
    rp_nobody = f"rp-nobody-{rp_pid}"   # platform operator with no membership
    rp_multi = f"rp-multi-{rp_pid}"     # editor in tenant 1, viewer in T

    for uname, role in ((rp_owner, "owner"), (rp_admin, "admin"),
                        (rp_editor, "editor"), (rp_viewer, "viewer")):
        r = s.post(f"{api}/members/", params={"tenant_id": rp_t},
                   json={"username": uname, "email": f"{uname}@example.com",
                         "password": "smokepass1", "role": role})
        check(f"phase2: new {role} member created", r.status_code == 200, r.text[:200])
    r = s.post(f"{api}/members/", params={"tenant_id": rp_t},
               json={"username": rp_pa, "email": f"{rp_pa}@example.com",
                     "password": "smokepass1", "role": "viewer"})
    check("phase2: platform user seeded as viewer", r.status_code == 200, r.text[:200])
    r = s.post(f"{api}/members/", params={"tenant_id": rp_t},
               json={"username": rp_nobody, "email": f"{rp_nobody}@example.com",
                     "password": "smokepass1", "role": "editor"})
    check("phase2: transient member created", r.status_code == 200, r.text[:200])

    # An EXISTING user joins T by username (the other half of add-member).
    r = s.post(f"{api}/users/", json={"username": rp_multi, "password": "smokepass1",
                                      "email": f"{rp_multi}@example.com"})
    check("phase2: existing user created (editor in tenant 1)", r.status_code == 200, r.text[:200])
    r = s.post(f"{api}/members/", params={"tenant_id": rp_t},
               json={"username": rp_multi, "role": "viewer"})
    check("phase2: existing user joined T as viewer", r.status_code == 200, r.text[:200])

    r = s.get(f"{api}/members/", params={"tenant_id": rp_t})
    rp_members = (r.json() or {}).get("members") or []
    rp_ids = {m["username"]: m["user_id"] for m in rp_members}
    check("phase2: member list carries role + platform flag",
          r.status_code == 200 and len(rp_members) == 7
          and all(k in rp_ids for k in (rp_owner, rp_admin, rp_editor, rp_viewer,
                                        rp_pa, rp_nobody, rp_multi))
          and next(m for m in rp_members if m["username"] == rp_owner)["role"] == "owner",
          r.text[:250])

    # Promote the two platform operators (global flag — not workspace authority).
    for uname in (rp_pa, rp_nobody):
        r = s.patch(f"{api}/users/{rp_ids[uname]}", json={"username": uname, "is_admin": True})
        check(f"phase2: {uname} promoted to platform operator", r.status_code == 200, r.text[:150])

    sm = rp_login(rp_multi)
    se = rp_login(rp_editor)
    sa = rp_login(rp_admin)
    so = rp_login(rp_owner)
    sv = rp_login(rp_viewer)
    spa = rp_login(rp_pa)

    # --- resolution is per tenant, not per user ---
    r = sm.post(f"{api}/campaigns/", json=rp_campaign_payload(f"smoke-rp-t1-{rp_pid}"))
    check("phase2: multi-tenant user writes in tenant 1 (editor)", r.status_code == 200, r.text[:200])
    r = sm.post(f"{api}/tenants/switch", json={"tenant_id": rp_t})
    check("phase2: multi-tenant user switches into T", r.status_code == 200, r.text[:150])
    r = sm.post(f"{api}/campaigns/", json=rp_campaign_payload(f"smoke-rp-t2-{rp_pid}"))
    check("phase2: same user is read-only in T (viewer)", r.status_code == 403, str(r.status_code))

    # --- viewer cannot write, editor can ---
    r = sv.get(f"{api}/campaigns/")
    check("phase2: viewer reads content", r.status_code == 200, r.text[:120])
    r = sv.post(f"{api}/campaigns/", json=rp_campaign_payload(f"smoke-rp-v-{rp_pid}"))
    check("phase2: viewer cannot write (403)", r.status_code == 403, str(r.status_code))
    r = se.post(f"{api}/campaigns/", json=rp_campaign_payload(f"smoke-rp-e-{rp_pid}"))
    check("phase2: editor can write content", r.status_code == 200, r.text[:200])

    # --- an editor cannot manage members; an admin can, within limits ---
    r = se.get(f"{api}/members/")
    check("phase2: editor cannot list members (403)", r.status_code == 403, str(r.status_code))
    r = se.post(f"{api}/members/", json={"username": rp_viewer, "role": "viewer"})
    check("phase2: editor cannot add a member (403)", r.status_code == 403, str(r.status_code))
    r = se.put(f"{api}/members/{rp_ids[rp_viewer]}", json={"role": "admin"})
    check("phase2: editor cannot change a role (403)", r.status_code == 403, str(r.status_code))
    r = se.delete(f"{api}/members/{rp_ids[rp_viewer]}")
    check("phase2: editor cannot remove a member (403)", r.status_code == 403, str(r.status_code))

    r = sa.get(f"{api}/members/")
    check("phase2: admin lists workspace members",
          r.status_code == 200 and len((r.json() or {}).get("members") or []) == 7, r.text[:200])
    r = sa.put(f"{api}/members/{rp_ids[rp_editor]}", json={"role": "viewer"})
    check("phase2: admin changes a member role", r.status_code == 200, r.text[:150])
    r = sa.put(f"{api}/members/{rp_ids[rp_editor]}", json={"role": "editor"})
    check("phase2: admin restores a member role", r.status_code == 200, r.text[:150])
    r = sa.put(f"{api}/members/{rp_ids[rp_viewer]}", json={"role": "owner"})
    check("phase2: admin cannot promote to owner (400)", r.status_code == 400, str(r.status_code))
    r = sa.put(f"{api}/members/{rp_ids[rp_owner]}", json={"role": "admin"})
    check("phase2: admin cannot demote the owner (400)", r.status_code == 400, str(r.status_code))
    r = sa.delete(f"{api}/members/{rp_ids[rp_owner]}")
    check("phase2: admin cannot remove the owner (400)", r.status_code == 400, str(r.status_code))
    r = sa.put(f"{api}/members/{rp_ids[rp_admin]}", json={"role": "editor"})
    check("phase2: admin cannot change their own role (400)", r.status_code == 400, str(r.status_code))

    # --- a non-platform admin cannot reach another workspace's members ---
    r = sa.get(f"{api}/members/", params={"tenant_id": 1})
    check("phase2: admin cannot list another workspace's members",
          r.status_code == 403, str(r.status_code))
    r = sa.post(f"{api}/members/", params={"tenant_id": 1},
                json={"username": rp_viewer, "role": "viewer"})
    check("phase2: admin's ?tenant_id= is refused (403)", r.status_code == 403, str(r.status_code))
    r = sa.put(f"{api}/members/{rp_ids[rp_viewer]}", params={"tenant_id": 1}, json={"role": "admin"})
    check("phase2: admin cannot modify another workspace's member",
          r.status_code == 403, str(r.status_code))
    r = sa.delete(f"{api}/members/{rp_ids[rp_viewer]}", params={"tenant_id": 1})
    check("phase2: admin cannot remove another workspace's member",
          r.status_code == 403, str(r.status_code))

    # --- the platform flag does not grant authority inside a viewer membership ---
    r = spa.get(f"{api}/campaigns/")
    check("phase2: platform operator reads as their membership allows", r.status_code == 200,
          r.text[:120])
    r = spa.post(f"{api}/campaigns/", json=rp_campaign_payload(f"smoke-rp-pa-{rp_pid}"))
    check("phase2: platform flag grants no write beyond the viewer membership",
          r.status_code == 403, str(r.status_code))

    # --- ownership transfer is explicit and single-owner ---
    r = so.post(f"{api}/members/{rp_ids[rp_admin]}/transfer-ownership")
    check("phase2: owner transfers ownership", r.status_code == 200, r.text[:150])
    r = s.get(f"{api}/members/", params={"tenant_id": rp_t})
    rp_roles = {m["username"]: m["role"] for m in (r.json() or {}).get("members", [])}
    check("phase2: target is owner and previous owner steps down to admin",
          rp_roles.get(rp_admin) == "owner" and rp_roles.get(rp_owner) == "admin", str(rp_roles))
    r = so.post(f"{api}/members/{rp_ids[rp_owner]}/transfer-ownership")
    check("phase2: a demoted admin cannot transfer ownership (403)",
          r.status_code in (400, 403), str(r.status_code))
    r = sa.post(f"{api}/members/{rp_ids[rp_owner]}/transfer-ownership")
    check("phase2: the new owner transfers ownership back", r.status_code == 200, r.text[:150])

    # --- removing a member removes the membership only ---
    r = sa.delete(f"{api}/members/{rp_ids[rp_multi]}")
    check("phase2: admin removes a membership", r.status_code == 200, r.text[:150])
    check("phase2: the global user row survives",
          pg_scalar(f"SELECT count(*) FROM users WHERE username = '{rp_multi}'") == "1")
    check("phase2: the removed member's sessions survive",
          pg_scalar(f"SELECT count(*) FROM auth_sessions WHERE username = '{rp_multi}'") != "0")
    check("phase2: the T membership itself is gone",
          pg_scalar("SELECT count(*) FROM tenant_memberships m JOIN users u ON u.id = m.user_id "
                    f"WHERE u.username = '{rp_multi}' AND m.tenant_id = {int(rp_t)}") == "0")
    rp_m2 = rp_login(rp_multi)
    r = rp_m2.get(f"{api}/campaigns/")
    check("phase2: the removed member still reaches their other workspace",
          r.status_code == 200, r.text[:120])

    # --- a platform operator with NO membership has no tenant access ---
    r = s.delete(f"{api}/members/{rp_ids[rp_nobody]}", params={"tenant_id": rp_t})
    check("phase2: platform removes a member from T", r.status_code == 200, r.text[:150])
    rp_nb = rp_login(rp_nobody)
    r = rp_nb.get(f"{api}/campaigns/")
    check("phase2: platform flag alone gives no tenant access (403)",
          r.status_code == 403, str(r.status_code))

    # --- seat limit ---
    r = s.post(f"{api}/tenants/", json={"name": f"RP Seats {rp_pid}",
                                        "slug": f"rp-seats-{rp_pid}"})
    rp_seats = (r.json() or {}).get("tenant_id")
    check("phase2: seats workspace provisioned", r.status_code == 200 and bool(rp_seats), r.text[:150])
    pg_exec(f"UPDATE tenants SET seats = 2 WHERE id = {int(rp_seats)}")
    for i in (1, 2):
        r = s.post(f"{api}/members/", params={"tenant_id": rp_seats},
                   json={"username": f"rp-seat{i}-{rp_pid}",
                         "email": f"rp-seat{i}-{rp_pid}@example.com",
                         "password": "smokepass1", "role": "editor"})
        check(f"phase2: seat {i} of 2 filled", r.status_code == 200, r.text[:150])
    r = s.post(f"{api}/members/", params={"tenant_id": rp_seats},
               json={"username": f"rp-seat3-{rp_pid}",
                     "email": f"rp-seat3-{rp_pid}@example.com",
                     "password": "smokepass1", "role": "editor"})
    check("phase2: a 2-seat workspace refuses the third member (400)",
          r.status_code == 400, str(r.status_code))
    pg_exec(f"UPDATE tenants SET seats = NULL WHERE id = {int(rp_seats)}")
    r = s.post(f"{api}/members/", params={"tenant_id": rp_seats},
               json={"username": f"rp-seat3-{rp_pid}",
                     "email": f"rp-seat3-{rp_pid}@example.com",
                     "password": "smokepass1", "role": "editor"})
    check("phase2: seats NULL is unlimited", r.status_code == 200, r.text[:150])

    check("phase2: member mutations are audit-logged",
          pg_scalar("SELECT count(*) FROM audit_log WHERE tenant_id = 1 AND action IN "
                    "('member_added','member_updated','member_removed',"
                    "'ownership_transferred')") != "0")

    # --- phase 2A self-cleanup ---
    pg_exec("DELETE FROM campaigns WHERE alias LIKE 'smoke-rp-%'")
    pg_exec(f"DELETE FROM campaigns WHERE tenant_id IN ({int(rp_t)}, {int(rp_seats)})")
    # Phase 2B seeds every new tenant's settings document, so the workspace
    # rows this section created must be removed with the tenants.
    pg_exec(f"DELETE FROM settings WHERE tenant_id IN ({int(rp_t)}, {int(rp_seats)})")
    pg_exec("DELETE FROM tenant_memberships WHERE user_id IN "
            f"(SELECT id FROM users WHERE username LIKE 'rp-%-{rp_pid}')")
    pg_exec(f"DELETE FROM auth_sessions WHERE username LIKE 'rp-%-{rp_pid}'")
    pg_exec(f"DELETE FROM users WHERE username LIKE 'rp-%-{rp_pid}'")
    pg_exec(f"DELETE FROM tenants WHERE id IN ({int(rp_t)}, {int(rp_seats)})")
    check("phase2: test tenants cleaned up",
          pg_scalar(f"SELECT count(*) FROM tenants WHERE id IN "
                    f"({int(rp_t)}, {int(rp_seats)})") == "0")
    check("phase2: test users cleaned up",
          pg_scalar(f"SELECT count(*) FROM users WHERE username LIKE 'rp-%-{rp_pid}'") == "0")

    # =====================================================================
    print("== Multi-tenancy phase 2B: seeded settings, retention, bind secret, scoped token ==")
    # A newly created workspace gets its own settings document (documented
    # defaults + a fresh API token); the retention prune applies each tenant's
    # own window; the bind secret is per tenant (one workspace's cookie cannot
    # be forged with another's); and a Bearer API token acts only inside the
    # workspace that owns it.
    p2_pid = os.getpid()
    import base64 as _b64
    import hashlib as _hashlib
    import hmac as _hmac
    import time as _time

    p2_b_tenant = p2_l_tenant = None
    p2_a_camp = p2_b_bind = p2_b_mon = None
    p2_b_user = f"p2b-owner-{p2_pid}"
    p2_t1_token_before = pg_scalar(
        "SELECT coalesce(value::jsonb->>'apiToken','') FROM settings "
        "WHERE name = 'settings' AND tenant_id = 1").strip()

    # Tenant ids are handed out from a sequence the backend's startup migration
    # setvals back to MAX(id), so a previous run's ids can be re-handed-out; the
    # frontend process caches each tenant's bind secret by id, and a reused id
    # would serve that stale cached secret. A time-based id range keeps this
    # section's bind-secret assertions deterministic across runs.
    pg_exec(f"SELECT setval('tenants_id_seq', {int(_time.time()) % 1000000000})")

    # ---- 1. a new tenant's settings document is seeded ----
    r = s.post(f"{api}/tenants/", json={
        "name": f"P2B Workspace {p2_pid}", "slug": f"p2b-{p2_pid}",
        "username": p2_b_user, "password": "smokepass1",
        "email": f"{p2_b_user}@example.com", "role": "owner"})
    p2_b_tenant = (r.json() or {}).get("tenant_id")
    check("phase2b: workspace provisioned", r.status_code == 200 and bool(p2_b_tenant),
          r.text[:200])
    r = s.post(f"{api}/tenants/", json={"name": f"P2B Long {p2_pid}",
                                        "slug": f"p2b-long-{p2_pid}"})
    p2_l_tenant = (r.json() or {}).get("tenant_id")
    check("phase2b: long-retention workspace provisioned",
          r.status_code == 200 and bool(p2_l_tenant), r.text[:200])

    p2_seed_currency = pg_scalar(f"SELECT value::jsonb->>'currency' FROM settings "
                                 f"WHERE name = 'settings' AND tenant_id = {int(p2_b_tenant)}")
    p2_seed_tz = pg_scalar(f"SELECT value::jsonb->>'timezone' FROM settings "
                           f"WHERE name = 'settings' AND tenant_id = {int(p2_b_tenant)}")
    p2_seed_token = pg_scalar(f"SELECT coalesce(value::jsonb->>'apiToken','') FROM settings "
                              f"WHERE name = 'settings' AND tenant_id = {int(p2_b_tenant)}").strip()
    check("phase2b: new tenant's settings document is seeded (currency/timezone readable)",
          p2_seed_currency == "USD" and p2_seed_tz == "UTC",
          f"currency={p2_seed_currency!r} timezone={p2_seed_tz!r}")
    check("phase2b: new tenant has its own fresh API token (not the shared default / tenant 1's)",
          len(p2_seed_token) >= 16 and p2_seed_token != "a1b2c3d4e5f6"
          and p2_seed_token != p2_t1_token_before, f"token={p2_seed_token[:6]!r}…")
    check("phase2b: no tenant-1 values leaked into the new tenant's settings",
          pg_scalar(f"SELECT (value::jsonb ? 'telegram')::text FROM settings "
                    f"WHERE name = 'settings' AND tenant_id = {int(p2_b_tenant)}") == "false"
          and pg_scalar(f"SELECT (value::jsonb ? 'data_retention')::text FROM settings "
                        f"WHERE name = 'settings' AND tenant_id = {int(p2_b_tenant)}") == "false")

    # ---- 2. the retention prune applies each tenant's own window ----
    p2_ret_b = 771000000 + (p2_pid % 10000) * 10 + 1   # campaign ids own these rows
    p2_ret_l = p2_ret_b + 1
    for _tid, _camp in ((p2_b_tenant, p2_ret_b), (p2_l_tenant, p2_ret_l)):
        ch_exec("INSERT INTO clicks_data (received_at, campaign_id, tenant_id, click_id, click) "
                f"VALUES (now() - INTERVAL 5 DAY, {_camp}, {int(_tid)}, 'p2-ret-old-{int(_tid)}', true), "
                f"(now(), {_camp}, {int(_tid)}, 'p2-ret-new-{int(_tid)}', true)")
    pg_exec(f"UPDATE tenants SET retention_days = 1 WHERE id = {int(p2_b_tenant)}")
    pg_exec(f"UPDATE tenants SET retention_days = 30 WHERE id = {int(p2_l_tenant)}")
    # Drive the prune directly instead of waiting for the daily schedule.
    subprocess.run(["docker", "exec", "tracker_frontend", "python", "-c",
                    "import asyncio, app; asyncio.run(app.prune_old_data())"],
                   capture_output=True, text=True, timeout=240)

    def p2_click_count(click_id):
        _rc, out, _err = ch_exec(f"SELECT count() FROM clicks_data WHERE click_id = '{click_id}'")
        return out.strip()

    check("phase2b: retention prune drops a short-retention tenant's old row",
          p2_click_count(f"p2-ret-old-{int(p2_b_tenant)}") == "0",
          p2_click_count(f"p2-ret-old-{int(p2_b_tenant)}"))
    check("phase2b: the short-retention tenant's fresh row stays",
          p2_click_count(f"p2-ret-new-{int(p2_b_tenant)}") == "1",
          p2_click_count(f"p2-ret-new-{int(p2_b_tenant)}"))
    check("phase2b: a longer-retention tenant's old row is untouched",
          p2_click_count(f"p2-ret-old-{int(p2_l_tenant)}") == "1",
          p2_click_count(f"p2-ret-old-{int(p2_l_tenant)}"))
    check("phase2b: the longer-retention tenant's fresh row stays",
          p2_click_count(f"p2-ret-new-{int(p2_l_tenant)}") == "1",
          p2_click_count(f"p2-ret-new-{int(p2_l_tenant)}"))

    # ---- set up both tenants' campaigns (bind secret + monitor + token) ----
    sb2 = requests.Session()
    sb2.verify = not INSECURE
    r = sb2.post(f"{api}/login", json={"username": p2_b_user, "password": "smokepass1"})
    check("phase2b: new workspace owner logs in", r.status_code == 200, r.text[:150])

    p2_bind_alias = f"p2b-bind-{p2_pid}"
    p2_mon_alias = f"p2b-mon-{p2_pid}"
    p2_a_alias = f"p2b-a-{p2_pid}"

    def p2_config(f0, f1=None):
        flows = [{"type": "regular", "position": 1, "enabled": True, "schema": "redirect",
                  "redirect_url": f0, "filters": [], "weight": 50}]
        if f1:
            flows.append({"type": "regular", "position": 2, "enabled": True,
                          "schema": "redirect", "redirect_url": f1, "filters": [], "weight": 50})
        return {"stickiness": True, "postbacks": [], "fallback_url": f0, "flows": flows}

    r = sb2.post(f"{api}/campaigns/", json={
        "name": p2_bind_alias, "alias": p2_bind_alias, "type": "campaign",
        "status": "active", "redirect_mode": "position",
        "config": p2_config("https://example.com/p2b-f0", "https://example.com/p2b-f1")})
    p2_b_bind = (r.json() or {}).get("id")
    check("phase2b: tenant B bind campaign created",
          r.status_code == 200 and p2_b_bind, r.text[:200])

    r = sb2.post(f"{api}/campaigns/", json={
        "name": p2_mon_alias, "alias": p2_mon_alias, "type": "campaign",
        "status": "active", "redirect_mode": "position",
        "config": p2_config("http://127.0.0.1:8501/api/auth-status")})
    p2_b_mon = (r.json() or {}).get("id")
    check("phase2b: tenant B monitor campaign created",
          r.status_code == 200 and p2_b_mon, r.text[:200])

    r = s.post(f"{api}/campaigns/", json={
        "name": p2_a_alias, "alias": p2_a_alias, "type": "campaign",
        "status": "active", "redirect_mode": "position",
        "config": p2_config("https://example.com/p2b-a0", "https://example.com/p2b-a1")})
    p2_a_camp = (r.json() or {}).get("id")
    check("phase2b: tenant 1 campaign created", r.status_code == 200 and p2_a_camp, r.text[:200])

    # ---- 3. the bind secret is per tenant ----
    # Routing hash computed from the stored config — the same algorithm the
    # tracking plane uses (weight excluded, sorted keys).
    _stored = json.loads(pg_scalar(f"SELECT config::text FROM campaigns WHERE id = {int(p2_b_bind)}"))
    _flows = [{k: v for k, v in f.items() if k != "weight"} for f in _stored.get("flows", [])]
    p2_rhash = _hashlib.sha256(json.dumps(
        {"flows": _flows, "redirect_mode": "position"}, sort_keys=True,
        default=str).encode()).hexdigest()[:16]

    def p2_forge(secret, fi):
        payload = {"cid": int(p2_b_bind), "fi": fi, "offer": None, "landing": None,
                   "exp": int(_time.time()) + 3600, "h": p2_rhash}
        raw = _b64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
        sig = _hmac.new(secret.encode(), raw.encode(), _hashlib.sha256).hexdigest()
        return f"{raw}.{sig}"

    def p2_hit(cookie=None):
        headers = {"Cookie": f"aaa_bind={cookie}"} if cookie else {}
        rr = requests.get(f"{BASE}/{p2_bind_alias}", verify=not INSECURE,
                          allow_redirects=False, headers=headers)
        return rr.status_code, rr.headers.get("location") or ""

    # The first bind use is what creates the workspace's secret.
    check("phase2b: a fresh visit serves the position-1 flow",
          "example.com/p2b-f0" in p2_hit()[1], p2_hit()[1])
    p2_sec_b = pg_scalar("SELECT value FROM settings WHERE name = 'aaa_bind_secret' "
                         f"AND tenant_id = {int(p2_b_tenant)}").strip()
    p2_sec_a = pg_scalar("SELECT value FROM settings WHERE name = 'aaa_bind_secret' "
                         "AND tenant_id = 1").strip()
    check("phase2b: both tenants have a bind secret and they differ",
          bool(p2_sec_b) and bool(p2_sec_a) and p2_sec_b != p2_sec_a,
          f"B={bool(p2_sec_b)} A={bool(p2_sec_a)} same={p2_sec_b == p2_sec_a}")

    check("phase2b: tenant B's own secret signs a valid binding (flow 2)",
          "example.com/p2b-f1" in p2_hit(p2_forge(p2_sec_b, 1))[1],
          p2_hit(p2_forge(p2_sec_b, 1))[1])
    check("phase2b: a cookie forged with tenant A's secret is rejected in tenant B",
          "example.com/p2b-f0" in p2_hit(p2_forge(p2_sec_a, 1))[1],
          p2_hit(p2_forge(p2_sec_a, 1))[1])

    # ---- 4. a loop function run for one tenant sees only that tenant's rows ----
    p2_mon_cmd = ("import asyncio; from tenant_context import set_current_tenant; "
                  f"set_current_tenant({int(p2_b_tenant)}); "
                  "from app_pages.monitor import run_monitor_cycle; "
                  "print(asyncio.run(run_monitor_cycle()))")
    subprocess.run(["docker", "exec", "tracker_backend", "python", "-c", p2_mon_cmd],
                   capture_output=True, text=True, timeout=240)
    check("phase2b: monitor cycle run for tenant B checked B's campaign",
          pg_scalar(f"SELECT count(*) FROM monitor_state WHERE tenant_id = {int(p2_b_tenant)} "
                    f"AND campaign_id = {int(p2_b_mon)}") != "0")
    check("phase2b: the same run did not touch tenant 1's campaign (no cross-tenant row)",
          pg_scalar(f"SELECT count(*) FROM monitor_state WHERE tenant_id = {int(p2_b_tenant)} "
                    f"AND campaign_id = {int(p2_a_camp)}") == "0")

    # ---- 5. the Bearer API token is scoped to one tenant ----
    p2_t1_token = f"p2b-t1-tok-{p2_pid}"
    pg_exec("UPDATE settings SET value = (value::jsonb || "
            f"jsonb_build_object('apiToken', '{p2_t1_token}'))::text "
            "WHERE name = 'settings' AND tenant_id = 1")
    p2_h_b = {"Authorization": f"Bearer {p2_seed_token}"}
    p2_h_a = {"Authorization": f"Bearer {p2_t1_token}"}

    r = requests.get(f"{api}/campaigns/", headers=p2_h_b, verify=not INSECURE)
    p2_b_ids = {c.get("id") for c in (r.json() if r.status_code == 200 else [])}
    check("phase2b: tenant B's token reads B's own campaign", int(p2_b_bind) in p2_b_ids,
          f"{r.status_code} {sorted(p2_b_ids)[:8]}")
    check("phase2b: tenant B's token cannot see tenant 1's campaign",
          int(p2_a_camp) not in p2_b_ids)

    r = requests.get(f"{api}/campaigns/", headers=p2_h_a, verify=not INSECURE)
    p2_a_ids = {c.get("id") for c in (r.json() if r.status_code == 200 else [])}
    check("phase2b: tenant 1's token keeps working and reads tenant 1's campaign",
          r.status_code == 200 and int(p2_a_camp) in p2_a_ids,
          f"{r.status_code} {sorted(p2_a_ids)[:8]}")
    check("phase2b: tenant 1's token cannot see tenant B's campaign",
          int(p2_b_bind) not in p2_a_ids)

    r = requests.get(f"{api}/optimizer/{int(p2_b_bind)}", headers=p2_h_b, verify=not INSECURE)
    check("phase2b: tenant B's token reads B's campaign detail", r.status_code == 200,
          r.text[:120])
    r = requests.get(f"{api}/optimizer/{int(p2_b_bind)}", headers=p2_h_a, verify=not INSECURE)
    check("phase2b: tenant 1's token is refused on tenant B's campaign detail",
          r.status_code in (403, 404), f"got {r.status_code}")
    r = requests.get(f"{api}/members/", params={"tenant_id": 1}, headers=p2_h_b,
                     verify=not INSECURE)
    check("phase2b: a token cannot target another workspace's members",
          r.status_code == 403, f"got {r.status_code}")
    r = requests.get(f"{api}/campaigns/", headers={"Authorization": "Bearer not-a-real-token"},
                     verify=not INSECURE)
    check("phase2b: an unknown Bearer token is rejected", r.status_code == 401,
          str(r.status_code))

    # ---- phase 2B self-cleanup ----
    p2_camps = [c for c in (p2_ret_b, p2_ret_l, p2_b_bind, p2_b_mon, p2_a_camp) if c]
    ch_exec("ALTER TABLE clicks_data DELETE WHERE campaign_id IN "
            f"({', '.join(str(int(c)) for c in p2_camps)})")
    for table in ("campaigns", "offers", "sources", "affiliate_networks", "domains",
                  "landings", "capi_pixels", "capi_pixel_bindings",
                  "capi_channel_settings", "capi_pixel_sent", "meta_capi_sent",
                  "meta_capi_log", "ad_cost_daily", "integration_connections",
                  "scripts", "filter_presets", "funnel_templates", "domain_groups",
                  "auto_rules", "monitor_state", "honeypot_hits", "postback_logs",
                  "click_forward_logs", "cost_update_logs", "conversions_data",
                  "audit_log", "settings"):
        pg_exec(f"DELETE FROM {table} WHERE tenant_id IN "
                f"({int(p2_b_tenant)}, {int(p2_l_tenant)})")
    pg_exec(f"DELETE FROM campaigns WHERE id = {int(p2_a_camp)}")
    pg_exec(f"DELETE FROM monitor_state WHERE campaign_id = {int(p2_a_camp)}")
    pg_exec(f"DELETE FROM tenant_memberships WHERE tenant_id IN "
            f"({int(p2_b_tenant)}, {int(p2_l_tenant)})")
    pg_exec(f"DELETE FROM auth_sessions WHERE username = '{p2_b_user}'")
    pg_exec(f"DELETE FROM users WHERE username = '{p2_b_user}'")
    pg_exec(f"DELETE FROM tenants WHERE id IN ({int(p2_b_tenant)}, {int(p2_l_tenant)})")
    # restore tenant 1's API token exactly as it was
    if p2_t1_token_before:
        pg_exec("UPDATE settings SET value = (value::jsonb || "
                f"jsonb_build_object('apiToken', '{p2_t1_token_before}'))::text "
                "WHERE name = 'settings' AND tenant_id = 1")
    else:
        pg_exec("UPDATE settings SET value = (value::jsonb - 'apiToken')::text "
                "WHERE name = 'settings' AND tenant_id = 1")
    check("phase2b: test tenants cleaned up",
          pg_scalar(f"SELECT count(*) FROM tenants WHERE id IN "
                    f"({int(p2_b_tenant)}, {int(p2_l_tenant)})") == "0")
    check("phase2b: tenant 1's API token restored",
          pg_scalar("SELECT coalesce(value::jsonb->>'apiToken','') FROM settings "
                    "WHERE name = 'settings' AND tenant_id = 1").strip() == p2_t1_token_before)

    # ===== CAPI pixels: more than one pixel bound to a single channel =====
    # Self-contained: the earlier pixel block tore its receiver down, so start a
    # fresh one on a new port and reuse its capture list + helper functions.
    mb_port = 21000 + (os.getpid() % 1000)
    mb_srv = None
    _capi_socketserver.TCPServer.allow_reuse_address = True
    for _attempt in range(8):
        try:
            mb_srv = _capi_socketserver.TCPServer(("0.0.0.0", mb_port), _CapiReceiver)
            break
        except OSError:
            mb_port += 1
    mb_base = f"http://host.docker.internal:{mb_port}"
    if mb_srv is not None:
        mb_srv.daemon_threads = True
        _capi_threading.Thread(target=mb_srv.serve_forever, daemon=True).start()

    mb_tag = f"smoke-mb-{os.getpid()}"
    mb_saved = (s.get(f"{api}/settings/").json().get("settings") or {}).get("meta_capi")
    mb_src = mb_cid = mb_px1 = mb_px2 = None
    try:
        def mb_mk(title, dataset):
            return s.post(f"{api}/settings/capi-pixels", json={
                "title": title, "platform": "meta", "pixel_id": dataset,
                "access_token": f"{mb_tag}-tok-{dataset}", "default_event_name": "Lead",
                "action_source": "website", "status": "active",
                "custom_matching": False, "conversion_matching": [],
                "payout_customisations": []})

        r = mb_mk("M1", f"{mb_tag}-ds1")
        mb_px1 = (r.json().get("pixel") or {}).get("id")
        r = mb_mk("M2", f"{mb_tag}-ds2")
        mb_px2 = (r.json().get("pixel") or {}).get("id")
        check("capi-pixels: two pixels created for the multi-bind check",
              bool(mb_px1) and bool(mb_px2), r.text[:150])

        r = s.post(f"{api}/sources/", json={"name": f"{mb_tag}-src"})
        mb_src = r.json().get("id")
        r = s.post(f"{api}/campaigns/", json={
            "name": f"{mb_tag}-chan", "alias": f"{mb_tag}-chan", "type": "campaign",
            "status": "active", "redirect_mode": "position", "traffic_source_id": mb_src,
            "config": {"flows": [], "postbacks": [], "hide_referrer": False}})
        mb_cid = r.json().get("id")

        # Global CAPI config gates the send; point it at the fresh receiver.
        s.post(f"{api}/settings/", json={"settings": {"meta_capi": {
            "enabled": True, "dry_run": False, "graph_base_url": mb_base,
            "dataset_id": "", "access_token": "", "test_event_code": "",
            "default_currency": "USD", "send_statuses": ["lead", "sale", "upsale"],
            "include_customer_match": True, "status_events": {}, "pixel_overrides": {}}}})
        settle_settings_cache()

        # Bind BOTH pixels to the one channel, order preserved on read.
        r = px_bind("channel", mb_src, [mb_px1, mb_px2], active=True)
        check("capi-pixels: channel binds two pixels (order preserved)",
              r.status_code == 200 and r.json().get("pixel_ids") == [mb_px1, mb_px2],
              r.text[:200])
        r = s.get(f"{api}/settings/capi-bindings",
                  params={"scope": "channel", "scope_id": mb_src})
        check("capi-pixels: both bound pixels round-trip on read",
              r.json().get("pixel_ids") == [mb_px1, mb_px2], r.text[:200])

        # A conversion on the channel reaches BOTH datasets exactly once each.
        mb_click = f"{mb_tag}-click"
        px_seed(mb_click, cid=mb_cid, status="sale", payout=5)
        before1 = len(capi_paths(f"{mb_tag}-ds1"))
        before2 = len(capi_paths(f"{mb_tag}-ds2"))
        requests.get(f"{BASE}/pb?clickid={mb_click}&status=sale&payout=5",
                     verify=not INSECURE)
        capi_wait_click(mb_click, 2)
        _capi_time.sleep(1.5)
        check("capi-pixels: multi-bound conversion reaches both pixels exactly once",
              capi_click_count(mb_click) == 2
              and len(capi_paths(f"{mb_tag}-ds1")) - before1 == 1
              and len(capi_paths(f"{mb_tag}-ds2")) - before2 == 1,
              f"count={capi_click_count(mb_click)} "
              f"ds1={len(capi_paths(mb_tag + '-ds1'))} "
              f"ds2={len(capi_paths(mb_tag + '-ds2'))}")

        # Removing one of the two leaves the other bound.
        px_bind("channel", mb_src, [mb_px2], active=True)
        r = s.get(f"{api}/settings/capi-bindings",
                  params={"scope": "channel", "scope_id": mb_src})
        check("capi-pixels: removing one of two bound pixels leaves the other",
              r.json().get("pixel_ids") == [mb_px2], r.text[:200])
    finally:
        if mb_saved is None:
            s.post(f"{api}/settings/", json={"settings": {"meta_capi": None}})
        else:
            s.post(f"{api}/settings/", json={"settings": {"meta_capi": mb_saved}})
        for _mb_px in (mb_px1, mb_px2):
            if _mb_px:
                s.delete(f"{api}/settings/capi-pixels/{_mb_px}")
        if mb_src:
            pg_exec(f"DELETE FROM capi_channel_settings WHERE source_id = {mb_src}")
        pg_exec(f"DELETE FROM capi_pixel_sent WHERE click_id LIKE '{mb_tag}%'")
        pg_exec(f"DELETE FROM meta_capi_log WHERE click_id LIKE '{mb_tag}%'")
        pg_exec(f"DELETE FROM conversions_data WHERE click_id LIKE '{mb_tag}%'")
        if mb_cid:
            s.delete(f"{api}/campaigns/{mb_cid}")
        if mb_src:
            s.delete(f"{api}/sources/{mb_src}")
        try:
            if mb_srv is not None:
                mb_srv.shutdown()
                mb_srv.server_close()
        except Exception:
            pass

    # ===== Meta Ads: channel impression cost sync for zero-click days =====
    import http.server as _ics_httpserver
    import socketserver as _ics_socketserver
    import threading as _ics_threading

    ics_pid = os.getpid()
    ics_port = 25000 + (ics_pid % 1000)
    ics_acct = f"impspend-{ics_pid}"
    ics_token = f"tok-ics-{ics_pid}"
    ics_day = ch_query("SELECT toString(toDate(now()))")
    ics_captured = []

    class _IcsGraph(_ics_httpserver.BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import urlparse
            ics_captured.append(urlparse(self.path).path)

            def _send(code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            if "/ready" in self.path:
                _send(200, {"ready": True})
                return
            # Two campaigns, both with platform spend but zero tracker clicks.
            _send(200, {"data": [
                {"campaign_id": f"ics-a-{ics_pid}", "campaign_name": f"ICS A {ics_pid}",
                 "date_start": ics_day, "spend": "7", "impressions": "700", "clicks": "0"},
                {"campaign_id": f"ics-b-{ics_pid}", "campaign_name": f"ICS B {ics_pid}",
                 "date_start": ics_day, "spend": "4", "impressions": "400", "clicks": "0"},
            ]})

        def log_message(self, *args):
            pass

    ics_srv = None
    _ics_socketserver.TCPServer.allow_reuse_address = True
    for _attempt in range(8):
        try:
            ics_srv = _ics_socketserver.TCPServer(("0.0.0.0", ics_port), _IcsGraph)
            break
        except OSError:
            ics_port += 1
    ics_base = f"http://host.docker.internal:{ics_port}"
    if ics_srv is not None:
        ics_srv.daemon_threads = True
        _ics_threading.Thread(target=ics_srv.serve_forever, daemon=True).start()

    ics_probe = ""
    try:
        ics_probe = subprocess.run(
            ["docker", "exec", "tracker_backend", "python", "-c",
             f"import urllib.request;print(urllib.request.urlopen('{ics_base}/ready',timeout=5).read().decode())"],
            capture_output=True, text=True, timeout=15).stdout
    except Exception:
        pass
    if "ready" not in ics_probe:
        try:
            ics_probe = requests.get(f"http://127.0.0.1:{ics_port}/ready", timeout=5).text
        except Exception:
            pass
    check("meta-ads: impression-sync mock Graph receiver reachable",
          "ready" in ics_probe, ics_probe[:80])

    ics_tag = f"smoke-ics-{ics_pid}"
    ics_saved = ((s.get(f"{api}/settings/").json().get("settings") or {}).get("meta_ads"))
    ics_src_on = ics_src_off = ics_cid_on = ics_cid_off = None
    try:
        r = s.post(f"{api}/sources/", json={"name": f"{ics_tag}-on"})
        ics_src_on = r.json().get("id")
        r = s.post(f"{api}/sources/", json={"name": f"{ics_tag}-off"})
        ics_src_off = r.json().get("id")
        r = px_bind("channel", ics_src_on, [], active=True, impression_cost_sync=True)
        check("meta-ads: impression cost sync enabled on the channel",
              r.status_code == 200 and r.json().get("impression_cost_sync") is True,
              r.text[:150])
        r = px_bind("channel", ics_src_off, [], active=True, impression_cost_sync=False)
        check("meta-ads: second channel left on the default (off)",
              r.status_code == 200 and r.json().get("impression_cost_sync") is False,
              r.text[:150])

        r = s.post(f"{api}/campaigns/", json={
            "name": f"ICS A {ics_pid}", "alias": f"{ics_tag}-a", "type": "campaign",
            "status": "active", "redirect_mode": "position",
            "traffic_source_id": ics_src_on,
            "ad_platform_campaign_id": f"ics-a-{ics_pid}",
            "config": {"flows": [], "postbacks": [], "hide_referrer": False}})
        ics_cid_on = r.json().get("id")
        r = s.post(f"{api}/campaigns/", json={
            "name": f"ICS B {ics_pid}", "alias": f"{ics_tag}-b", "type": "campaign",
            "status": "active", "redirect_mode": "position",
            "traffic_source_id": ics_src_off,
            "ad_platform_campaign_id": f"ics-b-{ics_pid}",
            "config": {"flows": [], "postbacks": [], "hide_referrer": False}})
        ics_cid_off = r.json().get("id")
        check("meta-ads: two zero-click campaigns created",
              bool(ics_cid_on) and bool(ics_cid_off), r.text[:150])

        def ics_set(**over):
            cfg = {"enabled": True, "ad_account_ids": [ics_acct], "access_token": ics_token,
                   "api_version": "v21.0", "dry_run": False, "cadence": "hourly",
                   "backfill_days": 7, "graph_base_url": ics_base, "match_preference": "auto"}
            cfg.update(over)
            return s.post(f"{api}/settings/", json={"settings": {"meta_ads": cfg}})

        def ics_sys_count(cid):
            return ch_query(
                f"SELECT count() FROM clicks_data WHERE campaign_id={cid} "
                f"AND visitor_id LIKE 'system-%'")

        def ics_total(cid):
            return ch_query(
                f"SELECT toString(ifNull(sum(cost), 0)) FROM clicks_data "
                f"WHERE campaign_id={cid}")

        # -- dry-run with the flag on writes nothing at all --
        ics_set(dry_run=True)
        r = s.post(f"{api}/meta-ads/sync")
        check("meta-ads: dry-run impression sync returns dry_run",
              r.status_code == 200 and (r.json() or {}).get("status") == "dry_run",
              r.text[:150])
        check("meta-ads: dry-run with the flag on writes no system click",
              ics_sys_count(ics_cid_on) in ("0", ""), ics_sys_count(ics_cid_on))
        check("meta-ads: dry-run with the flag on writes no ad_cost_daily row",
              pg_scalar(f"SELECT count(*) FROM ad_cost_daily "
                        f"WHERE ad_account_id='{ics_acct}'") == "0",
              pg_scalar(f"SELECT count(*) FROM ad_cost_daily "
                        f"WHERE ad_account_id='{ics_acct}'"))

        # -- live: the flagged zero-click day gets ONE system click = the spend --
        ics_set(dry_run=False)
        r = s.post(f"{api}/meta-ads/sync")
        live = r.json() if r.status_code == 200 else {}
        check("meta-ads: live impression sync returns ok",
              r.status_code == 200 and live.get("status") == "ok", r.text[:200])
        check("meta-ads: flagged zero-click day allocates exactly one system click",
              ics_sys_count(ics_cid_on) == "1", ics_sys_count(ics_cid_on))
        check("meta-ads: system click carries the day's whole spend",
              ics_total(ics_cid_on) in ("7", "7.0"), ics_total(ics_cid_on))
        check("meta-ads: campaign-day total cost equals the platform spend",
              ics_total(ics_cid_on) in ("7", "7.0"), ics_total(ics_cid_on))

        # -- unflagged channel keeps today's behaviour: nothing allocated --
        check("meta-ads: unflagged zero-click day still allocates nothing",
              ics_sys_count(ics_cid_off) in ("0", "")
              and ics_total(ics_cid_off) in ("0", "", "0.0"),
              f"sys={ics_sys_count(ics_cid_off)} total={ics_total(ics_cid_off)}")

        # -- re-run REPLACES, never stacks --
        r = s.post(f"{api}/meta-ads/sync")
        check("meta-ads: re-sync keeps exactly one system click",
              r.status_code == 200 and ics_sys_count(ics_cid_on) == "1",
              ics_sys_count(ics_cid_on))
        check("meta-ads: re-sync keeps the day's cost equal to the spend",
              ics_total(ics_cid_on) in ("7", "7.0"), ics_total(ics_cid_on))
    finally:
        if ics_saved is None:
            s.post(f"{api}/settings/", json={"settings": {"meta_ads": None}})
        else:
            s.post(f"{api}/settings/", json={"settings": {"meta_ads": ics_saved}})
        for _ics_cid in (ics_cid_on, ics_cid_off):
            if _ics_cid:
                s.delete(f"{api}/campaigns/{_ics_cid}")
        pg_exec(f"DELETE FROM ad_cost_daily WHERE ad_account_id = '{ics_acct}'")
        for _ics_src in (ics_src_on, ics_src_off):
            if _ics_src:
                pg_exec(f"DELETE FROM capi_channel_settings WHERE source_id = {_ics_src}")
                s.delete(f"{api}/sources/{_ics_src}")
        ch_query(f"ALTER TABLE clicks_data DELETE WHERE campaign_id IN "
                 f"({ics_cid_on or -1}, {ics_cid_off or -1}) SETTINGS mutations_sync = 1")
        try:
            if ics_srv is not None:
                ics_srv.shutdown()
                ics_srv.server_close()
        except Exception:
            pass

    # =====================================================================
    print("== Multi-tenancy phase 3: invitations, onboarding, hierarchy traversal ==")
    # Invitations are one-time, expiring grants of a role in ONE workspace; only
    # the token's SHA-256 hash is stored. A workspace's owner/admin also reaches
    # its DESCENDANTS (walking parent_tenant_id upward from the target), never
    # its parent or a sibling — and only the visited workspace's data is visible.
    p3_pid = os.getpid()
    import hashlib as _p3_hashlib
    p3_parent = p3_child = p3_sibling = p3_fresh = p3_other = None
    p3_owner = f"p3-owner-{p3_pid}"
    p3_admin = f"p3-admin-{p3_pid}"
    p3_member = f"p3-member-{p3_pid}"
    p3_invitee = f"p3-invitee-{p3_pid}"
    p3_other_owner = f"p3-other-{p3_pid}"
    p3_child_admin = f"p3-childadmin-{p3_pid}"
    p3_other_invitee = f"p3-otherinvitee-{p3_pid}"
    p3_expired_token = f"p3-expired-token-{p3_pid}"
    p3_camp = f"p3-camp-{p3_pid}"
    p3_parent_camp = None
    p3_created_invites = []

    def p3_login(username, password="smokepass1"):
        sess = requests.Session()
        sess.verify = not INSECURE
        rr = sess.post(f"{api}/login", json={"username": username, "password": password})
        check(f"p3: login {username}", rr.status_code == 200, rr.text[:120])
        return sess

    def p3_lookup(token):
        return requests.get(f"{api}/invitations/lookup", params={"token": token},
                            verify=not INSECURE)

    def p3_accept(token, username, email=None):
        body = {"token": token, "username": username, "password": "smokepass1"}
        if email:
            body["email"] = email
        return requests.post(f"{api}/invitations/accept", json=body, verify=not INSECURE)

    try:
        # ---- fixtures: a parent workspace, its children, an unrelated one ----
        r = s.post(f"{api}/tenants/", json={
            "name": f"P3 Parent {p3_pid}", "slug": f"p3-parent-{p3_pid}",
            "username": p3_owner, "password": "smokepass1",
            "email": f"{p3_owner}@example.com", "role": "owner"})
        p3_parent = (r.json() or {}).get("tenant_id")
        check("p3: parent workspace provisioned", r.status_code == 200 and bool(p3_parent),
              r.text[:200])
        for label in ("child", "sibling", "fresh"):
            r = s.post(f"{api}/tenants/", json={
                "name": f"P3 {label.title()} {p3_pid}", "slug": f"p3-{label}-{p3_pid}",
                "parent_tenant_id": p3_parent})
            tid = (r.json() or {}).get("tenant_id")
            if label == "child":
                p3_child = tid
            elif label == "sibling":
                p3_sibling = tid
            else:
                p3_fresh = tid
            check(f"p3: {label} child workspace provisioned", r.status_code == 200, r.text[:200])
        r = s.post(f"{api}/tenants/", json={
            "name": f"P3 Other {p3_pid}", "slug": f"p3-other-{p3_pid}",
            "username": p3_other_owner, "password": "smokepass1",
            "email": f"{p3_other_owner}@example.com", "role": "owner"})
        p3_other = (r.json() or {}).get("tenant_id")
        check("p3: unrelated workspace provisioned", r.status_code == 200 and bool(p3_other),
              r.text[:200])

        sp = p3_login(p3_owner)
        so3 = p3_login(p3_other_owner)
        r = sp.post(f"{api}/members/", json={"username": p3_admin,
                                             "email": f"{p3_admin}@example.com",
                                             "password": "smokepass1", "role": "admin"})
        check("p3: parent admin created", r.status_code == 200, r.text[:200])
        r = sp.post(f"{api}/members/", json={"username": p3_member,
                                             "email": f"{p3_member}@example.com",
                                             "password": "smokepass1", "role": "viewer"})
        check("p3: parent viewer created", r.status_code == 200, r.text[:200])
        sa3 = p3_login(p3_admin)
        sv3 = p3_login(p3_member)

        # ---- invite lifecycle: create -> public lookup -> accept -> login ----
        inv_token = inv_id = None
        r = sp.post(f"{api}/invitations/",
                    json={"email": f"{p3_invitee}@example.com", "role": "editor"})
        inv = r.json() if r.status_code == 200 else {}
        inv_token = inv.get("token")
        inv_id = inv.get("id")
        check("p3: owner creates an invitation with a one-time token",
              r.status_code == 200 and bool(inv_token), r.text[:200])
        if inv_id:
            p3_created_invites.append(inv_id)
        check("p3: creation returns a ready-to-use accept URL carrying the token",
              bool(inv.get("accept_url")) and inv_token in (inv.get("accept_url") or ""),
              str(inv.get("accept_url"))[:140])
        check("p3: the invitation records its expiry",
              bool(inv.get("expires_at")), str(inv.get("expires_at")))

        r = p3_lookup(inv_token)
        lk = r.json() if r.status_code == 200 else {}
        check("p3: public lookup resolves the invitation without a session",
              r.status_code == 200 and lk.get("role") == "editor"
              and lk.get("workspace_name") == f"P3 Parent {p3_pid}"
              and lk.get("email") == f"{p3_invitee}@example.com", r.text[:200])
        check("p3: lookup never returns the raw token", inv_token not in r.text,
              "raw token leaked by lookup")

        r = sp.get(f"{api}/invitations/")
        listing = r.json() if r.status_code == 200 else {}
        pending = listing.get("invitations") or []
        check("p3: the owner lists the workspace's pending invitations",
              r.status_code == 200 and any(i.get("id") == inv_id for i in pending),
              r.text[:200])
        check("p3: no list item carries a token or a hash",
              all("token" not in i and "token_hash" not in i for i in pending),
              str(pending)[:200])
        check("p3: the handed-out token never appears in the list response",
              inv_token not in r.text, "raw token in list response")

        r = p3_accept(inv_token, p3_invitee, email=f"{p3_invitee}@example.com")
        check("p3: the invitation is accepted", r.status_code == 200, r.text[:200])
        si = p3_login(p3_invitee)
        r = si.get(f"{api}/tenants/")
        il = r.json() if r.status_code == 200 else {}
        check("p3: the invitee lands in the inviting workspace with the invited role",
              il.get("current_tenant_id") == p3_parent and il.get("role") == "editor"
              and [t["tenant_id"] for t in il.get("tenants", [])] == [p3_parent], r.text[:200])
        r = p3_accept(inv_token, f"p3-again-{p3_pid}")
        check("p3: the same token cannot be accepted twice (410)", r.status_code == 410,
              f"got {r.status_code} {r.text[:120]}")
        r = p3_lookup(inv_token)
        check("p3: lookup of an accepted token is 410", r.status_code == 410, str(r.status_code))

        stored_hash = pg_scalar(f"SELECT token_hash FROM tenant_invitations "
                                f"WHERE id = {int(inv_id)}")
        check("p3: only a sha256 hash of the token is stored",
              bool(stored_hash) and stored_hash != inv_token and len(stored_hash) == 64
              and stored_hash == _p3_hashlib.sha256(inv_token.encode()).hexdigest(),
              stored_hash[:24])

        # ---- revoke a pending invitation ----
        rev_token = rev_id = None
        r = sp.post(f"{api}/invitations/", json={"username": f"p3-revoked-{p3_pid}",
                                                 "role": "viewer"})
        rev = r.json() if r.status_code == 200 else {}
        rev_token, rev_id = rev.get("token"), rev.get("id")
        check("p3: a username invitation is created", r.status_code == 200 and bool(rev_token),
              r.text[:200])
        if rev_id:
            p3_created_invites.append(rev_id)
        r = sp.delete(f"{api}/invitations/{rev_id}")
        check("p3: a pending invitation is revoked", r.status_code == 200, r.text[:150])
        r = p3_lookup(rev_token)
        check("p3: lookup of a revoked invitation is 410", r.status_code == 410, str(r.status_code))
        r = p3_accept(rev_token, f"p3-revokedinvitee-{p3_pid}")
        check("p3: a revoked invitation cannot be accepted (410)", r.status_code == 410,
              f"got {r.status_code} {r.text[:120]}")

        # ---- duplicate pending invitation ----
        r = sp.post(f"{api}/invitations/", json={"email": f"p3-dup-{p3_pid}@example.com",
                                                 "role": "viewer"})
        dup1 = r.json() if r.status_code == 200 else {}
        if dup1.get("id"):
            p3_created_invites.append(dup1["id"])
        check("p3: first pending invitation created", r.status_code == 200, r.text[:150])
        r = sp.post(f"{api}/invitations/", json={"email": f"p3-dup-{p3_pid}@example.com",
                                                 "role": "editor"})
        check("p3: a second pending invitation for the same invitee is 409",
              r.status_code == 409, str(r.status_code))

        # ---- unknown / expired tokens ----
        r = p3_lookup(f"p3-nope-{p3_pid}")
        check("p3: an unknown token is 404", r.status_code == 404, str(r.status_code))
        pg_exec("INSERT INTO tenant_invitations (tenant_id, email, role, token_hash, invited_by, "
                "expires_at) VALUES "
                f"({int(p3_parent)}, 'p3-expired-{p3_pid}@example.com', 'viewer', "
                f"'{_p3_hashlib.sha256(p3_expired_token.encode()).hexdigest()}', 'smoke', "
                "now() - interval '1 day')")
        r = p3_lookup(p3_expired_token)
        check("p3: an expired token is 410", r.status_code == 410, str(r.status_code))
        r = p3_accept(p3_expired_token, f"p3-expireduser-{p3_pid}")
        check("p3: an expired invitation cannot be accepted (410)", r.status_code == 410,
              f"got {r.status_code} {r.text[:120]}")

        # ---- role ceiling + viewer cannot manage ----
        r = sa3.post(f"{api}/invitations/", json={"email": f"p3-ceiling-{p3_pid}@example.com",
                                                  "role": "owner"})
        check("p3: an admin cannot invite an owner (role ceiling, 403)",
              r.status_code == 403, str(r.status_code))
        r = sa3.post(f"{api}/invitations/", json={"email": f"p3-ceiling-ok-{p3_pid}@example.com",
                                                  "role": "editor"})
        check("p3: an admin may invite below their own role", r.status_code == 200, r.text[:200])
        pg_exec(f"DELETE FROM tenant_invitations "
                f"WHERE email = 'p3-ceiling-ok-{p3_pid}@example.com'")
        r = sv3.get(f"{api}/invitations/")
        check("p3: a workspace viewer cannot list invitations (403)",
              r.status_code == 403, str(r.status_code))
        r = sv3.post(f"{api}/invitations/", json={"email": "p3-viewer-invite@example.com",
                                                  "role": "viewer"})
        check("p3: a workspace viewer cannot create invitations (403)",
              r.status_code == 403, str(r.status_code))
        r = sv3.delete(f"{api}/invitations/{inv_id}")
        check("p3: a workspace viewer cannot revoke invitations (403)",
              r.status_code == 403, str(r.status_code))

        # ---- isolation: another workspace's manager reaches nothing here ----
        r = so3.get(f"{api}/invitations/")
        other_pending = ((r.json() or {}).get("invitations") or []) if r.status_code == 200 else []
        check("p3: another workspace's list is its own, never this one's",
              r.status_code == 200
              and all(i.get("id") not in p3_created_invites for i in other_pending),
              r.text[:200])
        r = so3.post(f"{api}/invitations/", json={"email": f"{p3_other_invitee}@example.com",
                                                  "role": "editor"})
        other_inv = r.json() if r.status_code == 200 else {}
        check("p3: a manager can invite into their own workspace",
              r.status_code == 200 and bool(other_inv.get("token")), r.text[:200])
        check("p3: that invitation belongs to the other workspace",
              pg_scalar(f"SELECT tenant_id FROM tenant_invitations "
                        f"WHERE id = {int(other_inv.get('id') or 0)}") == str(p3_other))
        r = so3.delete(f"{api}/invitations/{inv_id}")
        check("p3: another workspace cannot revoke this one's invitation",
              r.status_code in (403, 404), str(r.status_code))
        check("p3: that delete attempt did not revoke this workspace's invitation",
              pg_scalar(f"SELECT count(*) FROM tenant_invitations WHERE id = {int(inv_id)} "
                        "AND revoked_at IS NULL") == "1")
        r = p3_accept(other_inv.get("token"), p3_other_invitee,
                      email=f"{p3_other_invitee}@example.com")
        check("p3: the other workspace's invitation is accepted", r.status_code == 200,
              r.text[:200])
        check("p3: acceptance creates a membership ONLY in the inviting workspace",
              pg_scalar("SELECT count(*) FROM tenant_memberships m JOIN users u ON u.id = m.user_id "
                        f"WHERE u.username = '{p3_other_invitee}' "
                        f"AND m.tenant_id = {int(p3_other)}") == "1"
              and pg_scalar("SELECT count(*) FROM tenant_memberships m "
                            "JOIN users u ON u.id = m.user_id "
                            f"WHERE u.username = '{p3_other_invitee}' "
                            f"AND m.tenant_id = {int(p3_parent)}") == "0")
        soi = p3_login(p3_other_invitee)
        r = soi.get(f"{api}/tenants/")
        check("p3: the invitee's session resolves to the inviting workspace only",
              r.status_code == 200 and (r.json() or {}).get("current_tenant_id") == p3_other,
              r.text[:150])

        # ---- hierarchy traversal ----
        r = sp.post(f"{api}/campaigns/", json={
            "name": f"P3 Parent Campaign {p3_pid}", "alias": p3_camp, "type": "campaign",
            "status": "active", "redirect_mode": "position",
            "config": {"flows": [], "postbacks": [], "hide_referrer": False}})
        p3_parent_camp = (r.json() or {}).get("id")
        check("p3: the parent workspace has a campaign",
              r.status_code == 200 and bool(p3_parent_camp), r.text[:150])

        r = sp.get(f"{api}/tenants/")
        tl = r.json() if r.status_code == 200 else {}
        p3_tids = [t["tenant_id"] for t in tl.get("tenants", [])]
        check("p3: the parent owner sees its child workspaces",
              r.status_code == 200 and all(tid in p3_tids for tid in (p3_child, p3_sibling, p3_fresh)),
              str(p3_tids))
        check("p3: can_switch is true when there are children to move into",
              tl.get("can_switch") is True, str(tl.get("can_switch")))
        check("p3: child workspaces are offered with the inherited role",
              all(t.get("role") == "owner" for t in tl.get("tenants", [])
                  if t.get("tenant_id") in (p3_child, p3_sibling, p3_fresh)),
              str([(t.get("tenant_id"), t.get("role")) for t in tl.get("tenants", [])]))

        r = sp.post(f"{api}/tenants/switch", json={"tenant_id": p3_child})
        check("p3: the parent owner switches into the child workspace",
              r.status_code == 200 and (r.json() or {}).get("role") == "owner", r.text[:150])
        r = sp.get(f"{api}/tenants/current")
        check("p3: the session's current workspace is the child",
              r.status_code == 200 and (r.json() or {}).get("tenant_id") == p3_child,
              r.text[:150])
        r = sp.get(f"{api}/campaigns/")
        child_aliases = {c["alias"] for c in (r.json() if r.status_code == 200 else [])}
        check("p3: inside the child only the child's data is visible",
              r.status_code == 200 and p3_camp not in child_aliases, str(child_aliases)[:150])
        r = sp.get(f"{api}/tenants/")
        child_view_ids = [t["tenant_id"] for t in ((r.json() or {}).get("tenants") or [])]
        check("p3: the child does not expose its sibling workspaces",
              p3_sibling not in child_view_ids and p3_fresh not in child_view_ids,
              str(child_view_ids))
        r = sp.post(f"{api}/tenants/switch", json={"tenant_id": p3_parent})
        check("p3: switching back to the parent works", r.status_code == 200, r.text[:150])
        r = sp.get(f"{api}/campaigns/")
        check("p3: the parent's data is visible again after switching back",
              p3_camp in {c["alias"] for c in (r.json() if r.status_code == 200 else [])},
              r.text[:150])

        r = sv3.post(f"{api}/tenants/switch", json={"tenant_id": p3_child})
        check("p3: a plain member of the parent cannot switch into the child (403)",
              r.status_code == 403, str(r.status_code))

        # ---- a child-only admin cannot reach the parent ----
        r = s.post(f"{api}/members/", params={"tenant_id": p3_child},
                   json={"username": p3_child_admin,
                         "email": f"{p3_child_admin}@example.com",
                         "password": "smokepass1", "role": "admin"})
        check("p3: child admin provisioned", r.status_code == 200, r.text[:200])
        sc3 = p3_login(p3_child_admin)
        r = sc3.get(f"{api}/tenants/")
        check("p3: the child admin's session starts in the child",
              r.status_code == 200 and (r.json() or {}).get("current_tenant_id") == p3_child,
              r.text[:150])
        r = sc3.post(f"{api}/tenants/switch", json={"tenant_id": p3_parent})
        check("p3: a child admin cannot switch up into the parent (403)",
              r.status_code == 403, str(r.status_code))

        # ---- onboarding checklist ----
        r = sp.post(f"{api}/tenants/switch", json={"tenant_id": p3_fresh})
        check("p3: the owner enters the fresh child workspace", r.status_code == 200,
              r.text[:150])
        r = sp.get(f"{api}/tenants/onboarding")
        ob = r.json() if r.status_code == 200 else {}
        ob_keys = ("has_team", "has_traffic_source", "has_campaign", "has_offer",
                   "has_domain", "has_integration")
        check("p3: a fresh workspace's checklist is all false",
              r.status_code == 200 and all(ob.get(k) is False for k in ob_keys), r.text[:250])
        check("p3: a fresh workspace's checklist names the next step",
              isinstance(ob.get("next_step"), str) and ob.get("next_step") in ob_keys,
              str(ob.get("next_step")))

        r = sp.post(f"{api}/tenants/switch", json={"tenant_id": p3_parent})
        r = sp.get(f"{api}/tenants/onboarding")
        ob2 = r.json() if r.status_code == 200 else {}
        check("p3: the parent's checklist reflects its own members and campaign",
              r.status_code == 200 and ob2.get("has_team") is True
              and ob2.get("has_campaign") is True
              and ob2.get("has_traffic_source") is False
              and ob2.get("next_step") == "has_traffic_source", str(ob2))

        # ---- audit trail ----
        check("p3: invitation mutations are audit-logged",
              pg_scalar(f"SELECT count(*) FROM audit_log WHERE tenant_id = {int(p3_parent)} "
                        "AND action IN ('invitation_created','invitation_revoked',"
                        "'invitation_accepted')") != "0")
    finally:
        p3_ids = ",".join(str(int(t)) for t in
                          (p3_parent, p3_child, p3_sibling, p3_fresh, p3_other) if t) or "0"
        pg_exec(f"DELETE FROM tenant_invitations WHERE tenant_id IN ({p3_ids})")
        pg_exec(f"DELETE FROM settings WHERE tenant_id IN ({p3_ids})")
        pg_exec(f"DELETE FROM campaigns WHERE tenant_id IN ({p3_ids})")
        pg_exec("DELETE FROM tenant_memberships WHERE user_id IN "
                f"(SELECT id FROM users WHERE username LIKE 'p3-%-{p3_pid}')")
        pg_exec(f"DELETE FROM auth_sessions WHERE username LIKE 'p3-%-{p3_pid}'")
        pg_exec(f"DELETE FROM users WHERE username LIKE 'p3-%-{p3_pid}'")
        pg_exec(f"DELETE FROM tenants WHERE id IN ({p3_ids})")
        check("p3: test tenants cleaned up",
              pg_scalar(f"SELECT count(*) FROM tenants WHERE id IN ({p3_ids})") == "0")
        check("p3: test users cleaned up",
              pg_scalar(f"SELECT count(*) FROM users WHERE username LIKE 'p3-%-{p3_pid}'") == "0")

    # ===== OAuth: the redirect URI we advertise must be a real route =====
    # derive_callback_url() is both what we send the provider and what the operator
    # registers there, so a mismatch with the registered path sends the provider's
    # redirect to a 404 and no connection can ever complete. Both sides build the
    # URL from that one helper, so only an HTTP check can see the shape.
    r = s.get(f"{api}/integrations")
    for _plat in ("meta", "snapchat", "tiktok"):
        _adv = ((((r.json() or {}).get("platforms") or {}).get(_plat)) or {}).get("callback_url") or ""
        _path = _adv.split("//", 1)[-1]
        _path = _path.split("/", 1)[-1] if "/" in _path else _path
        rr = requests.get(f"{BASE}/{_path}", verify=not INSECURE, allow_redirects=False)
        check(f"oauth: the advertised {_plat} callback URL is served (not 404)",
              rr.status_code not in (404, 405), f"{_adv} -> {rr.status_code}")

    # ===== Meta ad accounts: chosen under Integrations, saved by the API =====
    # The cost sync reads meta_ads.ad_account_ids from the settings document, and
    # the Integrations card is the only UI that chooses them, so this endpoint is
    # the single writer (besides a whole-settings save). Numeric ids only.
    maa_pid = os.getpid()
    maa_tag = f"maa-{maa_pid}"
    maa_user = f"{maa_tag}-user"
    maa_uid = None
    maa_saved = ((s.get(f"{api}/settings/").json().get("settings") or {})
                 .get("meta_ads"))
    try:
        # Seed a block with other keys so the "does not disturb them" check is real.
        s.post(f"{api}/settings/", json={"settings": {"meta_ads": {
            "enabled": True, "cadence": "daily", "match_preference": "name",
            "backfill_days": 3, "ad_account_ids": []}}})

        # -- normalisation: act_ prefix stripped, dedup, trim, order kept --
        r = s.post(f"{api}/meta-ads/accounts", json={"ad_account_ids": [
            f"act_{maa_pid}", str(maa_pid + 1), f"act_{maa_pid}",
            f"  {maa_pid + 2}  "]})
        maa_body = r.json() if r.status_code == 200 else {}
        maa_expect = [str(maa_pid), str(maa_pid + 1), str(maa_pid + 2)]
        check("meta-ads accounts: POST normalises (act_ stripped, deduped, trimmed, ordered)",
              r.status_code == 200 and maa_body.get("ad_account_ids") == maa_expect
              and maa_body.get("count") == 3, r.text[:200])
        r = s.get(f"{api}/meta-ads/status")
        check("meta-ads accounts: status reflects the saved list",
              r.status_code == 200 and r.json().get("ad_account_ids") == maa_expect,
              r.text[:200])
        # persisted in the settings document — not merely echoed back
        r = s.get(f"{api}/settings/")
        maa_doc = ((r.json().get("settings") or {}).get("meta_ads") or {})
        check("meta-ads accounts: the list is really persisted in the settings document",
              maa_doc.get("ad_account_ids") == maa_expect, str(maa_doc)[:200])
        check("meta-ads accounts: unrelated meta_ads keys survive the write",
              maa_doc.get("enabled") is True and maa_doc.get("cadence") == "daily"
              and maa_doc.get("match_preference") == "name"
              and maa_doc.get("backfill_days") == 3, str(maa_doc)[:200])

        # -- a later POST replaces the list (never appends) --
        r = s.post(f"{api}/meta-ads/accounts", json={"ad_account_ids": [str(maa_pid + 9)]})
        check("meta-ads accounts: a later POST replaces the saved list",
              r.status_code == 200 and r.json().get("ad_account_ids") == [str(maa_pid + 9)],
              r.text[:150])

        # -- garbage is rejected 400 with the offending value named --
        r = s.post(f"{api}/meta-ads/accounts",
                   json={"ad_account_ids": [str(maa_pid), "not-a-number"]})
        check("meta-ads accounts: a non-numeric id is rejected 400 naming it",
              r.status_code == 400 and "not-a-number" in r.text, r.text[:200])
        r = s.post(f"{api}/meta-ads/accounts", json={"ad_account_ids": [""]})
        check("meta-ads accounts: an empty list entry is rejected 400",
              r.status_code == 400, r.text[:200])
        r = s.get(f"{api}/meta-ads/status")
        check("meta-ads accounts: a rejected POST leaves the saved list untouched",
              r.json().get("ad_account_ids") == [str(maa_pid + 9)], r.text[:150])

        # -- the list is capped at 100 --
        r = s.post(f"{api}/meta-ads/accounts",
                   json={"ad_account_ids": [str(900000 + i) for i in range(150)]})
        maa_capped = r.json().get("ad_account_ids") or [] if r.status_code == 200 else []
        check("meta-ads accounts: the saved list is capped at 100",
              r.status_code == 200 and len(maa_capped) == 100
              and maa_capped[0] == "900000" and maa_capped[-1] == "900099",
              f"{r.status_code} len={len(maa_capped)}")

        # -- a settings-reader without write permission is refused --
        r = s.post(f"{api}/users/", json={
            "username": maa_user, "password": "smokepass1",
            "permissions": {"sections": {"settings": True}, "write": False}})
        maa_uid = (r.json() or {}).get("id")
        check("meta-ads accounts: restricted settings-reader created",
              r.status_code == 200, r.text[:150])
        maa_sess = requests.Session()
        maa_sess.verify = not INSECURE
        maa_sess.post(f"{api}/login", json={"username": maa_user, "password": "smokepass1"})
        r = maa_sess.post(f"{api}/meta-ads/accounts",
                          json={"ad_account_ids": [str(maa_pid + 77)]})
        check("meta-ads accounts: a viewer without write permission is refused (403)",
              r.status_code == 403, str(r.status_code))
    finally:
        # restore the meta_ads block exactly as it was before this block
        s.post(f"{api}/settings/", json={"settings": {"meta_ads": None}})
        if maa_saved is not None:
            s.post(f"{api}/settings/", json={"settings": {"meta_ads": maa_saved}})
        if maa_uid:
            s.delete(f"{api}/users/{maa_uid}")

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
