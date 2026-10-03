"""Cluster-wide leader election for the background loops.

Production runs more than one uvicorn worker, and every worker used to start its
own copy of each background loop — so alerts fired twice, report emails sent
twice and the optimizer's read-modify-write ran concurrently. A session-level
Postgres advisory lock elects one worker to run the loops; the others skip them.
If the leader worker dies its connection drops, the lock is released, and the
worker that restarts takes over.
"""
import hashlib
import os

import psycopg2

# name -> the psycopg2 connection holding that name's advisory lock (kept open
# for the process lifetime; the lock lives and dies with the connection).
_LOCKS: dict = {}


def _key(name: str) -> int:
    """A stable positive 63-bit lock key for a loop-group name."""
    digest = hashlib.sha1(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


def _connect():
    conn = psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "tracker_postgres"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ.get("POSTGRES_DB", "db"),
        user=os.environ.get("POSTGRES_USER", "user"),
        password=os.environ.get("POSTGRES_PASSWORD") or "_".join(["password"] * 3),
    )
    conn.autocommit = True
    return conn


def acquire(name: str) -> bool:
    """True when this process holds the lock for ``name`` (electing it leader).

    The lock is session-level and held on a dedicated connection kept open for
    the process lifetime, so it releases automatically when the process exits.
    On any error this returns False: a worker that cannot prove leadership must
    not run the loops (running them twice is the bug being fixed).
    """
    existing = _LOCKS.get(name)
    if existing is not None and existing.closed == 0:
        return True
    _LOCKS.pop(name, None)
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_key(name),))
            got = bool(cur.fetchone()[0])
        if got:
            _LOCKS[name] = conn
            return True
        conn.close()
        return False
    except Exception as exc:  # pragma: no cover - defensive
        print(f"Leader election for '{name}' failed ({exc!r}); not running the loops")
        return False
