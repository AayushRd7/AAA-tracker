from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

import socket
import subprocess
from pathlib import Path

import asyncio

router = APIRouter()


def _resolve(domain: str) -> list:
    try:
        infos = socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
        return sorted({i[4][0] for i in infos})
    except OSError:
        return []


def _local_ips() -> set:
    ips = {"127.0.0.1", "::1"}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return ips


@router.get("/domain_ping", response_class=PlainTextResponse)
def ping():
    return "OK"


@router.get("/domain_update_ssl")
async def create_nginx(request: Request, domain_id: int):
    pg = request.app.state.pg

    async with pg.acquire() as conn:
        row = await conn.fetchrow("""
                                  SELECT domain
                                  FROM domains
                                  WHERE id = $1
                                  """, domain_id)

    if not row:
        raise HTTPException(status_code=404, detail="Domain not found")

    async with pg.acquire() as conn:
        await conn.fetchrow("""
                            UPDATE domains
                            SET updated_at = NOW(),
                                ssl_status= 'pending'
                            WHERE id = $1
                            """, domain_id)

    domain = row["domain"]

    # Pre-flight: the Let's Encrypt HTTP-01 challenge is fetched over plain
    # HTTP at the DOMAIN, so the domain must already route here. Fail with a
    # clear message instead of a 10-minute certbot timeout.
    addresses = _resolve(domain)
    if not addresses:
        await _mark_ssl_status(pg, domain_id, "error")
        raise HTTPException(
            status_code=400,
            detail=f"{domain} does not resolve yet — create a DNS record "
                   f"(CNAME to your tracker host, or A record to this server's "
                   f"public IP), wait for propagation, then retry.")
    if not (set(addresses) & _local_ips()):
        await _mark_ssl_status(pg, domain_id, "error")
        raise HTTPException(
            status_code=400,
            detail=f"{domain} resolves to {', '.join(addresses)} which is not "
                   f"this server — point it at this machine first, then retry.")

    try:
        path = await generate_nginx_conf(domain, domain_id)
    except HTTPException:
        await _mark_ssl_status(pg, domain_id, "error")
        raise

    if path:
        async with pg.acquire() as conn:
            await conn.fetchrow("""
                                UPDATE domains
                                SET updated_at = NOW(),
                                    ssl_status= 'success'
                                WHERE id = $1
                                """, domain_id)

    return {"status": "ok", "file": str(path)}


async def _mark_ssl_status(pg, domain_id: int, status: str):
    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE domains SET updated_at = NOW(), ssl_status = $2 WHERE id = $1",
            domain_id, status)


async def request_ssl_letsencrypt(domain: str) -> bool:
    try:
        email = f"admin@{domain}"

        result = subprocess.run([
            "certbot", "certonly", "--webroot",
            "-w", "/var/www/certbot",  # webroot for the ACME challenge
            "-d", domain,
            "--agree-tos",
            "--email", email,
            "--non-interactive"
        ], check=True)

        cert_path = Path(f"/etc/letsencrypt/live/{domain}/fullchain.pem")

        for _ in range(300):  # 10 minutes
            if cert_path.exists():
                print(f"✅ Certificate found: {cert_path}")
                return True
            await asyncio.sleep(2)

        return cert_path.exists()

    except subprocess.CalledProcessError as e:
        print(f"❌ Certbot failed for {domain}: {e}")
        return False


def reload_nginx():
    # subprocess.run(["docker", "stop", "tracker_nginx"], check=True)
    # subprocess.run(["docker", "start", "tracker_nginx"], check=True)
    subprocess.run([
        "docker", "exec", "tracker_nginx", "nginx", "-s", "reload"
    ])


@router.get("/_reload_nginx")
def show_logs():
    reload_nginx()


async def generate_nginx_conf(domain: str, domain_id: int) -> Path:
    template_path = Path("/var/www/nginx/_domain_nginx.prod.conf")
    output_dir = Path("/var/www/nginx/domains")

    if not template_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_path}")

    if await request_ssl_letsencrypt(domain):
        # Read the template
        content = template_path.read_text()

        # Replace the placeholder
        updated = content.replace("server_name _;", f"server_name {domain};")
        updated = updated.replace("yourdomain.com", domain)

        # Build the target path
        output_dir.mkdir(parents=True, exist_ok=True)
        target_path = output_dir / f"{domain_id}_{domain}.conf"

        if not target_path.exists() or target_path.read_text() != updated:
            target_path.write_text(updated)
            reload_nginx()
            print(f"✅ Updated: {target_path}")
        else:
            print(f"ℹ️ No changes: {target_path}")
    else:
        raise HTTPException(status_code=500, detail="SSL certificate request failed")

    return target_path
