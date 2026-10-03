"""Shared, Postgres-backed attempt limiter for the auth endpoints.

Login and TOTP failures used to live in per-process dicts, so N uvicorn workers
each kept their own counter and the effective threshold was multiplied by N.
The state now lives in one tiny table, ``auth_throttle``::

    auth_throttle(key TEXT PRIMARY KEY,
                  attempts JSONB NOT NULL,
                  updated_at TIMESTAMP NOT NULL DEFAULT now())

Every worker read-modify-writes the same row with ``INSERT ... ON CONFLICT``,
so a client is limited by the sum of every worker's failures rather than by
whichever worker happened to answer. Rows are pruned once they have been idle
for the limiter's own window.

The database is only a *hint*, never a hard dependency: if it is unreachable the
limiter falls back to a per-process dict (with the same stale-key eviction the
old code used) so a login still succeeds. Failing open on the limiter is
deliberate — failing closed would lock every operator out of a broken install.
"""
import json
import logging
import time

from sqlalchemy import text

from db import SessionLocal

log = logging.getLogger(__name__)

_throttle_table_ready = False

# Cap on the (attacker-chosen) in-process fallback map; above it, stale keys
# are evicted so a flood of unique IP:username keys cannot exhaust memory.
DEFAULT_MAX_KEYS = 10_000


def ensure_throttle_table() -> None:
    """Create the shared table lazily, like ``auth_sessions``."""
    global _throttle_table_ready
    if _throttle_table_ready:
        return
    db = SessionLocal()
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS auth_throttle (
                key TEXT PRIMARY KEY,
                attempts JSONB NOT NULL DEFAULT '[]'::jsonb,
                updated_at TIMESTAMP NOT NULL DEFAULT now()
            )
        """))
        db.commit()
    finally:
        db.close()
    _throttle_table_ready = True


def _attempts_of(raw) -> list:
    """The stored JSONB attempts as a list of floats (tolerant of str/list)."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for value in raw:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


class Throttle:
    """A named rolling-window attempt limiter backed by ``auth_throttle``.

    ``name`` prefixes every key so two limiters sharing the table (login, TOTP)
    can prune their own rows without deleting the other's before its window
    elapsed.
    """

    def __init__(self, name: str, window_seconds: int, max_attempts: int,
                 max_keys: int = DEFAULT_MAX_KEYS):
        self.name = name
        self.window = float(window_seconds)
        self.max_attempts = int(max_attempts)
        self.max_keys = int(max_keys)
        self._prefix = f"{name}:"
        self._mem: dict = {}

    def _key(self, key: str) -> str:
        return self._prefix + str(key)

    # --- in-process fallback (used only when the DB is unreachable) ---
    def _mem_record(self, key: str, now: float) -> None:
        cutoff = now - self.window
        attempts = [t for t in self._mem.get(key, []) if t > cutoff]
        attempts.append(now)
        self._mem[key] = attempts
        if len(self._mem) > self.max_keys:
            for stale in [k for k, v in self._mem.items()
                          if not any(t > cutoff for t in v)]:
                self._mem.pop(stale, None)

    def _mem_limited(self, key: str, now: float) -> bool:
        cutoff = now - self.window
        return len([t for t in self._mem.get(key, []) if t > cutoff]) >= self.max_attempts

    # --- public API ---
    def record(self, key: str) -> None:
        """Record one failure against ``key`` (pruning attempts outside the window)."""
        now = time.time()
        full = self._key(key)
        try:
            ensure_throttle_table()
            db = SessionLocal()
            try:
                db.execute(text("""
                    INSERT INTO auth_throttle (key, attempts, updated_at)
                    VALUES (:k, CAST(:a AS JSONB), now())
                    ON CONFLICT (key) DO UPDATE SET
                        attempts = (
                            SELECT COALESCE(jsonb_agg(v), '[]'::jsonb)
                            FROM jsonb_array_elements_text(auth_throttle.attempts) AS t(v)
                            WHERE v::double precision > :cutoff
                        ) || CAST(:a AS JSONB),
                        updated_at = now()
                """), {"k": full, "a": json.dumps([now]),
                       "cutoff": now - self.window})
                # Drop rows for THIS limiter that have been idle longer than the
                # window (the other limiter's longer window must not be pruned).
                db.execute(text(
                    "DELETE FROM auth_throttle WHERE key LIKE :p "
                    "AND updated_at < now() - (:w * interval '1 second')"),
                    {"p": self._prefix + "%", "w": self.window})
                db.commit()
                return
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001 — limiter must never break login
            log.warning("rate limiter '%s' DB unavailable, using in-process "
                        "fallback: %s", self.name, e)
        self._mem_record(key, now)

    def limited(self, key: str) -> bool:
        """True when ``key`` has reached the failure threshold within the window."""
        now = time.time()
        full = self._key(key)
        try:
            ensure_throttle_table()
            db = SessionLocal()
            try:
                row = db.execute(
                    text("SELECT attempts FROM auth_throttle WHERE key = :k"),
                    {"k": full}).fetchone()
            finally:
                db.close()
            attempts = _attempts_of(row[0] if row else None)
            cutoff = now - self.window
            return len([t for t in attempts if t > cutoff]) >= self.max_attempts
        except Exception as e:  # noqa: BLE001
            log.warning("rate limiter '%s' DB unavailable, using in-process "
                        "fallback: %s", self.name, e)
        return self._mem_limited(key, now)

    def clear(self, key: str) -> None:
        """Forget a key (a successful login wipes the failure history)."""
        full = self._key(key)
        self._mem.pop(key, None)
        try:
            ensure_throttle_table()
            db = SessionLocal()
            try:
                db.execute(text("DELETE FROM auth_throttle WHERE key = :k"), {"k": full})
                db.commit()
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            pass
