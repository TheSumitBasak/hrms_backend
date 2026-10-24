"""Password hashing and JWT access-token helpers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from app.core.config import settings

try:  # bcrypt is preferred when available
    import bcrypt as _bcrypt
except Exception:  # pragma: no cover
    _bcrypt = None

_PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    """Hash a plaintext password. Uses bcrypt when available, else PBKDF2."""
    if _bcrypt is not None:
        # bcrypt only considers the first 72 bytes.
        raw = password.encode("utf-8")[:72]
        return _bcrypt.hashpw(raw, _bcrypt.gensalt()).decode("utf-8")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("utf-8"),
        base64.b64encode(digest).decode("utf-8"),
    )


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    if password_hash.startswith("pbkdf2_sha256$"):
        try:
            _, iterations, salt_b64, digest_b64 = password_hash.split("$")
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(digest_b64)
            actual = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt, int(iterations)
            )
            return hmac.compare_digest(actual, expected)
        except Exception:
            return False
    if _bcrypt is not None:
        try:
            return _bcrypt.checkpw(password.encode("utf-8")[:72], password_hash.encode("utf-8"))
        except Exception:
            return False
    return False


def create_access_token(subject: str, extra: dict | None = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload: dict = {"sub": str(subject), "exp": expire, "type": "access"}
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def create_refresh_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    payload = {"sub": str(subject), "exp": expire, "type": "refresh"}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def create_reset_token(subject: str, password_hash: str) -> str:
    """Short-lived single-use password-reset token.

    The token embeds a fingerprint of the current password hash, so it becomes
    invalid as soon as the password changes.
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=30)
    fingerprint = hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]
    payload = {"sub": str(subject), "exp": expire, "type": "reset", "fp": fingerprint}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        return None


def decode_access_token(token: str) -> dict | None:
    payload = decode_token(token)
    if payload is None or payload.get("type") not in (None, "access"):
        return None
    return payload
