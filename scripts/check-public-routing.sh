#!/usr/bin/env bash
# Verify the public marketing vhost still routes every application path.
#
# The dashboard is served from /backend/, but the tracking plane and the
# landings / landing-editor / domain API answer on ROOT paths of the same host.
# Those are proxied one by one (nginx/_site_locations.conf); if the frontend
# gains a route that is not listed there, the dashboard's JSON call would get
# the static 404 page instead — the "Unexpected token '<'" console error.
#
# This checks each known path is answered by the application, not by the site.
#
# Usage:  scripts/check-public-routing.sh [base-url] [host]
#         scripts/check-public-routing.sh https://localhost aaatracker.website
set -u

BASE="${1:-https://localhost}"
HOST="${2:-aaatracker.website}"
INSECURE="-k"

PATHS=(
  /backend/
  /t.js
  /pb
  /i/smoke
  /p/smoke
  /c/smoke/1
  /optout
  /click-api/smoke
  /simulate/smoke
  /meta-capi/test
  /landings
  /landing/grab
  /landings_editor/1/files
  /domain_update_ssl
)

fail=0
for path in "${PATHS[@]}"; do
  body=$(curl -s $INSECURE -m 15 -o /tmp/_route_body -w '%{http_code}' \
    -X POST -H "Host: $HOST" "$BASE$path" 2>/dev/null || echo "000")
  # A 404 is fine (route exists but the request is incomplete); what must never
  # happen is the marketing site's own pages being returned.
  if grep -q '<!doctype html>' /tmp/_route_body 2>/dev/null \
     && grep -qi 'aaa tracker' /tmp/_route_body 2>/dev/null \
     && [[ "$body" == "404" ]]; then
    echo "  FAIL  $path -> $body (served the marketing site)"
    fail=1
  else
    echo "  ok    $path -> $body"
  fi
done

if [[ $fail -ne 0 ]]; then
  echo
  echo "A root application path is no longer proxied — see nginx/_site_locations.conf"
  exit 1
fi
echo
echo "All application paths are routed."
