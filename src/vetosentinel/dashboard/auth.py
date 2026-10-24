"""Dashboard login (demo-grade access control).

One username/password protects the dashboard page and every data/control API route.

* Credentials come from ``VETOSENTINEL_LOGIN_USER`` / ``VETOSENTINEL_LOGIN_PASSWORD``. Without
  them the built-in demo account is used. Its password is stored only as a salted scrypt
  hash, never in plain text.
* A successful login creates a random session token stored in an ``HttpOnly``,
  ``SameSite=Strict`` cookie. Sessions live in memory, so restarting the process logs
  everyone out.
* Failed logins are rate-limited per client address. Behind a reverse proxy every request
  shares the proxy's address, so the limit then applies to everyone together.

This is hackathon-grade protection, not a security boundary for anything valuable. For a
public deployment set a strong password through the environment variables, serve over HTTPS,
or put an access proxy (e.g. Cloudflare Access) in front.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta

COOKIE_NAME = "vs_session"
DEFAULT_USER = "oxhack26"
# scrypt(password, salt) of the built-in demo password.
_DEFAULT_SALT = bytes.fromhex("500978b279aad3c6261ef82523f98cb9")
_DEFAULT_DIGEST = bytes.fromhex("0c0347824c3bc18c9a77ecb8d90d37161091f48b98bf8c00ecc4b28b84918e2e")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)


@dataclass(frozen=True)
class Credentials:
    username: str
    salt: bytes
    digest: bytes

    @classmethod
    def from_password(cls, username: str, password: str) -> Credentials:
        salt = secrets.token_bytes(16)
        return cls(username, salt, hash_password(password, salt))

    @classmethod
    def from_env(cls) -> Credentials:
        user = os.environ.get("VETOSENTINEL_LOGIN_USER") or DEFAULT_USER
        password = os.environ.get("VETOSENTINEL_LOGIN_PASSWORD")
        if password:
            return cls.from_password(user, password)
        return cls(user, _DEFAULT_SALT, _DEFAULT_DIGEST)

    def verify(self, username: str, password: str) -> bool:
        # Always hash, so response time does not reveal whether the username was right.
        password_ok = hmac.compare_digest(hash_password(password, self.salt), self.digest)
        user_ok = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
        return password_ok and user_ok


class SessionStore:
    def __init__(self, ttl: timedelta = timedelta(hours=12), max_sessions: int = 1000):
        self.ttl = ttl
        self.max_sessions = max_sessions
        self._sessions: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def create(self, user: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if len(self._sessions) >= self.max_sessions:  # drop the oldest
                oldest = min(self._sessions, key=lambda k: self._sessions[k][1])
                del self._sessions[oldest]
            self._sessions[token] = (user, now + self.ttl.total_seconds())
        return token

    def user_for(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            entry = self._sessions.get(token)
            if entry is None:
                return None
            user, expires = entry
            if time.monotonic() >= expires:
                del self._sessions[token]
                return None
            return user

    def revoke(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)

    def _prune(self, now: float) -> None:
        for k in [k for k, (_, exp) in self._sessions.items() if now >= exp]:
            del self._sessions[k]


class LoginLimiter:
    """After ``max_failures`` failures within ``window_s``, refuse logins for ``lockout_s``."""

    def __init__(self, max_failures: int = 8, window_s: float = 300.0, lockout_s: float = 60.0):
        self.max_failures = max_failures
        self.window_s = window_s
        self.lockout_s = lockout_s
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._failures.get(key)
            if not q:
                return 0.0
            while q and now - q[0] > self.window_s:
                q.popleft()
            if len(q) < self.max_failures:
                return 0.0
            return max(0.0, q[-1] + self.lockout_s - now)

    def failure(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._failures.setdefault(key, deque(maxlen=self.max_failures * 4)).append(now)

    def success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


@dataclass
class Auth:
    credentials: Credentials
    sessions: SessionStore = field(default_factory=SessionStore)
    limiter: LoginLimiter = field(default_factory=LoginLimiter)

    @classmethod
    def from_env(cls) -> Auth:
        return cls(Credentials.from_env())
