"""Salary components, templates and the effective-dated timeline (§16-§18)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in, utc_now
from app.core.context import OrgContext
from app.core.errors import (
    ApprovalRequired,
    BackdateNotAllowed,
    ComponentNotOverridable,
    DuplicateResource,
    ErrorDetail,
    ResourceNotFound,
    SalaryAfterEmploymentEnd,
    SalaryAssignmentOverlap,
    SalaryNotCancellable,
    SalaryNotCorrectable,
    SalaryNotEditable,
    SelfApprovalNotAllowed,
    TemplateArchived,
    TemplateDraftExists,
    TemplateVersionNotEffective,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.salary import (
    BASIC_COMPONENT_CODE,
    CONFIRMED_STATUSES,
    AmountPeriod,
    ApprovalState,
    AssignmentStatus,
    CancellationType,
    ComponentType,
    EffectiveToSource,
    EmployeeSalaryAssignment,
    EmployeeSalaryComponent,
    RevisionEventType,
    SalaryComponent,
    SalaryRevisionHistory,
    SalaryTemplate,
    SalaryTemplateComponent,
    SalaryTemplateVersion,
    TemplateVersionStatus,
    ValueType,
)
from app.repositories.employee import EmployeeRepository
from app.repositories.organization import (
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.repositories.salary import (
    AssignmentComponentRepository,
    SalaryAssignmentRepository,
    SalaryComponentRepository,
    SalaryRevisionHistoryRepository,
    SalaryTemplateComponentRepository,
    SalaryTemplateRepository,
    SalaryTemplateVersionRepository,
)
from app.schemas.salary import AssignmentComponentInput, ComponentMappingInput
from app.services.organizations import assert_etag

CONFIRMED = {status.value for status in CONFIRMED_STATUSES}
PERCENT_OF_BASIC_LABEL = "% of Basic"
_MONEY_STEP = Decimal("0.01")
_SYSTEM_EDITABLE = {"component_name", "display_name", "description", "display_order"}


class SalaryService:
    def __init__(
        self,
        *,
        components: SalaryComponentRepository,
        templates: SalaryTemplateRepository,
        versions: SalaryTemplateVersionRepository,
        mappings: SalaryTemplateComponentRepository,
        assignments: SalaryAssignmentRepository,
        assignment_components: AssignmentComponentRepository,
        history: SalaryRevisionHistoryRepository,
        employees: EmployeeRepository,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        audit: AuditWriter,
    ) -> None:
        self._components = components
        self._templates = templates
        self._versions = versions
        self._mappings = mappings
        self._assignments = assignments
        self._assignment_components = assignment_components
        self._history = history
        self._employees = employees
        self._organizations = organizations
        self._settings = settings
        self._audit = audit

    # ---------------------------------------------------------------- components

    def list_components(
        self, context: OrgContext, *, include_inactive: bool = False
    ) -> Sequence[SalaryComponent]:
        context.require_any(Permission.SALARY_VIEW, Permission.SALARY_TEMPLATE_MANAGE)
        return self._components.list_components(include_inactive=include_inactive)

    def create_component(self, context: OrgContext, values: dict[str, Any]) -> SalaryComponent:
        context.require(Permission.SALARY_TEMPLATE_MANAGE)
        code = str(values["component_code"])
        if self._components.get_by_code(code) is not None:
            raise DuplicateResource(
                "A salary component with that code already exists."
            ).with_detail(
                field="component_code",
                code="DUPLICATE",
                message="Component codes are unique.",
            )
        row = SalaryComponent(
            id=uuid7(),
            component_code=code,
            component_name=str(values["component_name"]),
            display_name=str(values.get("display_name") or values["component_name"]),
            component_type=str(values.get("component_type") or ComponentType.EARNING.value),
            description=values.get("description"),
            component_category=str(values.get("component_category") or "OTHER"),
            value_type=str(values["value_type"]),
            amount_period=str(values.get("amount_period") or "NOT_APPLICABLE"),
            percentage_reference_label=(
                values.get("percentage_reference_label") or PERCENT_OF_BASIC_LABEL
                if values["value_type"] == ValueType.PERCENTAGE.value
                else values.get("percentage_reference_label")
            ),
            validation_regex=values.get("validation_regex"),
            default_value_amount=values.get("default_value_amount"),
            default_value_percentage=values.get("default_value_percentage"),
            default_value_text=values.get("default_value_text"),
            default_value_boolean=values.get("default_value_boolean"),
            currency=values.get("currency"),
            is_required=bool(values.get("is_required")),
            is_editable_per_employee=bool(values.get("is_editable_per_employee", True)),
            display_order=int(values.get("display_order") or 0),
            status="ACTIVE",
            created_by=context.user_id,
        )
        self._components.add(row)
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_COMPONENT_CREATED,
            entity_type="salary_component",
            entity_id=row.id,
            after={"component_code": code, "component_name": row.component_name},
            fields=["component_code", "component_name"],
        )
        return row

    def update_component(
        self,
        context: OrgContext,
        component_id: uuid.UUID,
        changes: dict[str, Any],
        *,
        if_match: int | None,
    ) -> SalaryComponent:
        context.require(Permission.SALARY_TEMPLATE_MANAGE)
        row = self._components.require(component_id, resource="salary component")
        assert_etag(row.row_version, if_match)
        if row.is_system and not set(changes) <= _SYSTEM_EDITABLE:
            raise ValidationFailed("Basic is a system component; only its names can be edited.")
        value_type_changed = "value_type" in changes and changes["value_type"] != row.value_type
        if value_type_changed and (
            self._mappings.exists_component(row.id)
            or self._assignment_components.exists_component(row.id)
        ):
            raise ValidationFailed("value_type is immutable once the component is used.")
        before = {field: getattr(row, field) for field in changes}
        for field, value in changes.items():
            setattr(row, field, value)
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_COMPONENT_UPDATED,
            entity_type="salary_component",
            entity_id=row.id,
            before=before,
            after={field: getattr(row, field) for field in changes},
            fields=list(changes),
        )
        return row

    def deactivate_component(
        self, context: OrgContext, component_id: uuid.UUID, reason: str
    ) -> SalaryComponent:
        context.require(Permission.SALARY_TEMPLATE_MANAGE)
        row = self._components.require(component_id, resource="salary component")
        if row.is_system:
            raise ValidationFailed("Basic is a system component and cannot be deactivated.")
        row.status = "INACTIVE"
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_COMPONENT_DEACTIVATED,
            entity_type="salary_component",
            entity_id=row.id,
            after={"status": "INACTIVE"},
            fields=["status"],
            reason=reason,
        )
        return row

    def component_usage(self, context: OrgContext, component_id: uuid.UUID) -> dict[str, int]:
        """How many template versions and assignments reference a component (§16.3)."""
        context.require_any(Permission.SALARY_VIEW, Permission.SALARY_TEMPLATE_MANAGE)
        row = self._components.require(component_id, resource="salary component")
        return {
            "template_version_count": self._mappings.count_versions_using(row.id),
            "assignment_count": self._assignment_components.count_assignments_using(row.id),
        }

    # ---------------------------------------------------------------- templates

    def list_templates(
        self, context: OrgContext, *, include_archived: bool = False
    ) -> Sequence[SalaryTemplate]:
        context.require_any(Permission.SALARY_VIEW, Permission.SALARY_TEMPLATE_MANAGE)
        return self._templates.list_templates(include_archived=include_archived)

    def create_template(
        self, context: OrgContext, values: dict[str, Any]
    ) -> tuple[SalaryTemplate, SalaryTemplateVersion]:
        context.require(Permission.SALARY_TEMPLATE_MANAGE)
        code = str(values["template_code"])
        if self._templates.get_by_code(code) is not None:
            raise DuplicateResource("A salary template with that code already exists.").with_detail(
                field="template_code",
                code="DUPLICATE",
                message="Template codes are unique.",
            )
        header = SalaryTemplate(
            id=uuid7(),
            template_code=code,
            template_name=str(values["template_name"]),
            description=values.get("description"),
            is_archived=False,
            created_by=context.user_id,
        )
        self._templates.add(header)
        self._templates.flush()
        version = SalaryTemplateVersion(
            id=uuid7(),
            template_id=header.id,
            version_number=1,
            currency=str(values["currency"]),
            status=TemplateVersionStatus.DRAFT.value,
            effective_from=values.get("effective_from"),
            change_note=values.get("version_change_note"),
            created_by=context.user_id,
        )
        self._versions.add(version)
        self._versions.flush()
        self._replace_mappings(header.id, version, values.get("mappings", []), context)
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_TEMPLATE_CREATED,
            entity_type="salary_template",
            entity_id=header.id,
            after={"template_code": code, "template_name": header.template_name},
            fields=["template_code", "template_name"],
        )
        return header, version

    def update_template(
        self,
        context: OrgContext,
        template_id: uuid.UUID,
        changes: dict[str, Any],
        *,
        if_match: int | None,
    ) -> SalaryTemplate:
        context.require(Permission.SALARY_TEMPLATE_MANAGE)
        header = self._templates.require(template_id, resource="salary template")
        assert_etag(header.row_version, if_match)
        before = {field: getattr(header, field) for field in changes}
        for field, value in changes.items():
            setattr(header, field, value)
        header.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_TEMPLATE_CREATED,
            entity_type="salary_template",
            entity_id=header.id,
            before=before,
            after={field: getattr(header, field) for field in changes},
            fields=list(changes),
        )
        return header

    def list_versions(
        self, context: OrgContext, template_id: uuid.UUID
    ) -> Sequence[SalaryTemplateVersion]:
        context.require_any(Permission.SALARY_VIEW, Permission.SALARY_TEMPLATE_MANAGE)
        header = self._templates.require(template_id, resource="salary template")
        return self._versions.list_for_template(header.id)

    def get_version(
        self, context: OrgContext, template_id: uuid.UUID, version_id: uuid.UUID
    ) -> SalaryTemplateVersion:
        context.require_any(Permission.SALARY_VIEW, Permission.SALARY_TEMPLATE_MANAGE)
        version = self._versions.require(version_id, resource="template version")
        if version.template_id != template_id:
            raise ResourceNotFound("template version")
        return version

    def new_version(self, context: OrgContext, template_id: uuid.UUID) -> SalaryTemplateVersion:
        context.require(Permission.SALARY_TEMPLATE_MANAGE)
        header = self._templates.require(template_id, resource="salary template")
        if header.is_archived:
            raise TemplateArchived()
        if self._versions.open_draft(header.id) is not None:
            raise TemplateDraftExists()
        latest = self._versions.latest(header.id)
        number = (latest.version_number if latest else 0) + 1
        version = SalaryTemplateVersion(
            id=uuid7(),
            template_id=header.id,
            version_number=number,
            currency=latest.currency if latest else "INR",
            status=TemplateVersionStatus.DRAFT.value,
            created_by=context.user_id,
        )
        self._versions.add(version)
        self._versions.flush()
        if latest is not None:
            copied = [
                SalaryTemplateComponent(
                    id=uuid7(),
                    template_id=header.id,
                    template_version_id=version.id,
                    component_id=row.component_id,
                    default_value_amount=row.default_value_amount,
                    default_value_percentage=row.default_value_percentage,
                    default_value_text=row.default_value_text,
                    default_value_boolean=row.default_value_boolean,
                    is_required=row.is_required,
                    is_overridable=row.is_overridable,
                    display_order=row.display_order,
                    effective_from=row.effective_from,
                    effective_to=row.effective_to,
                    created_by=context.user_id,
                )
                for row in self._mappings.list_for_version(latest.id)
            ]
            basic_id = self._basic().id
            if all(row.component_id != basic_id for row in copied):
                copied.append(self._basic_mapping(header.id, version.id, context))
            self._mappings.replace(version, copied)
        return version

    def update_version(
        self,
        context: OrgContext,
        template_id: uuid.UUID,
        version_id: uuid.UUID,
        changes: dict[str, Any],
        *,
        if_match: int | None,
    ) -> SalaryTemplateVersion:
        context.require(Permission.SALARY_TEMPLATE_MANAGE)
        version = self._versions.require(version_id, resource="template version")
        if version.template_id != template_id:
            raise ResourceNotFound("template version")
        if version.status != TemplateVersionStatus.DRAFT.value:
            raise ValidationFailed("Only a DRAFT version can be edited.")
        assert_etag(version.row_version, if_match)
        before = {field: getattr(version, field) for field in changes}
        for field, value in changes.items():
            setattr(version, field, value)
        version.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_TEMPLATE_CREATED,
            entity_type="salary_template_version",
            entity_id=version.id,
            before=before,
            after={field: getattr(version, field) for field in changes},
            fields=list(changes),
        )
        return version

    def replace_mappings(
        self,
        context: OrgContext,
        template_id: uuid.UUID,
        version_id: uuid.UUID,
        mappings: Sequence[ComponentMappingInput],
    ) -> SalaryTemplateVersion:
        context.require(Permission.SALARY_TEMPLATE_MANAGE)
        version = self._versions.require(version_id, resource="template version")
        if version.template_id != template_id:
            raise ResourceNotFound("template version")
        if version.status != TemplateVersionStatus.DRAFT.value:
            raise ValidationFailed("Only a DRAFT version can be edited.")
        self._replace_mappings(template_id, version, mappings, context)
        return version

    def _replace_mappings(
        self,
        template_id: uuid.UUID,
        version: SalaryTemplateVersion,
        mappings: Sequence[Any],
        context: OrgContext,
    ) -> None:
        seen: set[uuid.UUID] = set()
        rows: list[SalaryTemplateComponent] = []
        for item in mappings:
            data = item.model_dump() if hasattr(item, "model_dump") else item
            component = self._components.require(data["component_id"], resource="salary component")
            if component.status != "ACTIVE":
                raise ValidationFailed(f"Component {component.component_code} is inactive.")
            if component.id in seen:
                raise ValidationFailed(
                    details=[
                        ErrorDetail("component_id", "DUPLICATE", "Duplicate component mapping.")
                    ]
                )
            seen.add(component.id)
            overridable = bool(data.get("is_overridable", True))
            if not component.is_editable_per_employee and overridable:
                raise ValidationFailed(
                    f"{component.component_code} cannot be overridden per employee."
                )
            if (
                component.value_type == ValueType.FIXED_AMOUNT.value
                and component.currency
                and component.currency != version.currency
            ):
                raise ValidationFailed(
                    f"{component.component_code} currency differs from the version currency."
                )
            rows.append(
                SalaryTemplateComponent(
                    id=uuid7(),
                    template_id=template_id,
                    template_version_id=version.id,
                    component_id=component.id,
                    default_value_amount=data.get("default_value_amount"),
                    default_value_percentage=data.get("default_value_percentage"),
                    default_value_text=data.get("default_value_text"),
                    default_value_boolean=data.get("default_value_boolean"),
                    is_required=bool(data.get("is_required")),
                    is_overridable=overridable,
                    display_order=int(data.get("display_order") or 0),
                    created_by=context.user_id,
                )
            )
        if self._basic().id not in seen:
            rows.insert(0, self._basic_mapping(template_id, version.id, context))
        self._mappings.replace(version, rows)
        self._mappings.flush()

    def _basic(self) -> SalaryComponent:
        basic = self._components.get_by_code(BASIC_COMPONENT_CODE)
        if basic is None:
            raise ValidationFailed("The organization has no Basic component.")
        return basic

    def _basic_mapping(
        self, template_id: uuid.UUID, version_id: uuid.UUID, context: OrgContext
    ) -> SalaryTemplateComponent:
        """Basic is in every template version; its amount can be filled in later."""
        return SalaryTemplateComponent(
            id=uuid7(),
            template_id=template_id,
            template_version_id=version_id,
            component_id=self._basic().id,
            is_required=True,
            is_overridable=True,
            display_order=0,
            created_by=context.user_id,
        )

    def publish_version(
        self,
        context: OrgContext,
        template_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        changes: dict[str, Any] | None = None,
        mappings: Sequence[ComponentMappingInput] | None = None,
    ) -> SalaryTemplateVersion:
        """Publish a draft, first saving any ``changes``/``mappings`` the caller still holds.

        Saving and publishing share the caller's transaction, so a failed publish rolls the
        unsaved edits back with it rather than leaving a half-saved draft.
        """
        context.require(Permission.SALARY_TEMPLATE_MANAGE)
        version = self._versions.require(version_id, resource="template version")
        if version.template_id != template_id:
            raise ResourceNotFound("template version")
        if version.status != TemplateVersionStatus.DRAFT.value:
            raise ValidationFailed("Only a DRAFT version can be published.")
        for field, value in (changes or {}).items():
            setattr(version, field, value)
        if mappings is not None:
            self._replace_mappings(template_id, version, mappings, context)
        if version.effective_from is None:
            raise ValidationFailed(
                details=[ErrorDetail("effective_from", "REQUIRED", "Set an effective date first.")]
            )
        if version.version_number > 1 and not version.change_note:
            raise ValidationFailed(
                details=[ErrorDetail("change_note", "REQUIRED", "A change note is required.")]
            )
        conflicts = self._versions.overlapping(
            template_id, version.effective_from, version.effective_to, version.id
        )
        if conflicts:
            raise ValidationFailed("This version overlaps an ACTIVE version of the template.")
        active = [
            row
            for row in self._versions.list_for_template(template_id)
            if row.status == TemplateVersionStatus.ACTIVE.value
            and row.effective_from is not None
            and row.effective_from < version.effective_from
            and (row.effective_to is None or row.effective_to > version.effective_from)
        ]
        for previous in active:
            previous.effective_to = version.effective_from
            previous.updated_by = context.user_id
        version.status = TemplateVersionStatus.ACTIVE.value
        version.published_at = utc_now()
        version.published_by = context.user_id
        version.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_TEMPLATE_VERSION_PUBLISHED,
            entity_type="salary_template_version",
            entity_id=version.id,
            after={
                "status": version.status,
                "effective_from": version.effective_from.isoformat(),
            },
            fields=["status"],
        )
        return version

    def archive_template(
        self, context: OrgContext, template_id: uuid.UUID, reason: str
    ) -> SalaryTemplate:
        context.require(Permission.SALARY_TEMPLATE_MANAGE)
        header = self._templates.require(template_id, resource="salary template")
        header.is_archived = True
        header.archived_at = utc_now()
        header.archived_by = context.user_id
        header.archive_reason = reason
        for version in self._versions.list_for_template(header.id):
            version.status = TemplateVersionStatus.ARCHIVED.value
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_TEMPLATE_ARCHIVED,
            entity_type="salary_template",
            entity_id=header.id,
            after={"archived": True},
            fields=["is_archived"],
            reason=reason,
        )
        return header

    # ---------------------------------------------------------------- assignments

    def create_assignment(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        values: dict[str, Any],
    ) -> EmployeeSalaryAssignment:
        context.require(Permission.SALARY_REVISION_CREATE)
        employee = self._employees.require(employee_id, resource="employee")
        if values["effective_from"] < (employee.joining_date or date.min):
            raise ValidationFailed(
                details=[ErrorDetail("effective_from", "BEFORE_JOINING", "Before joining_date.")]
            )
        template, version = self._resolve_template(context, values)
        components = self._build_components(context, template, version, values["components"])
        row = EmployeeSalaryAssignment(
            id=uuid7(),
            employee_id=employee.id,
            template_id=template.id if template else None,
            template_version_id=version.id if version else None,
            template_version_number=version.version_number if version else None,
            template_code_snapshot=template.template_code if template else None,
            template_name_snapshot=template.template_name if template else None,
            currency=version.currency if version else str(values.get("currency") or "INR"),
            assignment_status=AssignmentStatus.DRAFT.value,
            approval_state=(
                ApprovalState.PENDING.value
                if self._settings.require(context.organization_id).salary_revision_approval_required
                else ApprovalState.NOT_REQUIRED.value
            ),
            effective_from=values["effective_from"],
            effective_to=values.get("effective_to"),
            effective_to_source=(
                EffectiveToSource.USER.value
                if values.get("effective_to")
                else EffectiveToSource.NONE.value
            ),
            revision_reason=str(values["revision_reason"]),
            revision_remarks=values.get("revision_remarks"),
            revision_letter_document_id=values.get("revision_letter_document_id"),
            is_backdated=values["effective_from"] < self._today(context),
            backdate_reason=values.get("backdate_reason"),
            created_by=context.user_id,
        )
        self._assignments.add(row)
        self._assignments.flush()
        self._save_components(row, components, context)
        self._event(context, row, RevisionEventType.CREATED_DRAFT)
        if values.get("confirm"):
            self._confirm(context, row, values.get("backdate_reason"))
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_REVISION_CREATED,
            entity_type="salary_assignment",
            entity_id=row.id,
            employee_id=employee.id,
            after={
                "effective_from": row.effective_from.isoformat(),
                "status": row.assignment_status,
            },
            fields=["effective_from", "assignment_status"],
        )
        return row

    def update_assignment(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        assignment_id: uuid.UUID,
        changes: dict[str, Any],
        *,
        if_match: int | None,
    ) -> EmployeeSalaryAssignment:
        context.require(Permission.SALARY_REVISION_CREATE)
        row = self._assignments.require_for_employee(employee_id, assignment_id)
        if row.assignment_status != AssignmentStatus.DRAFT.value:
            raise SalaryNotEditable()
        assert_etag(row.row_version, if_match)
        components = changes.pop("components", None)
        before = {field: getattr(row, field) for field in changes}
        for field, value in changes.items():
            setattr(row, field, value)
        if components is not None:
            built = self._build_components(context, None, None, components)
            self._save_components(row, built, context)
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_REVISION_UPDATED,
            entity_type="salary_assignment",
            entity_id=row.id,
            employee_id=employee_id,
            before=before,
            after={field: getattr(row, field) for field in changes},
            fields=list(changes),
        )
        return row

    def delete_draft(
        self, context: OrgContext, employee_id: uuid.UUID, assignment_id: uuid.UUID
    ) -> None:
        context.require(Permission.SALARY_REVISION_CREATE)
        row = self._assignments.require_for_employee(employee_id, assignment_id)
        if row.assignment_status != AssignmentStatus.DRAFT.value:
            raise SalaryNotEditable()
        if row.approval_state != ApprovalState.NOT_REQUIRED.value:
            raise SalaryNotEditable("A submitted draft cannot be deleted.")
        self._assignments.delete(row)

    def submit(
        self, context: OrgContext, employee_id: uuid.UUID, assignment_id: uuid.UUID
    ) -> EmployeeSalaryAssignment:
        context.require(Permission.SALARY_REVISION_CREATE)
        row = self._assignments.require_for_employee(employee_id, assignment_id)
        if row.approval_state != ApprovalState.PENDING.value:
            raise ValidationFailed("This revision does not require approval.")
        row.submitted_by = context.user_id
        row.submitted_at = utc_now()
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_REVISION_SUBMITTED,
            entity_type="salary_assignment",
            entity_id=row.id,
            employee_id=employee_id,
            fields=["approval_state"],
        )
        return row

    def approve(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        assignment_id: uuid.UUID,
        *,
        confirm: bool,
    ) -> EmployeeSalaryAssignment:
        context.require(Permission.SALARY_REVISION_APPROVE)
        row = self._assignments.require_for_employee(employee_id, assignment_id)
        if context.user_id in {row.created_by, row.submitted_by}:
            raise SelfApprovalNotAllowed()
        row.approval_state = ApprovalState.APPROVED.value
        row.approved_by = context.user_id
        row.approved_at = utc_now()
        if confirm:
            self._confirm(context, row, row.backdate_reason)
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_REVISION_APPROVED,
            entity_type="salary_assignment",
            entity_id=row.id,
            employee_id=employee_id,
            fields=["approval_state"],
        )
        return row

    def reject(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        assignment_id: uuid.UUID,
        reason: str,
    ) -> EmployeeSalaryAssignment:
        context.require(Permission.SALARY_REVISION_APPROVE)
        row = self._assignments.require_for_employee(employee_id, assignment_id)
        row.approval_state = ApprovalState.REJECTED.value
        row.rejection_reason = reason
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_REVISION_REJECTED,
            entity_type="salary_assignment",
            entity_id=row.id,
            employee_id=employee_id,
            fields=["approval_state"],
            reason=reason,
        )
        return row

    def confirm(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        assignment_id: uuid.UUID,
        *,
        backdate_reason: str | None,
    ) -> EmployeeSalaryAssignment:
        context.require(Permission.SALARY_REVISION_CONFIRM)
        row = self._assignments.require_for_employee(employee_id, assignment_id)
        self._confirm(context, row, backdate_reason)
        return row

    def _confirm(  # noqa: PLR0912, PLR0915 - the §18.3 ten-step algorithm
        self,
        context: OrgContext,
        row: EmployeeSalaryAssignment,
        backdate_reason: str | None,
    ) -> None:
        if row.assignment_status != AssignmentStatus.DRAFT.value:
            raise ValidationFailed("Only a draft assignment can be confirmed.")
        employee = self._employees.lock(row.employee_id)
        today = self._today(context)
        settings = self._settings.require(context.organization_id)
        if (
            settings.salary_revision_approval_required
            and row.approval_state != ApprovalState.APPROVED.value
        ):
            raise ApprovalRequired()
        if row.effective_from < today:
            if not context.has(Permission.SALARY_REVISION_BACKDATE):
                raise BackdateNotAllowed()
            if not backdate_reason:
                raise ValidationFailed(
                    details=[
                        ErrorDetail("backdate_reason", "REQUIRED", "Backdating requires a reason.")
                    ]
                )
            if (today - row.effective_from).days > settings.backdated_salary_max_days:
                raise BackdateNotAllowed()
            row.is_backdated = True
            row.backdate_reason = backdate_reason
        if (
            employee.employment_end_date is not None
            and row.effective_from > employee.employment_end_date
        ):
            raise SalaryAfterEmploymentEnd()

        confirmed = self._assignments.confirmed_for_employee(employee.id)
        if any(other.effective_from == row.effective_from for other in confirmed):
            raise SalaryAssignmentOverlap()
        predecessor = next(
            (
                other
                for other in confirmed
                if other.effective_from < row.effective_from
                and (other.effective_to is None or row.effective_from < other.effective_to)
            ),
            None,
        )
        successor = next(
            (other for other in confirmed if other.effective_from > row.effective_from),
            None,
        )
        explicit_end = row.effective_to
        if explicit_end is not None:
            if successor is not None and explicit_end > successor.effective_from:
                raise SalaryAssignmentOverlap()
            source = EffectiveToSource.USER.value
        elif predecessor is not None:
            explicit_end = predecessor.effective_to
            source = predecessor.effective_to_source
        elif successor is not None:
            explicit_end = successor.effective_from
            source = EffectiveToSource.SUCCESSOR.value
        elif employee.employment_end_date is not None:
            explicit_end = employee.employment_end_date + timedelta(days=1)
            source = EffectiveToSource.EMPLOYMENT_END.value
        else:
            explicit_end = None
            source = EffectiveToSource.NONE.value
        if explicit_end is not None and explicit_end <= row.effective_from:
            raise SalaryAssignmentOverlap()

        if predecessor is not None:
            row.predecessor_assignment_id = predecessor.id
            row.predecessor_original_effective_to = predecessor.effective_to
            before = predecessor.effective_to
            predecessor.effective_to = row.effective_from
            predecessor.effective_to_source = EffectiveToSource.SUCCESSOR.value
            predecessor.updated_by = context.user_id
            self._event(
                context,
                predecessor,
                RevisionEventType.BOUNDARY_ADJUSTED,
                effective_to_before=before,
                effective_to_after=predecessor.effective_to,
            )

        row.effective_to = explicit_end
        row.effective_to_source = source
        row.assignment_status = self._derive_status(row.effective_from, explicit_end, today)
        row.confirmed_by = context.user_id
        row.confirmed_at = utc_now()
        row.updated_by = context.user_id
        self._event(context, row, RevisionEventType.CONFIRMED)
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_REVISION_CONFIRMED,
            entity_type="salary_assignment",
            entity_id=row.id,
            employee_id=employee.id,
            after={"status": row.assignment_status, "effective_to": explicit_end},
            fields=["assignment_status", "effective_to"],
            reason=backdate_reason,
        )

    def cancel(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        assignment_id: uuid.UUID,
        reason: str,
    ) -> EmployeeSalaryAssignment:
        context.require(Permission.SALARY_REVISION_CANCEL)
        row = self._assignments.require_for_employee(employee_id, assignment_id)
        if row.assignment_status != AssignmentStatus.SCHEDULED.value:
            raise SalaryNotCancellable()
        employee = self._employees.lock(employee_id)
        previous = next(
            (
                other
                for other in self._assignments.confirmed_for_employee(employee.id)
                if other.id != row.id
                and other.effective_to == row.effective_from
                and other.effective_to_source == EffectiveToSource.SUCCESSOR.value
            ),
            None,
        )
        row.assignment_status = AssignmentStatus.CANCELLED.value
        row.cancellation_type = CancellationType.USER_CANCEL.value
        row.cancellation_reason = reason
        row.cancelled_by = context.user_id
        row.cancelled_at = utc_now()
        self._event(context, row, RevisionEventType.CANCELLED, reason=reason)
        if previous is not None:
            previous.effective_to = row.effective_to
            previous.effective_to_source = row.effective_to_source
            self._event(
                context,
                previous,
                RevisionEventType.BOUNDARY_ADJUSTED,
                effective_to_after=previous.effective_to,
            )
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_REVISION_CANCELLED,
            entity_type="salary_assignment",
            entity_id=row.id,
            employee_id=employee_id,
            fields=["assignment_status"],
            reason=reason,
        )
        return row

    def correct(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        assignment_id: uuid.UUID,
        *,
        components: Sequence[AssignmentComponentInput],
        reason: str,
    ) -> EmployeeSalaryAssignment:
        context.require(Permission.SALARY_REVISION_CORRECT)
        original = self._assignments.require_for_employee(employee_id, assignment_id)
        if original.assignment_status not in {
            AssignmentStatus.ACTIVE.value,
            AssignmentStatus.EXPIRED.value,
        }:
            raise SalaryNotCorrectable()
        employee = self._employees.lock(employee_id)
        built = self._build_components(context, None, None, components)
        original.assignment_status = AssignmentStatus.CANCELLED.value
        original.cancellation_type = CancellationType.CORRECTION.value
        original.cancellation_reason = reason
        original.cancelled_by = context.user_id
        original.cancelled_at = utc_now()
        replacement = EmployeeSalaryAssignment(
            id=uuid7(),
            employee_id=employee.id,
            template_id=original.template_id,
            template_version_id=original.template_version_id,
            template_version_number=original.template_version_number,
            template_code_snapshot=original.template_code_snapshot,
            template_name_snapshot=original.template_name_snapshot,
            currency=original.currency,
            assignment_status=original.assignment_status,
            approval_state=original.approval_state,
            effective_from=original.effective_from,
            effective_to=original.effective_to,
            effective_to_source=original.effective_to_source,
            revision_reason=original.revision_reason,
            revision_remarks=original.revision_remarks,
            revision_letter_document_id=original.revision_letter_document_id,
            is_backdated=original.is_backdated,
            backdate_reason=original.backdate_reason,
            predecessor_assignment_id=original.predecessor_assignment_id,
            predecessor_original_effective_to=original.predecessor_original_effective_to,
            replaces_assignment_id=original.id,
            created_by=context.user_id,
        )
        replacement.assignment_status = self._derive_status(
            replacement.effective_from,
            replacement.effective_to,
            self._today(context),
        )
        self._assignments.add(replacement)
        self._assignments.flush()
        self._save_components(replacement, built, context)
        original.superseded_by_assignment_id = replacement.id
        self._event(
            context,
            replacement,
            RevisionEventType.CORRECTED,
            related_assignment_id=original.id,
            reason=reason,
        )
        self._audit.record_change(
            context,
            action=AuditAction.SALARY_REVISION_CORRECTED,
            entity_type="salary_assignment",
            entity_id=replacement.id,
            employee_id=employee_id,
            after={"effective_from": replacement.effective_from.isoformat()},
            fields=["components"],
            reason=reason,
        )
        return replacement

    def replace(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        assignment_id: uuid.UUID,
        values: dict[str, Any],
    ) -> EmployeeSalaryAssignment:
        context.require_any(Permission.SALARY_REVISION_CANCEL, Permission.SALARY_REVISION_CONFIRM)
        existing = self._assignments.require_for_employee(employee_id, assignment_id)
        if existing.assignment_status != AssignmentStatus.SCHEDULED.value:
            raise SalaryNotCancellable()
        self.cancel(context, employee_id, assignment_id, "Replaced by a new revision")
        return self.create_assignment(context, employee_id, {**values, "confirm": True})

    def apply_employment_end(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        end_date: date | None,
    ) -> int:
        """§18.6 O4: adjust or restore salary boundaries after an employment end date change."""
        employee = self._employees.lock(employee_id)
        assignments = list(self._assignments.confirmed_for_employee(employee.id))
        if end_date is not None:
            future = [row for row in assignments if row.effective_from > end_date]
            if future:
                raise SalaryAfterEmploymentEnd()
        changed = 0
        for row in assignments:
            if end_date is None:
                if row.effective_to_source == EffectiveToSource.EMPLOYMENT_END.value:
                    row.effective_to = None
                    row.effective_to_source = EffectiveToSource.NONE.value
                    self._event(
                        context,
                        row,
                        RevisionEventType.BOUNDARY_ADJUSTED,
                        effective_to_after=None,
                    )
                    changed += 1
            elif row.effective_from <= end_date and (
                row.effective_to is None or row.effective_to > end_date + timedelta(days=1)
            ):
                before = row.effective_to
                row.effective_to = end_date + timedelta(days=1)
                row.effective_to_source = EffectiveToSource.EMPLOYMENT_END.value
                self._event(
                    context,
                    row,
                    RevisionEventType.BOUNDARY_ADJUSTED,
                    effective_to_before=before,
                    effective_to_after=row.effective_to,
                )
                changed += 1
        if changed:
            self._audit.record_change(
                context,
                action=AuditAction.SALARY_BOUNDARY_ADJUSTED,
                entity_type="employee_salary_assignment",
                entity_id=employee.id,
                employee_id=employee.id,
                fields=["effective_to"],
            )
        return changed

    # ---------------------------------------------------------------- reads'

    def timeline(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        *,
        as_of: date | None = None,
        include_cancelled: bool = False,
    ) -> Sequence[EmployeeSalaryAssignment]:
        context.require(Permission.SALARY_VIEW)
        self._employees.require(employee_id, resource="employee")
        if as_of is not None:
            row = self._assignments.as_of(employee_id, as_of)
            return [row] if row is not None else []
        return self._assignments.list_for_employee(employee_id, include_cancelled=include_cancelled)

    def current(
        self, context: OrgContext, employee_id: uuid.UUID
    ) -> EmployeeSalaryAssignment | None:
        context.require(Permission.SALARY_VIEW)
        self._employees.require(employee_id, resource="employee")
        return self._assignments.as_of(employee_id, self._today(context))

    def components_for(self, assignment_id: uuid.UUID) -> Sequence[EmployeeSalaryComponent]:
        return self._assignment_components.list_for_assignment(assignment_id)

    def history(
        self, context: OrgContext, employee_id: uuid.UUID
    ) -> Sequence[SalaryRevisionHistory]:
        context.require(Permission.SALARY_VIEW)
        self._employees.require(employee_id, resource="employee")
        return self._history.list_for_employee(employee_id)

    def compare(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        left_id: uuid.UUID,
        right_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        context.require(Permission.SALARY_VIEW)
        left = self._assignments.require_for_employee(employee_id, left_id)
        right = self._assignments.require_for_employee(employee_id, right_id)
        left_values = {row.component_code_snapshot: row for row in self.components_for(left.id)}
        right_values = {row.component_code_snapshot: row for row in self.components_for(right.id)}
        result = []
        for code in sorted(set(left_values) | set(right_values)):
            left_row = left_values.get(code)
            right_row = right_values.get(code)
            if left_row is None:
                change = "ADDED"
            elif right_row is None:
                change = "REMOVED"
            elif _value(left_row) == _value(right_row):
                change = "UNCHANGED"
            else:
                change = "CHANGED"
            source = left_row if left_row is not None else right_row
            if source is None:
                continue
            result.append(
                {
                    "component_code": code,
                    "component_name": source.component_name_snapshot,
                    "left_value": _value(left_row),
                    "right_value": _value(right_row),
                    "change_type": change,
                    "left_overridden": left_row.is_overridden if left_row else False,
                    "right_overridden": right_row.is_overridden if right_row else False,
                }
            )
        return result

    def preview(
        self, context: OrgContext, employee_id: uuid.UUID, values: dict[str, Any]
    ) -> dict[str, Any]:
        context.require(Permission.SALARY_REVISION_CREATE)
        employee = self._employees.require(employee_id, resource="employee")
        today = self._today(context)
        start = values["effective_from"]
        confirmed = self._assignments.confirmed_for_employee(employee_id)
        predecessor = next(
            (
                row
                for row in confirmed
                if row.effective_from < start
                and (row.effective_to is None or start < row.effective_to)
            ),
            None,
        )
        successor = next((row for row in confirmed if row.effective_from > start), None)
        end, source = values.get("effective_to"), EffectiveToSource.USER.value
        if end is None and predecessor is not None:
            end, source = predecessor.effective_to, predecessor.effective_to_source
        elif end is None and successor is not None:
            end, source = successor.effective_from, EffectiveToSource.SUCCESSOR.value
        errors = []
        if start < (employee.joining_date or date.min):
            errors.append({"field": "effective_from", "code": "BEFORE_JOINING"})
        if any(row.effective_from == start for row in confirmed):
            errors.append({"field": "effective_from", "code": "SALARY_ASSIGNMENT_OVERLAP"})
        status = self._derive_status(start, end, today)
        return {
            "proposed_status": status,
            "effective_from": start,
            "effective_to": end,
            "effective_to_source": source,
            "predecessor_assignment_id": predecessor.id if predecessor else None,
            "boundary_adjustments": (
                [
                    {
                        "assignment_id": predecessor.id,
                        "effective_to_before": predecessor.effective_to,
                        "effective_to_after": start,
                    }
                ]
                if predecessor
                else []
            ),
            "validation_errors": errors,
        }

    # ---------------------------------------------------------------- internals

    def _today(self, context: OrgContext) -> date:
        organization = self._organizations.get(context.organization_id)
        return today_in(organization.timezone if organization else None)

    def _derive_status(self, start: date, end: date | None, today: date) -> str:
        if start > today:
            return AssignmentStatus.SCHEDULED.value
        if end is not None and end <= today:
            return AssignmentStatus.EXPIRED.value
        return AssignmentStatus.ACTIVE.value

    def _resolve_template(
        self, context: OrgContext, values: dict[str, Any]
    ) -> tuple[SalaryTemplate | None, SalaryTemplateVersion | None]:
        template_id = values.get("template_id")
        if template_id is None:
            if not values.get("components"):
                raise ValidationFailed("An assignment needs a template or explicit components.")
            # Picking existing catalogue components is ordinary assignment; the custom switch
            # and permission guard only ad-hoc items that have no catalogue entry.
            if any(item.component_id is None for item in values["components"]):
                settings = self._settings.require(context.organization_id)
                if not settings.allow_custom_salary_components:
                    raise ValidationFailed("Custom salary components are disabled.")
                context.require(Permission.SALARY_CUSTOM_COMPONENT)
            return None, None
        template = self._templates.require(template_id, resource="salary template")
        if template.is_archived:
            raise TemplateArchived()
        version = self._versions.effective_on(template.id, values["effective_from"])
        if version is None:
            raise TemplateVersionNotEffective()
        return template, version

    def _build_components(
        self,
        context: OrgContext,
        template: SalaryTemplate | None,
        version: SalaryTemplateVersion | None,
        supplied: Sequence[AssignmentComponentInput],
    ) -> list[dict[str, Any]]:
        overrides = {item.component_id: item for item in supplied if item.component_id is not None}
        rows: list[dict[str, Any]] = []
        if template is not None and version is not None:
            mappings = self._mappings.list_for_version(version.id)
            for mapping in mappings:
                component = self._components.require(
                    mapping.component_id, resource="salary component"
                )
                override = overrides.pop(component.id, None)
                if override is not None and not mapping.is_overridable:
                    raise ComponentNotOverridable()
                value = (
                    override.model_dump()
                    if override is not None
                    else {
                        "value_amount": mapping.default_value_amount,
                        "value_percentage": mapping.default_value_percentage,
                        "value_text": mapping.default_value_text,
                        "value_boolean": mapping.default_value_boolean,
                    }
                )
                default_value = {
                    "value_amount": mapping.default_value_amount,
                    "value_percentage": mapping.default_value_percentage,
                    "value_text": mapping.default_value_text,
                    "value_boolean": mapping.default_value_boolean,
                }
                default_present = _typed_value(component, default_value) is not None
                overridden = override is not None and _typed_value(
                    component, value
                ) != _typed_value(component, default_value)
                # Only replacing a real template default needs a reason; supplying the amount a
                # template left empty (Basic) does not.
                if overridden and default_present and not (override and override.override_reason):
                    raise ValidationFailed(
                        details=[
                            ErrorDetail(
                                "override_reason",
                                "REQUIRED",
                                f"An override of {component.component_code} needs a reason.",
                            )
                        ]
                    )
                rows.append(
                    _component_row(
                        component,
                        value,
                        template_defaults={
                            "present": default_present,
                            "amount": mapping.default_value_amount,
                            "percentage": mapping.default_value_percentage,
                            "text": mapping.default_value_text,
                            "boolean": mapping.default_value_boolean,
                        },
                        overridden=overridden,
                        override_reason=(
                            override.override_reason if override is not None else None
                        ),
                        is_custom=False,
                    )
                )
            if overrides:
                raise ValidationFailed("A component does not belong to the template version.")
        else:
            for item in supplied:
                if item.component_id is not None:
                    component = self._components.require(
                        item.component_id, resource="salary component"
                    )
                else:
                    if not item.component_code or not item.component_name or not item.value_type:
                        raise ValidationFailed(
                            "A custom component needs code, name and value_type."
                        )
                    if item.component_code.strip().upper() == BASIC_COMPONENT_CODE:
                        raise ValidationFailed("BASIC is reserved for the Basic component.")
                    component = SalaryComponent(
                        id=uuid7(),
                        component_code=item.component_code,
                        component_name=item.component_name,
                        display_name=item.display_name or item.component_name,
                        component_type=(item.component_type or ComponentType.EARNING).value,
                        component_category=str(
                            getattr(item.component_category, "value", item.component_category)
                            or "OTHER"
                        ),
                        value_type=item.value_type.value,
                        amount_period=(item.amount_period or AmountPeriod.NOT_APPLICABLE).value,
                        currency="INR",
                        created_by=context.user_id,
                    )
                rows.append(
                    _component_row(
                        component,
                        item.model_dump(),
                        template_defaults={},
                        overridden=False,
                        override_reason=item.override_reason,
                        is_custom=item.component_id is None,
                    )
                )
        _apply_basic(rows)
        return rows

    def _save_components(
        self,
        assignment: EmployeeSalaryAssignment,
        rows: Sequence[dict[str, Any]],
        context: OrgContext,
    ) -> None:
        built = []
        for index, data in enumerate(rows):
            built.append(
                EmployeeSalaryComponent(
                    id=uuid7(),
                    salary_assignment_id=assignment.id,
                    component_id=data["component_id"],
                    component_code_snapshot=data["component_code"],
                    component_name_snapshot=data["component_name"],
                    display_name_snapshot=data["display_name"],
                    component_type_snapshot=data["component_type"],
                    component_category_snapshot=data["component_category"],
                    value_type=data["value_type"],
                    amount_period_snapshot=data.get("amount_period"),
                    value_amount=data.get("value_amount"),
                    value_percentage=data.get("value_percentage"),
                    value_text=data.get("value_text"),
                    value_boolean=data.get("value_boolean"),
                    computed_amount=data.get("computed_amount"),
                    template_default_amount=data.get("template_default_amount"),
                    template_default_percentage=data.get("template_default_percentage"),
                    template_default_text=data.get("template_default_text"),
                    template_default_boolean=data.get("template_default_boolean"),
                    template_default_present=data.get("template_default_present", False),
                    currency=data.get("currency"),
                    is_overridden=data.get("is_overridden", False),
                    override_reason=data.get("override_reason"),
                    is_custom=data.get("is_custom", False),
                    display_order_snapshot=index,
                    created_by=context.user_id,
                )
            )
        self._assignment_components.replace(assignment.id, built)
        self._assignment_components.flush()

    def _event(
        self,
        context: OrgContext,
        assignment: EmployeeSalaryAssignment,
        event_type: RevisionEventType,
        *,
        effective_to_before: date | None = None,
        effective_to_after: date | None = None,
        related_assignment_id: uuid.UUID | None = None,
        reason: str | None = None,
    ) -> None:
        self._history.add(
            SalaryRevisionHistory(
                id=uuid7(),
                employee_id=assignment.employee_id,
                salary_assignment_id=assignment.id,
                related_assignment_id=related_assignment_id,
                event_type=event_type.value,
                effective_from_snapshot=assignment.effective_from,
                effective_to_before=effective_to_before,
                effective_to_after=effective_to_after,
                reason=reason,
                actor_user_id=context.user_id,
                created_by=context.user_id,
            )
        )


def _typed_value(component: SalaryComponent, values: dict[str, Any]) -> Any:
    match component.value_type:
        case ValueType.FIXED_AMOUNT.value:
            return values.get("value_amount")
        case ValueType.PERCENTAGE.value:
            return values.get("value_percentage")
        case ValueType.TEXT.value | ValueType.OTHER_CONFIGURED.value:
            return values.get("value_text")
        case ValueType.BOOLEAN.value:
            return values.get("value_boolean")
    return None


def _apply_basic(rows: list[dict[str, Any]]) -> None:
    """Require Basic's amount and resolve each ``%`` component to ``percentage x Basic``.

    The only arithmetic in the salary module: one component's own amount, rounded half-up to
    the money scale. No component is ever summed with another.
    """
    basic = next(
        (row for row in rows if str(row["component_code"]).upper() == BASIC_COMPONENT_CODE), None
    )
    if basic is None or basic.get("value_amount") is None:
        raise ValidationFailed(
            details=[
                ErrorDetail("components", "BASIC_REQUIRED", "Basic and its amount are required.")
            ]
        )
    for row in rows:
        if row["value_type"] != ValueType.PERCENTAGE.value:
            continue
        if row.get("value_percentage") is None:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "components",
                        "PERCENTAGE_REQUIRED",
                        f"{row['component_code']} needs a percentage.",
                    )
                ]
            )
        row["computed_amount"] = (basic["value_amount"] * row["value_percentage"] / 100).quantize(
            _MONEY_STEP, rounding=ROUND_HALF_UP
        )


def _component_row(
    component: SalaryComponent,
    values: dict[str, Any],
    *,
    template_defaults: dict[str, Any],
    overridden: bool,
    override_reason: str | None,
    is_custom: bool,
) -> dict[str, Any]:
    return {
        "component_id": None if is_custom else component.id,
        "component_code": component.component_code,
        "component_name": component.component_name,
        "display_name": component.display_name,
        "component_type": component.component_type,
        "component_category": component.component_category,
        "value_type": component.value_type,
        "amount_period": component.amount_period,
        "value_amount": values.get("value_amount"),
        "value_percentage": values.get("value_percentage"),
        "value_text": values.get("value_text"),
        "value_boolean": values.get("value_boolean"),
        "template_default_amount": template_defaults.get("amount"),
        "template_default_percentage": template_defaults.get("percentage"),
        "template_default_text": template_defaults.get("text"),
        "template_default_boolean": template_defaults.get("boolean"),
        "template_default_present": bool(template_defaults.get("present")),
        "currency": component.currency,
        "is_overridden": overridden,
        "override_reason": override_reason,
        "is_custom": is_custom,
    }


def _value(row: EmployeeSalaryComponent | None) -> str | None:
    if row is None:
        return None
    match row.value_type:
        case ValueType.FIXED_AMOUNT.value:
            return None if row.value_amount is None else str(row.value_amount)
        case ValueType.PERCENTAGE.value:
            return None if row.value_percentage is None else str(row.value_percentage)
        case ValueType.TEXT.value | ValueType.OTHER_CONFIGURED.value:
            return row.value_text
        case ValueType.BOOLEAN.value:
            return None if row.value_boolean is None else str(row.value_boolean)
    return None


__all__ = ["SalaryService"]
