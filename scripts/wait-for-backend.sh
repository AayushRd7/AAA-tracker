#!/usr/bin/env bash
# Wait until the backend's auth gate answers through nginx (401 = up and authenticated).
# Exit 0 when up, 1 when it never came back. Prints the last status code on failure.
set -u

for _ in $(seq 1 "${1:-25}"); do
    code=$(curl -s -o /dev/null -m 2 -w '%{http_code}' http://localhost/backend/api/status 2>/dev/null || echo "---")
    if [ "$code" = "401" ]; then
        exit 0
    fi
    sleep 2
done

echo "${code:-timeout}" >&2
exit 1
