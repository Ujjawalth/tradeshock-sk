"""Admin lock for live AI features.

If AI_ACCESS_PASSWORD is set, live AI calls (which spend the owner's API credit)
need an unlock. Everything else keeps working in offline mode.

Security:
- the password lives only in an environment variable (Render dashboard / .env)
- comparison is constant-time (hmac.compare_digest) on SHA-256 digests
- a correct password returns a signed token: "<expiry>.<HMAC-SHA256>" in an
  HttpOnly, Secure, SameSite=Strict cookie; the signing key is derived from the
  password + API key, so changing either revokes every token
- fail closed: a password shorter than MIN_LENGTH never unlocks anything
- brute force is rate-limited in app.py (per IP and globally)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

log = logging.getLogger("tradeshock.access")

COOKIE_NAME = "ts_ai_unlock"
TOKEN_TTL_SECONDS = 12 * 3600
MIN_LENGTH = 12


def _password() -> str:
    return os.getenv("AI_ACCESS_PASSWORD", "")


def lock_enabled() -> bool:
    """AI is locked whenever a password is configured (any length - short ones fail closed)."""
    return bool(_password())


def password_too_weak() -> bool:
    return lock_enabled() and len(_password()) < MIN_LENGTH


def _signing_key() -> bytes:
    material = f"tradeshock-unlock-v1|{_password()}|{os.getenv('ANTHROPIC_API_KEY', '')}"
    return hashlib.sha256(material.encode()).digest()


def check_password(candidate: str) -> bool:
    if not lock_enabled() or password_too_weak():
        return False
    a = hashlib.sha256(str(candidate or "").encode()).digest()
    b = hashlib.sha256(_password().encode()).digest()
    return hmac.compare_digest(a, b)


def make_token(now: float | None = None) -> str:
    expiry = int((now or time.time()) + TOKEN_TTL_SECONDS)
    sig = hmac.new(_signing_key(), str(expiry).encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def token_valid(token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token or password_too_weak():
        return False
    expiry_s, sig = token.split(".", 1)
    if not expiry_s.isdigit() or int(expiry_s) < (now or time.time()):
        return False
    expected = hmac.new(_signing_key(), expiry_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def ai_allowed(token: str | None) -> bool:
    """True if live AI may run for this request."""
    if not lock_enabled():
        return True
    return token_valid(token)
