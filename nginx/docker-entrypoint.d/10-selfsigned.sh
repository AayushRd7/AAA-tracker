#!/bin/sh
# Create the self-signed certificate inside the container if it is missing or if the
# certificate and key do not match. Runs before nginx starts (nginx image entrypoint),
# so an HTTPS server block can never fail on a missing/invalid pair -- which otherwise
# shows up as "cannot load certificate ... no such file" or "key values mismatch" when
# the host-side ssl/ directory is empty, wiped, or not the one that is mounted.
set -e

CERT=/etc/nginx/ssl/selfsigned.crt
KEY=/etc/nginx/ssl/selfsigned.key

matches() {
    [ -s "$CERT" ] && [ -s "$KEY" ] || return 1
    [ "$(openssl x509 -noout -modulus -in "$CERT" 2>/dev/null | openssl md5)" = \
      "$(openssl rsa  -noout -modulus -in "$KEY"  2>/dev/null | openssl md5)" ]
}

if matches; then
    echo "[selfsigned] existing certificate and key are valid — keeping them"
    exit 0
fi

mkdir -p /etc/nginx/ssl

IP=$(hostname -i 2>/dev/null | awk '{print $1}')
SAN="DNS:localhost,IP:127.0.0.1"
[ -n "$IP" ] && SAN="$SAN,IP:$IP"

echo "[selfsigned] generating a self-signed certificate (SAN: $SAN)"
rm -f "$CERT" "$KEY"
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout "$KEY" -out "$CERT" \
    -subj "/C=US/ST=Local/L=Local/O=Dev/OU=Dev/CN=localhost" \
    -addext "subjectAltName=$SAN" 2>/dev/null

if matches; then
    echo "[selfsigned] certificate ready"
else
    echo "[selfsigned] WARNING: generated certificate and key do not match" >&2
fi
