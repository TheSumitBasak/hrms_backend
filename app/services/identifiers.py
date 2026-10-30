"""Encrypted identifiers, duplicate detection, masking and audited reveal (§11)."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import date
from typing import Any

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.context import OrgContext
from app.core.crypto import BlindIndexer, FieldCipher, FieldContext
from app.core.errors import (
    DuplicateIdentifier,
    ErrorDetail,
    FieldNotCollected,
    RateLimited,
    ResourceNotFound,
    ValidationFailed,
)
from app.core.masking import IdentifierType, last_four_of, mask_identifier
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.identifier import EmployeeIdentifier
from app.models.org_config import IdentifierRequirement
from app.repositories.employee import EmployeeRepository
from app.repositories.identifier import EmployeeIdentifierRepository
from app.repositories.organization import (
    IdentifierPolicyRepository,
    OrganizationRepository,
)
from app.schemas.identifier import IdentifierResponse
from app.validation.rules import RuleId, check

TYPE_RULES: dict[str, RuleId] = {
    IdentifierType.PAN.value: RuleId.PAN,
    IdentifierType.AADHAAR.value: RuleId.AADHAAR,
    IdentifierType.UAN.value: RuleId.UAN,
    IdentifierType.PF_MEMBER_ID.value: RuleId.PFMID,
    IdentifierType.PREVIOUS_PF_MEMBER_ID.value: RuleId.PFMID,
    IdentifierType.ESIC_IP.value: RuleId.ESIC_IP,
    IdentifierType.PASSPORT.value: RuleId.PASSPORT,
    IdentifierType.VOTER_ID.value: RuleId.EPIC,
    IdentifierType.DRIVING_LICENSE.value: RuleId.DL_IN,
}

MULTI_VALUE_TYPES = {IdentifierType.PREVIOUS_PF_MEMBER_ID.value}

_REVEAL_WINDOW_SECONDS = 3600
_reveal_log: dict[tuple[uuid.UUID, uuid.UUID], list[float]] = {}
_reveal_lock = threading.Lock()


class IdentifierService:
    def __init__(
        self,
        *,
        identifiers: EmployeeIdentifierRepository,
        employees: EmployeeRepository,
        organizations: OrganizationRepository,
        policies: IdentifierPolicyRepository,
        cipher: FieldCipher,
        blind_indexer: BlindIndexer,
        audit: AuditWriter,
        reveal_limit_per_hour: int,
    ) -> None:
        self._identifiers = identifiers
        self._employees = employees
        self._organizations = organizations
        self._policies = policies
        self._cipher = cipher
        self._blind = blind_indexer
        self._audit = audit
        self._reveal_limit = reveal_limit_per_hour

    # ---------------------------------------------------------------- reads

    def list_identifiers(
        self, context: OrgContext, employee_id: uuid.UUID, *, current_only: bool = True
    ) -> list[IdentifierResponse]:
        context.require(Permission.IDENTITY_VIEW_MASKED)
        self._employees.require(employee_id, resource="employee")
        rows = self._identifiers.list_for_employee(employee_id, current_only=current_only)
        return [
            IdentifierResponse(
                id=row.id,
                identifier_type=row.identifier_type,
                masked_value=mask_identifier(row.identifier_type, row.value_last_four),
                is_current=row.is_current,
                issuing_country=row.issuing_country,
                issue_date=row.issue_date,
                expiry_date=row.expiry_date,
                verification_status=row.verification_status,
                verified_at=row.verified_at,
                created_at=row.created_at,
            )
            for row in rows
        ]

    # ---------------------------------------------------------------- writes

    def put_identifier(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        identifier_type: str,
        values: dict[str, Any],
    ) -> EmployeeIdentifier:
        context.require(Permission.IDENTITY_EDIT)
        employee = self._employees.require(employee_id, resource="employee")
        try:
            kind = IdentifierType(identifier_type)
        except ValueError as error:
            raise ValidationFailed(
                details=[ErrorDetail("identifier_type", "UNKNOWN", "Unknown identifier type.")]
            ) from error
        self._assert_collectable(context, kind)
        normalized = self._validate_value(kind, values)

        scope_id, organization_ids = self._duplicate_scope(context)
        digest = self._blind.compute(
            normalized,
            scope_id=scope_id,
            identifier_type=kind.value,
        )
        duplicates = self._identifiers.find_duplicates(
            identifier_type=kind.value,
            blind_index=digest,
            organization_ids=organization_ids,
            exclude_employee_id=employee_id,
        )
        override_reason = values.get("duplicate_override_reason")
        if duplicates:
            if not override_reason:
                raise DuplicateIdentifier()
            context.require(Permission.IDENTITY_DUPLICATE_OVERRIDE)

        row_id = uuid7()
        ciphertext, key_version = self._cipher.encrypt(
            normalized,
            FieldContext(
                organization_id=context.organization_id,
                entity_type="employee_identifier",
                entity_id=row_id,
                field=kind.value,
            ),
        )

        if kind.value not in MULTI_VALUE_TYPES:
            for existing in self._identifiers.current_for_type(employee.id, kind.value):
                existing.is_current = False

        row = EmployeeIdentifier(
            id=row_id,
            employee_id=employee.id,
            identifier_type=kind.value,
            value_encrypted=ciphertext,
            value_blind_index=digest,
            value_last_four=last_four_of(normalized),
            key_version=key_version,
            issuing_country=values.get("issuing_country"),
            issue_date=values.get("issue_date"),
            expiry_date=values.get("expiry_date"),
            issue_place=values.get("issue_place"),
            is_current=True,
            duplicate_override_reason=override_reason,
            created_by=context.user_id,
        )
        self._identifiers.add(row)
        self._identifiers.flush()

        if duplicates:
            self._audit.record_change(
                context,
                action=AuditAction.DUPLICATE_OVERRIDE,
                entity_type="employee_identifier",
                entity_id=row.id,
                employee_id=employee.id,
                after={"identifier_type": kind.value},
                fields=["identifier_type"],
                reason=override_reason,
            )
        self._audit.record_change(
            context,
            action=(
                AuditAction.IDENTIFIER_CHANGED
                if duplicates or kind.value in MULTI_VALUE_TYPES
                else AuditAction.IDENTIFIER_ADDED
            ),
            entity_type="employee_identifier",
            entity_id=row.id,
            employee_id=employee.id,
            after={
                "identifier_type": kind.value,
                "masked": mask_identifier(kind, row.value_last_four),
            },
            fields=["identifier_type"],
        )
        return row

    def reveal(
        self, context: OrgContext, employee_id: uuid.UUID, identifier_type: str, reason: str
    ) -> str:
        context.require(Permission.IDENTITY_REVEAL)
        employee = self._employees.require(employee_id, resource="employee")
        rows = list(self._identifiers.current_for_type(employee.id, identifier_type))
        if not rows:
            raise ResourceNotFound("identifier")
        self._rate_limit(context)
        row = rows[0]
        plaintext = self._cipher.decrypt(
            row.value_encrypted,
            FieldContext(
                organization_id=context.organization_id,
                entity_type="employee_identifier",
                entity_id=row.id,
                field=row.identifier_type,
            ),
            row.key_version,
        )
        self._audit.record_access(
            context,
            action=AuditAction.IDENTIFIER_REVEALED,
            entity_type="employee_identifier",
            entity_id=row.id,
            employee_id=employee.id,
            reason=reason,
        )
        return plaintext

    def duplicate_check(
        self, context: OrgContext, identifier_type: IdentifierType, value: str
    ) -> list[dict[str, Any]]:
        context.require(Permission.EMPLOYEE_CREATE)
        kind = IdentifierType(identifier_type)
        self._assert_collectable(context, kind)
        normalized = self._validate_value(kind, {"value": value})
        scope_id, organization_ids = self._duplicate_scope(context)
        digest = self._blind.compute(normalized, scope_id=scope_id, identifier_type=kind.value)
        matches = self._identifiers.find_duplicates(
            identifier_type=kind.value,
            blind_index=digest,
            organization_ids=organization_ids,
        )
        self._audit.record_access(
            context,
            action=AuditAction.IDENTIFIER_SEARCHED,
            entity_type="employee_identifier",
            entity_id=matches[0].id if matches else context.organization_id,
            reason=None,
        )
        result = []
        for row in matches:
            employee = self._employees.get(row.employee_id)
            if employee is None:
                continue
            result.append(
                {
                    "employee_id": employee.id,
                    "employee_code": employee.employee_code,
                    "display_name": employee.display_name,
                    "masked_value": mask_identifier(kind, row.value_last_four),
                }
            )
        return result

    def search_employees(
        self, context: OrgContext, identifier_type: str, value: str
    ) -> list[uuid.UUID]:
        """Blind-index search for employees by identifier (§26.2). Audited."""
        context.require(Permission.IDENTITY_REVEAL)
        kind = IdentifierType(identifier_type)
        searchable = {
            IdentifierType.PAN,
            IdentifierType.UAN,
            IdentifierType.PF_MEMBER_ID,
            IdentifierType.ESIC_IP,
            IdentifierType.PASSPORT,
        }
        if kind not in searchable:
            raise ValidationFailed("That identifier type is not searchable.")
        self._assert_collectable(context, kind)
        normalized = self._validate_value(kind, {"value": value})
        scope_id, organization_ids = self._duplicate_scope(context)
        digest = self._blind.compute(normalized, scope_id=scope_id, identifier_type=kind.value)
        matches = self._identifiers.find_duplicates(
            identifier_type=kind.value,
            blind_index=digest,
            organization_ids=organization_ids,
        )
        self._audit.record_access(
            context,
            action=AuditAction.IDENTIFIER_SEARCHED,
            entity_type="employee_identifier",
            entity_id=matches[0].id if matches else context.organization_id,
        )
        return [row.employee_id for row in matches]

    # ---------------------------------------------------------------- internals

    def _duplicate_scope(self, context: OrgContext) -> tuple[uuid.UUID, list[uuid.UUID]]:
        organization = self._organizations.get(context.organization_id)
        if organization is None:
            raise ValidationFailed("Unknown organization.")
        if organization.group_id is not None:
            return organization.group_id, list(
                self._organizations.organizations_in_group(organization.group_id)
            )
        return organization.id, [organization.id]

    def _assert_collectable(self, context: OrgContext, kind: IdentifierType) -> None:
        policy = self._policies.get_by_type(kind.value)
        if policy is None or policy.requirement == IdentifierRequirement.NOT_COLLECTED.value:
            raise FieldNotCollected(
                details=[
                    ErrorDetail(
                        "identifier_type",
                        "FIELD_NOT_COLLECTED",
                        f"{kind.value} is not collected by this organization.",
                    )
                ]
            )

    def _validate_value(self, kind: IdentifierType, values: dict[str, Any]) -> str:
        rule = TYPE_RULES[kind.value]
        outcome = check(
            rule,
            str(values["value"]),
            country=str(values.get("issuing_country") or "IN"),
        )
        if not outcome.ok:
            raise ValidationFailed(details=[ErrorDetail("value", rule.value, outcome.message)])
        if kind is IdentifierType.PASSPORT and (
            not values.get("issuing_country") or not values.get("expiry_date")
        ):
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "expiry_date",
                        "REQUIRED",
                        "A passport requires an issuing country and an expiry date.",
                    )
                ]
            )
        expire = values.get("expiry_date")
        issued = values.get("issue_date")
        if isinstance(expire, date) and isinstance(issued, date) and expire <= issued:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "expiry_date", "INVALID_INTERVAL", "expiry_date must be after issue_date."
                    )
                ]
            )
        return outcome.normalized or str(values["value"])

    def _rate_limit(self, context: OrgContext) -> None:
        key = (context.user_id, context.organization_id)
        now = time.monotonic()
        with _reveal_lock:
            stamps = [
                stamp for stamp in _reveal_log.get(key, []) if now - stamp < _REVEAL_WINDOW_SECONDS
            ]
            if len(stamps) >= self._reveal_limit:
                raise RateLimited("Too many reveals; try again in a minute.")
            stamps.append(now)
            _reveal_log[key] = stamps


__all__ = ["IdentifierService"]
