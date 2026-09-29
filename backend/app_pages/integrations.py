"""Ad-platform OAuth "Connect" flow (platform-generic, Meta first).

Lets an operator connect the tracker to an ad platform without hand-copying a
token: an OAuth consent redirect, a server-side code exchange, and an
*encrypted* stored token the asset-discovery endpoints read back. Meta is
implemented; Snapchat / TikTok / Pinterest / Google are declared (so the UI can
list them) but not implemented.

APP CREDENTIALS COME FROM THE ENVIRONMENT — never the database or the UI
-----------------------------------------------------------------------
Per-platform app credentials are read from environment variables (see
``CREDENTIAL_ENV``): ``META_APP_ID`` / ``META_APP_SECRET``, and the
``*_CLIENT_ID`` / ``*_APP_ID`` / ``*_SECRET`` names for the others. There is no
``client_id`` / ``client_secret`` anywhere in the settings document, in any API
response, or in the Settings UI. ``PUBLIC_BASE_URL`` (the public origin) drives
the callback URL; ``INTEGRATIONS_ENCRYPTION_KEY`` drives token encryption.

TOKEN ENCRYPTION AT REST
------------------------
Stored tokens are encrypted with ``cryptography``'s Fernet. The key is
``INTEGRATIONS_ENCRYPTION_KEY``: a real Fernet key is used as-is; any other
non-empty passphrase is deterministically derived into a Fernet key via
SHA-256 (so a human-chosen secret works). If the key is unset, the code
REFUSES to store a new token (a clear operator-facing error) rather than
writing plaintext. A token that cannot be decrypted (wrong/missing key, or a
rotated key) degrades cleanly: the connection is reported as "needs reconnect"
and the raw ciphertext is never returned — never a 500.

OTHER RULES
-----------
* The authorization code and the tokens never appear in a response, a log line
  or an error message; HTTP "would send" logs mask them like
  app_pages/meta_ads.py does.
* ``oauth_states`` is a single-use CSRF state with a 10-minute TTL, tied to the
  admin session, deleted the moment it is consumed.
* The callback URL is derived from ``PUBLIC_BASE_URL`` when set (giving exactly
  ``https://<domain>/backend/api/integrations/<platform>/callback``) and falls
  back to the incoming request host otherwise.
* Meta user tokens last ~60 days and have NO refresh token; expiry is stored as
  ``expires_at`` and surfaced as "reconnect needed". Unattended jobs (cost sync
  / pause-resume) should keep using the System User token configured under
  ``meta_ads.access_token``.
* Every HTTP call has a timeout and bounded retries and never raises into a
  request.

Provider URLs (defaults; the base hosts are overridable for tests)
------------------------------------------------------------------
* Consent:  https://www.facebook.com/{version}/dialog/oauth
* Exchange: https://graph.facebook.com/{version}/oauth/access_token
* Assets:   https://graph.facebook.com/{version}/me/adaccounts
            https://graph.facebook.com/{version}/{ad_account_id}/adspixels
            https://graph.facebook.com/{version}/{ad_account_id}/campaigns
* Revoke:   DELETE https://graph.facebook.com/{version}/me/permissions

Scope-creep note: business-owned pixel listing needs the ``business_management``
scope; the Connect flow deliberately requests only ``ads_read,ads_management``,
so pixels owned by a Business Manager (not the ad account) will not appear.
"""
import base64
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db, SessionLocal
from tenant_context import current_tenant
from graph_version import DEFAULT_GRAPH_VERSION
from models.settings import SettingsORM

# Reuse the Meta Graph client primitives (timeout, bounded retries + backoff,
# masked-request shape) — there is deliberately no second HTTP client.
from app_pages.meta_ads import (
    _get_with_retries,
    _safe_json,
    HTTP_TIMEOUT_SECONDS,
    MAX_PAGING_PAGES,
)

router = APIRouter()

DEFAULT_GRAPH_BASE = "https://graph.facebook.com"
DEFAULT_OAUTH_BASE = "https://www.facebook.com"
DEFAULT_API_VERSION = DEFAULT_GRAPH_VERSION
STATE_TTL_MINUTES = 10
META_SCOPES = "ads_read,ads_management"
ASSET_LIMIT = 100
_SECRET_MASK = "\u2022" * 8

# Platform registry. ``implemented`` gates the Connect flow; the others are
# present so the UI can show them as "coming soon".
PLATFORMS = {
    "meta": {"label": "Meta", "implemented": True, "api_version": DEFAULT_API_VERSION},
    "snapchat": {"label": "Snapchat", "implemented": False},
    "tiktok": {"label": "TikTok", "implemented": False},
    "pinterest": {"label": "Pinterest", "implemented": False},
    "google": {"label": "Google Ads", "implemented": False},
}
PLATFORM_ORDER = ("meta", "snapchat", "tiktok", "pinterest", "google")

# Environment variable names holding each platform's app credentials.
CREDENTIAL_ENV = {
    "meta": ("META_APP_ID", "META_APP_SECRET"),
    "snapchat": ("SNAPCHAT_CLIENT_ID", "SNAPCHAT_CLIENT_SECRET"),
    "tiktok": ("TIKTOK_APP_ID", "TIKTOK_APP_SECRET"),
    "pinterest": ("PINTEREST_APP_ID", "PINTEREST_APP_SECRET"),
    "google": ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
}
# Extra, non-OAuth variables each platform needs (documented; not read here).
EXTRA_CREDENTIAL_ENV = {
    "google": ("GOOGLE_ADS_DEVELOPER_TOKEN",),
}

ENC_KEY_VAR = "INTEGRATIONS_ENCRYPTION_KEY"
PUBLIC_BASE_VAR = "PUBLIC_BASE_URL"
# Reserved keys inside the ``integrations`` settings block — TEST-ONLY endpoint
# host overrides (mirrors meta_ads.graph_base_url). They hold no credentials.
ENDPOINT_OVERRIDES = ("graph_base_url", "oauth_base_url")


# ---------------------------------------------------------------------------
# App credentials (environment only)
# ---------------------------------------------------------------------------

def platform_credentials(platform: str) -> dict:
    """App credentials for a platform, read from the environment.

    Returns ``{client_id, client_secret, configured, id_var, secret_var}``. The
    secret value is only ever used server-side; it is never placed in a
    response.
    """
    id_var, secret_var = CREDENTIAL_ENV.get(platform, ("", ""))
    client_id = (os.environ.get(id_var) or "").strip() if id_var else ""
    client_secret = (os.environ.get(secret_var) or "").strip() if secret_var else ""
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "configured": bool(client_id and client_secret),
        "id_var": id_var,
        "secret_var": secret_var,
    }


# ---------------------------------------------------------------------------
# Token encryption (Fernet; passphrase derived deterministically)
# ---------------------------------------------------------------------------

def _fernet():
    """The Fernet instance for INTEGRATIONS_ENCRYPTION_KEY, or None when unset
    (or when ``cryptography`` is unavailable)."""
    raw = (os.environ.get(ENC_KEY_VAR) or "").strip()
    if not raw:
        return None
    try:
        from cryptography.fernet import Fernet
    except Exception:
        return None
    try:
        return Fernet(raw.encode())
    except Exception:
        # Not a Fernet key — derive one deterministically from the passphrase.
        digest = hashlib.sha256(raw.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def encryption_configured() -> bool:
    return _fernet() is not None


def encrypt_token(token: str):
    """Encrypt a token for storage. Returns None when no key is set (refuse to
    store plaintext) or the token is empty."""
    fernet = _fernet()
    if fernet is None or not token:
        return None
    return fernet.encrypt(token.encode()).decode()


def decrypt_token(stored: str):
    """Decrypt a stored token. Returns None on any failure (missing key, wrong
    key, tampered ciphertext) — callers degrade to "needs reconnect"."""
    if not stored:
        return None
    fernet = _fernet()
    if fernet is None:
        return None
    try:
        return fernet.decrypt(stored.encode()).decode()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Endpoint host overrides (test-only) and Graph/consent bases
# ---------------------------------------------------------------------------

def load_endpoint_overrides() -> dict:
    """The test-only ``graph_base_url`` / ``oauth_base_url`` settings keys."""
    out = {"graph_base_url": "", "oauth_base_url": ""}
    db = SessionLocal()
    try:
        row = db.query(SettingsORM).filter_by(name="settings").first()
        if row and row.value:
            block = (json.loads(row.value) or {}).get("integrations")
            if isinstance(block, dict):
                for key in ENDPOINT_OVERRIDES:
                    if isinstance(block.get(key), str):
                        out[key] = block[key].strip()
    except Exception:
        pass
    finally:
        db.close()
    return out


def _graph_base(cfg: dict) -> str:
    override = (cfg.get("graph_base_url")
                or os.environ.get("INTEGRATIONS_GRAPH_BASE_URL") or "").strip()
    return (override or DEFAULT_GRAPH_BASE).rstrip("/")


def _oauth_base(cfg: dict) -> str:
    override = (cfg.get("oauth_base_url")
                or os.environ.get("INTEGRATIONS_OAUTH_BASE_URL") or "").strip()
    return (override or DEFAULT_OAUTH_BASE).rstrip("/")


def _api_version(platform: str) -> str:
    """The Graph version for this platform.

    For Meta the cost-sync setting (``meta_ads.api_version``) wins when an operator set one, so
    the OAuth flow, asset discovery and insights all speak the same version; otherwise the shared
    default applies. Keeping two independent versions is how they drifted apart in the first
    place.
    """
    if platform == "meta":
        try:
            from app_pages.meta_ads import load_settings as _meta_ads_settings
            configured = str((_meta_ads_settings() or {}).get("api_version") or "").strip()
            if configured:
                return configured
        except Exception:
            pass
    return PLATFORMS.get(platform, {}).get("api_version", DEFAULT_API_VERSION)


# ---------------------------------------------------------------------------
# Callback URL derivation
# ---------------------------------------------------------------------------

def derive_callback_url(request: Request, platform: str) -> str:
    """The redirect URI to register with the provider.

    Prefers ``PUBLIC_BASE_URL`` (the public origin) so it renders exactly
    ``https://<domain>/backend/api/integrations/<platform>/callback`` behind a
    proxy. Falls back to the request: scheme from ``X-Forwarded-Proto`` else the
    request scheme, host from ``X-Forwarded-Host`` / Host / netloc, and the app
    ``root_path`` (``/backend``) as the prefix.
    """
    base = (os.environ.get(PUBLIC_BASE_VAR) or "").strip().rstrip("/")
    if base:
        return f"{base}/backend/api/integrations/{platform}/callback"

    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if not proto:
        proto = request.url.scheme
    host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if not host:
        host = (request.headers.get("host") or request.url.netloc or "").strip()
    root = (request.scope.get("root_path") or "").rstrip("/")
    if not root:
        root = "/backend"
    return f"{proto}://{host}{root}/api/integrations/{platform}/callback"


# ---------------------------------------------------------------------------
# Masking / scrubbing
# ---------------------------------------------------------------------------

def _mask(value) -> str:
    return _SECRET_MASK if value else ""


def _masked_request(req: dict) -> dict:
    """Same-shaped request with credentials redacted — safe to log / return."""
    out = {"method": req.get("method", "GET"), "url": req["url"]}
    for key in ("params", "body"):
        if key in req:
            out[key] = dict(req[key])
            for secret_key in ("access_token", "client_secret", "code",
                               "fb_exchange_token"):
                if secret_key in out[key]:
                    out[key][secret_key] = _SECRET_MASK
    return out


def _scrub(message: str, secrets_list) -> str:
    """Replace any literal secret occurrence in a message with the mask."""
    out = str(message or "")
    for secret in secrets_list:
        if secret and len(str(secret)) >= 4:
            out = out.replace(str(secret), _SECRET_MASK)
    return out[:300]


# ---------------------------------------------------------------------------
# OAuth state (single-use CSRF, 10-minute TTL)
# ---------------------------------------------------------------------------

def create_state(db: Session, platform: str, username: str) -> str:
    state = secrets.token_urlsafe(32)
    db.execute(text(
        "INSERT INTO oauth_states (state, platform, username, created_at) "
        "VALUES (:s, :p, :u, now())"),
        {"s": state, "p": platform, "u": (username or "")[:255]})
    db.commit()
    return state


def consume_state(db: Session, state: str, platform: str, username: str):
    """Validate + delete a state in one shot. Returns (ok, error_message).

    The row is deleted whether or not it validates, so a replayed state can
    never be used twice. Rejects: unknown, expired (>10 min), wrong platform,
    or a state issued to a different admin session.
    """
    state = (state or "").strip()
    if not state:
        return False, "The connect link is missing its state parameter. Start the connection again."
    row = db.execute(text(
        "SELECT platform, username, created_at FROM oauth_states WHERE state = :s"),
        {"s": state}).fetchone()
    if row is None:
        return False, ("This connect link is invalid or has already been used. "
                       "Start the connection again.")
    db.execute(text("DELETE FROM oauth_states WHERE state = :s"), {"s": state})
    db.commit()
    st_platform, st_user, created = row
    if created is not None and created < datetime.utcnow() - timedelta(minutes=STATE_TTL_MINUTES):
        return False, ("This connect link has expired (valid for 10 minutes). "
                       "Start the connection again.")
    if st_platform != platform:
        return False, "This connect link was issued for a different platform."
    if username and st_user and st_user != username:
        return False, "This connect link was issued to a different admin session."
    return True, ""


# ---------------------------------------------------------------------------
# Connection storage (token stored encrypted)
# ---------------------------------------------------------------------------

def _connection_row(db: Session, platform: str):
    from tenant_context import current_tenant
    # Raw SQL: the ORM tenant filter does not reach here, so the predicate is
    # written out — without it a tenant could read another tenant's stored
    # (encrypted) access token.
    return db.execute(text(
        "SELECT platform, access_token, token_type, expires_at, scopes, "
        "account_label, raw, created_at, updated_at "
        "FROM integration_connections WHERE platform = :p AND tenant_id = :tid"),
        {"p": platform, "tid": current_tenant()}).fetchone()


def _connection_view(row) -> dict:
    """API shape of a stored connection.

    The stored value is ciphertext; it is only ever returned masked. A token
    that cannot be read (missing/wrong key) or that has expired reports
    ``needs_reconnect`` — never an error.
    """
    if row is None:
        return None
    (_p, stored, token_type, expires_at, scopes, account_label,
     raw, created_at, updated_at) = row
    has_token = bool(stored)
    readable = decrypt_token(stored) is not None if has_token else False
    expired = bool(expires_at and expires_at < datetime.utcnow())
    needs_reconnect = (has_token and not readable) or expired
    return {
        "account_label": account_label or "",
        "token_type": token_type or "bearer",
        "access_token": _mask(stored),
        "has_token": has_token,
        "readable": readable,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "expired": expired,
        "reconnect_needed": needs_reconnect,
        "scopes": [s for s in (scopes or "").split(",") if s.strip()],
        "connected_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


def store_connection(db: Session, platform: str, raw_token: str, token_type: str,
                     expires_at, scopes: str, account_label: str,
                     raw: dict) -> bool:
    """Encrypt + upsert the connection. Returns False (storing nothing) when no
    ``INTEGRATIONS_ENCRYPTION_KEY`` is set — plaintext is never written."""
    cipher = encrypt_token(raw_token)
    if cipher is None:
        return False
    db.execute(text("""
        INSERT INTO integration_connections
            (platform, access_token, token_type, expires_at, scopes,
             account_label, raw, created_at, updated_at, tenant_id)
        VALUES (:p, :t, :tt, :e, :sc, :al, CAST(:raw AS JSONB), now(), now(), :tid)
        ON CONFLICT (tenant_id, platform) DO UPDATE SET
            access_token = EXCLUDED.access_token,
            token_type = EXCLUDED.token_type,
            expires_at = EXCLUDED.expires_at,
            scopes = EXCLUDED.scopes,
            account_label = EXCLUDED.account_label,
            raw = EXCLUDED.raw,
            updated_at = now()
    """), {"p": platform, "t": cipher, "tt": token_type or "bearer",
           "e": expires_at, "sc": scopes or "", "al": (account_label or "")[:255],
           "raw": json.dumps(raw or {}), "tid": current_tenant()})
    db.commit()
    return True


def clear_connection(db: Session, platform: str) -> bool:
    res = db.execute(text("DELETE FROM integration_connections "
                          "WHERE platform = :p AND tenant_id = :tid"),
                     {"p": platform, "tid": current_tenant()})
    db.commit()
    return bool(res.rowcount)


# ---------------------------------------------------------------------------
# Meta Graph calls (all bounded, timeout'd, never raising)
# ---------------------------------------------------------------------------

def _paged_get(client: httpx.Client, url: str, params: dict):
    """GET that follows ``paging.next`` verbatim. Returns (rows, attempts, error)."""
    rows = []
    attempts = 0
    resp, attempts, err = _get_with_retries(client, url, params)
    if resp is None:
        return [], attempts, err
    if resp.status_code != 200:
        return [], attempts, f"HTTP {resp.status_code}: {resp.text[:200]}"
    data = _safe_json(resp)
    pages = 0
    while pages < MAX_PAGING_PAGES:
        pages += 1
        for row in (data.get("data") or []):
            if isinstance(row, dict):
                rows.append(row)
        nxt = ((data.get("paging") or {}).get("next") or "").strip()
        if not nxt:
            break
        nresp, nattempts, nerr = _get_with_retries(client, nxt, None)
        attempts += nattempts
        if nresp is None or nresp.status_code != 200:
            return rows, attempts, nerr or f"HTTP {nresp.status_code if nresp else '?'}"
        data = _safe_json(nresp)
    return rows, attempts, ""


def exchange_code(cfg: dict, platform: str, code: str, redirect_uri: str,
                  client_id: str, client_secret: str):
    """Exchange an authorization code for a short-lived token. Never raises.

    Returns (token_dict_or_None, attempts, error_str). ``token_dict`` carries
    access_token / token_type / expires_in.
    """
    base = _graph_base(cfg)
    version = _api_version(platform)
    url = f"{base}/{version}/oauth/access_token"
    params = {"client_id": client_id, "client_secret": client_secret,
              "redirect_uri": redirect_uri, "code": code}
    req = {"method": "GET", "url": url, "params": params}
    print(f"integrations[{platform}]: GET {url} params={_masked_request(req)['params']}")
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)
    with httpx.Client(timeout=timeout) as client:
        resp, attempts, err = _get_with_retries(client, url, params)
    if resp is None:
        return None, attempts, _scrub(err, [client_secret, code])
    if resp.status_code != 200:
        return None, attempts, _provider_error(resp, [client_secret, code])
    data = _safe_json(resp)
    if not data.get("access_token"):
        return None, attempts, _provider_error(resp, [client_secret, code])
    return data, attempts, ""


def upgrade_token(cfg: dict, platform: str, short_token: str, client_id: str,
                  client_secret: str):
    """Upgrade a short-lived Meta token to a long-lived one. Never raises.

    ``grant_type=fb_exchange_token``. Meta user tokens last ~60 days and have no
    refresh token, so this is the longest-lived interactive credential.
    """
    base = _graph_base(cfg)
    version = _api_version(platform)
    url = f"{base}/{version}/oauth/access_token"
    params = {"grant_type": "fb_exchange_token", "client_id": client_id,
              "client_secret": client_secret, "fb_exchange_token": short_token}
    req = {"method": "GET", "url": url, "params": params}
    print(f"integrations[{platform}]: GET {url} params={_masked_request(req)['params']}")
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)
    with httpx.Client(timeout=timeout) as client:
        resp, attempts, err = _get_with_retries(client, url, params)
    if resp is None:
        return None, attempts, _scrub(err, [client_secret, short_token])
    if resp.status_code != 200:
        return None, attempts, _provider_error(resp, [client_secret, short_token])
    data = _safe_json(resp)
    if not data.get("access_token"):
        return None, attempts, _provider_error(resp, [client_secret, short_token])
    return data, attempts, ""


def _provider_error(resp, secrets_list) -> str:
    """Readable provider error that never leaks a credential."""
    try:
        body = resp.json()
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("error_user_msg") or err.get("type") or ""
            code = err.get("code")
            text_msg = f"{msg} ({code})" if code else str(msg)
            return _scrub(f"HTTP {resp.status_code}: {text_msg}", secrets_list)
    except Exception:
        pass
    return _scrub(f"HTTP {resp.status_code}: {resp.text[:160]}", secrets_list)


def revoke_token(cfg: dict, platform: str, token: str) -> bool:
    """Best-effort revoke so Disconnect actually disconnects. Never raises."""
    if not token:
        return False
    base = _graph_base(cfg)
    version = _api_version(platform)
    url = f"{base}/{version}/me/permissions"
    try:
        timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)
        with httpx.Client(timeout=timeout) as client:
            resp = client.delete(url, params={"access_token": token})
            print(f"integrations[{platform}]: DELETE {url} params="
                  f"{_masked_request({'url': url, 'params': {'access_token': token}})['params']}")
            return resp.status_code < 400
    except Exception as e:
        print(f"integrations[{platform}]: revoke failed: {type(e).__name__}")
        return False


def discover_assets(cfg: dict, platform: str, token: str) -> dict:
    """List ad accounts, their datasets and their campaigns. Never raises.

    Meta:
      GET /me/adaccounts?fields=id,name,account_id,currency,account_status&limit=…
      GET /{ad_account_id}/adspixels?fields=id,name&limit=…
      GET /{ad_account_id}/campaigns?fields=id,name,status&limit=…
    ``paging.next`` is followed verbatim.
    """
    out = {"ad_accounts": [], "datasets": [], "campaigns": [],
           "counts": {"ad_accounts": 0, "datasets": 0, "campaigns": 0},
           "errors": [], "requests": [], "attempts": 0}
    base = _graph_base(cfg)
    version = _api_version(platform)
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)
    with httpx.Client(timeout=timeout) as client:
        acct_url = f"{base}/{version}/me/adaccounts"
        acct_params = {"fields": "id,name,account_id,currency,account_status",
                       "limit": str(ASSET_LIMIT), "access_token": token}
        out["requests"].append(_masked_request(
            {"url": acct_url, "params": acct_params}))
        rows, attempts, err = _paged_get(client, acct_url, acct_params)
        out["attempts"] += attempts
        if err:
            out["errors"].append({"what": "ad_accounts", "error": err})
        for raw in rows:
            out["ad_accounts"].append({
                "id": str(raw.get("id") or "").strip(),
                "name": str(raw.get("name") or "").strip(),
                "account_id": str(raw.get("account_id") or "").strip(),
                "currency": str(raw.get("currency") or "").strip(),
                "account_status": raw.get("account_status"),
            })
        for acct in out["ad_accounts"]:
            acct_id = acct["id"]
            if not acct_id:
                continue
            pix_url = f"{base}/{version}/{acct_id}/adspixels"
            pix_params = {"fields": "id,name", "limit": str(ASSET_LIMIT),
                          "access_token": token}
            out["requests"].append(_masked_request(
                {"url": pix_url, "params": pix_params}))
            pix, pattempts, perr = _paged_get(client, pix_url, pix_params)
            out["attempts"] += pattempts
            if perr:
                out["errors"].append({"what": "datasets", "account_id": acct_id,
                                      "error": perr})
            for raw in pix:
                out["datasets"].append({
                    "id": str(raw.get("id") or "").strip(),
                    "name": str(raw.get("name") or "").strip(),
                    "ad_account_id": acct_id,
                })
            camp_url = f"{base}/{version}/{acct_id}/campaigns"
            camp_params = {"fields": "id,name,status", "limit": str(ASSET_LIMIT),
                           "access_token": token}
            out["requests"].append(_masked_request(
                {"url": camp_url, "params": camp_params}))
            camps, cattempts, cerr = _paged_get(client, camp_url, camp_params)
            out["attempts"] += cattempts
            if cerr:
                out["errors"].append({"what": "campaigns", "account_id": acct_id,
                                      "error": cerr})
            for raw in camps:
                out["campaigns"].append({
                    "id": str(raw.get("id") or "").strip(),
                    "name": str(raw.get("name") or "").strip(),
                    "status": str(raw.get("status") or "").strip(),
                    "ad_account_id": acct_id,
                })
    out["counts"] = {"ad_accounts": len(out["ad_accounts"]),
                     "datasets": len(out["datasets"]),
                     "campaigns": len(out["campaigns"])}
    return out


def empty_assets(platform: str, reason: str, account_label: str = "") -> dict:
    """The clean, non-erroring asset shape when there is no readable token."""
    return {"platform": platform, "connected": False, "expired": False,
            "needs_reconnect": bool(reason and "reconnect" in reason.lower()),
            "account_label": account_label, "expires_at": None, "error": reason or None,
            "ad_accounts": [], "datasets": [], "campaigns": [],
            "counts": {"ad_accounts": 0, "datasets": 0, "campaigns": 0},
            "requests": [], "attempts": 0, "errors": []}


def fetch_account_label(cfg: dict, platform: str, token: str) -> str:
    """The connected user's display name, for 'Connected as <label>'. Never raises."""
    base = _graph_base(cfg)
    version = _api_version(platform)
    url = f"{base}/{version}/me"
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp, _attempts, _err = _get_with_retries(
                client, url, {"fields": "id,name", "access_token": token})
        if resp is not None and resp.status_code == 200:
            return str(_safe_json(resp).get("name") or "").strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _platform_or_404(platform: str) -> str:
    platform = (platform or "").strip().lower()
    if platform not in PLATFORMS:
        raise HTTPException(status_code=404, detail=f"Unknown integration '{platform}'")
    return platform


@router.get("")
def list_integrations(request: Request, db: Session = Depends(get_db)):
    """Per-platform configuration + connection status + the derived callback URL.

    No app credentials are ever returned; a platform whose env vars are absent
    reports ``configured: false`` and ``connect_available: false``.
    """
    platforms = {}
    for name in PLATFORM_ORDER:
        info = PLATFORMS[name]
        creds = platform_credentials(name)
        implemented = bool(info.get("implemented"))
        conn = _connection_view(_connection_row(db, name))
        connected = bool(conn and conn.get("has_token") and conn.get("readable")
                         and not conn.get("expired"))
        platforms[name] = {
            "label": info["label"],
            "implemented": implemented,
            "configured": creds["configured"],
            "connect_available": implemented and creds["configured"],
            "env_vars": [v for v in (creds["id_var"], creds["secret_var"]) if v]
                        + list(EXTRA_CREDENTIAL_ENV.get(name, ())),
            "callback_url": derive_callback_url(request, name),
            "connected": connected,
            "connection": conn,
        }
    return {
        "platforms": platforms,
        "public_base_url": (os.environ.get(PUBLIC_BASE_VAR) or "").strip(),
        "encryption_configured": encryption_configured(),
    }


def _require_connectable(platform: str):
    """Validate the platform is implemented + has env credentials. Returns creds."""
    info = PLATFORMS[platform]
    if not info.get("implemented"):
        raise HTTPException(status_code=400, detail=(
            f"{info['label']} integration is not implemented yet."))
    creds = platform_credentials(platform)
    if not creds["configured"]:
        raise HTTPException(status_code=400, detail=(
            f"{info['label']} is not configured by this deployment. Set "
            f"{creds['id_var']} and {creds['secret_var']} in the environment."))
    return creds


@router.get("/{platform}/oauth/start")
def oauth_start(platform: str, request: Request, db: Session = Depends(get_db)):
    """Create a single-use state, then 302 the browser to the consent URL."""
    from audit_logger import audit_event
    from auth import get_caller
    platform = _platform_or_404(platform)
    creds = _require_connectable(platform)
    cfg = load_endpoint_overrides()

    username = None
    try:
        from auth import get_session_username
        username = get_session_username(request)
    except Exception:
        username = None
    state = create_state(db, platform, username or "")

    redirect_uri = derive_callback_url(request, platform)
    params = {
        "client_id": creds["client_id"],
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": META_SCOPES if platform == "meta" else "",
        "response_type": "code",
    }
    consent_url = (f"{_oauth_base(cfg)}/{_api_version(platform)}/dialog/oauth?"
                   + urlencode(params))

    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "oauth_start", "integrations", platform,
                {"callback_url": redirect_uri},
                request.client.host if request.client else "")
    print(f"integrations[{platform}]: oauth start -> consent redirect "
          f"(client_id from env, state issued)")
    return RedirectResponse(url=consent_url, status_code=302)


def _settings_redirect(platform: str, ok: bool, message: str = "", **extra):
    """Redirect the browser back to the Settings page (never a raw JSON page)."""
    query = {"integration": platform, "status": "ok" if ok else "error"}
    if message:
        query["message"] = message[:300]
    for key, value in extra.items():
        if value is not None:
            query[key] = str(value)
    return RedirectResponse(url=f"/backend/settings?{urlencode(query)}",
                            status_code=302)


# The provider is given the URL derive_callback_url() advertises, so the route
# must answer at exactly that path — ``/callback``. The older
# ``/oauth/callback`` stays as an alias so a redirect URI registered before the
# rename keeps working. A mismatch here means the provider's redirect lands on a
# 404 and no connection can ever complete, which no unit test would notice
# because both sides build the URL from the same helper.
@router.get("/{platform}/callback")
@router.get("/{platform}/oauth/callback")
async def oauth_callback(platform: str, request: Request, code: str = None,
                         state: str = None, error: str = None,
                         error_description: str = None,
                         db: Session = Depends(get_db)):
    """Validate the state, exchange the code server-side, store the encrypted
    token, run asset discovery, then redirect back to the Settings page."""
    from audit_logger import audit_event
    from auth import get_session_username
    platform = _platform_or_404(platform)
    cfg = load_endpoint_overrides()
    username = get_session_username(request)

    provider_error = error
    if provider_error:
        if state:
            consume_state(db, state, platform, username)
        readable = _scrub(error_description or provider_error, [])
        audit_event(username or "api_token", "oauth_callback", "integrations",
                    platform, {"result": "provider_error", "error": provider_error},
                    request.client.host if request.client else "")
        return _settings_redirect(platform, False,
                                  f"Authorization was not granted: {readable}")

    ok, state_error = consume_state(db, state, platform, username)
    if not ok:
        audit_event(username or "api_token", "oauth_callback", "integrations",
                    platform, {"result": "invalid_state"},
                    request.client.host if request.client else "")
        return _settings_redirect(platform, False, state_error)

    creds = platform_credentials(platform)
    if not creds["configured"]:
        return _settings_redirect(platform, False,
                                  "This platform is no longer configured by this deployment.")
    if not code:
        return _settings_redirect(platform, False,
                                  "The provider did not return an authorization code.")

    redirect_uri = derive_callback_url(request, platform)
    token_data, attempts, err = exchange_code(
        cfg, platform, code, redirect_uri, creds["client_id"], creds["client_secret"])
    if token_data is None:
        audit_event(username or "api_token", "oauth_callback", "integrations",
                    platform, {"result": "exchange_failed"},
                    request.client.host if request.client else "")
        return _settings_redirect(platform, False,
                                  f"Could not exchange the authorization code: {err}")

    short_token = token_data.get("access_token")
    long_token = short_token
    long_data = None
    if platform == "meta":
        # Immediately trade the short-lived token for a long-lived (~60 day) one.
        upgraded, _uattempts, uerr = upgrade_token(
            cfg, platform, short_token, creds["client_id"], creds["client_secret"])
        if upgraded is not None:
            long_token = upgraded.get("access_token") or short_token
            long_data = upgraded
        else:
            print(f"integrations[{platform}]: token upgrade failed "
                  f"(keeping short-lived token): {uerr}")

    # Refuse to store when there is no encryption key — never write plaintext.
    if not encryption_configured():
        audit_event(username or "api_token", "oauth_callback", "integrations",
                    platform, {"result": "no_encryption_key"},
                    request.client.host if request.client else "")
        return _settings_redirect(platform, False, (
            f"{ENC_KEY_VAR} is not set, so the token cannot be stored securely. "
            "Set it in the environment and connect again."))

    final = long_data or token_data
    try:
        expires_in = int(float(final.get("expires_in"))) if final.get("expires_in") else None
    except (TypeError, ValueError):
        expires_in = None
    expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)
                  if expires_in else None)
    scopes = META_SCOPES if platform == "meta" else ""
    account_label = fetch_account_label(cfg, platform, long_token)
    stored = store_connection(db, platform, long_token,
                              final.get("token_type") or "bearer", expires_at,
                              scopes, account_label, {"source": "oauth"})
    if not stored:
        return _settings_redirect(platform, False,
                                  "Could not store the token securely — see the server log.")

    # Asset discovery is best-effort: a failure must not lose the token.
    assets = discover_assets(cfg, platform, long_token)
    counts = assets["counts"]
    if not account_label and assets["ad_accounts"]:
        account_label = assets["ad_accounts"][0]["name"]
        store_connection(db, platform, long_token,
                         final.get("token_type") or "bearer", expires_at, scopes,
                         account_label, {"source": "oauth"})

    audit_event(username or "api_token", "oauth_callback", "integrations", platform,
                {"result": "connected", "account_label": account_label,
                 "accounts": counts["ad_accounts"], "datasets": counts["datasets"],
                 "expires_at": expires_at.isoformat() if expires_at else None,
                 "attempts": attempts},
                request.client.host if request.client else "")
    return _settings_redirect(
        platform, True,
        f"Connected to {PLATFORMS[platform]['label']}"
        + (f" as {account_label}" if account_label else ""),
        accounts=counts["ad_accounts"], datasets=counts["datasets"])


@router.delete("/{platform}/connection")
def disconnect(platform: str, request: Request, db: Session = Depends(get_db)):
    """Clear the stored token (and best-effort revoke it) so Disconnect works."""
    from audit_logger import audit_event
    from auth import get_caller
    platform = _platform_or_404(platform)
    row = _connection_row(db, platform)
    revoked = False
    if row and row[1]:
        token = decrypt_token(row[1])
        if token:
            cfg = load_endpoint_overrides()
            revoked = revoke_token(cfg, platform, token)
    cleared = clear_connection(db, platform)
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "disconnect", "integrations", platform,
                {"cleared": cleared, "revoked": revoked},
                request.client.host if request.client else "")
    return {"status": "ok", "platform": platform, "cleared": cleared,
            "revoked": revoked}


@router.get("/{platform}/assets")
def platform_assets(platform: str, db: Session = Depends(get_db)):
    """Discovered ad accounts, datasets and campaigns from the stored token.

    Degrades cleanly (HTTP 200, empty assets, a readable ``error``) when the
    platform is not implemented / not configured / not connected / the token is
    unreadable — never a 500.
    """
    platform = _platform_or_404(platform)
    info = PLATFORMS[platform]
    if not info.get("implemented"):
        return empty_assets(platform,
                            f"{info['label']} integration is not implemented yet.")
    if not platform_credentials(platform)["configured"]:
        return empty_assets(platform,
                            f"{info['label']} is not configured by this deployment.")
    row = _connection_row(db, platform)
    if row is None or not row[1]:
        return empty_assets(platform, f"{info['label']} is not connected.")
    view = _connection_view(row)
    if not view.get("readable"):
        return empty_assets(platform,
                            f"The {info['label']} connection needs reconnect "
                            "(the stored token cannot be read).",
                            account_label=view.get("account_label") or "")
    if view.get("expired"):
        return empty_assets(platform,
                            f"The {info['label']} connection has expired — reconnect needed.",
                            account_label=view.get("account_label") or "")
    token = decrypt_token(row[1])
    cfg = load_endpoint_overrides()
    assets = discover_assets(cfg, platform, token)
    assets.update({
        "platform": platform,
        "connected": True,
        "expired": False,
        "needs_reconnect": False,
        "account_label": view["account_label"],
        "expires_at": view["expires_at"],
        "error": None,
    })
    if assets["errors"]:
        assets["error"] = assets["errors"][0].get("error")
    return assets
