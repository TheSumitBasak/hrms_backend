"""Storage abstraction and signed local URLs (§15.4).

The development backend writes under a private directory and issues HMAC-signed, expiring
tokens. The token contains the storage key, a purpose and an expiry, so a leaked URL expires
and cannot be repurposed to another object. An S3 deployment replaces this class; the service
layer only depends on ``put/get/delete`` and a signed URL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any, Protocol, cast


class StorageBackend(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        candidate = (self._root / key).resolve()
        if self._root not in candidate.parents and candidate != self._root:
            raise ValueError("storage key escapes the private root")
        return candidate

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()


class SignedUrlSigner:
    """HMAC-signed, expiring tokens for local upload/download routes."""

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode()

    def issue(
        self, *, key: str, purpose: str, ttl_seconds: int, organization_id: str
    ) -> tuple[str, int]:
        expires_at = int(time.time()) + ttl_seconds
        payload = json.dumps(
            {
                "k": key,
                "p": purpose,
                "e": expires_at,
                "o": organization_id,
                "n": secrets.token_hex(8),
            },
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        token = base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + signature
        return token, expires_at

    def verify(self, token: str, *, purpose: str) -> dict[str, object]:
        try:
            encoded, signature = token.split(".", 1)
            padded = encoded + "=" * (-len(encoded) % 4)
            payload = base64.urlsafe_b64decode(padded)
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("malformed storage token") from error
        expected = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid storage token")
        data = cast(dict[str, Any], json.loads(payload))
        if data.get("p") != purpose or int(data.get("e", 0)) < int(time.time()):
            raise ValueError("expired or mis-scoped storage token")
        return data


__all__ = ["LocalStorage", "SignedUrlSigner", "StorageBackend"]
