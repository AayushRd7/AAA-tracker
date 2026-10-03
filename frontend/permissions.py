"""Workspace permission matrix — the tracking-host service's mirror of
``backend/auth.py``.

The dashboard API resolves authority from the caller's **membership in the
request's workspace**, never from the install-global ``users.is_admin`` flag.
The tracking host (this container) shares the same database but not the same
code — it only mounts ``frontend/`` — so the landings/domains editors, traffic
simulation and the CAPI test need the matrix restated here.

This is the one place the two services must agree. Keep the three constants in
step with ``backend/auth.py`` (PERMISSION_SECTIONS / ADMIN_ONLY_SECTIONS /
ROLE_*); the smoke suite compares the two files so drift fails the build.

Pure functions only: no database access, no FastAPI. ``app.py`` owns the
session lookup and feeds the resolved (role, raw permissions) in here.
"""

# Sections a workspace can grant. Admin-only sections are the workspace's
# management plane; the rest are content.
PERMISSION_SECTIONS = ["dashboard", "campaigns", "landings", "affiliates", "offers",
                       "sources", "reports", "domains", "settings", "users", "documentation",
                       "fraud", "optimizer", "conversion-tracking", "logs", "scripts",
                       "integrations", "capi-integrations", "bot-rules", "rules",
                       "filter-presets", "fallback", "funnels",
                       "acquisition", "creative-analytics", "copilot"]
ADMIN_ONLY_SECTIONS = {"users", "settings", "domains", "fraud", "optimizer",
                       "conversion-tracking", "logs", "scripts",
                       "integrations", "capi-integrations", "bot-rules", "rules",
                       "fallback", "copilot"}

# Role -> default permission map (the documented matrix):
#   owner  — everything in the tenant, including ownership transfer.
#   admin  — everything in the tenant except changing the owner.
#   editor — read+write on content sections; no workspace/user/settings plane.
#   viewer — read-only on content sections.
ROLE_DEFAULT_WRITE = {"owner": True, "admin": True, "editor": True, "viewer": False}
ROLE_HAS_ADMIN_SECTIONS = {"owner": True, "admin": True, "editor": False, "viewer": False}

# Manager roles that traverse the workspace hierarchy: an owner/admin of a
# workspace also acts in that workspace's descendants.
MANAGER_ROLES = ("owner", "admin")


def role_default_permissions(role):
    role = (role or "viewer").lower()
    admin_ok = ROLE_HAS_ADMIN_SECTIONS.get(role, False)
    sections = {s: (True if s not in ADMIN_ONLY_SECTIONS else admin_ok)
                for s in PERMISSION_SECTIONS}
    return {"sections": sections, "write": bool(ROLE_DEFAULT_WRITE.get(role, False))}


def resolve_membership_permissions(raw, role):
    """Effective map = role defaults layered under the membership's explicit
    per-section overrides (and the optional global write flag)."""
    perms = role_default_permissions(role)
    raw = raw if isinstance(raw, dict) else {}
    raw_sections = raw.get("sections")
    if isinstance(raw_sections, dict):
        for s in PERMISSION_SECTIONS:
            if s in raw_sections:
                perms["sections"][s] = bool(raw_sections[s])
    if "write" in raw:
        perms["write"] = bool(raw["write"])
    return perms


def allowed(role, raw_permissions, section, write=False):
    """True when a membership with this role/permissions may touch ``section``
    (and, for ``write``, holds the write flag). No membership -> no access."""
    if not role:
        return False
    perms = resolve_membership_permissions(raw_permissions, role)
    if not perms["sections"].get(section, False):
        return False
    if write and not perms["write"]:
        return False
    return True
