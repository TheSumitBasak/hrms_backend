"""Application-level encryption and blind indexes for sensitive fields (§23.7).

Only vetted primitives from ``cryptography`` are used: AES-256-GCM for authenticated
encryption, HKDF-SHA256 for key derivation and HMAC-SHA256 for blind indexes. Nothing here
invents a scheme.

Two properties matter beyond "it is encrypted":

**Ciphertext is bound to its location.** Every value is sealed with additional authenticated
data naming the organization, the entity and the field. A ciphertext lifted out of one row
and pasted into another — including into another tenant's row — fails to decrypt instead of
silently revealing a value in the wrong context.

**Blind-index keys are separate from encryption keys and scoped per group.** Exact-match
search and duplicate detection (§11.5) run entirely on HMAC digests, so no plaintext is ever
compared or decrypted for those purposes, and matching across organization groups is
impossible by construction rather than by a query filter.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

AES_KEY_BYTES = 32
"""AES-256."""

GCM_NONCE_BYTES = 12
"""96-bit nonce, the size AES-GCM is specified for."""

BLIND_INDEX_BYTES = 32
"""HMAC-SHA256 output; matches ``BYTES(32)`` in §11.1."""


class DecryptionError(Exception):
    """Raised when a ciphertext fails authentication.

    Carries no detail about the value or the key. A failure means the ciphertext was
    corrupted, the key version is wrong, or the AAD does not match — which includes an
    attempt to decrypt a value sealed for a different organization, entity or field.
    """


@dataclass(frozen=True, slots=True)
class FieldContext:
    """Identifies where a sensitive value lives, for AAD binding.

    ``entity_id`` is the row that owns the value (an employee id for identifiers and bank
    accounts, a document id for document numbers). ``field`` distinguishes several sensitive
    values on the same row, so a PAN ciphertext cannot be swapped into the UAN column.
    """

    organization_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    field: str

    def aad(self) -> bytes:
        """Serialise to additional authenticated data.

        Pipe-separated with fixed-length UUID hex, so no field value can be shifted into
        another position by containing a separator.
        """
        return (
            f"ems:v1|{self.organization_id.hex}|{self.entity_type}|"
            f"{self.entity_id.hex}|{self.field}"
        ).encode()


@runtime_checkable
class KeyProvider(Protocol):
    """Supplies key material. Implementations live in :mod:`app.integrations.keys`.

    Keys never reside in the database or in source (§23.7). A provider is the only component
    that touches master key material; everything else works through :class:`FieldCipher` and
    :class:`BlindIndexer`.
    """

    @property
    def current_key_version(self) -> int:
        """Version used for new writes. Older versions stay readable for lazy re-encryption."""
        ...

    def data_key(self, organization_id: uuid.UUID, key_version: int) -> bytes:
        """32-byte data encryption key for one organization and key version."""
        ...

    def blind_index_key(self, scope_id: uuid.UUID, key_version: int) -> bytes:
        """32-byte blind-index key for a *group* scope (or an organization when ungrouped)."""
        ...


def derive_subkey(master: bytes, *, info: bytes, length: int = AES_KEY_BYTES) -> bytes:
    """HKDF-SHA256 expansion of a master key into a purpose-bound subkey.

    ``info`` must encode the purpose, the key version and the scope, so two scopes can never
    derive the same subkey. No salt is used: HKDF-Expand with a distinct ``info`` per purpose
    is sufficient when the input key material is already a uniformly random 256-bit key,
    which :func:`generate_master_key` guarantees.
    """
    if len(master) != AES_KEY_BYTES:
        raise ValueError("master key material must be exactly 32 bytes")
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(master)


def generate_master_key() -> bytes:
    """A fresh 32-byte key from the OS CSPRNG. Used by the key-generation CLI only."""
    return os.urandom(AES_KEY_BYTES)


class FieldCipher:
    """Seals and opens individual field values with AES-256-GCM.

    The stored blob is ``nonce || ciphertext || tag``. The key version is stored in its own
    column (``key_version`` in §11.1) rather than inside the blob, so a rotation job can
    find rows needing re-encryption with an indexed query.
    """

    __slots__ = ("_keys",)

    def __init__(self, keys: KeyProvider) -> None:
        self._keys = keys

    @property
    def current_key_version(self) -> int:
        return self._keys.current_key_version

    def encrypt(self, plaintext: str, context: FieldContext) -> tuple[bytes, int]:
        """Seal ``plaintext``. Returns the blob and the key version used."""
        version = self._keys.current_key_version
        key = self._keys.data_key(context.organization_id, version)
        nonce = os.urandom(GCM_NONCE_BYTES)
        sealed = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), context.aad())
        return nonce + sealed, version

    def decrypt(self, blob: bytes, context: FieldContext, key_version: int) -> str:
        """Open a blob. Raises :class:`DecryptionError` on any authentication failure."""
        if len(blob) <= GCM_NONCE_BYTES:
            raise DecryptionError("ciphertext is too short to be valid")
        key = self._keys.data_key(context.organization_id, key_version)
        nonce, sealed = blob[:GCM_NONCE_BYTES], blob[GCM_NONCE_BYTES:]
        try:
            opened = AESGCM(key).decrypt(nonce, sealed, context.aad())
        except InvalidTag as exc:
            raise DecryptionError("ciphertext failed authentication") from exc
        return opened.decode("utf-8")

    def needs_rotation(self, key_version: int) -> bool:
        """True when a row was written under a superseded key version."""
        return key_version < self._keys.current_key_version


class BlindIndexer:
    """Computes keyed HMAC digests for exact-match search and duplicate detection (§11.5).

    The scope is the organization **group** when the organization belongs to one, and the
    organization itself otherwise. Two organizations in different groups therefore derive
    different keys, so their digests for the same underlying value differ and cross-group
    matching cannot happen even if someone writes a query that forgets to filter — which is
    what §23.7 means by "impossible by design".
    """

    __slots__ = ("_keys",)

    def __init__(self, keys: KeyProvider) -> None:
        self._keys = keys

    @property
    def current_key_version(self) -> int:
        return self._keys.current_key_version

    def compute(
        self,
        normalized_value: str,
        *,
        scope_id: uuid.UUID,
        identifier_type: str,
        key_version: int | None = None,
    ) -> bytes:
        """Digest a **normalised** value.

        The caller must normalise first (§21.1): the index is only useful if the same logical
        value always produces the same bytes. ``identifier_type`` is mixed in so the same
        digits recorded as two different identifier types do not collide.
        """
        if not normalized_value:
            raise ValueError("cannot compute a blind index over an empty value")
        version = self._keys.current_key_version if key_version is None else key_version
        key = self._keys.blind_index_key(scope_id, version)
        message = f"{identifier_type}\x00{normalized_value}".encode()
        return hmac.new(key, message, hashlib.sha256).digest()

    @staticmethod
    def matches(left: bytes, right: bytes) -> bool:
        """Constant-time digest comparison."""
        return hmac.compare_digest(left, right)


def sha256_hex(data: bytes) -> str:
    """Lower-case SHA-256 hex digest. Used for document checksums (§15.3)."""
    return hashlib.sha256(data).hexdigest()


def hash_opaque_token(token: str) -> str:
    """Hash a bearer-style token for storage.

    Refresh tokens are high-entropy random strings, so a single SHA-256 is appropriate and a
    password KDF is not: there is no low-entropy secret to slow down guessing against. The
    plaintext token is never stored (DEC-005).
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_opaque_token(byte_length: int = 32) -> str:
    """A URL-safe random token with at least 256 bits of entropy."""
    return secrets.token_urlsafe(byte_length)


def constant_time_equals(left: str, right: str) -> bool:
    """Compare two strings without leaking their common prefix length through timing."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


__all__ = [
    "AES_KEY_BYTES",
    "BLIND_INDEX_BYTES",
    "GCM_NONCE_BYTES",
    "BlindIndexer",
    "DecryptionError",
    "FieldCipher",
    "FieldContext",
    "KeyProvider",
    "constant_time_equals",
    "derive_subkey",
    "generate_master_key",
    "generate_opaque_token",
    "hash_opaque_token",
    "sha256_hex",
]
