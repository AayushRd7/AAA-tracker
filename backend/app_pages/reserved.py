"""Reserved install-global usernames — the platform-operator namespace.

The built-in platform operator (``tracker_admin``) is an install-global
account owned by the platform plane. No workspace-level path may mint it or
attach it to a workspace: member management, invitations and tenant
provisioning all consult this module so the name stays exclusive to the
operator namespace. The platform-gated user API remains the only plane that
may manage such accounts.
"""

RESERVED_USERNAMES = frozenset({"tracker_admin"})


def is_reserved_username(username) -> bool:
    """True when ``username`` is reserved for the platform plane.

    Matching is case-insensitive and ignores surrounding whitespace, so a
    workspace admin cannot mint ``TRACKER_ADMIN`` or `` tracker_admin `` to
    slip past the guard.
    """
    return bool(username) and str(username).strip().lower() in RESERVED_USERNAMES
