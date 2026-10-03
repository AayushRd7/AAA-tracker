#!/usr/bin/env bash
# Renew the Let's Encrypt certificates for every domain this tracker serves.
#
# `certbot renew` runs INSIDE the nginx container, because that is where certbot
# and the webroot / letsencrypt volumes live — the same container the app shells
# into from frontend/domains.py. certbot only acts on certificates within 30
# days of expiry, so running this twice a day is cheap and safe to repeat.
#
#   * --deploy-hook "nginx -s reload" runs only when a certificate was actually
#     renewed, so nginx is never reloaded on a no-op run.
#   * CERTBOT_STAGING=1 adds --staging, so a flaky DNS setup can be tested
#     against the staging CA without burning production rate limits.
#
# Usage:  scripts/renew-certs.sh
#         CERTBOT_STAGING=1 scripts/renew-certs.sh
set -euo pipefail

CONTAINER="${NGINX_CONTAINER:-tracker_nginx}"
WEBROOT="${CERTBOT_WEBROOT:-/var/www/certbot}"

# --deploy-hook is passed as a single argument so the command the container runs
# is `nginx -s reload`, only after a successful renewal.
certbot_args=(renew --webroot -w "$WEBROOT" --non-interactive
              --deploy-hook "nginx -s reload")

case "${CERTBOT_STAGING:-0}" in
    1|true|yes|on)
        certbot_args+=(--staging)
        echo "[cert-renew] staging CA requested (CERTBOT_STAGING=${CERTBOT_STAGING})"
        ;;
esac

echo "[cert-renew] $(date -u '+%Y-%m-%dT%H:%M:%SZ') running certbot renew in $CONTAINER"

if docker exec "$CONTAINER" certbot "${certbot_args[@]}"; then
    echo "[cert-renew] $(date -u '+%Y-%m-%dT%H:%M:%SZ') done"
else
    status=$?
    echo "[cert-renew] $(date -u '+%Y-%m-%dT%H:%M:%SZ') certbot renew failed (exit $status)" >&2
    exit "$status"
fi
