"""Audit event construction and the §20.3 redaction rules.

An audit row must describe *what changed* without becoming a second copy of the personal data
(§20.3, and §9 of the build brief: "Audit events should identify the action and resource
without unnecessarily duplicating personal information").

:func:`build_change_set` is the one place field diffs are produced. It classifies each field:

* **sensitive identifier** — recorded as the masked before/after (last four only);
* **sensitive, non-identifier** — recorded as ``"***changed***"``;
* **never recorded** — passwords, tokens, keys, ciphertext, file bytes; dropped entirely;
* **ordinary** — recorded as-is.

Salary amounts are deliberately in the "ordinary" class: §20.3 permits them in before/after
values, and §20.3 gates *reading* those rows behind ``salary.view``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.audit.actions import REASON_REQUIRED_ACTIONS, AuditAction
from app.core.masking import AUDIT_REDACTED, IdentifierType, audit_safe_value
from app.core.permissions import Role

NEVER_AUDITED_FIELDS: frozenset[str] = frozenset(
    {
        "password",
        "password_hash",
        "token",
        "token_hash",
        "refresh_token",
        "access_token",
        "secret",
        "secret_key",
        "encryption_key",
        "blind_index_key",
        "key_material",
        "value_encrypted",
        "value_blind_index",
        "account_number_encrypted",
        "account_number_blind_index",
        "document_number_encrypted",
        "file_bytes",
        "storage_key",
    }
)
"""§20.3: audit records MUST NOT contain passwords, tokens, secrets, encryption keys, full
identifiers or file contents. ``storage_key`` is included because §15.3 marks it ``[R]`` and
"never returned by API"."""

MASKED_IDENTIFIER_FIELDS: dict[str, IdentifierType] = {
    "pan": IdentifierType.PAN,
    "aadhaar_number": IdentifierType.AADHAAR,
    "uan": IdentifierType.UAN,
    "pf_member_id": IdentifierType.PF_MEMBER_ID,
    "previous_pf_member_id": IdentifierType.PREVIOUS_PF_MEMBER_ID,
    "esic_ip_number": IdentifierType.ESIC_IP,
    "passport_number": IdentifierType.PASSPORT,
    "voter_id": IdentifierType.VOTER_ID,
    "driving_license_number": IdentifierType.DRIVING_LICENSE,
}
"""§20.3: "Identifier changes are recorded as field name + masked before/after (last 4)"."""

REDACTED_VALUE_FIELDS: frozenset[str] = frozenset(
    {
        "account_number",
        "account_number_last_four",
        "value",
        "value_last_four",
        "document_number",
        "remarks",
        "personal_mobile",
        "alternate_mobile",
        "personal_email",
        "emergency_contact_phone",
        "emergency_contact_alternate_phone",
        "reference_contact",
        "last_drawn_ctc",
    }
)
"""Sensitive or personal values recorded only as "changed". Contact numbers and remarks are
personal data whose *content* an audit trail does not need in order to be useful."""


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """A field-level diff ready for ``audit_logs.changed_fields``/``before``/``after``."""

    changed_fields: list[str] = field(default_factory=list)
    before_values: dict[str, Any] = field(default_factory=dict)
    after_values: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.changed_fields


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One audit row, before persistence (§20.1).

    Built by a service and handed to the writer, which inserts it inside the same transaction
    as the business change (NFR-10).
    """

    action: AuditAction
    entity_type: str
    entity_id: uuid.UUID
    actor_user_id: uuid.UUID
    actor_role_codes: str
    organization_id: uuid.UUID | None = None
    employee_id: uuid.UUID | None = None
    changes: ChangeSet | None = None
    reason: str | None = None
    acknowledged_warnings: list[str] = field(default_factory=list)
    request_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.action in REASON_REQUIRED_ACTIONS and not (self.reason and self.reason.strip()):
            # Failing here rather than writing an incomplete row: §20.1 marks the reason
            # mandatory for exactly these actions, and a reveal without a reason is the kind
            # of gap an audit is supposed to make impossible.
            raise ValueError(f"audit action {self.action.value} requires a reason")


def format_actor_roles(roles: frozenset[Role]) -> str:
    """§20.1 ``actor_role_codes``: "Roles active at time of action, sorted"."""
    return ",".join(sorted(role.value for role in roles))


def build_change_set(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    fields: list[str] | None = None,
) -> ChangeSet:
    """Diff two field mappings, applying the §20.3 redaction rules.

    ``fields`` restricts the comparison to a known set, which is how a PATCH records only the
    fields the caller actually submitted rather than every column on the row.
    """
    before = before or {}
    after = after or {}
    candidate_fields = fields if fields is not None else sorted(set(before) | set(after))

    changed: list[str] = []
    before_values: dict[str, Any] = {}
    after_values: dict[str, Any] = {}

    for name in candidate_fields:
        if name in NEVER_AUDITED_FIELDS:
            continue
        old = before.get(name)
        new = after.get(name)
        if old == new:
            continue
        changed.append(name)
        before_values[name] = _audit_value(name, old)
        after_values[name] = _audit_value(name, new)

    return ChangeSet(changed_fields=changed, before_values=before_values, after_values=after_values)


def _audit_value(name: str, value: Any) -> Any:
    """Reduce one field value to its audit-safe form.

    Field-name rules come first: a field named in :data:`MASKED_IDENTIFIER_FIELDS` or
    :data:`REDACTED_VALUE_FIELDS` is reduced regardless of its runtime type, so a value
    arriving as an unexpected type cannot bypass redaction.
    """
    if value is None:
        return None
    if (identifier_type := MASKED_IDENTIFIER_FIELDS.get(name)) is not None:
        return audit_safe_value(str(value), identifier_type=identifier_type)
    if name in REDACTED_VALUE_FIELDS:
        return AUDIT_REDACTED
    return _as_json_scalar(value)


def _as_json_scalar(value: Any) -> Any:
    """Render a non-sensitive value as something JSON can hold."""
    if isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes):
        # Any raw bytes reaching here is ciphertext or a digest; never record it.
        return AUDIT_REDACTED
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def assert_audit_payload_is_safe(payload: dict[str, Any]) -> None:
    """Fail loudly if a payload still contains something §20.3 forbids.

    Called by the writer as a last line of defence. A bug that puts a full Aadhaar into an
    audit row is unrecoverable once committed — the row is append-only by design — so this
    check exists even though :func:`build_change_set` should already have prevented it.
    """
    for key, value in payload.items():
        if key in NEVER_AUDITED_FIELDS:
            raise ValueError(f"audit payload must not contain {key!r} (§20.3)")
        if isinstance(value, bytes):
            raise ValueError(f"audit payload field {key!r} contains raw bytes (§20.3)")
        if key in MASKED_IDENTIFIER_FIELDS and isinstance(value, str) and value.isalnum():
            # A masked identifier always contains a mask character; a purely alphanumeric
            # value means an unmasked identifier slipped through.
            raise ValueError(f"audit payload field {key!r} appears to be unmasked (§20.3)")


__all__ = [
    "MASKED_IDENTIFIER_FIELDS",
    "NEVER_AUDITED_FIELDS",
    "REDACTED_VALUE_FIELDS",
    "AuditEvent",
    "ChangeSet",
    "assert_audit_payload_is_safe",
    "build_change_set",
    "format_actor_roles",
]
