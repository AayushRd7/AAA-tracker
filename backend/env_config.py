"""Deployment-owned configuration, read from the environment.

A few settings belong to the deployment rather than to a workspace: the email
relay credentials, the Telegram bot, and the ad-platform Graph endpoint. The
product never asks a user for them — an operator sets the environment variables
below. Each accessor falls back to a value already stored in settings so an
installation configured before these moved out of the UI keeps working, but an
environment value always wins when it is present.
"""
import os


def _env(*names: str, default: str = "") -> str:
    """First non-empty environment value among ``names``, else ``default``."""
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return default


def smtp_host(fallback: str = "") -> str:
    return _env("SMTP_HOST", "EMAIL_SMTP_HOST", "BREVO_SMTP_HOST", default=fallback)


def smtp_port(fallback=587) -> int:
    raw = _env("SMTP_PORT", "EMAIL_SMTP_PORT", "BREVO_SMTP_PORT")
    try:
        return int(raw) if raw else int(fallback or 587)
    except (TypeError, ValueError):
        return 587


def smtp_login(fallback: str = "") -> str:
    return _env("SMTP_LOGIN", "SMTP_USER", "EMAIL_SMTP_LOGIN", "BREVO_SMTP_LOGIN",
                default=fallback)


def smtp_password(fallback: str = "") -> str:
    return _env("SMTP_PASSWORD", "SMTP_KEY", "EMAIL_SMTP_PASSWORD", "BREVO_SMTP_KEY",
                default=fallback)


def email_api_key(fallback: str = "") -> str:
    """Key for the relay's HTTP API (used instead of SMTP when set)."""
    return _env("EMAIL_API_KEY", "BREVO_API_KEY", default=fallback)


def email_from_address(fallback: str = "") -> str:
    """The address reports are sent from (the relay's verified sender)."""
    return _env("EMAIL_FROM_ADDRESS", "EMAIL_FROM", "SMTP_FROM", default=fallback)


def email_from_name(fallback: str = "") -> str:
    """Display name recipients see next to the From address."""
    return _env("EMAIL_FROM_NAME", default=fallback) or "AAA Tracker"


def telegram_bot_token(fallback: str = "") -> str:
    return _env("TELEGRAM_BOT_TOKEN", default=fallback)


def telegram_bot_username(fallback: str = "") -> str:
    """Public @handle of the deployment's bot, without the leading @."""
    return _env("TELEGRAM_BOT_USERNAME", default=fallback).lstrip("@")


def telegram_bot_url(fallback: str = "") -> str:
    """Deep link a user can open to start a chat with the deployment's bot."""
    username = telegram_bot_username()
    return f"https://t.me/{username}" if username else fallback


def meta_graph_base_url(fallback: str = "") -> str:
    """Graph host override; empty means the real Graph host."""
    return _env("META_GRAPH_BASE_URL", default=fallback)


def meta_graph_version(fallback: str = "") -> str:
    return _env("META_GRAPH_VERSION", default=fallback)


def resolve_email_config(cfg: dict) -> dict:
    """Overlay the deployment's relay credentials and From identity onto a
    tenant email config.

    Tenant-owned fields (enabled, recipients, hour) pass through untouched. The
    result is only ever used to send — never written back to settings, so an
    environment value is not persisted by a save.
    """
    out = dict(cfg or {})
    out["smtp_host"] = smtp_host(str(out.get("smtp_host") or ""))
    out["smtp_port"] = smtp_port(out.get("smtp_port") or 587)
    out["smtp_login"] = smtp_login(str(out.get("smtp_login") or ""))
    out["smtp_password"] = smtp_password(str(out.get("smtp_password") or ""))
    out["api_key"] = email_api_key(str(out.get("api_key") or ""))
    out["from_email"] = email_from_address(str(out.get("from_email") or ""))
    out["from_name"] = email_from_name(str(out.get("from_name") or ""))
    return out


def email_configured(cfg: dict = None) -> bool:
    """True when a relay is usable — an API key, or a full SMTP triple."""
    resolved = resolve_email_config(cfg or {})
    if (resolved.get("api_key") or "").strip():
        return True
    return all((resolved.get(f) or "").strip()
               for f in ("smtp_host", "smtp_login", "smtp_password"))
