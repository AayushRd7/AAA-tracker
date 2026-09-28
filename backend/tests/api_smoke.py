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
    r = requests.get(f"{BASE}/pb/{dt_click}/sale/3.25", verify=not INSECURE)
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
    r = requests.get(f"{BASE}/pb/{ltv_click}/sale/5", verify=not INSECURE)
    check("LTV: first sale postback ok", r.status_code == 200 and r.json().get("duplicate") is False,
          r.text[:150])
    r = requests.get(f"{BASE}/pb/{ltv_click}/sale/5", verify=not INSECURE)
    check("LTV: identical refire within 60s dedupes", r.status_code == 200
          and r.json().get("duplicate") is True, r.text[:150])
    r = requests.get(f"{BASE}/pb/{ltv_click}/sale/5",
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
    r = requests.get(f"{BASE}/pb/{cs_click}/Registration%20Bonus/2.5", verify=not INSECURE)
    check("postback accepts custom status (weird casing/spaces)", r.status_code == 200
          and r.json().get("status") == "ok", r.text[:150])
    rec = poll_first({"click_id": cs_click})
    check("custom status normalized and stored", rec and rec["status"] == "registration_bonus",
          str(rec and rec.get("status")))
    if rec:
        econ_conv_ids.append(rec["id"])
    r = requests.get(f"{BASE}/pb/{cs_click}/definitely_not_a_status/1", verify=not INSECURE)
    check("unknown status still rejected", r.status_code == 400, f"{r.status_code} {r.text[:80]}")
    r = s.get(f"{api}/reports/", params={"status": "registration_bonus"})
    check("reports filters by custom status", r.status_code == 200
          and any(c["click_id"] == cs_click for c in r.json()), r.text[:150])

    # -- G20: clickless conversions --
    cl_tid = f"smoke-clickless-{os.getpid()}"
    r = requests.get(f"{BASE}/pb/none/sale/2", params={"transaction_id": cl_tid}, verify=not INSECURE)
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
    r = requests.get(f"{BASE}/pb/none/sale/4", params={"sub_id_1": sub_token}, verify=not INSECURE)
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
        requests.get(f"{BASE}/pb/{cap_match.group(1)}/sale/5", verify=not INSECURE)
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
        requests.get(f"{BASE}/pb/{capb_match.group(1)}/sale/5", verify=not INSECURE)
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
    requests.get(f"{BASE}/pb/{pb_click}/sale/2", verify=not INSECURE)
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
    requests.get(f"{BASE}/pb/{conc_click}/sale/2.5", verify=not INSECURE)
    errors = []

    def fire_pb():
        try:
            requests.get(f"{BASE}/pb/{conc_click}/sale/2.5", verify=not INSECURE, timeout=30)
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
        r = requests.get(f"{BASE}/pb/{fn_click_ids[1]}/sale/9.5", verify=not INSECURE)
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
          set((body.get("loops") or {}).keys()) == {"monitor", "rules", "optimizer"},
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
        r = requests.get(f"{BASE}/pb/{cp_click_id}/sale/10", verify=not INSECURE)
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
        r = requests.get(f"{BASE}/pb/{cp_click_id}/sale/2,25", verify=not INSECURE)
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
            requests.get(f"{BASE}/pb/{m.group(1)}/sale/5", verify=not INSECURE)
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
        requests.get(f"{BASE}/pb/smoke-cpadmin-conv-{cp_pid}/sale/7", verify=not INSECURE)
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
    r = fx_anon.get(f"{BASE}/pb/{fx_ch_click}/sale/3.5")
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
    r = fx_anon.get(f"{BASE}/pb/smoke-sec-nan-{fx_pid}/sale/nan")
    check("sec: /pb NaN payout -> 400", r.status_code == 400, str(r.status_code))
    r = fx_anon.get(f"{BASE}/pb/smoke-sec-inf-{fx_pid}/sale/Infinity")
    check("sec: /pb Infinity payout -> 400", r.status_code == 400, str(r.status_code))
    r = fx_anon.get(f"{BASE}/p/{fx_alias}",
                    params={"click_id": f"smoke-sec-pnan-{fx_pid}", "payout": "nan", "fmt": "json"})
    check("sec: /p NaN payout -> 400", r.status_code == 400, str(r.status_code))

    # --- P2: over-long click_id / sub_id values accepted and truncated ---
    fx_long = "L" * 300
    r = fx_anon.get(f"{BASE}/pb/{fx_long}/sale/1", params={"sub_id_1": "S" * 200})
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
    r = fx_anon.get(f"{BASE}/pb/smoke-sec-key1-{fx_pid}/sale/1", params={"key": "é"})
    check("sec: non-ASCII postback key -> 403 (not 500)", r.status_code == 403, str(r.status_code))
    r = fx_anon.get(f"{BASE}/pb/smoke-sec-key2-{fx_pid}/sale/1", params={"key": "smoke-sec-key"})
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
    r = requests.get(f"{BASE}/pb/{g8_rej}/sale/5", verify=not INSECURE)
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
        requests.get(f"{BASE}/pb/{g8_rej2}/sale/5", verify=not INSECURE)
        _g8time.sleep(3)
        check("g85: reject suppresses fanout", g8_paths(g8_rej2) == [], str(g8_paths(g8_rej2)))

    g8_ss = f"g85status-{g8_pid}"
    requests.get(f"{BASE}/pb/{g8_ss}/sale/5", verify=not INSECURE)
    check("g85: set_status remaps the written status",
          g8_db(f"SELECT status FROM conversions_data WHERE click_id='{g8_ss}'") == "rejected",
          g8_db(f"SELECT status FROM conversions_data WHERE click_id='{g8_ss}'"))

    g8_pa = f"g85payout-{g8_pid}"
    requests.get(f"{BASE}/pb/{g8_pa}/sale/2", verify=not INSECURE)
    check("g85: set_payout absolute value applied",
          g8_db(f"SELECT payout FROM conversions_data WHERE click_id='{g8_pa}'") == "12.5",
          g8_db(f"SELECT payout FROM conversions_data WHERE click_id='{g8_pa}'"))
    g8_pm = f"g85mult-{g8_pid}"
    requests.get(f"{BASE}/pb/{g8_pm}/sale/2", verify=not INSECURE)
    check("g85: set_payout multiplier applied",
          g8_db(f"SELECT payout FROM conversions_data WHERE click_id='{g8_pm}'") == "6",
          g8_db(f"SELECT payout FROM conversions_data WHERE click_id='{g8_pm}'"))

    g8_dis = f"g85dis-{g8_pid}"
    r = requests.get(f"{BASE}/pb/{g8_dis}/sale/5", verify=not INSECURE)
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
            requests.get(f"{BASE}/pb/{cl}/lead/1", verify=not INSECURE)
            requests.get(f"{BASE}/pb/{cl}/sale/2", verify=not INSECURE)
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
            requests.get(f"{BASE}/pb/{cl}/sale/1", verify=not INSECURE)
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
        requests.get(f"{BASE}/pb/{su}/sale/1", verify=not INSECURE)
        uu = f"g86up-ups-{g8_pid}"
        g8_seed(uu, g8_cid_upsell)
        requests.get(f"{BASE}/pb/{uu}/upsale/1", verify=not INSECURE)
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
        requests.get(f"{BASE}/pb/{g8_tok}/sale/7.5", verify=not INSECURE)
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
    r = requests.head(f"{BASE}/pb/{g8_head}/sale/1.5", verify=not INSECURE, allow_redirects=False)
    check("g87: HEAD /pb returns 200", r.status_code == 200, str(r.status_code))
    check("g87: HEAD /pb writes nothing",
          g8_db(f"SELECT count(*) FROM conversions_data WHERE click_id='{g8_head}'") == "0",
          g8_db(f"SELECT count(*) FROM conversions_data WHERE click_id='{g8_head}'"))
    r = requests.head(f"{BASE}/pb/{g8_head}/definitely_not_a_status/1.5", verify=not INSECURE)
    check("g87: HEAD /pb keeps status validation (400)", r.status_code == 400, str(r.status_code))

    g8_postj = f"g87postj-{g8_pid}"
    r = requests.post(f"{BASE}/pb/{g8_postj}/sale/2.25",
                      json={"sub_id_1": f"json-{g8_pid}"}, verify=not INSECURE)
    check("g87: POST /pb with JSON body accepted",
          r.status_code == 200 and r.json().get("updated_status") == "sale", r.text[:160])
    check("g87: POST JSON extra field recorded",
          g8_db(f"SELECT sub_id_1 FROM conversions_data WHERE click_id='{g8_postj}'")
          == f"json-{g8_pid}",
          g8_db(f"SELECT sub_id_1 FROM conversions_data WHERE click_id='{g8_postj}'"))
    g8_postf = f"g87postf-{g8_pid}"
    r = requests.post(f"{BASE}/pb/{g8_postf}/lead/1.25",
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
        requests.get(f"{BASE}/pb/{_ck}/{_st}/1", verify=not INSECURE)
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
    requests.get(f"{BASE}/pb/{capi_dis_click}/sale/5", verify=not INSECURE)
    _capi_time.sleep(2.5)
    check("meta-capi: disabled sends nothing",
          capi_paths(f"smoke-meta-dis-{capi_pid}") == [], str(capi_paths(""))[:150])

    # -- dry_run -> payload built + logged, no HTTP call --
    capi_dry_click = f"smoke-meta-dry-{capi_pid}"
    capi_seed(capi_dry_click, fbc="fb.1.1.smokedryfbc", fbp="fb.1.2.smokedryfbp")
    r = capi_set(enabled=True, dry_run=True, dataset_id=f"smoke-meta-dry-{capi_pid}")
    check("meta-capi: dry-run config saved", r.status_code == 200, r.text[:120])
    settle_settings_cache()
    requests.get(f"{BASE}/pb/{capi_dry_click}/sale/5", verify=not INSECURE)
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
    r = requests.get(f"{BASE}/pb/{capi_live_click}/sale/5",
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
    requests.get(f"{BASE}/pb/{capi_rej_click}/rejected/1", verify=not INSECURE)
    _capi_time.sleep(2.5)
    check("meta-capi: status outside send_statuses is not sent",
          len(capi_paths(capi_live_ds)) == before_rej, str(capi_paths(capi_live_ds))[:150])

    # -- duplicate click_id+status -> exactly one send --
    capi_dup_click = f"smoke-meta-dup-{capi_pid}"
    capi_seed(capi_dup_click)
    requests.get(f"{BASE}/pb/{capi_dup_click}/sale/5", verify=not INSECURE)
    capi_wait_click(capi_dup_click, 1)
    # different payout => not a row-level duplicate, but the CAPI (click,status)
    # dedupe must still suppress the second send
    requests.get(f"{BASE}/pb/{capi_dup_click}/sale/9", verify=not INSECURE)
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
    r = requests.get(f"{BASE}/pb/{capi_fail_click}/sale/3", verify=not INSECURE)
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
    requests.get(f"{BASE}/pb/{px_chan_click}/sale/5", verify=not INSECURE)
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
    requests.get(f"{BASE}/pb/{px_none_click}/sale/5", verify=not INSECURE)
    _capi_time.sleep(2.0)
    check("capi-pixels: unbound conversion sends nothing",
          capi_click_count(px_none_click) == 0, f"count={capi_click_count(px_none_click)}")

    # -- offer binding: all of that offer's conversions use the pixel --
    r = px_bind("offer", px_offer, [pxb["id"]])
    check("capi-pixels: offer binding saved", r.status_code == 200, r.text[:150])
    px_offer_click = f"{px_tag}-offer-click"
    px_seed(px_offer_click, cid=px_plain_cid, offer_id=px_offer, status="sale", payout=5)
    requests.get(f"{BASE}/pb/{px_offer_click}/sale/5", verify=not INSECURE)
    capi_wait_click(px_offer_click, 1)
    check("capi-pixels: offer-bound conversion sent to its pixel",
          capi_click_count(px_offer_click) == 1
          and capi_paths(f"{px_tag}-dsB") != [],
          f"count={capi_click_count(px_offer_click)} paths={capi_paths(px_tag)}")

    # -- bound at both levels -> exactly one send (channel resolves first) --
    px_both_click = f"{px_tag}-both-click"
    px_seed(px_both_click, cid=px_chan_cid, offer_id=px_offer, status="sale", payout=5)
    requests.get(f"{BASE}/pb/{px_both_click}/sale/5", verify=not INSECURE)
    capi_wait_click(px_both_click, 1)
    _capi_time.sleep(1.5)
    check("capi-pixels: pixel bound at both levels sends exactly once",
          capi_click_count(px_both_click) == 1,
          f"count={capi_click_count(px_both_click)}")

    # -- inactive pixel skipped --
    r = px_bind("channel", px_src2, [pxc["id"]], active=True)
    px_off_click = f"{px_tag}-off-click"
    px_seed(px_off_click, cid=px_off_cid, status="sale", payout=5)
    requests.get(f"{BASE}/pb/{px_off_click}/sale/5", verify=not INSECURE)
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
    requests.get(f"{BASE}/pb/{px_dry_click}/sale/5", verify=not INSECURE)
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
    r = requests.get(f"{BASE}/pb/{qp_path}/sale/1.25", verify=not INSECURE)
    check("qp: path-style /pb still works",
          r.status_code == 200 and r.json().get("duplicate") is False, r.text[:150])
    r = requests.get(f"{BASE}/pb/{qp_path}/sale/1.25", verify=not INSECURE)
    check("qp: path-style identical refire still dedupes",
          r.status_code == 200 and r.json().get("duplicate") is True, r.text[:150])

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
    requests.get(f"{BASE}/pb/{qp_new_click}/{qp_new_status}/2",
                 params={"transaction_id": f"{qp_tag}-n1"}, verify=not INSECURE)
    r = requests.get(f"{BASE}/pb/{qp_new_click}/{qp_new_status}/3",
                     params={"transaction_id": f"{qp_tag}-n2"}, verify=not INSECURE)
    check("qp: mode=new writes a second conversion row",
          r.status_code == 200 and r.json().get("duplicate") is False and qp_count(qp_new_click) == "2",
          f"{r.text[:100]} rows={qp_count(qp_new_click)}")
    qp_payouts = pg_query(f"SELECT string_agg(payout::text, ',' ORDER BY received_at) "
                          f"FROM conversions_data WHERE click_id='{qp_new_click}'")
    check("qp: mode=new keeps both payouts on separate rows", qp_payouts == "2,3", qp_payouts)

    r = requests.get(f"{BASE}/pb/{qp_new_click}/{qp_new_status}/3",
                     params={"transaction_id": f"{qp_tag}-n2"}, verify=not INSECURE)
    check("qp: mode=new still dedupes a repeated transaction id",
          r.status_code == 200 and r.json().get("duplicate") is True and qp_count(qp_new_click) == "2",
          f"{r.text[:100]} rows={qp_count(qp_new_click)}")

    r = requests.get(f"{BASE}/pb/{qp_new_click}/{qp_new_status}/3",
                     params={"transaction_id": f"{qp_tag}-n3"}, verify=not INSECURE)
    check("qp: mode=new fresh transaction id not swallowed by the 60s dedupe",
          r.status_code == 200 and r.json().get("duplicate") is False and qp_count(qp_new_click) == "3",
          f"{r.text[:100]} rows={qp_count(qp_new_click)}")

    qp_rep_click = f"{qp_tag}-rep"
    requests.get(f"{BASE}/pb/{qp_rep_click}/{qp_rep_status}/2",
                 params={"transaction_id": f"{qp_tag}-r1"}, verify=not INSECURE)
    r = requests.get(f"{BASE}/pb/{qp_rep_click}/{qp_rep_status}/3",
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
