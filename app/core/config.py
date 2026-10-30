"""Application configuration.

Every setting is supplied by the environment and validated at startup. There are no
production-usable defaults for anything secret: a deployment that forgets a key fails to
boot rather than running with a guessable one (§25.9, OWASP A02).
"""

from __future__ import annotations

import base64
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Placeholder values that ship in .env.example. Refusing to start on these prevents the
# single most common production misconfiguration.
PLACEHOLDER_SECRETS = frozenset(
    {
        "change-me",
        "change-me-in-production",
        "changeme",
        "secret",
        "placeholder",
        "",
    }
)

MIN_SECRET_LENGTH = 32
"""Minimum length for the application secret, giving >=128 bits of entropy when random."""


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production_like(self) -> bool:
        return self in (Environment.STAGING, Environment.PRODUCTION)


class Settings(BaseSettings):
    """Validated application settings.

    Loaded from the process environment, falling back to a local ``.env`` file. Field names
    map to upper-case environment variables (``DATABASE_URL``, ``SECRET_KEY``, ...).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------------------------------------------------------- application
    app_name: str = "Employee Management System"
    environment: Environment = Environment.LOCAL
    debug: bool = False
    api_prefix: str = "/api/v1"
    """Base path for all versioned endpoints (§24.1)."""

    # NoDecode stops pydantic-settings JSON-decoding this before validation runs, which
    # is what lets `split_cors_origins` accept the comma-separated form a .env file uses.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ---------------------------------------------------------------- database
    database_url: str = Field(
        ...,
        description=(
            "SQLAlchemy URL for MySQL 8.0+, e.g. "
            "mysql+pymysql://user:pass@host:3306/ems?charset=utf8mb4"
        ),
    )
    db_pool_size: Annotated[int, Field(ge=1, le=200)] = 10
    db_max_overflow: Annotated[int, Field(ge=0, le=200)] = 20
    db_pool_recycle_seconds: Annotated[int, Field(ge=60, le=28800)] = 1800
    db_pool_pre_ping: bool = True
    db_lock_wait_timeout_seconds: Annotated[int, Field(ge=1, le=120)] = 10
    """InnoDB lock wait timeout. Exceeding it surfaces as 409 CONCURRENT_MODIFICATION."""

    db_echo: bool = False

    # ---------------------------------------------------------------- secrets / tokens
    secret_key: SecretStr = Field(
        ...,
        description="Signs local access tokens and opaque pagination cursors (§26.5).",
    )
    access_token_ttl_seconds: Annotated[int, Field(ge=60, le=900)] = 900
    """NFR-13 caps access token lifetime at 15 minutes."""

    refresh_token_ttl_hours: Annotated[int, Field(ge=1, le=12)] = 12
    """NFR-13 absolute session cap of 12 hours."""

    build_sha: str = "unknown"
    """Commit the running build came from, reported by ``GET /meta``. Set by the deploy."""

    session_idle_timeout_seconds: Annotated[int, Field(ge=60, le=3600)] = 1800
    """Advisory idle timeout reported by ``GET /auth/me`` (EMS: 30 minutes). The server does not
    track idleness itself; the client signs out, and the 15-minute access token bounds the rest."""

    # Non-secret token metadata. Flagged by S105 only because the field names contain
    # "token"; the signing secret is `secret_key` above.
    token_algorithm: Literal["HS256", "RS256"] = "HS256"  # noqa: S105
    token_issuer: str = "ems-backend"  # noqa: S105
    token_audience: str = "ems-api"  # noqa: S105

    # OIDC. Primary identity strategy per §24.1/A-04. Inert until an issuer is configured;
    # see docs/AUTH.md for what remains unconfigured.
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_jwks_cache_seconds: Annotated[int, Field(ge=60, le=86400)] = 3600

    local_login_enabled: bool = True
    """Local password accounts are the documented fallback when no IdP is configured."""

    # ---------------------------------------------------------------- encryption (§23.7)
    key_provider: Literal["env", "kms"] = "env"
    encryption_master_key: SecretStr | None = Field(
        default=None,
        description="base64-encoded 32-byte master key, required when key_provider=env.",
    )
    blind_index_master_key: SecretStr | None = Field(
        default=None,
        description=(
            "base64-encoded 32-byte key for HMAC blind indexes, required when "
            "key_provider=env. MUST differ from encryption_master_key (§23.7)."
        ),
    )
    encryption_key_version: Annotated[int, Field(ge=1, le=32767)] = 1
    kms_key_id: str | None = None
    kms_endpoint: str | None = None

    # ---------------------------------------------------------------- storage (§15)
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_root: Path = Path("./var/private-storage")
    """Private directory for development. MUST NOT be served by any static file handler."""

    storage_s3_bucket: str | None = None
    storage_s3_quarantine_bucket: str | None = None
    storage_s3_region: str | None = None
    storage_s3_endpoint: str | None = None
    upload_url_ttl_seconds: Annotated[int, Field(ge=60, le=3600)] = 900
    """§15.4: upload URL expires in 15 minutes."""

    download_url_ttl_seconds: Annotated[int, Field(ge=30, le=900)] = 300
    """§15.6: download URL valid for 5 minutes."""

    # ---------------------------------------------------------------- scanner (A-06)
    malware_scanner_backend: Literal["noop", "external"] = "noop"
    malware_scanner_url: str | None = None
    malware_scanner_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 30.0

    # ---------------------------------------------------------------- behaviour limits
    permission_cache_ttl_seconds: Annotated[int, Field(ge=0, le=60)] = 60
    """§5.5: permission changes take effect within 60 seconds."""

    identifier_reveal_rate_per_hour: Annotated[int, Field(ge=1, le=10000)] = 30
    """§24.3 RATE_LIMITED, flagged [ASSUMPTION] in the spec. See DEC-025."""

    job_interval_seconds: Annotated[int, Field(ge=60, le=86400)] = 3600
    """Interval for the one-process scheduler command (§24.7)."""

    idempotency_retention_hours: Annotated[int, Field(ge=1, le=168)] = 24
    """§24.1: a replay within 24 hours returns the original response."""

    default_page_size: Annotated[int, Field(ge=1, le=100)] = 25
    max_page_size: Annotated[int, Field(ge=1, le=100)] = 100
    exact_total_count_limit: Annotated[int, Field(ge=100, le=1_000_000)] = 10_000
    """§26.5: exact total only when the filtered set is at or below this size."""

    cursor_ttl_seconds: Annotated[int, Field(ge=60, le=604800)] = 86400
    """§26.5: cursors are valid for 24 hours."""

    audit_max_query_days: Annotated[int, Field(ge=1, le=3660)] = 366
    """§24.4.8: audit queries span at most 366 days."""

    # ---------------------------------------------------------------- logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    # ---------------------------------------------------------------- first-run bootstrap
    # Read only by `python -m app.cli seed` / `bootstrap`, and only on a database that holds
    # no users and no organizations. Nothing in the request path looks at them, so leaving
    # them unset simply means the first administrator is created some other way.
    seed_admin_email: str | None = None
    seed_admin_password: SecretStr | None = None
    organization_name: str | None = None
    """Name of the organization created alongside the first administrator. Its
    ``organization_code`` is derived from it (§7.2) and its registered address is a
    placeholder the operator is expected to correct."""

    # ---------------------------------------------------------------- validators

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_cors_origins(cls, value: Any) -> Any:
        """Accept a comma-separated list, which is how a .env file expresses one.

        pydantic-settings otherwise parses a ``list[str]`` field as JSON, so the documented
        ``CORS_ORIGINS=http://a,http://b`` form would fail to load at all.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return stripped  # genuine JSON; let pydantic parse it
            return [origin.strip() for origin in stripped.split(",") if origin.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        """Only MySQL is supported (ADR-002); fail loudly rather than half-working."""
        if not value.startswith("mysql+"):
            raise ValueError(
                "database_url must use a MySQL driver (mysql+pymysql://...). "
                "MySQL 8.0+ is the only supported dialect (ADR-002)."
            )
        return value

    @field_validator("api_prefix")
    @classmethod
    def validate_api_prefix(cls, value: str) -> str:
        if not value.startswith("/") or value.endswith("/"):
            raise ValueError("api_prefix must start with '/' and must not end with '/'")
        return value

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        stripped = raw.strip()
        if stripped.lower() in PLACEHOLDER_SECRETS or stripped.startswith("REPLACE_ME"):
            raise ValueError(f"secret_key is still a placeholder: {GENERATE_KEYS_HINT}")
        if len(raw) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"secret_key must be at least {MIN_SECRET_LENGTH} characters: {GENERATE_KEYS_HINT}"
            )
        return value

    @model_validator(mode="after")
    def validate_key_material(self) -> Self:
        """Encryption keys must be present, correctly sized and distinct (§23.7)."""
        if self.key_provider == "env":
            enc = _decode_key(self.encryption_master_key, "encryption_master_key")
            blind = _decode_key(self.blind_index_master_key, "blind_index_master_key")
            if enc == blind:
                raise ValueError(
                    "blind_index_master_key must differ from encryption_master_key: §23.7 "
                    "requires blind-index keys separate from encryption keys"
                )
        elif not self.kms_key_id:
            raise ValueError("kms_key_id is required when key_provider=kms")
        return self

    @model_validator(mode="after")
    def validate_storage(self) -> Self:
        if self.storage_backend == "s3" and not (
            self.storage_s3_bucket and self.storage_s3_quarantine_bucket
        ):
            raise ValueError(
                "storage_s3_bucket and storage_s3_quarantine_bucket are required when "
                "storage_backend=s3 (§15.4 uses a separate quarantine bucket)"
            )
        return self

    @model_validator(mode="after")
    def validate_scanner(self) -> Self:
        if self.malware_scanner_backend == "external" and not self.malware_scanner_url:
            raise ValueError(
                "malware_scanner_url is required when malware_scanner_backend=external"
            )
        return self

    @model_validator(mode="after")
    def validate_oidc(self) -> Self:
        """An OIDC issuer is only usable with an audience and a JWKS endpoint."""
        configured = [self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url]
        if any(configured) and not all(configured):
            raise ValueError(
                "oidc_issuer, oidc_audience and oidc_jwks_url must be configured together"
            )
        return self

    @model_validator(mode="after")
    def validate_production_hardening(self) -> Self:
        """Refuse configurations that are acceptable locally but unsafe in production."""
        if not self.environment.is_production_like:
            return self

        problems: list[str] = []
        if self.debug:
            problems.append("debug must be false")
        if self.db_echo:
            problems.append("db_echo must be false (SQL logs can contain personal data, §25.9)")
        if self.key_provider != "kms":
            problems.append(
                "key_provider must be 'kms': §23.7 requires keys in a managed KMS/HSM, "
                "not in environment files"
            )
        if self.storage_backend != "s3":
            problems.append("storage_backend must be 's3'; local disk storage is development only")
        if self.malware_scanner_backend != "external":
            problems.append("malware_scanner_backend must be 'external' (§15.4 requires scanning)")
        if self.local_login_enabled and not self.oidc_issuer:
            problems.append(
                "configure OIDC or disable local_login_enabled: NFR-12 requires MFA for "
                "privileged roles, which local password login does not provide"
            )
        if "*" in self.cors_origins:
            problems.append("cors_origins must not contain '*'")
        if self.seed_admin_password is not None:
            # A bootstrap password in a production environment file is a privileged
            # credential sitting in plaintext, and §19 of the brief forbids a known-password
            # administrator. The first production account is provisioned through the IdP.
            problems.append(
                "seed_admin_password must not be set: the first-run bootstrap is for "
                "development only (§19)"
            )
        if problems:
            joined = "; ".join(problems)
            raise ValueError(f"unsafe configuration for environment={self.environment}: {joined}")
        return self

    # ---------------------------------------------------------------- derived

    @property
    def oidc_enabled(self) -> bool:
        return self.oidc_issuer is not None

    @property
    def bootstrap_configured(self) -> bool:
        """Whether the first-run bootstrap has every value it needs (all three or none)."""
        return bool(self.seed_admin_email and self.seed_admin_password and self.organization_name)

    def decoded_encryption_key(self) -> bytes:
        """Raw master encryption key bytes. Only valid when ``key_provider == 'env'``."""
        return _decode_key(self.encryption_master_key, "encryption_master_key")

    def decoded_blind_index_key(self) -> bytes:
        """Raw master blind-index key bytes. Only valid when ``key_provider == 'env'``."""
        return _decode_key(self.blind_index_master_key, "blind_index_master_key")


GENERATE_KEYS_HINT = "run 'python -m app.cli generate-keys' and paste the values into .env"


def _decode_key(value: SecretStr | None, name: str) -> bytes:
    """Decode a base64 32-byte key.

    Error messages name the setting and say what to do about it, but never include the value:
    a configuration error is the most likely moment for a key to end up in a log or a
    screenshot. The placeholder case is detected separately, because "must be valid base64" is
    a confusing thing to read when the real problem is that ``.env.example`` was copied without
    being filled in.
    """
    if value is None:
        raise ValueError(f"{name} is required when key_provider=env: {GENERATE_KEYS_HINT}")

    raw_text = value.get_secret_value().strip()
    if not raw_text:
        raise ValueError(f"{name} is empty: {GENERATE_KEYS_HINT}")
    if raw_text.startswith("REPLACE_ME") or raw_text.lower() in PLACEHOLDER_SECRETS:
        raise ValueError(f"{name} is still the placeholder from .env.example: {GENERATE_KEYS_HINT}")

    try:
        raw = base64.b64decode(raw_text, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be base64-encoded 32 bytes: {GENERATE_KEYS_HINT}") from exc
    if len(raw) != 32:
        raise ValueError(
            f"{name} decodes to {len(raw)} bytes but must be exactly 32 "
            f"(AES-256 / HMAC-SHA256): {GENERATE_KEYS_HINT}"
        )
    return raw


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so validation runs once. Tests clear the cache via ``get_settings.cache_clear()``
    after changing the environment.
    """
    # Required fields are supplied by the environment, which pydantic-settings reads.
    return Settings()
