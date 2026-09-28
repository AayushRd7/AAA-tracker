#!/bin/bash
# AAA Tracker installer — works with either Docker Compose flavour:
# the modern `docker compose` plugin or the standalone `docker-compose` binary.
set -e

APP_DIR="$HOME/AAA-tracker"
REPO_URL="https://github.com/AayushRd7/AAA-tracker.git"

have() { command -v "$1" >/dev/null 2>&1; }

pkg_install() {
    if have apt-get; then sudo apt-get update -y && sudo apt-get install -y "$@"
    elif have dnf; then sudo dnf install -y "$@"
    elif have yum; then sudo yum install -y "$@"
    else return 1; fi
}

compose_cmd() {
    if docker compose version >/dev/null 2>&1; then echo "docker compose"
    else echo "docker-compose"; fi
}

read -p "Enter domain (empty = HTTP only): " DOMAIN

echo "[1/9] Install Docker"
if ! have docker; then
    if have curl && have apt-get; then
        curl -fsSL https://get.docker.com | sudo sh
    else
        pkg_install docker || { echo "✗ Install Docker manually: https://docs.docker.com/engine/install/"; exit 1; }
    fi
fi
sudo systemctl enable docker >/dev/null 2>&1 || true
sudo systemctl start docker
sudo usermod -aG docker "$USER" || true

echo "[2/9] Install Docker Compose"
if docker compose version >/dev/null 2>&1; then
    echo "  → 'docker compose' plugin already present"
else
    # Prefer the distro/plugin package; fall back to the standalone v2 binary.
    pkg_install docker-compose-plugin >/dev/null 2>&1 || pkg_install docker-compose-v2 >/dev/null 2>&1 || true
    if ! docker compose version >/dev/null 2>&1 && ! have docker-compose; then
        case "$(uname -m)" in
            x86_64|amd64) BIN="linux-x86_64" ;;
            aarch64|arm64) BIN="linux-aarch64" ;;
            *) BIN="linux-x86_64" ;;
        esac
        echo "  → installing standalone docker-compose ($BIN)"
        sudo curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-${BIN}" \
            -o /usr/local/bin/docker-compose
        sudo chmod +x /usr/local/bin/docker-compose
    fi
fi
COMPOSE="$(compose_cmd)"
echo "  → using: $COMPOSE"

echo "[3/9] Install git + make + openssl"
pkg_install git make openssl || true

echo "[4/9] Clone project"
mkdir -p "$APP_DIR"
if [ ! -d "$APP_DIR/.git" ]; then
    git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
git pull --ff-only || true

echo "[5/9] Env & permissions"
# make env creates .env from .env.example with generated secrets (never overwrites)
make env
sudo chown -R "$USER":"$USER" "$APP_DIR"
sudo chmod -R 0777 "$APP_DIR"

echo "[6/9] Use nginx NO-SSL config"
cp nginx/nginx.nossl.conf nginx/default.conf

echo "[7/9] Generate local cert + start containers (HTTP)"
sudo make install

if [ -n "$DOMAIN" ]; then
    echo "[8/9] Issue SSL certificate for $DOMAIN"
    sleep 10
    docker exec tracker_nginx certbot certonly \
        --webroot \
        -w /var/www/certbot \
        -d "$DOMAIN" \
        --agree-tos \
        -m admin@"$DOMAIN" \
        --non-interactive
    echo "→ Switch nginx to SSL config"
    cp nginx/nginx.prod.conf nginx/default.conf
    $COMPOSE restart nginx
else
    echo "[8/9] ⚠️  Domain empty — running HTTP only"
fi

echo "[9/9] ✅ Setup complete"
echo "🌐 Server public IP address:"
echo "https://$(curl -s https://checkip.amazonaws.com)/backend"
echo "tracker_admin / admin — please change the password after first login"