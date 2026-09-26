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
    payload["config"]["flows"][0]["weight"] = 0
    payload["config"]["flows"][1]["weight"] = 100
    s.put(f"{api}/campaigns/{sticky_id}", json=payload)
    loc4 = sess2.get(f"{BASE}/{sticky_alias}", headers=ua_sticky,
                     allow_redirects=False).headers.get("location") or ""
    check("sticky: flow edit invalidates binding (routes offer B)",
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

    today = datetime.now().date()
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
        f"SELECT toDateTime(toDate(now()) - 1) + toIntervalHour(number % 24), 999, true, '', "
        f"'seed-rd-{cmp_pid}-cur-' || toString(number), 'US', 0.01 FROM numbers(60)")
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, country, cost) "
        f"SELECT toDateTime(toDate(now()) - 3) + toIntervalHour(number), 999, true, '', "
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
         f"DELETE FROM conversions_data WHERE visitor_id LIKE 'seed-rd-{basis_pid}-basis'"],
        capture_output=True, text=True, timeout=30)
    leftover = ch_query("SELECT count() FROM clicks_data WHERE visitor_id LIKE 'seed-%'")
    check("seeded CH rows removed", leftover == "0", leftover[:80])

    print("== Regression: reporting polish ==")
    # Seed isolated campaign 776: 10 visits, 4 clicks, 2 rejected.
    ch_query(
        f"INSERT INTO clicks_data (received_at, campaign_id, click, status, visitor_id, country, cost) "
        f"SELECT now(), 776, number < 4, if(number IN (4, 5), 'rejected', ''), "
        f"'seed-g5-{os.getpid()}-' || toString(number), 'US', 0.01 FROM numbers(10)")
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
          r.status_code == 200 and "Shared Report" in r.text, f"{r.status_code}")
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

        codes = [requests.post(f"{api}/login/totp",
                               json={"totp_token": ttok, "code": "000000"},
                               verify=not INSECURE).status_code for _ in range(3)]
        check("2FA: wrong codes -> 401 x3", codes == [401, 401, 401], str(codes))
        r = requests.post(f"{api}/login/totp",
                          json={"totp_token": ttok, "code": pyotp.TOTP(sec_secret).now()},
                          verify=not INSECURE)
        check("2FA: token burned after 3 fails -> 429",
              r.status_code == 429 and "Too many TOTP attempts" in r.text, r.text[:150])

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
          and r.text.startswith("id,name,alias"), r.headers.get("content-type", ""))
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
