"""G75 — global search.

GET /api/search?q=... (min 2 chars) scans campaigns (name/alias/tags), offers
(name/url/tags), landings, domains, traffic sources, affiliate networks and
conversions (click_id/external_id/transaction_id), returning grouped results
for the top app-bar autocomplete. Non-admin users only see groups their
permissions allow, and campaigns:'own' users only see their own campaigns.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from tenant_context import current_tenant

router = APIRouter()

GROUP_LIMIT = 5
CONVERSION_LIMIT = 8


def _snippet(text_value: str, term: str, width: int = 64) -> str:
    s = str(text_value or "")
    if not s:
        return ""
    low, t = s.lower(), term.lower()
    i = low.find(t)
    if i < 0:
        return s[:width]
    start = max(0, i - width // 3)
    return ("…" if start > 0 else "") + s[start:start + width] + ("…" if start + width < len(s) else "")


def _escape_like(term: str) -> str:
    """Escape SQL LIKE wildcards so user text matches literally (Postgres's
    default escape character is the backslash)."""
    return str(term).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("")
@router.get("/")
def global_search(request: Request, q: str = "", db: Session = Depends(get_db)):
    term = (q or "").strip()
    if len(term) < 2:
        raise HTTPException(status_code=400, detail="Query must be at least 2 characters")

    from auth import get_caller, membership_for, resolve_membership_permissions
    caller, is_admin = get_caller(request)
    own_campaigns = False
    caller_perms = None
    if caller:
        # Authority comes from the membership in the current tenant, not the
        # users row: a platform operator with no membership here sees nothing.
        member = membership_for(db, caller)
        if member:
            raw = member[2] or {}
            own_campaigns = raw.get("campaigns") == "own"
            caller_perms = resolve_membership_permissions(raw, member[1])

    def allowed(section: str) -> bool:
        if caller is None:
            return True  # install-wide Bearer api_token principal
        return bool(caller_perms and caller_perms["sections"].get(section, False))

    like = f"%{_escape_like(term)}%"
    out = {}
    uid_row = db.execute(text("SELECT id FROM users WHERE username = :u"),
                         {"u": caller}).fetchone() if caller else None
    my_id = uid_row[0] if uid_row else None

    if allowed("campaigns"):
        scope = " AND owner_id = :my_id" if (own_campaigns and my_id) else ""
        params = {"like": like, "lim": GROUP_LIMIT, "tid": current_tenant()}
        if own_campaigns and my_id:
            params["my_id"] = my_id
        rows = db.execute(text(f"""
            SELECT id, name, alias, tags FROM campaigns
            WHERE tenant_id = :tid
              AND (name ILIKE :like OR alias ILIKE :like OR tags::text ILIKE :like){scope}
            ORDER BY id ASC LIMIT :lim"""), params).fetchall()
        if rows:
            out["campaigns"] = [{"id": r[0], "name": r[1],
                                 "snippet": _snippet(" ".join([r[1] or "", r[2] or "",
                                                               " ".join(r[3] or [])]), term)}
                                for r in rows]

    if allowed("offers"):
        rows = db.execute(text("""
            SELECT id, name, url FROM offers
            WHERE tenant_id = :tid
              AND (name ILIKE :like OR url ILIKE :like OR array_to_string(tags, ' ') ILIKE :like)
            ORDER BY id ASC LIMIT :lim"""),
            {"like": like, "lim": GROUP_LIMIT, "tid": current_tenant()}).fetchall()
        if rows:
            out["offers"] = [{"id": r[0], "name": r[1],
                              "snippet": _snippet(r[2], term)} for r in rows]

    if allowed("landings"):
        rows = db.execute(text("""
            SELECT id, name, folder FROM landings
            WHERE tenant_id = :tid
              AND (name ILIKE :like OR folder ILIKE :like OR tags ILIKE :like)
            ORDER BY id ASC LIMIT :lim"""),
            {"like": like, "lim": GROUP_LIMIT, "tid": current_tenant()}).fetchall()
        if rows:
            out["landings"] = [{"id": r[0], "name": r[1],
                                "snippet": _snippet(r[2], term)} for r in rows]

    if allowed("domains"):
        rows = db.execute(text("""
            SELECT id, domain FROM domains
            WHERE tenant_id = :tid AND domain ILIKE :like
            ORDER BY id ASC LIMIT :lim"""),
            {"like": like, "lim": GROUP_LIMIT, "tid": current_tenant()}).fetchall()
        if rows:
            out["domains"] = [{"id": r[0], "name": r[1], "snippet": r[1]} for r in rows]

    if allowed("sources"):
        rows = db.execute(text("""
            SELECT id, name FROM sources
            WHERE tenant_id = :tid AND name ILIKE :like
            ORDER BY id ASC LIMIT :lim"""),
            {"like": like, "lim": GROUP_LIMIT, "tid": current_tenant()}).fetchall()
        if rows:
            out["sources"] = [{"id": r[0], "name": r[1], "snippet": r[1]} for r in rows]

    if allowed("affiliates"):
        rows = db.execute(text("""
            SELECT id, name FROM affiliate_networks
            WHERE tenant_id = :tid AND name ILIKE :like
            ORDER BY id ASC LIMIT :lim"""),
            {"like": like, "lim": GROUP_LIMIT, "tid": current_tenant()}).fetchall()
        if rows:
            out["affiliates"] = [{"id": r[0], "name": r[1], "snippet": r[1]} for r in rows]

    if allowed("reports"):
        conv_sql = """
            SELECT id, click_id, external_id, transaction_id, status
            FROM conversions_data
            WHERE tenant_id = :tid
              AND (click_id ILIKE :like OR external_id ILIKE :like
               OR transaction_id ILIKE :like)"""
        conv_params = {"like": like, "lim": CONVERSION_LIMIT, "tid": current_tenant()}
        # campaigns:'own' parity with the conversions list: a scoped caller
        # only searches conversions of campaigns they own (unattributable
        # rows stay hidden).
        if own_campaigns and my_id:
            conv_sql += (" AND campaign_id IN (SELECT id FROM campaigns "
                         "WHERE tenant_id = :tid AND owner_id = :my_id)")
            conv_params["my_id"] = my_id
        conv_sql += " ORDER BY id DESC LIMIT :lim"
        rows = db.execute(text(conv_sql), conv_params).fetchall()
        if rows:
            out["conversions"] = [{"id": r[0],
                                   "name": r[1] if r[1] and r[1] != "none" else "(unattributed)",
                                   "snippet": _snippet(" ".join(x for x in (r[2], r[3], r[4]) if x), term)}
                                  for r in rows]

    return {"q": term, "groups": out}
