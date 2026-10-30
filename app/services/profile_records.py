"""Education, previous employment, family and nomination use cases (§12, §13)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import age_on, utc_now
from app.core.context import OrgContext
from app.core.errors import ErrorDetail, ValidationFailed
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.org_config import NominationTypeConfig
from app.models.profile import (
    EmployeeEducation,
    EmployeeFamilyMember,
    EmployeeNominee,
    EmployeePreviousEmployment,
    OverlapReviewStatus,
)
from app.repositories.employee import EmployeeRepository
from app.repositories.organization import NominationConfigRepository
from app.repositories.profile import (
    EducationRepository,
    FamilyRepository,
    NominationRepository,
    PreviousEmploymentDocumentRepository,
    PreviousEmploymentRepository,
)
from app.schemas.profile import NomineeInput


class ProfileRecordService:
    def __init__(
        self,
        *,
        education: EducationRepository,
        previous: PreviousEmploymentRepository,
        previous_documents: PreviousEmploymentDocumentRepository,
        family: FamilyRepository,
        nominations: NominationRepository,
        employees: EmployeeRepository,
        nomination_configs: NominationConfigRepository,
        audit: AuditWriter,
    ) -> None:
        self._education = education
        self._previous = previous
        self._previous_documents = previous_documents
        self._family = family
        self._nominations = nominations
        self._employees = employees
        self._nomination_configs = nomination_configs
        self._audit = audit

    # ---------------------------------------------------------------- education

    def list_education(
        self, context: OrgContext, employee_id: uuid.UUID
    ) -> Sequence[EmployeeEducation]:
        context.require_any(Permission.EMPLOYEE_VIEW_PERSONAL, Permission.EMPLOYEE_VIEW)
        self._employees.require(employee_id, resource="employee")
        return self._education.list_for_employee(employee_id)

    def create_education(
        self, context: OrgContext, employee_id: uuid.UUID, values: dict[str, Any]
    ) -> EmployeeEducation:
        context.require(Permission.EMPLOYEE_EDIT)
        employee = self._employees.require(employee_id, resource="employee")
        row = EmployeeEducation(
            id=uuid7(), employee_id=employee.id, created_by=context.user_id, **values
        )
        self._education.add(row)
        self._audit.record_change(
            context,
            action=AuditAction.EDUCATION_CREATED,
            entity_type="employee_education",
            entity_id=row.id,
            employee_id=employee.id,
            after={"qualification_level": row.qualification_level},
            fields=["qualification_level"],
        )
        return row

    def update_education(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        record_id: uuid.UUID,
        changes: dict[str, Any],
    ) -> EmployeeEducation:
        context.require(Permission.EMPLOYEE_EDIT)
        row = self._education.require_for_employee(employee_id, record_id)
        before = {field: getattr(row, field) for field in changes}
        for field, value in changes.items():
            setattr(row, field, value)
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.EDUCATION_UPDATED,
            entity_type="employee_education",
            entity_id=row.id,
            employee_id=employee_id,
            before=before,
            after={field: getattr(row, field) for field in changes},
            fields=list(changes),
        )
        return row

    def archive_education(
        self, context: OrgContext, employee_id: uuid.UUID, record_id: uuid.UUID, reason: str
    ) -> None:
        context.require(Permission.EMPLOYEE_EDIT)
        row = self._education.require_for_employee(employee_id, record_id)
        row.archived_at = utc_now()
        row.archived_by = context.user_id
        row.archive_reason = reason
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.EDUCATION_ARCHIVED,
            entity_type="employee_education",
            entity_id=row.id,
            employee_id=employee_id,
            after={"archived": True},
            fields=["archived_at"],
            reason=reason,
        )

    # ---------------------------------------------------------------- previous employment

    def list_previous_employment(
        self, context: OrgContext, employee_id: uuid.UUID
    ) -> list[tuple[EmployeePreviousEmployment, list[uuid.UUID]]]:
        context.require_any(Permission.EMPLOYEE_VIEW_PERSONAL, Permission.EMPLOYEE_VIEW)
        self._employees.require(employee_id, resource="employee")
        rows = self._previous.list_for_employee(employee_id)
        return [
            (
                row,
                [link.document_id for link in self._previous_documents.list_for_employment(row.id)],
            )
            for row in rows
        ]

    def create_previous_employment(
        self, context: OrgContext, employee_id: uuid.UUID, values: dict[str, Any]
    ) -> tuple[EmployeePreviousEmployment, list[uuid.UUID]]:
        context.require(Permission.EMPLOYEE_EDIT)
        employee = self._employees.require(employee_id, resource="employee")
        payslips = values.pop("payslip_document_ids", [])
        row = EmployeePreviousEmployment(
            id=uuid7(), employee_id=employee.id, created_by=context.user_id, **values
        )
        self._previous.add(row)
        self._previous.flush()
        if payslips:
            self._previous_documents.replace(row.id, payslips)
        row.overlap_review_status = self._overlap_status(employee_id, row)
        self._audit.record_change(
            context,
            action=AuditAction.PREVIOUS_EMPLOYMENT_CREATED,
            entity_type="employee_previous_employment",
            entity_id=row.id,
            employee_id=employee.id,
            after={"employer_name": row.employer_name},
            fields=["employer_name"],
        )
        return row, list(payslips)

    def update_previous_employment(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        record_id: uuid.UUID,
        changes: dict[str, Any],
    ) -> tuple[EmployeePreviousEmployment, list[uuid.UUID]]:
        context.require(Permission.EMPLOYEE_EDIT)
        row = self._previous.require_for_employee(employee_id, record_id)
        if any(field.startswith("background_verification") for field in changes):
            context.require(Permission.BGV_VIEW)
        payslips = changes.pop("payslip_document_ids", None)
        before = {field: getattr(row, field) for field in changes}
        for field, value in changes.items():
            setattr(row, field, value)
        if row.employment_end_date < row.employment_start_date:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "employment_end_date",
                        "INVALID_INTERVAL",
                        "employment_end_date must not precede employment_start_date.",
                    )
                ]
            )
        if payslips is not None:
            self._previous_documents.replace(row.id, payslips)
        row.overlap_review_status = self._overlap_status(employee_id, row)
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.PREVIOUS_EMPLOYMENT_UPDATED,
            entity_type="employee_previous_employment",
            entity_id=row.id,
            employee_id=employee_id,
            before=before,
            after={field: getattr(row, field) for field in changes},
            fields=list(changes),
        )
        return row, [
            link.document_id for link in self._previous_documents.list_for_employment(row.id)
        ]

    def archive_previous_employment(
        self, context: OrgContext, employee_id: uuid.UUID, record_id: uuid.UUID, reason: str
    ) -> None:
        context.require(Permission.EMPLOYEE_EDIT)
        row = self._previous.require_for_employee(employee_id, record_id)
        row.archived_at = utc_now()
        row.archived_by = context.user_id
        row.archive_reason = reason
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.PREVIOUS_EMPLOYMENT_ARCHIVED,
            entity_type="employee_previous_employment",
            entity_id=row.id,
            employee_id=employee_id,
            after={"archived": True},
            fields=["archived_at"],
            reason=reason,
        )

    def review_overlap(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        record_id: uuid.UUID,
        *,
        remarks: str | None,
    ) -> EmployeePreviousEmployment:
        context.require(Permission.EMPLOYEE_EDIT)
        row = self._previous.require_for_employee(employee_id, record_id)
        if row.overlap_review_status != OverlapReviewStatus.PENDING_REVIEW.value:
            raise ValidationFailed("This record has no pending overlap review.")
        row.overlap_review_status = OverlapReviewStatus.ACCEPTED.value
        row.overlap_reviewed_by = context.user_id
        row.overlap_reviewed_at = utc_now()
        row.remarks = remarks or row.remarks
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.PREVIOUS_EMPLOYMENT_OVERLAP_REVIEWED,
            entity_type="employee_previous_employment",
            entity_id=row.id,
            employee_id=employee_id,
            after={"overlap_review_status": row.overlap_review_status},
            fields=["overlap_review_status"],
            reason=remarks,
        )
        return row

    def _overlap_status(self, employee_id: uuid.UUID, candidate: EmployeePreviousEmployment) -> str:
        for other in self._previous.list_for_employee(employee_id):
            if other.id == candidate.id:
                continue
            if (
                other.employment_start_date <= candidate.employment_end_date
                and candidate.employment_start_date <= other.employment_end_date
            ):
                return OverlapReviewStatus.PENDING_REVIEW.value
        return OverlapReviewStatus.NOT_APPLICABLE.value

    # ---------------------------------------------------------------- family

    def list_family(
        self, context: OrgContext, employee_id: uuid.UUID
    ) -> Sequence[EmployeeFamilyMember]:
        context.require_any(Permission.EMPLOYEE_VIEW_PERSONAL, Permission.EMPLOYEE_VIEW)
        self._employees.require(employee_id, resource="employee")
        return self._family.list_for_employee(employee_id)

    def create_family(
        self, context: OrgContext, employee_id: uuid.UUID, values: dict[str, Any]
    ) -> EmployeeFamilyMember:
        context.require(Permission.EMPLOYEE_EDIT)
        employee = self._employees.require(employee_id, resource="employee")
        row = EmployeeFamilyMember(
            id=uuid7(), employee_id=employee.id, created_by=context.user_id, **values
        )
        self._family.add(row)
        self._audit.record_change(
            context,
            action=AuditAction.FAMILY_CREATED,
            entity_type="employee_family_member",
            entity_id=row.id,
            employee_id=employee.id,
            after={"relationship_code": row.relationship_code},
            fields=["relationship_code"],
        )
        return row

    def update_family(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        record_id: uuid.UUID,
        changes: dict[str, Any],
    ) -> EmployeeFamilyMember:
        context.require(Permission.EMPLOYEE_EDIT)
        row = self._family.require_for_employee(employee_id, record_id)
        before = {field: getattr(row, field) for field in changes}
        for field, value in changes.items():
            setattr(row, field, value)
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.FAMILY_UPDATED,
            entity_type="employee_family_member",
            entity_id=row.id,
            employee_id=employee_id,
            before=before,
            after={field: getattr(row, field) for field in changes},
            fields=list(changes),
        )
        return row

    def archive_family(
        self, context: OrgContext, employee_id: uuid.UUID, record_id: uuid.UUID, reason: str
    ) -> None:
        context.require(Permission.EMPLOYEE_EDIT)
        row = self._family.require_for_employee(employee_id, record_id)
        row.archived_at = utc_now()
        row.archived_by = context.user_id
        row.archive_reason = reason
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.FAMILY_ARCHIVED,
            entity_type="employee_family_member",
            entity_id=row.id,
            employee_id=employee_id,
            after={"archived": True},
            fields=["archived_at"],
            reason=reason,
        )

    def payslip_document_ids(self, employment_id: uuid.UUID) -> list[uuid.UUID]:
        return [
            link.document_id for link in self._previous_documents.list_for_employment(employment_id)
        ]

    # ---------------------------------------------------------------- nominations

    def nomination_sets(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        *,
        nomination_type: str | None = None,
        as_of: date | None = None,
    ) -> list[dict[str, Any]]:
        context.require(Permission.EMPLOYEE_VIEW_PERSONAL)
        self._employees.require(employee_id, resource="employee")
        rows = list(self._nominations.list_sets(employee_id))
        if nomination_type is not None:
            rows = [row for row in rows if row.nomination_type == nomination_type]
        if as_of is not None:
            rows = [
                row
                for row in rows
                if row.effective_from <= as_of
                and (row.effective_to is None or row.effective_to > as_of)
            ]
        grouped: dict[tuple[str, date], list[EmployeeNominee]] = {}
        for row in rows:
            grouped.setdefault((row.nomination_type, row.effective_from), []).append(row)
        return [
            {
                "nomination_type": key[0],
                "effective_from": key[1],
                "effective_to": group[0].effective_to,
                "nomination_label": group[0].nomination_label,
                "nominees": group,
                "total_share": sum((row.share_percentage for row in group), Decimal("0")),
            }
            for key, group in sorted(grouped.items())
        ]

    def replace_nominations(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        nomination_type: str,
        *,
        effective_from: date,
        nomination_label: str | None,
        nominees: Sequence[NomineeInput],
    ) -> list[EmployeeNominee]:
        context.require(Permission.EMPLOYEE_EDIT)
        self._employees.require(employee_id, resource="employee")
        config = self._nomination_configs.get_by_type(nomination_type)
        if config is None:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "nomination_type", "UNKNOWN", "No configuration for this nomination type."
                    )
                ]
            )
        self._validate_nominees(config, nominees)

        # End-date the previous current set on the day before the new one starts.
        for existing in self._nominations.list_sets(employee_id):
            if existing.nomination_type != nomination_type:
                continue
            if existing.effective_from >= effective_from:
                self._nominations.session.delete(existing)
            elif existing.effective_to is None or existing.effective_to > effective_from:
                existing.effective_to = effective_from
                existing.updated_by = context.user_id

        created: list[EmployeeNominee] = []
        for nominee in nominees:
            row = EmployeeNominee(
                id=uuid7(),
                employee_id=employee_id,
                nomination_type=nomination_type,
                nomination_label=nomination_label,
                effective_from=effective_from,
                created_by=context.user_id,
                **nominee.model_dump(),
            )
            self._nominations.add(row)
            created.append(row)
        self._audit.record_change(
            context,
            action=AuditAction.NOMINATION_SET_REPLACED,
            entity_type="employee_nominee",
            entity_id=employee_id,
            employee_id=employee_id,
            after={
                "nomination_type": nomination_type,
                "effective_from": effective_from.isoformat(),
                "nominee_count": len(created),
            },
            fields=["nomination_type", "effective_from"],
        )
        return created

    @staticmethod
    def _validate_nominees(config: NominationTypeConfig, nominees: Sequence[NomineeInput]) -> None:
        if config.max_nominees is not None and len(nominees) > config.max_nominees:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "nominees",
                        "MAX_EXCEEDED",
                        f"At most {config.max_nominees} nominees are permitted.",
                    )
                ]
            )
        total = sum((nominee.share_percentage for nominee in nominees), Decimal("0"))
        if config.requires_full_allocation and total != Decimal("100.00"):
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "share_percentage",
                        "NOMINEE_SHARE_TOTAL_INVALID",
                        "Nominee shares must total exactly 100.00.",
                    )
                ]
            )
        for nominee in nominees:
            if config.requires_dob and nominee.date_of_birth is None:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "date_of_birth",
                            "REQUIRED",
                            "A date of birth is required for this nomination type.",
                        )
                    ]
                )
            is_minor = (
                nominee.date_of_birth is not None
                and age_on(nominee.date_of_birth, utc_now().date()) < 18
            )
            if is_minor and (not nominee.guardian_name or not nominee.guardian_relationship):
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "guardian_name",
                            "REQUIRED",
                            "A minor nominee requires a guardian name and relationship.",
                        )
                    ]
                )


__all__ = ["ProfileRecordService"]
