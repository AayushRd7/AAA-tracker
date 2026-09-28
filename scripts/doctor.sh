#!/usr/bin/env bash
# AAA Tracker doctor — checks the running install end to end and prints what is wrong.
# Usage: make doctor   (or: bash scripts/doctor.sh)
set -u

if [ -t 1 ]; then
    B=$'\033[1m'; D=$'\033[2m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; RD=$'\033[31m'; R=$'\033[0m'
else
    B=; D=; G=; Y=; C=; RD=; R=
fi
ok()   { printf '  %s%s✓%s %s\n' "$G" "$B" "$R" "$1"; }
bad()  { printf '  %s%s✗%s %s\n' "$RD" "$B" "$R" "$1"; FAILED=1; }
warn() { printf '  %s%s!%s %s\n' "$Y" "$B" "$R" "$1"; }
hdr()  { printf '\n  %s%s%s%s\n' "$B" "$C" "$1" "$R"; }
FAILED=0
USER_NAME="${TEST_USER:-tracker_admin}"
USER_PASS="${TEST_PASS:-admin}"

printf '\n  %s%s  AAA TRACKER%s %s·%s %s%sdoctor%s\n' "$B" "$C" "" "$D" "$R" "$B" "" "$R"
printf '%s  ─────────────────────────────────────────────%s\n' "$D" "$R"

hdr "containers"
for c in tracker_postgres tracker_clickhouse tracker_backend tracker_frontend tracker_nginx; do
    state=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$c" 2>/dev/null || true)
    if [ "$state" = "running" ]; then
        ok "$c (running${health:+, $health})"
    else
        bad "$c is $state — try: make restart"
    fi
done

hdr "database (from the backend container)"
db_line=$(docker exec tracker_backend python3 -c "
import os, psycopg2
try:
    c = psycopg2.connect(host=os.getenv('POSTGRES_HOST','tracker_postgres'), port=os.getenv('POSTGRES_PORT','5432'),
                         dbname=os.getenv('POSTGRES_DB','db'), user=os.getenv('POSTGRES_USER','user'),
                         password=os.getenv('POSTGRES_PASSWORD',''))
    cur = c.cursor()
    cur.execute(\"SELECT count(*) FROM information_schema.tables WHERE table_schema='public'\")
    t = cur.fetchone()[0]
    cur.execute('SELECT count(*) FROM users')
    u = cur.fetchone()[0]
    cur.execute(\"SELECT count(*) FROM users WHERE username='tracker_admin' AND active\")
    a = cur.fetchone()[0]
    print('OK tables=%d users=%d active_admin=%d' % (t, u, a))
except Exception as e:
    print('ERR %s: %s' % (type(e).__name__, e))
" 2>&1 | tail -1)

case "$db_line" in
    OK*) ok "reachable — ${db_line#OK }"
         case "$db_line" in *active_admin=0*) bad "no active 'tracker_admin' — run: make install-db" ;; esac ;;
    *)   bad "cannot reach Postgres — ${db_line#ERR }" ;;
esac

hdr "http endpoints"
code80=$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://localhost/backend/api/status 2>/dev/null || echo "---")
[ "$code80" = "401" ] && ok "http  /backend/api/status -> 401 (auth gate live)" || warn "http  /backend/api/status -> $code80 (401 expected)"
code443=$(curl -sk -o /dev/null -m 5 -w '%{http_code}' https://localhost/backend/api/status 2>/dev/null || echo "---")
[ "$code443" = "401" ] && ok "https /backend/api/status -> 401 (auth gate live)" || warn "https /backend/api/status -> $code443 (401 expected)"
root=$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://localhost/ 2>/dev/null || echo "---")
case "$root" in 301|302) ok "bare host -> dashboard redirect ($root)" ;; *) warn "bare host returned $root (expected 3xx)" ;; esac

# A 502 through nginx while the backend is healthy almost always means nginx cached the
# upstream address from before a container recreate.
if [ "$code80" = "502" ] || [ "$code443" = "502" ]; then
    inside=$(docker exec tracker_nginx sh -c "wget -q -O- -S http://tracker_backend:8501/api/status 2>&1 | head -1" 2>/dev/null | tr -d '\r')
    case "$inside" in
        *401*) bad "nginx cannot reach the backend, but the backend answers directly — stale upstream address; run: make reload-nginx" ;;
        *)     bad "the backend is unreachable from the nginx container too — see the backend log below" ;;
    esac
fi

hdr "tls"
if [ -s ssl/selfsigned.crt ] && [ -s ssl/selfsigned.key ]; then
    cm=$(openssl x509 -noout -modulus -in ssl/selfsigned.crt 2>/dev/null | openssl md5)
    km=$(openssl rsa  -noout -modulus -in ssl/selfsigned.key 2>/dev/null | openssl md5)
    if [ -n "$cm" ] && [ "$cm" = "$km" ]; then
        ok "self-signed certificate and key match"
    else
        bad "certificate and key do NOT match — nginx cannot reload (emerg: key values mismatch). Fix: rm -f ssl/selfsigned.crt ssl/selfsigned.key && make generate-local-cert && make reload-nginx"
    fi
elif [ ! -s ssl/selfsigned.crt ]; then
    warn "no self-signed pair on disk (expected only if you use a real domain certificate)"
fi

hdr "login"
probe_login() {
    # prints its report on stderr and the bare HTTP code on stdout (captured by the caller)
    scheme="$1"; insecure="$2"
    raw=$(curl -s $insecure -i -m 8 -X POST "$scheme://localhost/backend/api/login" \
              -H 'Content-Type: application/json' \
              -d "{\"username\":\"$USER_NAME\",\"password\":\"$USER_PASS\"}" 2>/dev/null)
    code=$(printf '%s' "$raw" | head -1 | awk '{print $2}')
    cookie=$(printf '%s' "$raw" | grep -i '^set-cookie' | head -1)
    body=$(printf '%s' "$raw" | tail -1 | cut -c1-140)
    case "$code" in
        200) if printf '%s' "$cookie" | grep -qi 'secure'; then flag="cookie has Secure"; else flag="cookie without Secure"; fi
             if [ "$scheme" = "http" ] && printf '%s' "$cookie" | grep -qi 'secure'; then
                 bad "$scheme: 200 but the cookie is Secure — browsers drop it over http (upgrade nginx/auth)" >&2
             else
                 ok "$scheme: 200, session cookie issued ($flag)" >&2
             fi ;;
        401) warn "$scheme: 401 — wrong credentials (default is tracker_admin / admin)" >&2 ;;
        403) warn "$scheme: 403 — blocked; is the login IP whitelist set? ${body}" >&2 ;;
        429) warn "$scheme: 429 — too many attempts, wait a minute" >&2 ;;
        500|502|503|"") bad "$scheme: ${code:-no response} — backend error. ${body}" >&2 ;;
        *) warn "$scheme: $code ${body}" >&2 ;;
    esac
    printf '%s' "$code"
}
login_http=$(probe_login http "")
printf '\n'
login_https=$(probe_login https "-k")

if [ "$login_http" = "500" ] || [ "$login_https" = "500" ]; then
    hdr "backend traceback (last errors)"
    docker logs tracker_backend --tail 80 2>&1 \
        | grep -iE 'traceback|error|exception|psycopg2|asyncpg|sqlalchemy|column|relation' \
        | tail -15 | sed 's/^/    /'
fi

hdr "recent backend log"
docker logs tracker_backend --tail 6 2>&1 | sed 's/^/    /'

printf '\n'
if [ "$FAILED" = "1" ]; then
    printf '  %s%s✗ problems found above%s\n\n' "$RD" "$B" "$R"
    exit 1
fi
printf '  %s%s✓ no problems detected%s\n\n' "$G" "$B" "$R"
