"""Bank account lifecycle, primary invariant and audited reveal (§14)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.context import OrgContext
from app.core.crypto import BlindIndexer, FieldCipher, FieldContext
from app.core.errors import ErrorDetail, ResourceNotFound, ValidationFailed
from app.core.masking import last_four_of, mask_bank_account
from app.core.permissions import Permission
from app.core.warnings import WarningCode, WarningCollector
from app.db.types import uuid7
from app.models.profile import BankVerificationStatus, EmployeeBankAccount
from app.repositories.bank import EmployeeBankAccountRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.organization import OrganizationRepository
from app.schemas.bank import BankAccountResponse
from app.services.organizations import assert_etag
from app.validation.rules import RuleId, check


class BankAccountService:
    def __init__(
        self,
        *,
        accounts: EmployeeBankAccountRepository,
        employees: EmployeeRepository,
        organizations: OrganizationRepository,
        cipher: FieldCipher,
        blind_indexer: BlindIndexer,
        audit: AuditWriter,
    ) -> None:
        self._accounts = accounts
        self._employees = employees
        self._organizations = organizations
        self._cipher = cipher
        self._blind = blind_indexer
        self._audit = audit

    def list_accounts(
        self, context: OrgContext, employee_id: uuid.UUID
    ) -> list[BankAccountResponse]:
        context.require(Permission.BANK_VIEW_MASKED)
        self._employees.require(employee_id, resource="employee")
        return [_response(row) for row in self._accounts.list_for_employee(employee_id)]

    def add_account(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        values: dict[str, Any],
        acknowledged_warnings: list[str],
    ) -> tuple[EmployeeBankAccount, list[str]]:
        context.require(Permission.BANK_EDIT)
        employee = self._employees.require(employee_id, resource="employee")
        ifsc = check(RuleId.IFSC, str(values["ifsc_code"]))
        if not ifsc.ok:
            raise ValidationFailed(
                details=[ErrorDetail("ifsc_code", RuleId.IFSC.value, ifsc.message)]
            )
        number = check(RuleId.BANKACC, str(values["account_number"]))
        if not number.ok:
            raise ValidationFailed(
                details=[ErrorDetail("account_number", RuleId.BANKACC.value, number.message)]
            )
        ifsc_code = ifsc.normalized or str(values["ifsc_code"]).upper()
        account_number = number.normalized or str(values["account_number"])

        scope_id, organization_ids = self._scope(context)
        digest = self._blind.compute(
            f"{ifsc_code}\x00{account_number}",
            scope_id=scope_id,
            identifier_type="BANK_ACCOUNT",
        )
        warnings = WarningCollector(acknowledged_warnings)
        duplicate = self._accounts.find_duplicate(
            blind_index=digest, organization_ids=organization_ids
        )
        duplicate_employee = (
            self._employees.get(duplicate.employee_id) if duplicate is not None else None
        )
        warnings.add_if(
            duplicate is not None,
            WarningCode.BANK_ACCOUNT_DUPLICATE,
            employee_code=(
                duplicate_employee.employee_code if duplicate_employee is not None else ""
            ),
        )
        acknowledged = warnings.enforce()

        if values.get("is_primary"):
            self._employees.lock(employee.id)
            self._close_primary(employee.id, context, None)

        row_id = uuid7()
        ciphertext, key_version = self._cipher.encrypt(
            account_number,
            FieldContext(
                organization_id=context.organization_id,
                entity_type="employee_bank_account",
                entity_id=row_id,
                field="account_number",
            ),
        )
        row = EmployeeBankAccount(
            id=row_id,
            employee_id=employee.id,
            account_holder_name=str(values["account_holder_name"]),
            bank_name=str(values["bank_name"]),
            branch_name=values.get("branch_name"),
            account_number_encrypted=ciphertext,
            account_number_last_four=last_four_of(account_number),
            account_number_blind_index=digest,
            key_version=key_version,
            ifsc_code=ifsc_code,
            account_type=str(values["account_type"]),
            is_primary=bool(values.get("is_primary")),
            verification_status=BankVerificationStatus.UNVERIFIED.value,
            effective_from=values["effective_from"],
            created_by=context.user_id,
        )
        self._accounts.add(row)
        self._accounts.flush()
        self._audit.record_change(
            context,
            action=AuditAction.BANK_ACCOUNT_ADDED,
            entity_type="employee_bank_account",
            entity_id=row.id,
            employee_id=employee.id,
            after={
                "bank_name": row.bank_name,
                "masked": mask_bank_account(row.account_number_last_four),
                "is_primary": row.is_primary,
            },
            fields=["bank_name", "ifsc_code", "account_number"],
            acknowledged_warnings=acknowledged,
        )
        return row, acknowledged

    def update_account(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        account_id: uuid.UUID,
        *,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> EmployeeBankAccount:
        context.require(Permission.BANK_EDIT)
        self._employees.require(employee_id, resource="employee")
        row = self._accounts.require(account_id, resource="bank account")
        if row.employee_id != employee_id:
            raise ResourceNotFound("bank account")
        assert_etag(row.row_version, if_match)
        before = {field: getattr(row, field) for field in changes}
        for field, value in changes.items():
            setattr(row, field, value)
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.BANK_ACCOUNT_CLOSED
            if changes.get("effective_to") is not None
            else AuditAction.BANK_ACCOUNT_ADDED,
            entity_type="employee_bank_account",
            entity_id=row.id,
            employee_id=employee_id,
            before=before,
            after={field: getattr(row, field) for field in changes},
            fields=list(changes),
        )
        return row

    def set_primary(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        account_id: uuid.UUID,
        *,
        effective_from: date,
    ) -> EmployeeBankAccount:
        context.require(Permission.BANK_EDIT)
        self._employees.lock(employee_id)
        row = self._accounts.require(account_id, resource="bank account")
        if row.employee_id != employee_id:
            raise ValidationFailed("That account belongs to another employee.")
        self._close_primary(employee_id, context, row.id)
        row.is_primary = True
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.BANK_PRIMARY_CHANGED,
            entity_type="employee_bank_account",
            entity_id=row.id,
            employee_id=employee_id,
            after={"is_primary": True, "effective_from": effective_from.isoformat()},
            fields=["is_primary"],
        )
        return row

    def verify(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        account_id: uuid.UUID,
        *,
        status: str,
        verification_date: date,
        remarks: str | None,
        bank_proof_document_id: uuid.UUID | None,
    ) -> EmployeeBankAccount:
        context.require(Permission.BANK_VERIFY)
        row = self._accounts.require(account_id, resource="bank account")
        if row.employee_id != employee_id:
            raise ValidationFailed("That account belongs to another employee.")
        if status == BankVerificationStatus.VERIFIED.value and bank_proof_document_id is None:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "bank_proof_document_id",
                        "REQUIRED",
                        "A proof document is required before verification.",
                    )
                ]
            )
        if status == BankVerificationStatus.FAILED.value and not remarks:
            raise ValidationFailed(
                details=[ErrorDetail("remarks", "REQUIRED", "A failure requires a reason.")]
            )
        before = {"verification_status": row.verification_status}
        row.verification_status = status
        row.verification_date = verification_date
        row.verification_remarks = remarks
        row.bank_proof_document_id = bank_proof_document_id
        row.verified_by = context.user_id
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.BANK_VERIFIED,
            entity_type="employee_bank_account",
            entity_id=row.id,
            employee_id=employee_id,
            before=before,
            after={"verification_status": status},
            fields=["verification_status"],
        )
        return row

    def reveal(
        self, context: OrgContext, employee_id: uuid.UUID, account_id: uuid.UUID, reason: str
    ) -> str:
        context.require(Permission.BANK_REVEAL)
        self._employees.require(employee_id, resource="employee")
        row = self._accounts.require(account_id, resource="bank account")
        if row.employee_id != employee_id:
            raise ValidationFailed("That account belongs to another employee.")
        value = self._cipher.decrypt(
            row.account_number_encrypted,
            FieldContext(
                organization_id=context.organization_id,
                entity_type="employee_bank_account",
                entity_id=row.id,
                field="account_number",
            ),
            row.key_version,
        )
        self._audit.record_access(
            context,
            action=AuditAction.BANK_ACCOUNT_REVEALED,
            entity_type="employee_bank_account",
            entity_id=row.id,
            employee_id=employee_id,
            reason=reason,
        )
        return value

    def _close_primary(
        self,
        employee_id: uuid.UUID,
        context: OrgContext,
        keep_id: uuid.UUID | None,
    ) -> None:
        current = self._accounts.primary_for_employee(employee_id)
        if current is not None and current.id != keep_id:
            current.is_primary = False
            current.updated_by = context.user_id

    def _scope(self, context: OrgContext) -> tuple[uuid.UUID, Sequence[uuid.UUID]]:
        organization = self._organizations.get(context.organization_id)
        if organization is None:
            raise ValidationFailed("Unknown organization.")
        if organization.group_id is not None:
            return organization.group_id, self._organizations.organizations_in_group(
                organization.group_id
            )
        return organization.id, [organization.id]


def _response(row: EmployeeBankAccount) -> BankAccountResponse:
    return BankAccountResponse(
        id=row.id,
        account_holder_name=row.account_holder_name,
        bank_name=row.bank_name,
        branch_name=row.branch_name,
        masked_account_number=mask_bank_account(row.account_number_last_four),
        ifsc_code=row.ifsc_code,
        account_type=row.account_type,
        is_primary=row.is_primary,
        verification_status=row.verification_status,
        verification_date=row.verification_date,
        bank_proof_document_id=row.bank_proof_document_id,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        row_version=row.row_version,
        created_at=row.created_at,
    )


__all__ = ["BankAccountService"]
