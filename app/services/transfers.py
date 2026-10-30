"""Inter-organization transfer (§10.9)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in
from app.core.context import OrgContext
from app.core.crypto import FieldCipher, FieldContext
from app.core.errors import ErrorDetail, ResourceNotFound, ValidationFailed
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.employee import Employee, EmployeeAddress, EmployeeStatus, EmployeeStatusHistory
from app.models.profile import (
    EmployeeEducation,
    EmployeeFamilyMember,
    EmployeeNominee,
    EmployeePreviousEmployment,
)
from app.repositories.employee import (
    EmployeeAddressRepository,
    EmployeeRepository,
    EmployeeStatusHistoryRepository,
)
from app.repositories.identifier import EmployeeIdentifierRepository
from app.repositories.organization import OrganizationRepository
from app.repositories.profile import (
    EducationRepository,
    FamilyRepository,
    NominationRepository,
    PreviousEmploymentRepository,
)
from app.services.employees import EmployeeService

COPY_FIELDS = (
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
    "nationality",
    "blood_group",
    "religion",
    "social_category",
    "disability_status",
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
)


class TransferService:
    def __init__(
        self,
        *,
        session: Session,
        target_service: EmployeeService,
        organizations: OrganizationRepository,
        cipher: FieldCipher,
        audit: AuditWriter,
    ) -> None:
        self._session = session
        self._target = target_service
        self._organizations = organizations
        self._cipher = cipher
        self._audit = audit

    def transfer(
        self,
        context: OrgContext,
        *,
        source_employee_id: uuid.UUID,
        values: dict[str, Any],
    ) -> Employee:
        context.require(Permission.EMPLOYEE_CREATE)
        source_repo = EmployeeRepository(self._session, context.organization_id)
        source = source_repo.get_any_org(source_employee_id)
        if source is None:
            raise ResourceNotFound("employee")
        if source.organization_id == context.organization_id:
            raise ValidationFailed("Use the rehire flow within the same organization.")
        if source.status in {
            EmployeeStatus.RESIGNED.value,
            EmployeeStatus.TERMINATED.value,
            EmployeeStatus.RETIRED.value,
        }:
            raise ValidationFailed("The source employment record is already separated.")
        target_org = self._organizations.get(context.organization_id)
        if target_org is None or target_org.status != "ACTIVE":
            raise ValidationFailed("The target organization is not active.")

        end_date = values.get("source_employment_end_date") or date.fromisoformat(
            values["target_joining_date"].isoformat()
        ) - __import__("datetime").timedelta(days=1)
        if end_date >= values["target_joining_date"]:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "source_employment_end_date",
                        "INVALID_INTERVAL",
                        "The source end date must precede the target joining date.",
                    )
                ]
            )

        source_org_id = source.organization_id
        source_addresses = EmployeeAddressRepository(self._session, source_org_id)
        current = source_addresses.get_by_type(source.id, "CURRENT")
        permanent = source_addresses.get_by_type(source.id, "PERMANENT")
        create_values = {field: getattr(source, field) for field in COPY_FIELDS}
        create_values.update(
            {
                "joining_date": values["target_joining_date"],
                "employment_start_date": values["target_joining_date"],
                "employment_type_id": values["target_employment_type_id"],
                "designation_id": values["target_designation_id"],
                "department_id": values["target_department_id"],
                "branch_id": values.get("target_branch_id"),
                "employment_transition_type": "INTER_ORG_TRANSFER",
                "previous_employment_record_id": source.id,
                "current_address": _address_dict(current),
                "permanent_address": _address_dict(permanent),
                "permanent_address_same_as_current": False,
            }
        )
        self._close_source(context, source, end_date, uuid7())
        new_employee, _ = self._target.create(
            context, values=create_values, acknowledged_warnings=[]
        )
        self._copy(context, source, new_employee, values.get("copy_categories", []))
        return new_employee

    def _close_source(
        self, context: OrgContext, source: Employee, end_date: date, new_employee_id: uuid.UUID
    ) -> None:
        today = today_in(None)
        before = source.status
        source.status = (
            EmployeeStatus.ON_NOTICE.value if end_date >= today else EmployeeStatus.RESIGNED.value
        )
        source.status_effective_date = end_date
        source.employment_end_date = end_date
        source.status_reason = "INTER_ORG_TRANSFER"
        source.updated_by = context.user_id
        EmployeeStatusHistoryRepository(self._session, source.organization_id).add(
            EmployeeStatusHistory(
                id=uuid7(),
                employee_id=source.id,
                from_status=before,
                to_status=source.status,
                status_effective_date=end_date,
                employment_end_date_snapshot=end_date,
                status_reason="INTER_ORG_TRANSFER",
                remarks=f"Transferred to {context.organization_id}",
                is_correction=False,
                created_by=context.user_id,
            )
        )
        source_context = replace(context, organization_id=source.organization_id)
        self._audit.record_change(
            source_context,
            action=AuditAction.EMPLOYEE_STATUS_CHANGED,
            entity_type="employee",
            entity_id=source.id,
            employee_id=source.id,
            before={"status": before},
            after={"status": source.status, "employment_end_date": end_date},
            fields=["status", "employment_end_date"],
            reason=f"INTER_ORG_TRANSFER to {new_employee_id}",
        )

    def _copy(
        self,
        context: OrgContext,
        source: Employee,
        target: Employee,
        categories: list[str],
    ) -> None:
        source_org, target_org = source.organization_id, context.organization_id
        if "EDUCATION" in categories:
            education_source = EducationRepository(self._session, source_org)
            education_target = EducationRepository(self._session, target_org)
            for education_row in education_source.list_for_employee(source.id):
                education_target.add(
                    EmployeeEducation(
                        id=uuid7(),
                        employee_id=target.id,
                        qualification_level=education_row.qualification_level,
                        degree_name=education_row.degree_name,
                        specialization=education_row.specialization,
                        institution_name=education_row.institution_name,
                        board_or_university=education_row.board_or_university,
                        study_mode=education_row.study_mode,
                        is_ongoing=education_row.is_ongoing,
                        start_year=education_row.start_year,
                        passing_year=education_row.passing_year,
                        result_type=education_row.result_type,
                        result_value=education_row.result_value,
                        maximum_result_value=education_row.maximum_result_value,
                        remarks=education_row.remarks,
                        created_by=context.user_id,
                    )
                )
        if "FAMILY" in categories:
            family_source = FamilyRepository(self._session, source_org)
            family_target = FamilyRepository(self._session, target_org)
            for family_row in family_source.list_for_employee(source.id):
                family_target.add(
                    EmployeeFamilyMember(
                        id=uuid7(),
                        employee_id=target.id,
                        full_name=family_row.full_name,
                        relationship_code=family_row.relationship_code,
                        date_of_birth=family_row.date_of_birth,
                        occupation=family_row.occupation,
                        is_dependent=family_row.is_dependent,
                        insurance_coverage_reference=family_row.insurance_coverage_reference,
                        remarks=family_row.remarks,
                        created_by=context.user_id,
                    )
                )
        if "PREVIOUS_EMPLOYMENT" in categories:
            prev_source = PreviousEmploymentRepository(self._session, source_org)
            prev_target = PreviousEmploymentRepository(self._session, target_org)
            for prev_row in prev_source.list_for_employee(source.id):
                prev_target.add(
                    EmployeePreviousEmployment(
                        id=uuid7(),
                        employee_id=target.id,
                        employer_name=prev_row.employer_name,
                        designation=prev_row.designation,
                        employment_start_date=prev_row.employment_start_date,
                        employment_end_date=prev_row.employment_end_date,
                        last_drawn_ctc=prev_row.last_drawn_ctc,
                        last_drawn_ctc_currency=prev_row.last_drawn_ctc_currency,
                        reason_for_leaving=prev_row.reason_for_leaving,
                        reference_name=prev_row.reference_name,
                        reference_contact=prev_row.reference_contact,
                        overlap_review_status=prev_row.overlap_review_status,
                        remarks=prev_row.remarks,
                        created_by=context.user_id,
                    )
                )
        if "NOMINATIONS" in categories:
            nomination_source = NominationRepository(self._session, source_org)
            nomination_target = NominationRepository(self._session, target_org)
            for nomination_row in nomination_source.list_sets(source.id):
                nomination_target.add(
                    EmployeeNominee(
                        id=uuid7(),
                        employee_id=target.id,
                        nomination_type=nomination_row.nomination_type,
                        nomination_label=nomination_row.nomination_label,
                        effective_from=nomination_row.effective_from,
                        nominee_name=nomination_row.nominee_name,
                        relationship_code=nomination_row.relationship_code,
                        date_of_birth=nomination_row.date_of_birth,
                        contact_number=nomination_row.contact_number,
                        address=nomination_row.address,
                        share_percentage=nomination_row.share_percentage,
                        guardian_name=nomination_row.guardian_name,
                        guardian_relationship=nomination_row.guardian_relationship,
                        created_by=context.user_id,
                    )
                )
        if "IDENTIFIERS" in categories:
            identifier_source = EmployeeIdentifierRepository(self._session, source_org)
            identifier_target = EmployeeIdentifierRepository(self._session, target_org)
            for source_identifier in identifier_source.list_for_employee(
                source.id, current_only=True
            ):
                plaintext = self._cipher.decrypt(
                    source_identifier.value_encrypted,
                    FieldContext(
                        organization_id=source_org,
                        entity_type="employee_identifier",
                        entity_id=source_identifier.id,
                        field=source_identifier.identifier_type,
                    ),
                    source_identifier.key_version,
                )
                new_id = uuid7()
                ciphertext, key_version = self._cipher.encrypt(
                    plaintext,
                    FieldContext(
                        organization_id=target_org,
                        entity_type="employee_identifier",
                        entity_id=new_id,
                        field=source_identifier.identifier_type,
                    ),
                )
                identifier_target.add(
                    type(source_identifier)(
                        id=new_id,
                        employee_id=target.id,
                        identifier_type=source_identifier.identifier_type,
                        value_encrypted=ciphertext,
                        value_blind_index=source_identifier.value_blind_index,
                        value_last_four=source_identifier.value_last_four,
                        key_version=key_version,
                        issuing_country=source_identifier.issuing_country,
                        issue_date=source_identifier.issue_date,
                        expiry_date=source_identifier.expiry_date,
                        issue_place=source_identifier.issue_place,
                        is_current=True,
                        created_by=context.user_id,
                    )
                )


def _address_dict(row: EmployeeAddress | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "line_1": row.line_1,
        "line_2": row.line_2,
        "city": row.city,
        "district": row.district,
        "state": row.state,
        "state_code": row.state_code,
        "country": row.country,
        "postal_code": row.postal_code,
    }


__all__ = ["TransferService"]
