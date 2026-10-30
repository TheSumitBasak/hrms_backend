"""Privacy notices, exports, erasure, legal hold and retention execution (§25)."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import utc_now
from app.core.context import OrgContext
from app.core.crypto import FieldCipher, FieldContext
from app.core.errors import (
    ErrorDetail,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.employee import SEPARATED_STATUSES, Employee
from app.models.privacy import (
    EmployeeErasureRequest,
    EmployeePrivacyNotice,
    ErasureRequestStatus,
    ProcessingBasis,
    RetentionPurgeBatch,
)
from app.repositories.bank import EmployeeBankAccountRepository
from app.repositories.document import EmployeeDocumentRepository
from app.repositories.employee import EmployeeAddressRepository, EmployeeRepository
from app.repositories.identifier import EmployeeIdentifierRepository
from app.repositories.organization import OrganizationRepository
from app.repositories.privacy import (
    ErasureRequestRepository,
    PrivacyNoticeRepository,
    RetentionBatchRepository,
)
from app.repositories.profile import NominationRepository
from app.repositories.salary import (
    AssignmentComponentRepository,
    SalaryAssignmentRepository,
)
from app.storage.local import LocalStorage, SignedUrlSigner

EXPORT_TTL_HOURS = 72
EMPLOYEE_RETENTION_YEARS = 8


def _otp_key(password: str) -> bytes:
    return hashlib.sha256(password.encode("utf-8")).digest()


def encrypt_export(payload: dict[str, Any], password: str) -> bytes:
    nonce = secrets.token_bytes(12)
    data = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
    return nonce + AESGCM(_otp_key(password)).encrypt(nonce, data, None)


def decrypt_export(blob: bytes, password: str) -> bytes:
    if len(blob) < 13:
        raise ValueError("malformed export")
    nonce, ciphertext = blob[:12], blob[12:]
    return AESGCM(_otp_key(password)).decrypt(nonce, ciphertext, None)


class PrivacyService:
    def __init__(
        self,
        *,
        notices: PrivacyNoticeRepository,
        erasures: ErasureRequestRepository,
        batches: RetentionBatchRepository,
        employees: EmployeeRepository,
        addresses: EmployeeAddressRepository,
        identifiers: EmployeeIdentifierRepository,
        bank: EmployeeBankAccountRepository,
        documents: EmployeeDocumentRepository,
        assignments: SalaryAssignmentRepository,
        assignment_components: AssignmentComponentRepository,
        nominations: NominationRepository,
        organizations: OrganizationRepository,
        cipher: FieldCipher,
        storage: LocalStorage,
        signer: SignedUrlSigner,
        audit: AuditWriter,
        export_ttl_hours: int = EXPORT_TTL_HOURS,
    ) -> None:
        self._notices = notices
        self._erasures = erasures
        self._batches = batches
        self._employees = employees
        self._addresses = addresses
        self._identifiers = identifiers
        self._bank = bank
        self._documents = documents
        self._assignments = assignments
        self._assignment_components = assignment_components
        self._nominations = nominations
        self._organizations = organizations
        self._cipher = cipher
        self._storage = storage
        self._signer = signer
        self._audit = audit
        self._export_ttl = export_ttl_hours

    # ---------------------------------------------------------------- notices

    def list_notices(self, context: OrgContext, employee_id: uuid.UUID) -> Any:
        context.require_any(Permission.EMPLOYEE_VIEW_PERSONAL, Permission.EMPLOYEE_VIEW)
        self._employees.require(employee_id, resource="employee")
        return self._notices.list_for_employee(employee_id)

    def create_notice(
        self, context: OrgContext, employee_id: uuid.UUID, values: dict[str, Any]
    ) -> EmployeePrivacyNotice:
        context.require(Permission.EMPLOYEE_EDIT)
        employee = self._employees.require(employee_id, resource="employee")
        if values["basis"] == ProcessingBasis.CONSENT.value and not values.get("consent_scope"):
            raise ValidationFailed(
                details=[ErrorDetail("consent_scope", "REQUIRED", "Consent needs a scope.")]
            )
        if values.get("acknowledged_at") and values["acknowledged_at"] < values["delivered_at"]:
            raise ValidationFailed("acknowledged_at must not precede delivered_at.")
        row = EmployeePrivacyNotice(
            id=uuid7(),
            employee_id=employee.id,
            notice_version=values["notice_version"],
            basis=values["basis"],
            consent_scope=values.get("consent_scope"),
            delivered_at=values["delivered_at"],
            method=values.get("method") or "ELECTRONIC",
            document_id=values.get("document_id"),
            created_by=context.user_id,
        )
        self._notices.add(row)
        self._audit.record_change(
            context,
            action=AuditAction.PRIVACY_NOTICE_RECORDED,
            entity_type="employee_privacy_notice",
            entity_id=row.id,
            employee_id=employee.id,
            after={"notice_version": row.notice_version, "basis": row.basis},
            fields=["notice_version", "basis"],
        )
        return row

    def acknowledge_notice(
        self, context: OrgContext, employee_id: uuid.UUID, notice_id: uuid.UUID
    ) -> EmployeePrivacyNotice:
        context.require(Permission.EMPLOYEE_EDIT)
        row = self._notices.require_for_employee(employee_id, notice_id)
        row.acknowledged_at = utc_now()
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.PRIVACY_NOTICE_ACKNOWLEDGED,
            entity_type="employee_privacy_notice",
            entity_id=row.id,
            employee_id=employee_id,
            after={"acknowledged_at": row.acknowledged_at},
            fields=["acknowledged_at"],
        )
        return row

    def withdraw_notice(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        notice_id: uuid.UUID,
        reason: str | None,
    ) -> EmployeePrivacyNotice:
        context.require(Permission.EMPLOYEE_EDIT)
        row = self._notices.require_for_employee(employee_id, notice_id)
        if row.basis != ProcessingBasis.CONSENT.value:
            raise ValidationFailed("Only consent-based notices can be withdrawn.")
        row.withdrawn_at = utc_now()
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.PRIVACY_CONSENT_WITHDRAWN,
            entity_type="employee_privacy_notice",
            entity_id=row.id,
            employee_id=employee_id,
            after={"withdrawn_at": row.withdrawn_at},
            fields=["withdrawn_at"],
            reason=reason,
        )
        return row

    # ---------------------------------------------------------------- export

    def export_employee(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        *,
        unmasked: bool,
        include_documents: bool,
    ) -> tuple[uuid.UUID, str, datetime, str, bool]:
        context.require(Permission.EMPLOYEE_VIEW_PERSONAL)
        context.require(Permission.AUDIT_VIEW)
        if unmasked:
            context.require(Permission.IDENTITY_REVEAL)
            context.require(Permission.BANK_REVEAL)
        employee = self._employees.require(employee_id, resource="employee")
        payload = self._export_payload(
            context, employee, unmasked=unmasked, include_documents=include_documents
        )
        password = secrets.token_urlsafe(12)
        blob = encrypt_export(payload, password)
        export_id = uuid7()
        key = f"exports/{context.organization_id.hex}/{employee.id.hex}/{export_id.hex}.json.enc"
        self._storage.put(key, blob)
        token, expires = self._signer.issue(
            key=key,
            purpose="export",
            ttl_seconds=self._export_ttl * 3600,
            organization_id=context.organization_id.hex,
        )
        self._audit.record_access(
            context,
            action=AuditAction.DATA_EXPORT_GENERATED,
            entity_type="employee",
            entity_id=employee.id,
            employee_id=employee.id,
        )
        return (
            export_id,
            f"/api/v1/storage/export/{token}",
            datetime.fromtimestamp(expires, tz=UTC),
            password,
            not unmasked,
        )

    def _export_payload(
        self,
        context: OrgContext,
        employee: Employee,
        *,
        unmasked: bool,
        include_documents: bool,
    ) -> dict[str, Any]:
        personal = {
            key: getattr(employee, key)
            for key in (
                "id",
                "employee_code",
                "first_name",
                "middle_name",
                "last_name",
                "display_name",
                "date_of_birth",
                "gender",
                "marital_status",
                "nationality",
                "personal_mobile",
                "alternate_mobile",
                "personal_email",
                "official_email",
                "joining_date",
                "employment_end_date",
                "status",
            )
        }
        current = self._addresses.get_by_type(employee.id, "CURRENT")
        permanent = self._addresses.get_by_type(employee.id, "PERMANENT")
        identifiers = []
        for identifier_row in self._identifiers.list_for_employee(employee.id, current_only=True):
            value = f"****{identifier_row.value_last_four}"
            if unmasked:
                value = self._cipher.decrypt(
                    identifier_row.value_encrypted,
                    FieldContext(
                        organization_id=context.organization_id,
                        entity_type="employee_identifier",
                        entity_id=identifier_row.id,
                        field=identifier_row.identifier_type,
                    ),
                    identifier_row.key_version,
                )
            identifiers.append({"type": identifier_row.identifier_type, "value": value})
        accounts = []
        for account_row in self._bank.list_for_employee(employee.id):
            account = f"****{account_row.account_number_last_four}"
            if unmasked:
                account = self._cipher.decrypt(
                    account_row.account_number_encrypted,
                    FieldContext(
                        organization_id=context.organization_id,
                        entity_type="employee_bank_account",
                        entity_id=account_row.id,
                        field="account_number",
                    ),
                    account_row.key_version,
                )
            accounts.append(
                {
                    "bank_name": account_row.bank_name,
                    "ifsc": account_row.ifsc_code,
                    "account_number": account,
                    "is_primary": account_row.is_primary,
                }
            )
        documents = []
        if include_documents:
            documents = [
                {
                    "id": row.id,
                    "title": row.document_title,
                    "file_name": row.original_file_name,
                    "verification_status": row.verification_status,
                }
                for row in self._documents.list_for_employee(employee.id, include_versions=True)
            ]
        salary = []
        if context.has(Permission.SALARY_VIEW):
            for assignment_row in self._assignments.list_for_employee(
                employee.id, include_cancelled=True
            ):
                salary.append(
                    {
                        "id": assignment_row.id,
                        "effective_from": assignment_row.effective_from,
                        "effective_to": assignment_row.effective_to,
                        "status": assignment_row.assignment_status,
                        "currency": assignment_row.currency,
                        "components": [
                            {
                                "code": item.component_code_snapshot,
                                "name": item.component_name_snapshot,
                                "value": _component_value(item),
                            }
                            for item in self._assignment_components.list_for_assignment(
                                assignment_row.id
                            )
                        ],
                    }
                )
        return {
            "exported_at": utc_now(),
            "employee": personal,
            "current_address": _address(current),
            "permanent_address": _address(permanent),
            "identifiers": identifiers,
            "bank_accounts": accounts,
            "documents": documents,
            "salary": salary,
            "nominations": [
                {
                    "type": row.nomination_type,
                    "effective_from": row.effective_from,
                    "name": row.nominee_name,
                    "share": str(row.share_percentage),
                }
                for row in self._nominations.list_sets(employee.id)
            ],
        }

    # ---------------------------------------------------------------- erasure and hold

    def set_legal_hold(
        self, context: OrgContext, employee_id: uuid.UUID, *, hold: bool, reason: str
    ) -> Employee:
        context.require(Permission.EMPLOYEE_ARCHIVE)
        employee = self._employees.require(employee_id, resource="employee")
        employee.legal_hold = hold
        employee.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.LEGAL_HOLD_CHANGED,
            entity_type="employee",
            entity_id=employee.id,
            employee_id=employee.id,
            after={"legal_hold": hold},
            fields=["legal_hold"],
            reason=reason,
        )
        return employee

    def create_erasure(
        self, context: OrgContext, employee_id: uuid.UUID, note: str | None
    ) -> EmployeeErasureRequest:
        context.require(Permission.EMPLOYEE_VIEW_PERSONAL)
        employee = self._employees.require(employee_id, resource="employee")
        row = EmployeeErasureRequest(
            id=uuid7(),
            employee_id=employee.id,
            requester_note=note,
            status=ErasureRequestStatus.REQUESTED.value,
            created_by=context.user_id,
        )
        self._erasures.add(row)
        self._audit.record_change(
            context,
            action=AuditAction.ERASURE_REQUESTED,
            entity_type="employee_erasure_request",
            entity_id=row.id,
            employee_id=employee.id,
            after={"status": row.status},
            fields=["status"],
        )
        return row

    def list_erasures(self, context: OrgContext, employee_id: uuid.UUID) -> Any:
        context.require_any(Permission.EMPLOYEE_VIEW_PERSONAL, Permission.EMPLOYEE_ARCHIVE)
        self._employees.require(employee_id, resource="employee")
        return self._erasures.list_for_employee(employee_id)

    def decide_erasure(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        request_id: uuid.UUID,
        *,
        decision: str,
        reason: str | None,
    ) -> EmployeeErasureRequest:
        context.require(Permission.EMPLOYEE_ARCHIVE)
        row = self._erasures.require_for_employee(employee_id, request_id)
        if row.status != ErasureRequestStatus.REQUESTED.value:
            raise ValidationFailed("This request has already been decided.")
        employee = self._employees.require(employee_id, resource="employee")
        row.reviewed_by = context.user_id
        row.reviewed_at = utc_now()
        if decision == "REFUSED":
            if not reason:
                raise ValidationFailed("A refusal requires a reason.")
            row.status = ErasureRequestStatus.REFUSED.value
            row.refusal_reason = reason
        else:
            if employee.legal_hold:
                raise ValidationFailed("A legal hold blocks erasure.")
            fields = self._anonymize(context, employee)
            row.status = ErasureRequestStatus.COMPLETED.value
            row.completed_at = utc_now()
            row.anonymized_fields = fields
        self._audit.record_change(
            context,
            action=AuditAction.ERASURE_DECIDED,
            entity_type="employee_erasure_request",
            entity_id=row.id,
            employee_id=employee_id,
            after={"status": row.status},
            fields=["status"],
            reason=reason or "Approved data-principal erasure request",
        )
        return row

    def _anonymize(self, context: OrgContext, employee: Employee) -> list[str]:
        fields = [
            "first_name",
            "middle_name",
            "last_name",
            "display_name",
            "father_name",
            "mother_name",
            "spouse_name",
            "date_of_birth",
            "gender",
            "marital_status",
            "blood_group",
            "religion",
            "social_category",
            "disability_type",
            "disability_percentage",
            "personal_mobile",
            "alternate_mobile",
            "personal_email",
            "official_email",
            "emergency_contact_name",
            "emergency_contact_relationship",
            "emergency_contact_phone",
            "emergency_contact_alternate_phone",
            "remarks",
        ]
        employee.first_name = "ANON"
        employee.middle_name = None
        employee.last_name = None
        employee.display_name = "Anonymized Employee"
        employee.full_name_normalized = "ANON"
        employee.father_name = None
        employee.mother_name = None
        employee.spouse_name = None
        employee.date_of_birth = None
        employee.gender = None
        employee.marital_status = None
        employee.blood_group = None
        employee.religion = None
        employee.social_category = None
        employee.disability_status = None
        employee.disability_type = None
        employee.disability_percentage = None
        employee.personal_mobile = None
        employee.alternate_mobile = None
        employee.personal_email = None
        employee.official_email = None
        employee.emergency_contact_name = None
        employee.emergency_contact_relationship = None
        employee.emergency_contact_phone = None
        employee.emergency_contact_alternate_phone = None
        employee.remarks = None
        employee.updated_by = context.user_id
        for address in self._addresses.list_for_employee(employee.id):
            self._addresses.session.delete(address)
        for identifier in self._identifiers.list_for_employee(employee.id):
            self._identifiers.session.delete(identifier)
        for account in self._bank.list_for_employee(employee.id):
            self._bank.session.delete(account)
        for nomination in self._nominations.list_sets(employee.id):
            self._nominations.session.delete(nomination)
        for document in self._documents.list_for_employee(
            employee.id, include_archived=True, include_versions=True
        ):
            with suppress(OSError):
                self._storage.delete(document.storage_key)
            self._documents.session.delete(document)
        for assignment in self._assignments.list_for_employee(employee.id, include_cancelled=True):
            for component in self._assignment_components.list_for_assignment(assignment.id):
                self._assignment_components.session.delete(component)
        self._audit.record_change(
            context,
            action=AuditAction.EMPLOYEE_ANONYMIZED,
            entity_type="employee",
            entity_id=employee.id,
            employee_id=employee.id,
            after={"anonymized_fields": fields},
            fields=["anonymized_fields"],
        )
        return fields

    # ---------------------------------------------------------------- retention batches

    def propose_batch(
        self, context: OrgContext, scope: str, entity_ids: list[uuid.UUID]
    ) -> RetentionPurgeBatch:
        context.require_any(Permission.ORG_MANAGE, Permission.EMPLOYEE_ARCHIVE)
        if not entity_ids:
            raise ValidationFailed("A batch must list affected records.")
        row = RetentionPurgeBatch(
            id=uuid7(),
            status="PROPOSED",
            scope_description=scope,
            affected_record_count=len(entity_ids),
            affected_entity_ids=[str(item) for item in entity_ids],
        )
        self._batches.add(row)
        self._audit.record_change(
            context,
            action=AuditAction.RETENTION_BATCH_PROPOSED,
            entity_type="retention_purge_batch",
            entity_id=row.id,
            after={"affected_record_count": row.affected_record_count},
            fields=["affected_record_count"],
        )
        return row

    def approve_batch(self, context: OrgContext, batch_id: uuid.UUID) -> RetentionPurgeBatch:
        context.require(Permission.EMPLOYEE_ARCHIVE)
        row = self._batches.require(batch_id, resource="retention batch")
        if row.status != "PROPOSED":
            raise ValidationFailed("Only a proposed batch can be approved.")
        if row.approved_by is None:
            row.approved_by = context.user_id
            self._audit.record_change(
                context,
                action=AuditAction.RETENTION_BATCH_SIGNED,
                entity_type="retention_purge_batch",
                entity_id=row.id,
                after={"first_approver": str(context.user_id)},
                fields=["approved_by"],
            )
            return row
        if row.approved_by == context.user_id:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "approved_by",
                        "TWO_PERSON_RULE",
                        "A different second approver must confirm the batch.",
                    )
                ]
            )
        row.status = "APPROVED"
        row.second_approver_id = context.user_id
        row.approved_at = utc_now()
        self._audit.record_change(
            context,
            action=AuditAction.RETENTION_BATCH_APPROVED,
            entity_type="retention_purge_batch",
            entity_id=row.id,
            after={"status": row.status},
            fields=["status"],
        )
        return row

    def list_batches(self, context: OrgContext) -> Any:
        context.require(Permission.EMPLOYEE_ARCHIVE)
        return self._batches.list_batches()

    def execute_batch(self, context: OrgContext, batch_id: uuid.UUID) -> int:
        context.require(Permission.EMPLOYEE_ARCHIVE)
        row = self._batches.require(batch_id, resource="retention batch")
        if row.status != "APPROVED":
            raise ValidationFailed("Only an approved batch can be executed.")
        processed = 0
        for entity_id in row.affected_entity_ids or []:
            employee = self._employees.get(uuid.UUID(str(entity_id)), include_archived=True)
            if employee is None or employee.legal_hold:
                continue
            if employee.status not in {status.value for status in SEPARATED_STATUSES}:
                continue
            end = employee.employment_end_date
            retained_until = utc_now().date() - timedelta(days=365 * EMPLOYEE_RETENTION_YEARS)
            if end is None or end > retained_until:
                continue
            self._anonymize(context, employee)
            processed += 1
        row.status = "EXECUTED"
        row.executed_at = utc_now()
        self._audit.record_change(
            context,
            action=AuditAction.RETENTION_BATCH_EXECUTED,
            entity_type="retention_purge_batch",
            entity_id=row.id,
            after={"status": row.status, "processed": processed},
            fields=["status"],
        )
        return processed


def _address(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "line_1": row.line_1,
        "line_2": row.line_2,
        "city": row.city,
        "district": row.district,
        "state": row.state,
        "postal_code": row.postal_code,
        "country": row.country,
    }


def _component_value(row: Any) -> str | None:
    if row.value_amount is not None:
        return str(row.value_amount)
    if row.value_percentage is not None:
        return str(row.value_percentage)
    if row.value_text is not None:
        return str(row.value_text)
    if row.value_boolean is not None:
        return str(row.value_boolean)
    return None


__all__ = ["PrivacyService", "decrypt_export"]
