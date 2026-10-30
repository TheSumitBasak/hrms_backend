"""Key providers — the boundary between the application and key management (§23.7).

Two implementations:

``EnvKeyProvider``
    Development and test only. A single base64 master key per purpose comes from the
    environment, and per-organization data keys are derived from it with HKDF. This keeps the
    envelope shape of §23.7 (one key per organization, a version on every ciphertext) without
    needing a KMS, but the master key lives in the process environment, which §23.7 and §25.9
    forbid in production. :class:`app.core.config.Settings` refuses to boot a
    staging/production environment configured this way.

``KmsKeyProvider``
    The production shape: a wrapped DEK per organization, unwrapped by a managed KMS/HSM and
    cached in memory for the life of the process. **No KMS exists in this environment**, so
    the unwrap call is unimplemented and the class raises on use. It is present as a real
    integration seam, with the exact call sites a KMS client must fill, rather than as a
    silent fallback that would make the system look production-ready when it is not. See
    ``docs/CONFIGURATION.md`` and the limitations section of ``docs/STATUS.md``.
"""

from __future__ import annotations

import threading
import uuid

from app.core.crypto import AES_KEY_BYTES, KeyProvider, derive_subkey

_DEK_INFO_PREFIX = b"ems:dek:"
_BLIND_INFO_PREFIX = b"ems:blind:"


def _dek_info(organization_id: uuid.UUID, key_version: int) -> bytes:
    """HKDF ``info`` for an organization data key. Binds purpose, version and scope."""
    return _DEK_INFO_PREFIX + f"v{key_version}:{organization_id.hex}".encode()


def _blind_info(scope_id: uuid.UUID, key_version: int) -> bytes:
    """HKDF ``info`` for a blind-index key. ``scope_id`` is a group id, or an org id."""
    return _BLIND_INFO_PREFIX + f"v{key_version}:{scope_id.hex}".encode()


class EnvKeyProvider:
    """Derives per-scope keys from environment-supplied master keys.

    Derived keys are cached because HKDF runs on every encrypt and every duplicate check;
    the cache is process-local, bounded by the number of organizations, and holds no
    plaintext business data. Access is lock-guarded because FastAPI serves synchronous
    endpoints from a threadpool (ADR-001).
    """

    __slots__ = (
        "_blind_cache",
        "_blind_master",
        "_dek_cache",
        "_encryption_master",
        "_lock",
        "_version",
    )

    def __init__(
        self,
        *,
        encryption_master_key: bytes,
        blind_index_master_key: bytes,
        key_version: int,
    ) -> None:
        for name, material in (
            ("encryption_master_key", encryption_master_key),
            ("blind_index_master_key", blind_index_master_key),
        ):
            if len(material) != AES_KEY_BYTES:
                raise ValueError(f"{name} must be exactly {AES_KEY_BYTES} bytes")
        if encryption_master_key == blind_index_master_key:
            raise ValueError(
                "blind_index_master_key must differ from encryption_master_key (§23.7)"
            )
        if key_version < 1:
            raise ValueError("key_version must be >= 1")

        self._encryption_master = encryption_master_key
        self._blind_master = blind_index_master_key
        self._version = key_version
        self._dek_cache: dict[tuple[str, int], bytes] = {}
        self._blind_cache: dict[tuple[str, int], bytes] = {}
        self._lock = threading.Lock()

    @property
    def current_key_version(self) -> int:
        return self._version

    def data_key(self, organization_id: uuid.UUID, key_version: int) -> bytes:
        cache_key = (organization_id.hex, key_version)
        with self._lock:
            cached = self._dek_cache.get(cache_key)
            if cached is None:
                cached = derive_subkey(
                    self._encryption_master, info=_dek_info(organization_id, key_version)
                )
                self._dek_cache[cache_key] = cached
            return cached

    def blind_index_key(self, scope_id: uuid.UUID, key_version: int) -> bytes:
        cache_key = (scope_id.hex, key_version)
        with self._lock:
            cached = self._blind_cache.get(cache_key)
            if cached is None:
                cached = derive_subkey(self._blind_master, info=_blind_info(scope_id, key_version))
                self._blind_cache[cache_key] = cached
            return cached


class KmsKeyProvider:
    """Production key provider: wrapped DEKs unwrapped by a managed KMS.

    **Not configured in this environment.** Every method raises
    :class:`NotImplementedError` with the integration step it needs. The structure is
    deliberate: a deployment that sets ``key_provider=kms`` without providing a client fails
    at the first sensitive write rather than appearing to work.

    To complete the integration:

    1. Store one wrapped DEK per organization (ciphertext of the DEK under the customer
       master key), written when the organization is created.
    2. Implement :meth:`data_key` as "look up the wrapped DEK, call ``kms.decrypt`` once,
       cache the plaintext DEK in memory" — never persist the unwrapped key.
    3. Implement :meth:`blind_index_key` the same way against a separate key hierarchy, so
       §23.7's separation of blind-index keys from encryption keys is preserved.
    4. Keep every KMS network call **outside** database transactions (§24.7).
    """

    __slots__ = ("_key_id", "_version")

    def __init__(self, *, kms_key_id: str, key_version: int) -> None:
        self._key_id = kms_key_id
        self._version = key_version

    @property
    def current_key_version(self) -> int:
        return self._version

    def data_key(self, organization_id: uuid.UUID, key_version: int) -> bytes:
        raise NotImplementedError(
            "KmsKeyProvider.data_key requires a KMS client. No KMS is configured in this "
            "environment; see docs/CONFIGURATION.md."
        )

    def blind_index_key(self, scope_id: uuid.UUID, key_version: int) -> bytes:
        raise NotImplementedError(
            "KmsKeyProvider.blind_index_key requires a KMS client. No KMS is configured in "
            "this environment; see docs/CONFIGURATION.md."
        )


def build_key_provider(settings: object) -> KeyProvider:
    """Construct the provider named by configuration.

    Takes the settings object structurally rather than importing :class:`Settings`, so the
    integrations layer stays independent of configuration internals and is trivial to
    exercise with a stub in tests.
    """
    provider_name = getattr(settings, "key_provider", "env")
    key_version = int(getattr(settings, "encryption_key_version", 1))

    if provider_name == "kms":
        kms_key_id = getattr(settings, "kms_key_id", None)
        if not kms_key_id:
            raise ValueError("kms_key_id is required when key_provider=kms")
        return KmsKeyProvider(kms_key_id=str(kms_key_id), key_version=key_version)

    return EnvKeyProvider(
        encryption_master_key=settings.decoded_encryption_key(),  # type: ignore[attr-defined]
        blind_index_master_key=settings.decoded_blind_index_key(),  # type: ignore[attr-defined]
        key_version=key_version,
    )


__all__ = ["EnvKeyProvider", "KmsKeyProvider", "build_key_provider"]
