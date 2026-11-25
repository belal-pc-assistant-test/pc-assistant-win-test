"""One-time remote access secret, session cookie, lock-out and expiry.

Security properties (each one is covered by tests):

* The link secret has 256 random bits. Only its SHA-256 digest is kept.
* Every comparison is constant time (``hmac.compare_digest``) on digests.
* The secret is redeemable exactly once: the first correct presentation issues
  a random session cookie and locks the secret, so a copied link stops working.
* Repeated denials open a lock-out window in which every presentation is refused
  without any comparison. Too many denials in total ends the session.
* Expiry and revocation are checked on every request.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import deque
from typing import Callable

SECRET_BYTES = 32            # 256-bit link secret -> 43 URL-safe characters
COOKIE_BYTES = 32
MIN_SECRET_CHARS = 43
FAILURE_WINDOW = 60.0        # seconds
FAILURE_LIMIT = 5            # denials inside the window that trigger a lock-out
LOCKOUT_SECONDS = 60.0
TOTAL_FAILURE_CAP = 25       # after this many denials the whole session ends

FRESH = "fresh"
LOCKED = "locked"            # secret redeemed; only the cookie works now
REVOKED = "revoked"


def generate_secret() -> str:
    """A fresh, unguessable link secret (never reused across sessions)."""
    return secrets.token_urlsafe(SECRET_BYTES)


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).digest()


class AccessGate:
    """Decides whether a presented secret or cookie is accepted right now."""

    def __init__(self, secret: str, *, deadline: float,
                 now: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(secret, str) or len(secret) < MIN_SECRET_CHARS:
            raise ValueError("remote link secret is too short")
        self._now = now
        self._deadline = deadline
        self._secret_digest = _digest(secret)
        self._cookie_digest: bytes | None = None
        self._state = FRESH
        self._failures: deque[float] = deque()
        self._lockout_until = 0.0
        self.total_failures = 0

    @property
    def state(self) -> str:
        return self._state

    def expired(self) -> bool:
        return self._state == REVOKED or self._now() >= self._deadline

    def locked_out(self) -> bool:
        return self._now() < self._lockout_until

    def redeem(self, presented: str) -> tuple[str, str | None]:
        """Check the link secret. Returns (decision, cookie-or-None).

        decision is one of: ``ok`` (cookie issued), ``denied`` (wrong secret),
        ``used`` (secret already redeemed), ``limited`` (lock-out, not compared),
        ``expired`` (time is up or revoked; not counted as an attack).
        """
        if self.expired():
            return "expired", None
        if self.locked_out():
            return "limited", None
        matches = hmac.compare_digest(_digest(presented), self._secret_digest)
        if self._state == LOCKED:
            self._deny()
            return "used", None
        if not matches:
            self._deny()
            return "denied", None
        cookie = secrets.token_urlsafe(COOKIE_BYTES)
        self._cookie_digest = _digest(cookie)
        self._state = LOCKED
        return "ok", cookie

    def authorize(self, cookie: str | None) -> str:
        """Check the session cookie. Returns ok, missing, denied, limited or expired."""
        if self.expired():
            return "expired"
        if self.locked_out():
            return "limited"
        if not cookie:
            return "missing"
        matches = (self._state == LOCKED and self._cookie_digest is not None and
                   hmac.compare_digest(_digest(cookie), self._cookie_digest))
        if matches:
            return "ok"
        self._deny()
        return "denied"

    def revoke(self) -> None:
        """End access immediately (stop, panic, shutdown)."""
        self._state = REVOKED
        self._cookie_digest = None

    def _deny(self) -> None:
        now = self._now()
        self.total_failures += 1
        while self._failures and now - self._failures[0] > FAILURE_WINDOW:
            self._failures.popleft()
        self._failures.append(now)
        if len(self._failures) >= FAILURE_LIMIT:
            self._lockout_until = now + LOCKOUT_SECONDS
            self._failures.clear()


class EventLimiter:
    """Token bucket used to bound how many input events a viewer may send."""

    def __init__(self, rate: float, burst: float, now: Callable[[], float] = time.monotonic) -> None:
        self._rate = float(rate)
        self._burst = float(burst)
        self._tokens = float(burst)
        self._now = now
        self._last = now()

    def allow(self) -> bool:
        now = self._now()
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False
