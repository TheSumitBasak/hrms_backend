"""Employee creation, editing, lifecycle, status transitions and job history (§10)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import age_on, today_in, utc_now
from app.core.context import OrgContext
from app.core.errors import (
    AgeBelowAbsoluteMinimum,
    DuplicateResource,
    ErrorDetail,
    FieldNotCollected,
    FieldReadOnly,
    InvalidStatusTransition,
    ReportingCycle,
    ResourceNotFound,
    StatusCorrectionWindowExpired,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.core.warnings import WarningCode, WarningCollector
from app.db.types import uuid7
from app.models.employee import (
    SEPARATED_STATUSES,
    Employee,
    EmployeeAddress,
    EmployeeJobHistory,
    EmployeeStatus,
    EmployeeStatusHistory,
)
from app.models.master import EmployeeCustomStatus
from app.models.org_config import IdentifierRequirement
from app.repositories.branch import BranchRepository
from app.repositories.employee import (
    EmployeeAddressRepository,
    EmployeeJobHistoryRepository,
    EmployeeRepository,
    EmployeeStatusHistoryRepository,
)
from app.repositories.employee_code import EmployeeCodeRegistryRepository
from app.repositories.identifier import EmployeeIdentifierRepository
from app.repositories.master import CustomStatusRepository, MasterReferenceChecker
from app.repositories.organization import (
    IdentifierPolicyRepository,
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.repositories.reference import StateRepository
from app.schemas.common import CursorCodec, SortSpec
from app.schemas.employee import AddressResponse
from app.services.employee_codes import EmployeeCodeService
from app.services.lifecycle import LifecycleHooks
from app.services.organizations import assert_etag
from app.services.position_assignments import PositionAssignmentService
from app.services.salary import SalaryService
from app.validation.normalizers import (
    normalize_email,
    normalize_name,
    normalize_postal_code,
    normalize_search_name,
)
from app.validation.rules import RuleId, check

JOB_FIELDS = frozenset(
    {
        "joining_date",
        "employment_type_id",
        "employment_start_date",
        "employment_end_date",
        "probation_duration_months",
        "probation_end_date",
        "confirmation_date",
        "designation_id",
        "department_id",
        "sub_department_id",
        "grade_id",
        "business_unit_id",
        "cost_center_id",
        "branch_id",
        "work_location_id",
        "employee_category_id",
        "work_mode",
        "reporting_manager_employee_id",
        "hr_contact_employee_id",
        "notice_period_days",
        "source_of_hire",
        "referred_by_employee_id",
        "offer_letter_date",
        "appointment_letter_date",
        "status",
        "status_effective_date",
        "status_reason",
    }
)
"""§24.4.3: these are rejected by ``PATCH /employees/{id}``; job-changes owns them."""

ACTIVATION_FIELDS = (
    ("date_of_birth", "date_of_birth"),
    ("gender", "gender"),
    ("joining_date", "joining_date"),
    ("employment_type_id", "employment_type_id"),
    ("designation_id", "designation_id"),
    ("department_id", "department_id"),
    ("branch_id", "branch_id"),
    ("personal_mobile", "personal_mobile"),
)
"""§10.3 activation checks — the mandatory-at-activation subset."""

TERMINAL_STATUSES: frozenset[str] = frozenset(status.value for status in SEPARATED_STATUSES)


class EmployeeService:
    """Employee use cases (§10, §24.4.3)."""

    def __init__(
        self,
        *,
        lifecycle: LifecycleHooks | None = None,
        employees: EmployeeRepository,
        addresses: EmployeeAddressRepository,
        job_history: EmployeeJobHistoryRepository,
        status_history: EmployeeStatusHistoryRepository,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        branch_references: BranchRepository,
        masters: MasterReferenceChecker,
        states: StateRepository,
        codes: EmployeeCodeService,
        identifiers: IdentifierPolicyRepository,
        custom_statuses: CustomStatusRepository,
        registry: EmployeeCodeRegistryRepository,
        identifier_values: EmployeeIdentifierRepository,
        cursor_codec: CursorCodec,
        salary: SalaryService,
        position_assignments: PositionAssignmentService,
        audit: AuditWriter,
    ) -> None:
        self._lifecycle = lifecycle
        self._employees = employees
        self._addresses = addresses
        self._job_history = job_history
        self._status_history = status_history
        self._organizations = organizations
        self._settings = settings
        self._branches = branch_references
        self._masters = masters
        self._states = states
        self._codes = codes
        self._identifiers = identifiers
        self._custom_statuses = custom_statuses
        self._registry = registry
        self._identifier_values = identifier_values
        self._cursor_codec = cursor_codec
        self._salary = salary
        self._position_assignments = position_assignments
        self._audit = audit

    # ---------------------------------------------------------------- create

    def create(
        self,
        context: OrgContext,
        *,
        values: dict[str, Any],
        acknowledged_warnings: list[str],
        manual_code: str | None = None,
    ) -> tuple[Employee, list[str]]:
        context.require(Permission.EMPLOYEE_CREATE)
        organization = self._organization(context)
        if organization.status != "ACTIVE":
            raise ValidationFailed("The organization is inactive; employees cannot be created.")
        settings = self._settings.require(context.organization_id)
        warnings = WarningCollector(acknowledged_warnings)

        self._validate_collected_fields(values, settings)
        self._validate_references(values, context)
        self._validate_dates_and_age(values, settings, warnings)
        self._validate_transition_link(values, context)
        self._validate_contacts(values, warnings)

        email_taken = (
            bool(values.get("official_email"))
            and settings.official_email_uniqueness_scope != "NONE"
            and self._email_taken(
                str(values["official_email"]),
                context,
                settings.official_email_uniqueness_scope,
            )
        )
        if email_taken:
            raise DuplicateResource(
                "That official email is already in use by another employee."
            ).with_detail(
                field="official_email",
                code="DUPLICATE",
                message="Official email must be unique in its configured scope.",
            )

        if manual_code and not settings.allow_manual_employee_code:
            raise ValidationFailed("Manual employee codes are disabled for this organization.")
        if manual_code and not context.has(Permission.EMPLOYEE_CODE_MANUAL):
            context.require(Permission.EMPLOYEE_CODE_MANUAL)

        acknowledged = warnings.enforce()
        employee = Employee(
            id=uuid7(),
            first_name=normalize_name(str(values["first_name"])) or "",
            middle_name=normalize_name(values.get("middle_name")),
            last_name=normalize_name(values.get("last_name")),
            display_name=normalize_name(values.get("display_name")),
            father_name=normalize_name(values.get("father_name")),
            mother_name=normalize_name(values.get("mother_name")),
            spouse_name=normalize_name(values.get("spouse_name")),
            date_of_birth=values.get("date_of_birth"),
            gender=values.get("gender"),
            marital_status=values.get("marital_status"),
            nationality=values.get("nationality") or "IN",
            blood_group=values.get("blood_group"),
            religion=values.get("religion"),
            social_category=values.get("social_category"),
            disability_status=values.get("disability_status"),
            disability_type=values.get("disability_type"),
            disability_percentage=values.get("disability_percentage"),
            employment_transition_type=values.get("employment_transition_type") or "NEW_HIRE",
            previous_employment_record_id=values.get("previous_employment_record_id"),
            personal_mobile=values.get("personal_mobile"),
            alternate_mobile=values.get("alternate_mobile"),
            personal_email=values.get("personal_email"),
            official_email=values.get("official_email"),
            emergency_contact_name=values.get("emergency_contact_name"),
            emergency_contact_relationship=values.get("emergency_contact_relationship"),
            emergency_contact_phone=values.get("emergency_contact_phone"),
            emergency_contact_alternate_phone=values.get("emergency_contact_alternate_phone"),
            joining_date=values.get("joining_date"),
            employment_type_id=values.get("employment_type_id"),
            employment_start_date=values.get("employment_start_date") or values.get("joining_date"),
            employment_end_date=values.get("employment_end_date"),
            probation_duration_months=values.get("probation_duration_months"),
            probation_end_date=values.get("probation_end_date"),
            confirmation_date=values.get("confirmation_date"),
            designation_id=values.get("designation_id"),
            department_id=values.get("department_id"),
            sub_department_id=values.get("sub_department_id"),
            grade_id=values.get("grade_id"),
            business_unit_id=values.get("business_unit_id"),
            cost_center_id=values.get("cost_center_id"),
            branch_id=values.get("branch_id"),
            work_location_id=values.get("work_location_id"),
            employee_category_id=values.get("employee_category_id"),
            work_mode=values.get("work_mode") or "ON_SITE",
            reporting_manager_employee_id=values.get("reporting_manager_employee_id"),
            hr_contact_employee_id=values.get("hr_contact_employee_id"),
            notice_period_days=values.get("notice_period_days"),
            source_of_hire=values.get("source_of_hire"),
            referred_by_employee_id=values.get("referred_by_employee_id"),
            offer_letter_date=values.get("offer_letter_date"),
            appointment_letter_date=values.get("appointment_letter_date"),
            status=EmployeeStatus.DRAFT.value,
            age_exception_status=values.get("age_exception_status") or "NONE",
            permanent_address_same_as_current=bool(values.get("permanent_address_same_as_current")),
            remarks=values.get("remarks"),
            created_by=context.user_id,
        )
        self._employees.add(employee)
        self._employees.flush()
        self._set_display_name(employee)
        self._write_addresses(context, employee, values)

        if manual_code:
            code = manual_code.upper()
            if not settings.allow_manual_employee_code:
                raise ValidationFailed("Manual employee codes are disabled.")
            self._codes.claim_manual(context, employee=employee, code=code)
            employee.employee_code = code
        elif settings.employee_code_assignment_point == "ON_CREATE":
            if not employee.department_id or not employee.joining_date:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "department_id",
                            "REQUIRED",
                            "Department and joining date are required when codes are assigned "
                            "on creation.",
                        )
                    ]
                )
            code, _ = self._codes.issue_code(
                context, employee=employee, basis_date=employee.joining_date
            )
            employee.employee_code = code

        self._audit.record_change(
            context,
            action=AuditAction.EMPLOYEE_CREATED,
            entity_type="employee",
            entity_id=employee.id,
            employee_id=employee.id,
            after={
                "employee_code": employee.employee_code,
                "display_name": employee.display_name,
                "status": employee.status,
            },
            fields=["employee_code", "display_name", "status"],
            acknowledged_warnings=acknowledged,
        )
        return employee, acknowledged

    # ---------------------------------------------------------------- reads

    def list_employees(
        self,
        context: OrgContext,
        *,
        query: str | None = None,
        status: Sequence[str] | None = None,
        department_id: uuid.UUID | None = None,
        designation_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
    ) -> Sequence[Employee]:
        context.require(Permission.EMPLOYEE_LIST)
        rows = self._employees.search(
            query=query,
            status=status,
            department_id=department_id,
            designation_id=designation_id,
            branch_id=branch_id,
        )
        if context.is_reporting_scoped:
            rows = [row for row in rows if self._in_scope(context, row)]
        return rows

    def search(
        self,
        context: OrgContext,
        *,
        query: str | None = None,
        employee_code: str | None = None,
        official_email: str | None = None,
        status: Sequence[str] | None = None,
        department_ids: Sequence[uuid.UUID] | None = None,
        designation_ids: Sequence[uuid.UUID] | None = None,
        branch_ids: Sequence[uuid.UUID] | None = None,
        employment_type_ids: Sequence[uuid.UUID] | None = None,
        grade_ids: Sequence[uuid.UUID] | None = None,
        business_unit_ids: Sequence[uuid.UUID] | None = None,
        cost_center_ids: Sequence[uuid.UUID] | None = None,
        work_location_ids: Sequence[uuid.UUID] | None = None,
        employee_category_ids: Sequence[uuid.UUID] | None = None,
        work_modes: Sequence[str] | None = None,
        joining_date_from: date | None = None,
        joining_date_to: date | None = None,
        reporting_manager_employee_id: uuid.UUID | None = None,
        include_indirect: bool = False,
        employee_ids: Sequence[uuid.UUID] | None = None,
        include_archived: bool = False,
        sort: str | None = None,
        cursor: str | None = None,
        limit: int = 25,
        with_total: bool = False,
    ) -> tuple[list[dict[str, Any]], str | None, int | None]:
        context.require(Permission.EMPLOYEE_LIST)
        if include_archived:
            context.require(Permission.EMPLOYEE_ARCHIVE)
        if joining_date_from and joining_date_to and joining_date_from > joining_date_to:
            raise ValidationFailed(
                details=[
                    ErrorDetail("joining_date_from", "INVALID_RANGE", "from must be before to.")
                ]
            )
        if (
            joining_date_from
            and joining_date_to
            and (joining_date_to - joining_date_from).days > 365 * 50
        ):
            raise ValidationFailed("The date range may not exceed fifty years.")
        allowed = frozenset(
            {
                "employee_code",
                "display_name",
                "joining_date",
                "department_name",
                "designation_name",
                "status",
                "created_at",
            }
        )
        spec = SortSpec.parse(sort, allowed=allowed, default_field="display_name")
        after_value: Any = None
        after_id = None
        direction = spec.direction
        if cursor:
            payload = self._cursor_codec.decode(cursor)
            if payload.sort_field != spec.field:
                raise ValidationFailed("The cursor was issued for a different sort.")
            after_value = payload.sort_value
            after_id = payload.last_id
            direction = payload.direction
        if include_indirect and reporting_manager_employee_id is not None:
            employee_ids = self._employees.descendant_ids(
                reporting_manager_employee_id, max_depth=context.reporting_scope_depth
            )
            reporting_manager_employee_id = None
        if spec.field == "joining_date" and after_value is not None:
            after_value = date.fromisoformat(str(after_value))
        rows, has_more, total = self._employees.search_page(
            query=query,
            employee_code=employee_code,
            official_email=official_email,
            status=status,
            department_ids=department_ids,
            designation_ids=designation_ids,
            branch_ids=branch_ids,
            employment_type_ids=employment_type_ids,
            grade_ids=grade_ids,
            business_unit_ids=business_unit_ids,
            cost_center_ids=cost_center_ids,
            work_location_ids=work_location_ids,
            employee_category_ids=employee_category_ids,
            work_modes=work_modes,
            joining_from=joining_date_from,
            joining_to=joining_date_to,
            reporting_manager_employee_id=reporting_manager_employee_id,
            employee_ids=employee_ids,
            include_archived=include_archived,
            sort_field=spec.field,
            direction=direction.value,
            after_value=after_value,
            after_id=after_id,
            limit=limit,
            with_total=with_total,
        )
        if context.is_reporting_scoped:
            rows = [row for row in rows if self._in_scope(context, row[0])]
        managers = self._employees.list_for_reference_check(
            [
                row[0].reporting_manager_employee_id
                for row in rows
                if row[0].reporting_manager_employee_id is not None
            ]
        )
        manager_map = {row.id: row for row in managers}
        items = []
        for employee, department_name, designation_name in rows:
            manager = (
                manager_map.get(employee.reporting_manager_employee_id)
                if employee.reporting_manager_employee_id
                else None
            )
            items.append(
                {
                    "id": employee.id,
                    "employee_code": employee.employee_code,
                    "display_name": employee.display_name,
                    "department_id": employee.department_id,
                    "department_name": department_name,
                    "designation_id": employee.designation_id,
                    "designation_name": designation_name,
                    "branch_id": employee.branch_id,
                    "status": employee.status,
                    "joining_date": employee.joining_date,
                    "official_email": employee.official_email,
                    "work_mode": employee.work_mode,
                    "reporting_manager_code": manager.employee_code if manager else None,
                    "reporting_manager_name": manager.display_name if manager else None,
                }
            )
        next_cursor = None
        if has_more and rows:
            last = rows[-1][0]
            sort_value = _sort_value(spec.field, last, rows[-1][1], rows[-1][2])
            next_cursor = self._cursor_codec.encode(
                sort_field=spec.field,
                sort_value=sort_value,
                last_id=last.id,
                direction=direction,
            )
        return items, next_cursor, total

    def get(self, context: OrgContext, employee_id: uuid.UUID) -> Employee:
        context.require(Permission.EMPLOYEE_VIEW)
        employee = self._employees.require(employee_id, resource="employee")
        self._assert_visible(context, employee)
        return employee

    def get_row(self, employee_id: uuid.UUID) -> Employee:
        """The current row, for internal callers that need its ``row_version``."""
        return self._employees.require(employee_id, resource="employee")

    def addresses(self, context: OrgContext, employee_id: uuid.UUID) -> dict[str, Any]:
        context.require(Permission.EMPLOYEE_VIEW)
        employee = self._employees.require(employee_id, resource="employee")
        self._assert_visible(context, employee)
        current = self._addresses.get_by_type(employee_id, "CURRENT")
        permanent = self._addresses.get_by_type(employee_id, "PERMANENT")
        if employee.permanent_address_same_as_current and current is not None:
            permanent = current
        return {
            "current": _address_response(current),
            "permanent": _address_response(permanent),
            "permanent_address_same_as_current": employee.permanent_address_same_as_current,
        }

    # ---------------------------------------------------------------- update

    def update(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        changes: dict[str, Any],
        addresses: dict[str, Any],
        if_match: int | None,
        acknowledged_warnings: list[str],
    ) -> tuple[Employee, list[str]]:
        context.require(Permission.EMPLOYEE_EDIT)
        employee = self._employees.require(employee_id, resource="employee")
        self._assert_visible(context, employee)
        assert_etag(employee.row_version, if_match)

        forbidden = sorted(set(changes) & JOB_FIELDS)
        if forbidden:
            raise FieldReadOnly(
                f"{', '.join(forbidden)} is managed through the job-changes endpoint."
            )

        settings = self._settings.require(context.organization_id)
        warnings = WarningCollector(acknowledged_warnings)
        self._validate_collected_fields(changes, settings)
        self._validate_contacts(changes, warnings)
        updated_email_taken = (
            bool(changes.get("official_email"))
            and settings.official_email_uniqueness_scope != "NONE"
            and self._email_taken(
                str(changes["official_email"]),
                context,
                settings.official_email_uniqueness_scope,
                exclude_employee_id=employee.id,
            )
        )
        if updated_email_taken:
            raise DuplicateResource("That official email is already in use by another employee.")
        if "date_of_birth" in changes or "joining_date" in changes:
            self._validate_dates_and_age(
                {
                    "date_of_birth": changes.get("date_of_birth", employee.date_of_birth),
                    "joining_date": changes.get("joining_date", employee.joining_date),
                },
                settings,
                warnings,
            )
        acknowledged = warnings.enforce()

        before = {field: getattr(employee, field) for field in changes}
        for field, value in changes.items():
            setattr(employee, field, value)
        employee.updated_by = context.user_id
        self._set_display_name(employee)
        self._update_addresses(context, employee, addresses)
        if (
            self._lifecycle is not None
            and "joining_date" in changes
            and before.get("joining_date") != employee.joining_date
        ):
            self._lifecycle.on_joining_date_changed(context, employee)  # 4.7 due dates

        self._audit.record_change(
            context,
            action=AuditAction.EMPLOYEE_UPDATED,
            entity_type="employee",
            entity_id=employee.id,
            employee_id=employee.id,
            before=before,
            after={field: getattr(employee, field) for field in changes},
            fields=list(changes),
            acknowledged_warnings=acknowledged,
        )
        return employee, acknowledged

    # ---------------------------------------------------------------- activation and status

    def activate(self, context: OrgContext, *, employee_id: uuid.UUID) -> Employee:
        context.require(Permission.EMPLOYEE_STATUS)
        employee = self._employees.lock(employee_id)
        if employee.status != EmployeeStatus.DRAFT.value:
            raise InvalidStatusTransition("Only a draft employee can be activated.")

        settings = self._settings.require(context.organization_id)
        details = [
            ErrorDetail(field, "REQUIRED", f"{field} is required before activation.")
            for field, _ in ACTIVATION_FIELDS
            if getattr(employee, field) is None
        ]
        if self._addresses.get_by_type(employee.id, "CURRENT") is None:
            details.append(
                ErrorDetail("current_address", "REQUIRED", "A current address is required.")
            )
        if employee.joining_date is None:
            details.append(ErrorDetail("joining_date", "REQUIRED", "joining_date is required."))
        if employee.employment_start_date != employee.joining_date:
            details.append(
                ErrorDetail(
                    "employment_start_date",
                    "MUST_EQUAL_JOINING_DATE",
                    "employment_start_date must equal joining_date.",
                )
            )
        employment_type = self._masters.get("employment-types", employee.employment_type_id)
        if (
            employment_type is not None
            and employment_type.requires_end_date
            and employee.employment_end_date is None
        ):
            details.append(
                ErrorDetail(
                    "employment_end_date",
                    "REQUIRED",
                    "This employment type requires an end date.",
                )
            )
        if details:
            raise ValidationFailed(details=details)

        self._assert_age_policy(employee, settings)
        self._assert_identifier_requirements(context, employee)
        if self._lifecycle is not None:
            self._lifecycle.assert_can_activate(context, employee)  # ONB-04 / 4.9

        if employee.employee_code is None:
            basis = employee.joining_date or today_in(None)
            code, _ = self._codes.issue_code(context, employee=employee, basis_date=basis)
            employee.employee_code = code

        employee.status = EmployeeStatus.ACTIVE.value
        employee.status_effective_date = employee.joining_date
        employee.updated_by = context.user_id
        self._status_history.add(
            EmployeeStatusHistory(
                id=uuid7(),
                employee_id=employee.id,
                from_status=EmployeeStatus.DRAFT.value,
                to_status=EmployeeStatus.ACTIVE.value,
                status_effective_date=employee.joining_date,
                employment_end_date_snapshot=employee.employment_end_date,
                status_reason=None,
                remarks=None,
                is_correction=False,
                created_by=context.user_id,
            )
        )
        self._audit.record_change(
            context,
            action=AuditAction.EMPLOYEE_ACTIVATED,
            entity_type="employee",
            entity_id=employee.id,
            employee_id=employee.id,
            before={"status": EmployeeStatus.DRAFT.value},
            after={"status": employee.status, "employee_code": employee.employee_code},
            fields=["status"],
        )
        if self._lifecycle is not None:
            self._lifecycle.after_activation(context, employee)  # auto-completion (4.9)
        return employee

    def transition_status(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        to_status: str,
        effective_date: date,
        employment_end_date: date | None,
        status_reason: str | None,
        remarks: str | None,
        is_correction: bool,
    ) -> Employee:
        employee = self._employees.lock(employee_id)
        self._assert_status_permission(context, employee.status, to_status, is_correction)
        self._assert_transition(
            employee,
            to_status=to_status,
            effective_date=effective_date,
            employment_end_date=employment_end_date,
            status_reason=status_reason,
            remarks=remarks,
            is_correction=is_correction,
            context=context,
        )
        before = {
            "status": employee.status,
            "employment_end_date": employee.employment_end_date,
        }
        from_status = employee.status
        employee.status = to_status
        employee.status_effective_date = effective_date
        employee.status_reason = status_reason
        if employment_end_date is not None:
            employee.employment_end_date = employment_end_date
        if (
            to_status == EmployeeStatus.ACTIVE.value
            and from_status == EmployeeStatus.ON_NOTICE.value
        ):
            employee.employment_end_date = None
        employee.updated_by = context.user_id
        if (
            self._lifecycle is not None
            and employee.employment_end_date is not None
            and employee.employment_end_date != before["employment_end_date"]
        ):
            self._lifecycle.on_employment_end(context, employee, employee.employment_end_date)
        if to_status in TERMINAL_STATUSES and self._lifecycle is not None:
            self._lifecycle.on_separated(context, employee)
        if to_status in TERMINAL_STATUSES:
            # JA §18.3: no assignment may stay active for a separated employee. The
            # exclusive bound is employment_end_date + 1 day, applied by the
            # assignment service, since `employees` stores the last working day.
            self._position_assignments.end_assignments_for_employee(
                context,
                employee=employee,
                last_working_day=employee.employment_end_date,
                reason=f"Employment ended: {to_status}",
            )
        if to_status == EmployeeStatus.ON_NOTICE.value or employment_end_date is not None:
            self._salary.apply_employment_end(context, employee.id, employee.employment_end_date)
        elif (
            from_status == EmployeeStatus.ON_NOTICE.value
            and to_status == EmployeeStatus.ACTIVE.value
        ):
            self._salary.apply_employment_end(context, employee.id, None)

        self._status_history.add(
            EmployeeStatusHistory(
                id=uuid7(),
                employee_id=employee.id,
                from_status=from_status,
                to_status=to_status,
                status_effective_date=effective_date,
                employment_end_date_snapshot=employee.employment_end_date,
                status_reason=status_reason,
                remarks=remarks,
                is_correction=is_correction,
                created_by=context.user_id,
            )
        )
        self._audit.record_change(
            context,
            action=(
                AuditAction.EMPLOYEE_STATUS_CORRECTED
                if is_correction
                else AuditAction.EMPLOYEE_STATUS_CHANGED
            ),
            entity_type="employee",
            entity_id=employee.id,
            employee_id=employee.id,
            before=before,
            after={
                "status": employee.status,
                "employment_end_date": employee.employment_end_date,
            },
            fields=["status", "status_effective_date", "employment_end_date"],
            reason=status_reason,
        )
        return employee

    def decide_age_exception(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        decision: str,
        reason: str,
    ) -> Employee:
        context.require(Permission.EMPLOYEE_STATUS_RESTRICTED)
        employee = self._employees.lock(employee_id)
        if employee.age_exception_status != "PENDING":
            raise ValidationFailed("This employee has no pending age exception.")
        if employee.created_by == context.user_id:
            raise ValidationFailed("The requester cannot decide their own age exception.")
        if decision not in {"APPROVED", "REJECTED"}:
            raise ValidationFailed("Decision must be APPROVED or REJECTED.")
        before = employee.age_exception_status
        employee.age_exception_status = decision
        employee.age_exception_reason = reason
        employee.age_exception_decided_by = context.user_id
        employee.age_exception_decided_at = utc_now()
        employee.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.AGE_EXCEPTION_DECIDED,
            entity_type="employee",
            entity_id=employee.id,
            employee_id=employee.id,
            before={"age_exception_status": before},
            after={"age_exception_status": decision},
            fields=["age_exception_status"],
            reason=reason,
        )
        return employee

    # ---------------------------------------------------------------- job changes

    def change_job(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        changes: dict[str, Any],
    ) -> tuple[Employee, EmployeeJobHistory, Any]:
        """Record an effective-dated job change (§10.8), optionally moving position.

        The third element of the result is the new position assignment, or ``None``.
        A payload without ``position_change`` behaves exactly as it did before the
        job-architecture module existed and returns ``None`` there (JA §7.7 rule 5).

        Lock order is the employee row first, then the position (JA §8.9): the
        employee is locked here, and the assignment service takes the position lock.
        """
        context.require(Permission.EMPLOYEE_EDIT)
        employee = self._employees.lock(employee_id)
        position_change = changes.pop("position_change", None)
        acknowledged_warnings = changes.pop("acknowledged_warnings", None) or []
        warnings = WarningCollector(acknowledged_warnings)
        effective_date = changes["effective_date"]
        latest = self._job_history.latest(employee.id)
        self._assert_job_change_date(
            context, employee, effective_date, changes["change_type"], latest
        )
        self._validate_references(changes, context)

        assignment = None
        if position_change is not None:
            assignment = self._position_assignments.apply_position_change(
                context,
                employee=employee,
                change=position_change,
                effective_date=effective_date,
                job_change_values=changes,
                warnings=warnings,
            )

        manager_id = changes.get("reporting_manager_employee_id")
        if manager_id is not None:
            if manager_id == employee.id:
                raise ReportingCycle("An employee cannot report to themselves.")
            self._assert_no_reporting_cycle(employee.id, manager_id)

        # §21.3: any warning the position change raised must be acknowledged before
        # anything is written, so an unacknowledged one aborts the whole change.
        acknowledged = warnings.enforce()

        applied = {
            key: value
            for key, value in changes.items()
            if key
            in {
                "department_id",
                "sub_department_id",
                "designation_id",
                "grade_id",
                "business_unit_id",
                "cost_center_id",
                "branch_id",
                "work_location_id",
                "employment_type_id",
                "employee_category_id",
                "work_mode",
                "reporting_manager_employee_id",
                "hr_contact_employee_id",
            }
            and value is not None
        }
        before = {field: getattr(employee, field) for field in applied}
        for field, value in applied.items():
            setattr(employee, field, value)
        employee.updated_by = context.user_id
        self._employees.flush()
        if (
            self._lifecycle is not None
            and "reporting_manager_employee_id" in applied
            and before["reporting_manager_employee_id"] != employee.reporting_manager_employee_id
        ):
            self._lifecycle.on_manager_changed(context, employee)  # INT-36

        same_day = [
            row
            for row in self._job_history.list_for_employee(employee.id)
            if row.effective_date == effective_date
        ]
        history = EmployeeJobHistory(
            id=uuid7(),
            employee_id=employee.id,
            effective_date=effective_date,
            sequence_no=len(same_day) + 1,
            change_type=changes["change_type"],
            department_id=employee.department_id,
            sub_department_id=employee.sub_department_id,
            designation_id=employee.designation_id,
            grade_id=employee.grade_id,
            business_unit_id=employee.business_unit_id,
            cost_center_id=employee.cost_center_id,
            branch_id=employee.branch_id,
            work_location_id=employee.work_location_id,
            employment_type_id=employee.employment_type_id,
            employee_category_id=employee.employee_category_id,
            work_mode=employee.work_mode,
            reporting_manager_employee_id=employee.reporting_manager_employee_id,
            hr_contact_employee_id=employee.hr_contact_employee_id,
            reason=changes.get("reason"),
            position_id=self._position_assignments.current_position_id(employee.id),
            created_by=context.user_id,
        )
        self._job_history.add(history)
        self._audit.record_change(
            context,
            action=AuditAction.EMPLOYEE_JOB_CHANGED,
            entity_type="employee",
            entity_id=employee.id,
            employee_id=employee.id,
            before=before,
            after={field: getattr(employee, field) for field in applied},
            fields=list(applied),
            reason=changes.get("reason"),
            acknowledged_warnings=acknowledged,
        )
        return employee, history, assignment

    def job_history(
        self, context: OrgContext, employee_id: uuid.UUID
    ) -> Sequence[EmployeeJobHistory]:
        context.require(Permission.EMPLOYEE_VIEW)
        employee = self._employees.require(employee_id, resource="employee")
        self._assert_visible(context, employee)
        return self._job_history.list_for_employee(employee_id)

    def status_history(
        self, context: OrgContext, employee_id: uuid.UUID
    ) -> Sequence[EmployeeStatusHistory]:
        context.require(Permission.EMPLOYEE_VIEW)
        employee = self._employees.require(employee_id, resource="employee")
        self._assert_visible(context, employee)
        return self._status_history.list_for_employee(employee_id)

    # ---------------------------------------------------------------- archive

    def archive(self, context: OrgContext, *, employee_id: uuid.UUID, reason: str) -> Employee:
        context.require(Permission.EMPLOYEE_ARCHIVE)
        employee = self._employees.lock(employee_id)
        if employee.status != EmployeeStatus.DRAFT.value and len(reason) < 20:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "reason",
                        "MIN_LENGTH",
                        "Archiving a non-draft employee requires a reason of at least 20 "
                        "characters.",
                    )
                ]
            )
        employee.archived_at = utc_now()
        employee.archived_by = context.user_id
        employee.archive_reason = reason
        employee.updated_by = context.user_id
        if self._lifecycle is not None:
            self._lifecycle.on_archived(context, employee)
        for entry in self._registry.list_for_employee(employee.id):
            if entry.status == "ASSIGNED":
                self._registry.retire(entry, reason=reason, user_id=context.user_id)
        self._audit.record_change(
            context,
            action=AuditAction.EMPLOYEE_ARCHIVED,
            entity_type="employee",
            entity_id=employee.id,
            employee_id=employee.id,
            after={"archived": True, "reason": reason},
            fields=["archived_at"],
            reason=reason,
        )
        return employee

    # ---------------------------------------------------------------- validation

    def _organization(self, context: OrgContext):  # type: ignore[no-untyped-def]
        organization = self._organizations.get(context.organization_id)
        if organization is None or organization.archived_at is not None:
            raise ResourceNotFound("organization")
        return organization

    def _validate_collected_fields(self, values: dict[str, Any], settings: Any) -> None:
        violations: list[ErrorDetail] = []
        if not settings.collect_religion and values.get("religion"):
            violations.append(
                ErrorDetail("religion", "FIELD_NOT_COLLECTED", "Religion is not collected.")
            )
        if not settings.collect_social_category and values.get("social_category"):
            violations.append(
                ErrorDetail(
                    "social_category", "FIELD_NOT_COLLECTED", "Social category is not collected."
                )
            )
        if not settings.collect_disability_info and any(
            values.get(field) is not None
            for field in ("disability_status", "disability_type", "disability_percentage")
        ):
            violations.append(
                ErrorDetail(
                    "disability_status", "FIELD_NOT_COLLECTED", "Disability data is not collected."
                )
            )
        if violations:
            raise FieldNotCollected(details=violations)

    def _validate_references(self, values: dict[str, Any], context: OrgContext) -> None:
        self._masters.require_active("departments", values.get("department_id"))
        self._masters.require_active("sub-departments", values.get("sub_department_id"))
        self._masters.require_active("designations", values.get("designation_id"))
        self._masters.require_active("grades", values.get("grade_id"))
        self._masters.require_active("business-units", values.get("business_unit_id"))
        self._masters.require_active("cost-centers", values.get("cost_center_id"))
        self._masters.require_active("employment-types", values.get("employment_type_id"))
        work_location = self._masters.require_active(
            "work-locations", values.get("work_location_id")
        )
        self._masters.require_active("employee-categories", values.get("employee_category_id"))
        branch_id = values.get("branch_id")
        if branch_id is not None:
            branch = self._branches.get(branch_id)
            if branch is None:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "branch_id",
                            "INVALID_ORGANIZATION_REFERENCE",
                            "That branch does not belong to this organization.",
                        )
                    ]
                )
            if branch.status != "ACTIVE":
                raise ValidationFailed(
                    details=[
                        ErrorDetail("branch_id", "MASTER_INACTIVE", "That branch is inactive.")
                    ]
                )
        if work_location is not None and work_location.branch_id is not None:
            pass

        for field in (
            "reporting_manager_employee_id",
            "hr_contact_employee_id",
            "referred_by_employee_id",
        ):
            other = values.get(field)
            if other is not None and self._employees.get(other) is None:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            field,
                            "INVALID_ORGANIZATION_REFERENCE",
                            "That employee does not belong to this organization.",
                        )
                    ]
                )

    def _validate_dates_and_age(
        self, values: dict[str, Any], settings: Any, warnings: WarningCollector
    ) -> None:
        joining = values.get("joining_date")
        dob = values.get("date_of_birth")
        today = today_in(None)
        if joining is not None and not (date(1950, 1, 1) <= joining <= today + timedelta(days=365)):
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "joining_date",
                        "OUT_OF_RANGE",
                        "joining_date must be between 1950-01-01 and one year from today.",
                    )
                ]
            )
        if dob is not None and (dob > today or dob < date(1900, 1, 1)):
            raise ValidationFailed(
                details=[ErrorDetail("date_of_birth", "OUT_OF_RANGE", "Invalid date of birth.")]
            )
        employment_end = values.get("employment_end_date")
        if employment_end is not None and joining is not None and employment_end < joining:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "employment_end_date",
                        "INVALID_INTERVAL",
                        "employment_end_date must not precede joining_date.",
                    )
                ]
            )
        if dob is None:
            return
        reference = joining or today
        age = age_on(dob, reference)
        if age < settings.absolute_minimum_age_years:
            raise AgeBelowAbsoluteMinimum()
        if age > 100:
            warnings.add(WarningCode.AGE_HIGH, age=age)
        age_exception = None
        if age < settings.minimum_employment_age_years:
            age_exception = "PENDING"
        values["age_exception_status"] = age_exception or "NONE"

    def _validate_transition_link(self, values: dict[str, Any], context: OrgContext) -> None:
        transition = values.get("employment_transition_type") or "NEW_HIRE"
        previous_id = values.get("previous_employment_record_id")
        if transition not in {"REHIRE", "INTER_ORG_TRANSFER"}:
            return
        if previous_id is None:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "previous_employment_record_id",
                        "REQUIRED",
                        "A linked record is required for a rehire or transfer.",
                    )
                ]
            )
        previous = self._employees.get_any_org(previous_id)
        if previous is None:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "previous_employment_record_id",
                        "NOT_FOUND",
                        "The linked record does not exist.",
                    )
                ]
            )
        if previous.employment_end_date is None:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "previous_employment_record_id",
                        "NOT_SEPARATED",
                        "The linked record has no employment end date.",
                    )
                ]
            )

    def _validate_contacts(self, values: dict[str, Any], warnings: WarningCollector) -> None:
        mobile = values.get("personal_mobile")
        alternate = values.get("alternate_mobile")
        if mobile and alternate and mobile == alternate:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "alternate_mobile",
                        "MUST_DIFFER",
                        "alternate_mobile must differ from personal_mobile.",
                    )
                ]
            )
        emergency = [
            values.get("emergency_contact_name"),
            values.get("emergency_contact_relationship"),
            values.get("emergency_contact_phone"),
        ]
        if any(emergency) and not all(emergency):
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "emergency_contact_name",
                        "INCOMPLETE",
                        "Name, relationship and phone are all required for an emergency contact.",
                    )
                ]
            )
        if (
            mobile
            and values.get("emergency_contact_phone")
            and mobile == values["emergency_contact_phone"]
        ):
            warnings.add(WarningCode.EMERGENCY_CONTACT_SAME)

    def _email_taken(
        self,
        email: str,
        context: OrgContext,
        scope: str,
        *,
        exclude_employee_id: uuid.UUID | None = None,
    ) -> bool:
        organization_ids = [context.organization_id]
        if scope == "GROUP":
            organization = self._organizations.get(context.organization_id)
            if organization is not None and organization.group_id is not None:
                organization_ids = list(
                    self._organizations.organizations_in_group(organization.group_id)
                )
        taken = self._employees.email_exists(
            normalize_email(email) or email, organization_ids=organization_ids
        )
        if taken and exclude_employee_id is not None:
            # The unique key excludes the row being edited only if it is the same employee.
            other = self._employees.search(query=email)
            return any(row.id != exclude_employee_id for row in other)
        return taken

    def _write_addresses(
        self, context: OrgContext, employee: Employee, values: dict[str, Any]
    ) -> None:
        current = values.get("current_address")
        permanent = values.get("permanent_address")
        same = bool(values.get("permanent_address_same_as_current"))
        if current:
            record = self._address_record(employee, "CURRENT", current)
            self._addresses.add(record)
        elif same:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "current_address",
                        "REQUIRED",
                        "A current address is required when it is copied to permanent.",
                    )
                ]
            )
        if permanent and not same:
            self._addresses.add(self._address_record(employee, "PERMANENT", permanent))
        employee.permanent_address_same_as_current = same
        self._addresses.flush()

    def _update_addresses(
        self, context: OrgContext, employee: Employee, addresses: dict[str, Any]
    ) -> None:
        current = addresses.get("current_address")
        permanent = addresses.get("permanent_address")
        if current is not None:
            self._replace_address(employee, "CURRENT", current)
        same = addresses.get("permanent_address_same_as_current")
        if same is True:
            self._addresses.delete_for_employee(employee.id, "PERMANENT")
            employee.permanent_address_same_as_current = True
        elif same is False:
            if permanent is None and self._addresses.get_by_type(employee.id, "PERMANENT") is None:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "permanent_address",
                            "REQUIRED",
                            "A permanent address is required when it is not the current one.",
                        )
                    ]
                )
            employee.permanent_address_same_as_current = False
        if permanent is not None and not employee.permanent_address_same_as_current:
            self._replace_address(employee, "PERMANENT", permanent)
        self._addresses.flush()

    def _replace_address(
        self, employee: Employee, address_type: str, values: dict[str, Any]
    ) -> None:
        existing = self._addresses.get_by_type(employee.id, address_type)
        validated = self._validated_address(values)
        if existing is None:
            self._addresses.add(self._address_record(employee, address_type, validated))
            return
        for field, value in validated.items():
            setattr(existing, field, value)
        existing.updated_by = employee.updated_by

    def _address_record(
        self, employee: Employee, address_type: str, values: dict[str, Any]
    ) -> EmployeeAddress:
        validated = self._validated_address(values)
        return EmployeeAddress(
            id=uuid7(),
            employee_id=employee.id,
            address_type=address_type,
            created_by=employee.created_by,
            **validated,
        )

    def _validated_address(self, values: dict[str, Any]) -> dict[str, Any]:
        country = str(values.get("country") or "IN")
        postal = normalize_postal_code(values.get("postal_code"))
        postcode_check = check(RuleId.POSTAL, postal, country=country)
        if not postcode_check.ok:
            raise ValidationFailed(
                details=[ErrorDetail("postal_code", RuleId.POSTAL.value, postcode_check.message)]
            )
        state_code = values.get("state_code")
        if country == "IN":
            state = self._states.get_by_name(str(values.get("state") or ""))
            if state is None:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "state", "UNKNOWN_STATE", "Unknown Indian state or union territory."
                        )
                    ]
                )
            if state_code and str(state_code) != state.code:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "state_code",
                            "MISMATCH",
                            f"The state code for {state.name} is {state.code}.",
                        )
                    ]
                )
            state_code = state.code
        return {
            "line_1": str(values["line_1"]),
            "line_2": values.get("line_2"),
            "city": str(values["city"]),
            "district": values.get("district"),
            "state": str(values["state"]),
            "state_code": state_code,
            "country": country,
            "postal_code": postal,
        }

    def _assert_age_policy(self, employee: Employee, settings: Any) -> None:
        if employee.date_of_birth is None or employee.joining_date is None:
            return
        age = age_on(employee.date_of_birth, employee.joining_date)
        if age < settings.absolute_minimum_age_years:
            raise AgeBelowAbsoluteMinimum()
        if (
            age < settings.minimum_employment_age_years
            and employee.age_exception_status != "APPROVED"
        ):
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "age_exception_status",
                        "APPROVAL_REQUIRED",
                        "Activation requires an approved age exception.",
                    )
                ]
            )

    def _assert_identifier_requirements(self, context: OrgContext, employee: Employee) -> None:
        required = [
            row
            for row in self._identifiers.list_policies()
            if row.requirement == IdentifierRequirement.REQUIRED_FOR_ACTIVATION.value
        ]
        if not required:
            return
        recorded = {
            row.identifier_type
            for row in self._identifier_values.list_for_employee(employee.id, current_only=True)
        }
        missing = [row.identifier_type for row in required if row.identifier_type not in recorded]
        if missing:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "identifiers",
                        "REQUIRED",
                        f"Record the required identifiers before activation: {', '.join(missing)}.",
                    )
                ]
            )

    def _assert_status_permission(
        self, context: OrgContext, from_status: str, to_status: str, is_correction: bool
    ) -> None:
        separated = from_status in TERMINAL_STATUSES or _is_separated_custom(
            from_status, self._custom_statuses.list_statuses()
        )
        if separated:
            context.require(Permission.EMPLOYEE_STATUS_RESTRICTED)
            if not is_correction:
                raise InvalidStatusTransition(
                    "A separated status can only be changed as a correction."
                )
            return
        if is_correction:
            context.require(Permission.EMPLOYEE_STATUS_RESTRICTED)
            return
        if to_status in {EmployeeStatus.TERMINATED.value, EmployeeStatus.ABSCONDING.value}:
            context.require(Permission.EMPLOYEE_STATUS_RESTRICTED)
            return
        context.require(Permission.EMPLOYEE_STATUS)

    def _assert_transition(
        self,
        employee: Employee,
        *,
        to_status: str,
        effective_date: date,
        employment_end_date: date | None,
        status_reason: str | None,
        remarks: str | None,
        is_correction: bool,
        context: OrgContext,
    ) -> None:
        today = today_in(None)
        custom_statuses = self._custom_statuses.list_statuses()
        custom_target = next((row for row in custom_statuses if row.code == to_status), None)
        allowed = _ALLOWED_TRANSITIONS.get(employee.status, frozenset())
        if custom_target is not None and employee.status not in TERMINAL_STATUSES:
            allowed = allowed | {to_status}
        separated_custom = _is_separated_custom(employee.status, custom_statuses)
        if to_status not in allowed and not (
            is_correction and (employee.status in TERMINAL_STATUSES or separated_custom)
        ):
            raise InvalidStatusTransition(f"{employee.status} cannot transition to {to_status}.")
        if employee.joining_date is not None and effective_date < employee.joining_date:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "status_effective_date",
                        "BEFORE_JOINING",
                        "status_effective_date must not precede joining_date.",
                    )
                ]
            )
        if to_status == EmployeeStatus.ON_NOTICE.value and (
            employment_end_date is None or status_reason is None
        ):
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "employment_end_date",
                        "REQUIRED",
                        "ON_NOTICE requires an end date and a status reason.",
                    )
                ]
            )
        if to_status in TERMINAL_STATUSES or _is_separated_custom(
            to_status, self._custom_statuses.list_statuses()
        ):
            if employment_end_date is None or employment_end_date > today:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "employment_end_date",
                            "FUTURE_OR_MISSING",
                            "A separated status requires an end date no later than today.",
                        )
                    ]
                )
            if status_reason is None:
                raise ValidationFailed(
                    details=[
                        ErrorDetail("status_reason", "REQUIRED", "A status reason is required.")
                    ]
                )
        if to_status == EmployeeStatus.INACTIVE.value and status_reason is None:
            raise ValidationFailed(
                details=[ErrorDetail("status_reason", "REQUIRED", "A status reason is required.")]
            )
        if is_correction:
            if remarks is None or len(remarks) < 20:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "remarks",
                            "MIN_LENGTH",
                            "A correction requires remarks of at least 20 characters.",
                        )
                    ]
                )
            latest = self._status_history.latest(employee.id)
            settings = self._settings.require(context.organization_id)
            if latest is None:
                raise StatusCorrectionWindowExpired()
            changed_on = latest.created_at.date()
            if (today - changed_on).days > settings.status_correction_window_days:
                raise StatusCorrectionWindowExpired()

    def _assert_job_change_date(
        self,
        context: OrgContext,
        employee: Employee,
        effective_date: date,
        change_type: str,
        latest: EmployeeJobHistory | None,
    ) -> None:
        today = today_in(None)
        invalid = effective_date > today
        if employee.joining_date is not None and effective_date < employee.joining_date:
            invalid = True
        if (
            latest is not None
            and effective_date < latest.effective_date
            and not (
                change_type == "CORRECTION" and context.has(Permission.EMPLOYEE_STATUS_RESTRICTED)
            )
        ):
            invalid = True
        if invalid:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "effective_date",
                        "JOB_CHANGE_DATE_INVALID",
                        "The effective date is outside the permitted range.",
                    )
                ]
            )

    def _assert_no_reporting_cycle(
        self, employee_id: uuid.UUID, proposed_manager_id: uuid.UUID
    ) -> None:
        seen: set[uuid.UUID] = set()
        current_id: uuid.UUID | None = proposed_manager_id
        for _ in range(50):
            if current_id is None:
                return
            if current_id == employee_id:
                raise ReportingCycle()
            if current_id in seen:
                raise ReportingCycle()
            seen.add(current_id)
            row = self._employees.get_for_update(current_id)
            if row is None:
                return
            current_id = row.reporting_manager_employee_id
        raise ReportingCycle("The reporting chain exceeds the maximum depth of 50.")

    def _set_display_name(self, employee: Employee) -> None:
        parts = [employee.first_name, employee.middle_name, employee.last_name]
        employee.full_name_normalized = normalize_search_name(*parts)
        if not employee.display_name:
            employee.display_name = " ".join(part for part in parts if part)

    def _assert_visible(self, context: OrgContext, employee: Employee) -> None:
        if context.is_reporting_scoped and not self._in_scope(context, employee):
            raise ResourceNotFound("employee")

    def _in_scope(self, context: OrgContext, employee: Employee) -> bool:
        if context.is_self(employee.id):
            return True
        if context.employee_id is None:
            return False
        cursor: Employee | None = employee
        for _ in range(context.reporting_scope_depth):
            if cursor is None:
                return False
            manager_id = cursor.reporting_manager_employee_id
            if manager_id is None:
                return False
            if manager_id == context.employee_id:
                return True
            cursor = self._employees.get(manager_id)
        return False


def _sort_value(
    field: str, employee: Employee, department: str | None, designation: str | None
) -> Any:
    return {
        "employee_code": employee.employee_code_normalized,
        "display_name": employee.display_name,
        "joining_date": employee.joining_date,
        "department_name": department,
        "designation_name": designation,
        "status": employee.status,
        "created_at": employee.created_at,
    }.get(field, employee.display_name)


_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    EmployeeStatus.DRAFT.value: frozenset(),
    EmployeeStatus.ACTIVE.value: frozenset(
        {
            EmployeeStatus.ON_NOTICE.value,
            EmployeeStatus.INACTIVE.value,
            EmployeeStatus.RESIGNED.value,
            EmployeeStatus.TERMINATED.value,
            EmployeeStatus.RETIRED.value,
            EmployeeStatus.ABSCONDING.value,
        }
    ),
    EmployeeStatus.ON_NOTICE.value: frozenset(
        {
            EmployeeStatus.ACTIVE.value,
            EmployeeStatus.RESIGNED.value,
            EmployeeStatus.TERMINATED.value,
            EmployeeStatus.RETIRED.value,
            EmployeeStatus.ABSCONDING.value,
        }
    ),
    EmployeeStatus.INACTIVE.value: frozenset(
        {
            EmployeeStatus.ACTIVE.value,
            EmployeeStatus.ON_NOTICE.value,
            EmployeeStatus.RESIGNED.value,
            EmployeeStatus.TERMINATED.value,
            EmployeeStatus.RETIRED.value,
            EmployeeStatus.ABSCONDING.value,
        }
    ),
    EmployeeStatus.ABSCONDING.value: frozenset(
        {
            EmployeeStatus.ACTIVE.value,
            EmployeeStatus.RESIGNED.value,
            EmployeeStatus.TERMINATED.value,
        }
    ),
}
for _terminal in TERMINAL_STATUSES:
    _ALLOWED_TRANSITIONS[_terminal] = frozenset()


def _is_separated_custom(status: str, statuses: Sequence[EmployeeCustomStatus]) -> bool:
    return any(row.code == status and row.base_behaviour == "SEPARATED" for row in statuses)


def _address_response(address: EmployeeAddress | None) -> Any:
    if address is None:
        return None
    return AddressResponse(
        line_1=address.line_1,
        line_2=address.line_2,
        city=address.city,
        district=address.district,
        state=address.state,
        state_code=address.state_code,
        country=address.country,
        postal_code=address.postal_code,
    )


__all__ = ["EmployeeService"]
