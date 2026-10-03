#!/bin/bash
# AAA Tracker installer — works with either Docker Compose flavour:
# the modern `docker compose` plugin or the standalone `docker-compose` binary.
set -e

APP_DIR="$HOME/AAA-tracker"
REPO_URL="https://github.com/AayushRd7/AAA-tracker.git"

# ── presentation (colour only on a terminal) ─────────────────────────────────
if [ -t 1 ]; then
    B=$'\033[1m'; D=$'\033[2m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; R=$'\033[0m'
else
    B=; D=; G=; Y=; C=; R=
fi
step() { printf '  %s%s▸%s %s\n' "$C" "$B" "$R" "$1"; }
ok()   { printf '  %s%s✓%s %s\n' "$G" "$B" "$R" "$1"; }
warn() { printf '  %s%s!%s %s\n' "$Y" "$B" "$R" "$1"; }

have() { command -v "$1" >/dev/null 2>&1; }

pkg_install() {
    if have apt-get; then sudo apt-get update -qq && sudo apt-get install -y -qq "$@"
    elif have dnf; then sudo dnf install -y -q "$@"
    elif have yum; then sudo yum install -y -q "$@"
    else return 1; fi
}

compose_cmd() {
    if docker compose version >/dev/null 2>&1; then echo "docker compose"
    else echo "docker-compose"; fi
}

printf '\n%s%s  AAA TRACKER%s %s·%s %s%ssetup%s\n' "$B" "$C" "" "$D" "$R" "$B" "" "$R"
printf '%s  ─────────────────────────────────────────────%s\n\n' "$D" "$R"

read -p "  Domain (empty = HTTP only): " DOMAIN

step "[1/8] system packages"
if ! have docker; then
    if have curl && have apt-get; then
        # Official install docs: https://docs.docker.com/engine/install/
        # Download first, then execute — never pipe a remote script straight into sh.
        curl -fsSL -o /tmp/get-docker.sh https://get.docker.com
        sudo sh /tmp/get-docker.sh
        rm -f /tmp/get-docker.sh
    else
        pkg_install docker || { warn "install Docker manually: https://docs.docker.com/engine/install/"; exit 1; }
    fi
fi
sudo systemctl enable docker >/dev/null 2>&1 || true
sudo systemctl start docker
sudo usermod -aG docker "$USER" >/dev/null 2>&1 || true

step "[2/8] docker compose"
if docker compose version >/dev/null 2>&1; then
    ok "compose plugin already present"
else
    pkg_install docker-compose-plugin >/dev/null 2>&1 || pkg_install docker-compose-v2 >/dev/null 2>&1 || true
    if ! docker compose version >/dev/null 2>&1 && ! have docker-compose; then
        case "$(uname -m)" in
            x86_64|amd64) BIN="linux-x86_64" ;;
            aarch64|arm64) BIN="linux-aarch64" ;;
            *) BIN="linux-x86_64" ;;
        esac
        ok "installing the standalone binary ($BIN)"
        sudo curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-${BIN}" \
            -o /usr/local/bin/docker-compose
        sudo chmod +x /usr/local/bin/docker-compose
    fi
fi
COMPOSE="$(compose_cmd)"
ok "using: $COMPOSE"

step "[3/8] git + make + openssl"
pkg_install git make openssl cron >/dev/null 2>&1 || true

step "[4/8] fetching the project"
mkdir -p "$APP_DIR"
if [ ! -d "$APP_DIR/.git" ]; then
    git clone "$REPO_URL" "$APP_DIR" >/dev/null
fi
cd "$APP_DIR"
git pull --ff-only >/dev/null 2>&1 || true
ok "$APP_DIR"

step "[5/8] permissions"
sudo chown -R "$USER":"$USER" "$APP_DIR"
# Least privilege: source and secrets are never world-writable. Only the
# directories the app/nginx/certbot actually write at runtime get group write.
if [ -f "$APP_DIR/.env" ]; then sudo chmod 600 "$APP_DIR/.env"; fi
if [ -d "$APP_DIR/ssl" ]; then sudo chmod -R go-rwx "$APP_DIR/ssl"; fi
for d in frontend/landings nginx/domains certbot-var letsencrypt; do
    mkdir -p "$APP_DIR/$d"
    sudo chmod 775 "$APP_DIR/$d"
done

step "[6/8] nginx profile"
cp nginx/nginx.nossl.conf nginx/default.conf

step "[7/8] containers + databases"
sudo make install

if [ -n "$DOMAIN" ]; then
    step "[8/8] TLS certificate for $DOMAIN"
    sleep 10
    docker exec tracker_nginx certbot certonly \
        --webroot -w /var/www/certbot -d "$DOMAIN" \
        --agree-tos -m admin@"$DOMAIN" --non-interactive >/dev/null 2>&1 || true
    if docker exec tracker_nginx test -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem"; then
        # The static prod profile points at a placeholder domain; substitute the real
        # one so nginx can load the certificate that was just issued.
        cp nginx/nginx.prod.conf nginx/default.conf
        sed -i "s|/etc/letsencrypt/live/yourdomain.com/|/etc/letsencrypt/live/$DOMAIN/|g" nginx/default.conf
        $COMPOSE restart nginx >/dev/null
        ok "HTTPS enabled for $DOMAIN"
        printf '\n  %s%sdashboard%s   https://%s/backend\n' "$B" "$R" "" "$DOMAIN"
    else
        # Certbot failed: keep the self-signed HTTPS profile nginx is already running
        # so the stack stays up. Retry once DNS resolves here: make install-prod-domain
        warn "certbot failed — staying on the self-signed certificate; check that $DOMAIN resolves here, then retry"
        printf '\n  %s%sdashboard%s   https://%s/backend   %s(self-signed cert)%s\n' "$B" "$R" "" "$DOMAIN" "$D" "$R"
    fi
else
    step "[8/8] HTTP only"
    warn "no domain given — running on HTTP with a self-signed cert"
fi

step "certificate renewal"
# certbot renew runs twice a day, shortly after 03:00 and 15:00 UTC, with up to
# an hour of jitter so a fleet of installs does not hit Let's Encrypt together.
# The script is idempotent: certbot only renews certs within 30 days of expiry.
chmod +x "$APP_DIR/scripts/renew-certs.sh" 2>/dev/null || true
sudo systemctl enable --now cron >/dev/null 2>&1 \
    || sudo systemctl enable --now crond >/dev/null 2>&1 || true
CRON_FILE=/etc/cron.d/aaa-tracker-certrenew
CRON_LINE="17 3,15 * * * root sleep \$((RANDOM \\% 3600)); $APP_DIR/scripts/renew-certs.sh >> /var/log/aaa-tracker-certrenew.log 2>&1"
if [ ! -f "$CRON_FILE" ] || ! grep -qF "$APP_DIR/scripts/renew-certs.sh" "$CRON_FILE"; then
    printf 'SHELL=/bin/bash\n# Managed by AAA Tracker setup.sh - renew TLS certificates twice daily (with jitter).\n%s\n' "$CRON_LINE" \
        | sudo tee "$CRON_FILE" >/dev/null
    sudo chmod 644 "$CRON_FILE"
    ok "renewal cron installed ($CRON_FILE)"
else
    ok "renewal cron already current"
fi

printf '\n  %s%slogin%s       admin password + API token were generated at install and printed once\n' "$B" "$R" ""
printf '  %s%s         %sset AAA_ADMIN_PASSWORD before install to choose the password%s\n' "$D" "$B" "$Y" "$R"
printf '  %shelp%s        make logs · make restart · make update%s\n\n' "$B" "$R" "$R"
