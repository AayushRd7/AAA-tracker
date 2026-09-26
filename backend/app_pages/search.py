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


@router.get("")
@router.get("/")
def global_search(request: Request, q: str = "", db: Session = Depends(get_db)):
    term = (q or "").strip()
    if len(term) < 2:
        raise HTTPException(status_code=400, detail="Query must be at least 2 characters")

    from auth import get_caller
    caller, is_admin = get_caller(request)
    perms = {}
    if caller and not is_admin:
        row = db.execute(text("SELECT permissions FROM users WHERE username = :u"),
                         {"u": caller}).fetchone()
        raw = row[0] if row and row[0] else {}
        perms = raw.get("sections") or {}
        own_campaigns = raw.get("campaigns") == "own"
    else:
        own_campaigns = False

    def allowed(section: str) -> bool:
        # Mirror resolve_permissions defaults: sections absent from the user's
        # permission map default to open EXCEPT admin-only ones (domains,
        # settings, users) which default to denied.
        from auth import ADMIN_ONLY_SECTIONS
        return is_admin or perms.get(section, section not in ADMIN_ONLY_SECTIONS)

    like = f"%{term}%"
    out = {}
    uid_row = db.execute(text("SELECT id FROM users WHERE username = :u"),
                         {"u": caller}).fetchone() if caller else None
    my_id = uid_row[0] if uid_row else None

    if allowed("campaigns"):
        scope = " AND owner_id = :my_id" if (own_campaigns and my_id) else ""
        params = {"like": like, "lim": GROUP_LIMIT}
        if own_campaigns and my_id:
            params["my_id"] = my_id
        rows = db.execute(text(f"""
            SELECT id, name, alias, tags FROM campaigns
            WHERE (name ILIKE :like OR alias ILIKE :like OR tags::text ILIKE :like){scope}
            ORDER BY id ASC LIMIT :lim"""), params).fetchall()
        if rows:
            out["campaigns"] = [{"id": r[0], "name": r[1],
                                 "snippet": _snippet(" ".join([r[1] or "", r[2] or "",
                                                               " ".join(r[3] or [])]), term)}
                                for r in rows]

    if allowed("offers"):
        rows = db.execute(text("""
            SELECT id, name, url FROM offers
            WHERE name ILIKE :like OR url ILIKE :like OR array_to_string(tags, ' ') ILIKE :like
            ORDER BY id ASC LIMIT :lim"""), {"like": like, "lim": GROUP_LIMIT}).fetchall()
        if rows:
            out["offers"] = [{"id": r[0], "name": r[1],
                              "snippet": _snippet(r[2], term)} for r in rows]

    if allowed("landings"):
        rows = db.execute(text("""
            SELECT id, name, folder FROM landings
            WHERE name ILIKE :like OR folder ILIKE :like OR tags ILIKE :like
            ORDER BY id ASC LIMIT :lim"""), {"like": like, "lim": GROUP_LIMIT}).fetchall()
        if rows:
            out["landings"] = [{"id": r[0], "name": r[1],
                                "snippet": _snippet(r[2], term)} for r in rows]

    if allowed("domains"):
        rows = db.execute(text("""
            SELECT id, domain FROM domains WHERE domain ILIKE :like
            ORDER BY id ASC LIMIT :lim"""), {"like": like, "lim": GROUP_LIMIT}).fetchall()
        if rows:
            out["domains"] = [{"id": r[0], "name": r[1], "snippet": r[1]} for r in rows]

    if allowed("sources"):
        rows = db.execute(text("""
            SELECT id, name FROM sources WHERE name ILIKE :like
            ORDER BY id ASC LIMIT :lim"""), {"like": like, "lim": GROUP_LIMIT}).fetchall()
        if rows:
            out["sources"] = [{"id": r[0], "name": r[1], "snippet": r[1]} for r in rows]

    if allowed("affiliates"):
        rows = db.execute(text("""
            SELECT id, name FROM affiliate_networks WHERE name ILIKE :like
            ORDER BY id ASC LIMIT :lim"""), {"like": like, "lim": GROUP_LIMIT}).fetchall()
        if rows:
            out["affiliates"] = [{"id": r[0], "name": r[1], "snippet": r[1]} for r in rows]

    if allowed("reports"):
        rows = db.execute(text("""
            SELECT id, click_id, external_id, transaction_id, status
            FROM conversions_data
            WHERE click_id ILIKE :like OR external_id ILIKE :like
               OR transaction_id ILIKE :like
            ORDER BY id DESC LIMIT :lim"""),
            {"like": like, "lim": CONVERSION_LIMIT}).fetchall()
        if rows:
            out["conversions"] = [{"id": r[0],
                                   "name": r[1] if r[1] and r[1] != "none" else "(unattributed)",
                                   "snippet": _snippet(" ".join(x for x in (r[2], r[3], r[4]) if x), term)}
                                  for r in rows]

    return {"q": term, "groups": out}
