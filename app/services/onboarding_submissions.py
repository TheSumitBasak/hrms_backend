"""Employee data submissions: submit, review, apply (addendum 4.10, ONB-10..ONB-16).

Payloads are encrypted (AES-GCM bound to the submission) and never logged or audited; only the
field *names* are recorded. Acceptance applies the values through the same domain services as
the EMS edit endpoints, as the reviewer, so every EMS validator and audit event still runs.

Sections supported now: PERSONAL, CONTACT, EDUCATION, FAMILY. The remaining sections
(IDENTIFIERS, PREVIOUS_EMPLOYMENT, NOMINATIONS, BANK) are refused at submission with a clear
error rather than accepted and left unappliable.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from pydantic import BaseModel, ValidationError

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import utc_now
from app.core.context import OrgContext
from app.core.crypto import FieldCipher, FieldContext
from app.core.errors import (
    DataSubmissionAlreadyPending,
    FieldNotPermittedInSection,
    InvalidOnboardingTaskTransition,
    OnboardingTaskBlocked,
    PermissionDenied,
    ResourceNotFound,
    SelfReviewNotAllowed,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.onboarding import OnboardingDataSubmission
from app.repositories.onboarding import SubmissionRepository
from app.schemas.employee import EmployeeUpdateRequest
from app.schemas.profile import EducationCreateRequest, FamilyCreateRequest
from app.services.employees import EmployeeService
from app.services.onboarding import OnboardingService
from app.services.organizations import assert_etag
from app.services.profile_records import ProfileRecordService

PERSONAL_FIELDS = frozenset(
    {
        "first_name", "middle_name", "last_name", "father_name", "mother_name", "spouse_name",
        "date_of_birth", "gender", "marital_status", "nationality", "blood_group", "religion",
        "social_category", "disability_status", "disability_type", "disability_percentage",
    }
)  # fmt: skip
CONTACT_FIELDS = frozenset(
    {
        "personal_mobile", "alternate_mobile", "personal_email", "emergency_contact_name",
        "emergency_contact_relationship", "emergency_contact_phone",
        "emergency_contact_alternate_phone", "current_address", "permanent_address",
        "permanent_address_same_as_current",
    }
)  # fmt: skip
SUPPORTED = ("PERSONAL", "CONTACT", "EDUCATION", "FAMILY")
SCHEMA_VERSION = {section: f"{section}.v1" for section in SUPPORTED}
_ROW_MODELS: dict[str, type[BaseModel]] = {
    "EDUCATION": EducationCreateRequest,
    "FAMILY": FamilyCreateRequest,
}
_ROW_EXCLUDED = {"EDUCATION": {"certificate_document_id"}}


class SubmissionService:
    def __init__(
        self,
        *,
        submissions: SubmissionRepository,
        onboarding: OnboardingService,
        employee_service: EmployeeService,
        profile: ProfileRecordService,
        cipher: FieldCipher,
        audit: AuditWriter,
    ) -> None:
        self._submissions = submissions
        self._onboarding = onboarding
        self._employee_service = employee_service
        self._profile = profile
        self._cipher = cipher
        self._audit = audit

    # Validation (ONB-11).

    def _validate_payload(self, section: str, payload: dict[str, Any]) -> list[str]:
        """Strict section schema; returns the submitted field names (never values)."""
        if section not in SUPPORTED:
            raise ValidationFailed(
                f"The {section} section is not supported by this release."
            ).with_detail(
                field="section_code",
                code="SECTION_NOT_SUPPORTED",
                message="Use a supported section.",
            )
        if section in _ROW_MODELS:
            rows = payload.get("items")
            if (
                set(payload) != {"items"}
                or not isinstance(rows, list)
                or not rows
                or len(rows) > 20
            ):
                raise FieldNotPermittedInSection("Send {'items': [...]} with 1 to 20 rows.")
            excluded = _ROW_EXCLUDED.get(section, set())
            for row in rows:
                if not isinstance(row, dict) or (set(row) & excluded):
                    raise FieldNotPermittedInSection()
                try:
                    _ROW_MODELS[section].model_validate(row)
                except ValidationError as error:
                    raise self._invalid(error) from error
            return ["items"]
        allowed = PERSONAL_FIELDS if section == "PERSONAL" else CONTACT_FIELDS
        extra = sorted(set(payload) - allowed)
        if extra:
            raise FieldNotPermittedInSection(f"Not submittable in {section}: {', '.join(extra)}.")
        try:
            EmployeeUpdateRequest.model_validate(payload)
        except ValidationError as error:
            raise self._invalid(error) from error
        return sorted(payload)

    @staticmethod
    def _invalid(error: ValidationError) -> ValidationFailed:
        failure = ValidationFailed("The submitted values are invalid.")
        for item in error.errors():
            failure.with_detail(
                field=".".join(str(p) for p in item["loc"]), code="INVALID", message=item["msg"]
            )
        return failure

    # Submit / withdraw (ONB-10, ONB-14).

    def submit(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        payload: dict[str, Any],
        on_behalf: bool,
        on_behalf_reason: str | None,
    ) -> OnboardingDataSubmission:
        employee, instance, task, siblings = self._onboarding.load_task(instance_id, task_id)
        self._onboarding.assert_open(instance)
        if task.task_type != "DATA_SUBMISSION" or task.data_section_code is None:
            raise InvalidOnboardingTaskTransition()
        if on_behalf:
            context.require(Permission.ONBOARDING_MANAGE)
            if len((on_behalf_reason or "").strip()) < 10:
                raise ValidationFailed("on_behalf_reason of at least 10 characters is required.")
        else:
            self._onboarding.acting(context, task, instance, None)
        if task.status not in {"NOT_STARTED", "IN_PROGRESS", "REJECTED"}:
            raise (
                DataSubmissionAlreadyPending()
                if task.status == "SUBMITTED"
                else InvalidOnboardingTaskTransition()
            )
        if self._onboarding.is_blocked(task, siblings):
            raise OnboardingTaskBlocked()
        names = self._validate_payload(task.data_section_code, payload)
        existing = self._submissions.for_task(task.id)
        if any(s.status == "SUBMITTED" for s in existing):
            raise DataSubmissionAlreadyPending()
        submission_id = uuid7()
        blob, key_version = self._cipher.encrypt(
            json.dumps(payload, default=str, sort_keys=True),
            FieldContext(
                context.organization_id, "onboarding_submission", submission_id, "payload"
            ),
        )
        previous = next(
            (s for s in reversed(existing) if s.status in ("REJECTED", "WITHDRAWN")), None
        )
        row = OnboardingDataSubmission(
            id=submission_id,
            organization_id=context.organization_id,
            employee_id=employee.id,
            instance_id=instance.id,
            task_id=task.id,
            section_code=task.data_section_code,
            submission_no=self._submissions.next_number(task.id),
            status="SUBMITTED",
            supersedes_submission_id=previous.id if previous else None,
            payload_encrypted=blob,
            payload_schema_version=SCHEMA_VERSION[task.data_section_code],
            key_version=key_version,
            submitted_field_names=names,
            submitted_by=context.user_id,
            submitted_on_behalf=on_behalf,
            on_behalf_reason=(on_behalf_reason or "").strip() or None,
            submitted_at=utc_now(),
            created_by=context.user_id,
        )
        self._submissions.add(row)
        self._submissions.flush()
        self._onboarding.move(
            context,
            task,
            "SUBMITTED",
            source="DATA_SUBMISSION",
            on_behalf=on_behalf,
            reason=row.on_behalf_reason,
        )
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_SUBMISSION_CREATED,
            entity_type="onboarding_data_submission",
            entity_id=row.id,
            employee_id=employee.id,
            after={
                "section": row.section_code,
                "fields": names,
                "submission_no": row.submission_no,
            },
        )
        return row

    def withdraw(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        submission_id: uuid.UUID,
        *,
        reason: str | None,
        if_match: int | None,
    ) -> OnboardingDataSubmission:
        _employee, _instance, task, _siblings = self._onboarding.load_task(instance_id, task_id)
        submission = self._owned(task.id, submission_id)
        assert_etag(submission.row_version, if_match)
        if submission.status != "SUBMITTED":
            raise InvalidOnboardingTaskTransition()
        if not (
            submission.submitted_by == context.user_id or context.has(Permission.ONBOARDING_MANAGE)
        ):
            raise PermissionDenied("Only the submitter or onboarding.manage can withdraw.")
        submission.status = "WITHDRAWN"
        submission.decided_by, submission.decided_at = context.user_id, utc_now()
        submission.updated_by = context.user_id
        if task.status == "SUBMITTED":  # ONB-14: the one permitted SUBMITTED -> IN_PROGRESS cause
            self._onboarding.move(
                context, task, "IN_PROGRESS", source="DATA_SUBMISSION", reason=reason
            )
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_SUBMISSION_WITHDRAWN,
            entity_type="onboarding_data_submission",
            entity_id=submission.id,
            employee_id=submission.employee_id,
            reason=reason,
        )
        return submission

    # Review (ONB-12, ONB-13).

    def reject(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        submission_id: uuid.UUID,
        *,
        reason: str,
        field_comments: list[dict[str, str]] | None,
        if_match: int | None,
    ) -> OnboardingDataSubmission:
        _employee, instance, task, _siblings = self._onboarding.load_task(instance_id, task_id)
        self._onboarding.assert_open(instance)
        submission = self._owned(task.id, submission_id)
        assert_etag(submission.row_version, if_match)
        self._onboarding.reviewer_check(context, task, instance)
        if submission.submitted_by == context.user_id:
            raise SelfReviewNotAllowed()
        if submission.status != "SUBMITTED":
            raise InvalidOnboardingTaskTransition()
        if not 10 <= len(reason.strip()) <= 1000:
            raise ValidationFailed("A rejection reason of 10 to 1,000 characters is required.")
        for comment in field_comments or []:
            if len(comment.get("comment", "")) > 300:
                raise ValidationFailed("A field comment is limited to 300 characters.")
        submission.status = "REJECTED"
        submission.rejection_reason = reason.strip()
        submission.field_comments = field_comments
        submission.decided_by, submission.decided_at = context.user_id, utc_now()
        submission.updated_by = context.user_id
        task.rejection_reason = reason.strip()
        task.reviewed_at, task.reviewed_by = utc_now(), context.user_id
        self._onboarding.move(
            context, task, "REJECTED", source="DATA_SUBMISSION", reason=reason.strip()
        )
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_SUBMISSION_REJECTED,
            entity_type="onboarding_data_submission",
            entity_id=submission.id,
            employee_id=submission.employee_id,
            reason=reason.strip(),
        )
        return submission

    def accept(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        submission_id: uuid.UUID,
        *,
        acknowledged: list[str],
        if_match: int | None,
    ) -> OnboardingDataSubmission:
        employee, instance, task, siblings = self._onboarding.load_task(instance_id, task_id)
        self._onboarding.assert_open(instance)
        submission = self._owned(task.id, submission_id)
        assert_etag(submission.row_version, if_match)
        self._onboarding.reviewer_check(context, task, instance)
        context.require(Permission.EMPLOYEE_EDIT)  # the section accept permission (4.10)
        if submission.submitted_by == context.user_id:
            raise SelfReviewNotAllowed()
        if submission.status != "SUBMITTED" or submission.payload_encrypted is None:
            raise InvalidOnboardingTaskTransition()
        payload = json.loads(
            self._cipher.decrypt(
                submission.payload_encrypted,
                FieldContext(
                    context.organization_id, "onboarding_submission", submission.id, "payload"
                ),
                submission.key_version,
            )
        )
        refs = self._apply(context, employee.id, submission.section_code, payload, acknowledged)
        now = utc_now()
        submission.status = "ACCEPTED"
        submission.decided_by, submission.decided_at = context.user_id, now
        submission.acknowledged_warnings = acknowledged or None
        submission.applied_entity_refs = refs
        submission.payload_encrypted, submission.payload_purged_at = (
            None,
            now,
        )  # ONB-12: purge on accept
        submission.updated_by = context.user_id
        task.reviewed_at, task.reviewed_by = now, context.user_id
        self._onboarding.move(context, task, "COMPLETED", source="DATA_SUBMISSION")
        self._onboarding.after(context, employee, instance, siblings)
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_SUBMISSION_ACCEPTED,
            entity_type="onboarding_data_submission",
            entity_id=submission.id,
            employee_id=submission.employee_id,
            after={"applied": len(refs)},
            acknowledged_warnings=acknowledged,
        )
        return submission

    def _apply(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        section: str,
        payload: dict[str, Any],
        acknowledged: list[str],
    ) -> list[dict[str, str]]:
        """ONB-12: through the EMS services, as the reviewer, in the caller's transaction."""
        if section in ("PERSONAL", "CONTACT"):
            changes = EmployeeUpdateRequest.model_validate(payload).model_dump(exclude_unset=True)
            addresses = {
                "current_address": changes.pop("current_address", None),
                "permanent_address": changes.pop("permanent_address", None),
                "permanent_address_same_as_current": changes.pop(
                    "permanent_address_same_as_current", None
                ),
            }
            current = self._employee_service.get_row(employee_id)
            self._employee_service.update(
                context,
                employee_id=employee_id,
                changes=changes,
                addresses=addresses,
                if_match=current.row_version,
                acknowledged_warnings=acknowledged,
            )
            return [{"entity_type": "employee", "entity_id": str(employee_id)}]
        refs: list[dict[str, str]] = []
        for row in payload["items"]:
            if section == "EDUCATION":
                created = self._profile.create_education(
                    context, employee_id, EducationCreateRequest.model_validate(row).model_dump()
                )
                refs.append({"entity_type": "employee_education", "entity_id": str(created.id)})
            else:
                created_family = self._profile.create_family(
                    context, employee_id, FamilyCreateRequest.model_validate(row).model_dump()
                )
                refs.append(
                    {"entity_type": "employee_family_member", "entity_id": str(created_family.id)}
                )
        return refs

    # Reads (ONB-15, ONB-16).

    def _owned(self, task_id: uuid.UUID, submission_id: uuid.UUID) -> OnboardingDataSubmission:
        submission = self._submissions.require(submission_id, resource="submission")
        if submission.task_id != task_id:
            raise ResourceNotFound("submission")
        return submission

    def list_for_task(
        self, context: OrgContext, instance_id: uuid.UUID, task_id: uuid.UUID
    ) -> list[OnboardingDataSubmission]:
        _task, _events, _docs = self._onboarding.get_task(context, instance_id, task_id)
        own = context.is_self(_task.employee_id) and context.has(Permission.SELF_ONBOARDING)
        if not own:
            # Payload metadata is for people who may view personal data; never DM or OTO (ONB-15).
            context.require(Permission.EMPLOYEE_VIEW_PERSONAL)
        return list(self._submissions.for_task(task_id))
