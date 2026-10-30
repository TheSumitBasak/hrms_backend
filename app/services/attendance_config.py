"""Shifts, attendance policies and versions, shift assignments (addendum 5.3-5.5)."""

from __future__ import annotations

import uuid
from datetime import date, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in, utc_now
from app.core.context import OrgContext
from app.core.errors import (
    AttendancePolicyConflict,
    AttendancePolicyDraftExists,
    DuplicateResource,
    InvalidStatusTransition,
    ModuleNotEnabled,
    NoApplicableAttendancePolicy,
    PolicyVersionNotFuture,
    ResourceNotFound,
    ShiftAssignmentDateInvalid,
    ShiftAssignmentOverlap,
    ShiftInUseImmutable,
    ShiftWindowInvalid,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.core.warnings import WarningCode, WarningCollector
from app.db.types import uuid7
from app.models.attendance import (
    AttendancePolicy,
    AttendancePolicyVersion,
    EmployeeShiftAssignment,
    Shift,
)
from app.repositories.attendance import (
    AttendancePolicyRepository,
    AttendancePolicyVersionRepository,
    AttendanceRecordRepository,
    ShiftAssignmentRepository,
    ShiftRepository,
)
from app.repositories.employee import EmployeeRepository
from app.repositories.organization import (
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.services.organizations import assert_etag
from app.services.schedule import ScheduleService
from app.services.schedule_resolver import Schedule
from app.services.versioning import intervals_overlap

_APPLIES = (
    "applies_branch_id",
    "applies_department_id",
    "applies_work_location_id",
    "applies_employment_type_id",
    "applies_employee_category_id",
)
VERSION_FIELDS = frozenset(
    {
        "effective_from",
        "priority",
        *_APPLIES,
        "timezone",
        "default_shift_id",
        "default_weekly_off_pattern_id",
        "punch_evaluation_mode",
        "break_deduction_mode",
        "late_grace_minutes",
        "early_departure_grace_minutes",
        "flexible_full_day_min_work_minutes",
        "flexible_half_day_min_work_minutes",
        "flexible_day_start_time",
        "self_punch_enabled",
        "allow_remote_self_punch",
        "manual_punch_enabled",
        "integration_punch_enabled",
        "duplicate_punch_window_seconds",
        "regularization_enabled",
        "regularization_backdate_limit_days",
        "on_duty_requests_enabled",
        "regularization_approval_chain",
        "auto_finalize_after_days",
        "incomplete_on_finalize",
        "change_note",
    }
)
SHIFT_TIMING_FIELDS = frozenset(
    {
        "start_time",
        "end_time",
        "break_minutes",
        "full_day_min_work_minutes",
        "half_day_min_work_minutes",
        "window_before_minutes",
        "window_after_minutes",
    }
)
BACKDATE_REASON_MIN = 10


def _minutes(clock: time) -> int:
    return clock.hour * 60 + clock.minute


def shift_duration(start: time, end: time) -> int:
    return (_minutes(end) - _minutes(start)) % 1440


class AttendanceConfigService:
    def __init__(
        self,
        *,
        shifts: ShiftRepository,
        policies: AttendancePolicyRepository,
        versions: AttendancePolicyVersionRepository,
        assignments: ShiftAssignmentRepository,
        records: AttendanceRecordRepository,
        employees: EmployeeRepository,
        schedule: ScheduleService,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        audit: AuditWriter,
    ) -> None:
        self._shifts = shifts
        self._policies = policies
        self._versions = versions
        self._assignments = assignments
        self._records = records
        self._employees = employees
        self._schedule = schedule
        self._organizations = organizations
        self._settings = settings
        self._audit = audit

    # ------------------------------------------------------------------ guards

    def _today(self, context: OrgContext) -> date:
        organization = self._organizations.get(context.organization_id)
        return today_in(organization.timezone if organization else None)

    def _require_module(self, context: OrgContext) -> None:
        if not self._settings.require(context.organization_id).enable_attendance_module:
            raise ModuleNotEnabled()

    def _manage(self, context: OrgContext) -> None:
        context.require(Permission.ATTENDANCE_POLICY_MANAGE)
        self._require_module(context)

    # ------------------------------------------------------------------ shifts

    def list_shifts(self, context: OrgContext, *, status: str | None) -> list[Shift]:
        context.require(Permission.ATTENDANCE_VIEW)
        return list(self._shifts.list_shifts(status=status))

    def get_shift(self, context: OrgContext, shift_id: uuid.UUID) -> Shift:
        context.require(Permission.ATTENDANCE_VIEW)
        return self._shifts.require(shift_id, resource="shift")

    @staticmethod
    def _assert_shift_rules(values: dict[str, Any]) -> None:
        start, end = values["start_time"], values["end_time"]
        if start == end:
            raise ValidationFailed("end_time must differ from start_time.")
        duration = shift_duration(start, end)
        before, after = values["window_before_minutes"], values["window_after_minutes"]
        if duration + before + after > 1440:
            raise ShiftWindowInvalid(  # ATT-03: consecutive windows must never overlap
                "duration + window_before + window_after must not exceed 1,440 minutes."
            )
        if not 0 <= values["break_minutes"] < duration:
            raise ValidationFailed("break_minutes must be below the shift duration.")
        full, half = values["full_day_min_work_minutes"], values["half_day_min_work_minutes"]
        if not 1 <= half < full <= duration:
            raise ValidationFailed("Require 1 <= half_day < full_day <= shift duration.")

    def create_shift(self, context: OrgContext, *, values: dict[str, Any]) -> Shift:
        self._manage(context)
        if self._shifts.code_exists(values["shift_code"]):
            raise DuplicateResource("A shift with that code already exists.")
        self._assert_shift_rules(values)
        row = Shift(
            id=uuid7(),
            organization_id=context.organization_id,
            created_by=context.user_id,
            **values,
        )
        self._shifts.add(row)
        self._shifts.flush()
        self._audit.record_change(
            context,
            action=AuditAction.SHIFT_CREATED,
            entity_type="shift",
            entity_id=row.id,
            after={"shift_code": row.shift_code, "shift_name": row.shift_name},
        )
        return row

    def update_shift(
        self,
        context: OrgContext,
        *,
        shift_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> Shift:
        self._manage(context)
        row = self._shifts.require(shift_id, resource="shift")
        assert_etag(row.row_version, if_match)
        timing = {
            f: v for f, v in changes.items() if f in SHIFT_TIMING_FIELDS and getattr(row, f) != v
        }
        if timing and self._shifts.is_referenced_by_record(row.id):
            raise ShiftInUseImmutable()  # ATT-04
        merged = {f: getattr(row, f) for f in SHIFT_TIMING_FIELDS} | changes
        self._assert_shift_rules(merged)
        before = {f: getattr(row, f) for f in changes}
        for field, value in changes.items():
            setattr(row, field, value)
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.SHIFT_UPDATED,
            entity_type="shift",
            entity_id=row.id,
            before=before,
            after=dict(changes),
        )
        return row

    # ------------------------------------------------------------------ policies

    def list_policies(
        self, context: OrgContext, *, status: str | None
    ) -> tuple[list[AttendancePolicy], dict[uuid.UUID, list[AttendancePolicyVersion]]]:
        context.require(Permission.ATTENDANCE_VIEW)
        rows = list(self._policies.list_policies(status=status))
        return rows, self._versions.for_policies([r.id for r in rows])

    def get_policy(
        self, context: OrgContext, policy_id: uuid.UUID
    ) -> tuple[AttendancePolicy, list[AttendancePolicyVersion]]:
        context.require(Permission.ATTENDANCE_VIEW)
        policy = self._policies.require(policy_id, resource="attendance policy")
        return policy, self._versions.for_policies([policy.id])[policy.id]

    def create_policy(
        self, context: OrgContext, *, values: dict[str, Any], version: dict[str, Any]
    ) -> tuple[AttendancePolicy, list[AttendancePolicyVersion]]:
        self._manage(context)
        if self._policies.code_exists(values["policy_code"]):
            raise DuplicateResource("An attendance policy with that code already exists.")
        policy = AttendancePolicy(
            id=uuid7(),
            organization_id=context.organization_id,
            created_by=context.user_id,
            **values,
        )
        self._policies.add(policy)
        self._policies.flush()
        draft = self._new_version(context, policy, number=1, fields=version)
        self._audit.record_change(
            context,
            action=AuditAction.ATTENDANCE_POLICY_CREATED,
            entity_type="attendance_policy",
            entity_id=policy.id,
            after={"policy_code": policy.policy_code},
        )
        return policy, [draft]

    def update_policy(
        self,
        context: OrgContext,
        *,
        policy_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> AttendancePolicy:
        self._manage(context)
        policy = self._policies.require(policy_id, resource="attendance policy")
        assert_etag(policy.row_version, if_match)
        before = {f: getattr(policy, f) for f in changes}
        for field, value in changes.items():
            setattr(policy, field, value)
        policy.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.ATTENDANCE_POLICY_UPDATED,
            entity_type="attendance_policy",
            entity_id=policy.id,
            before=before,
            after=dict(changes),
        )
        return policy

    @staticmethod
    def _assert_version_fields(fields: dict[str, Any]) -> None:
        unknown = set(fields) - VERSION_FIELDS
        if unknown:
            raise ValidationFailed(f"Unknown policy fields: {', '.join(sorted(unknown))}.")
        zone = fields.get("timezone")
        if zone is not None:
            try:
                ZoneInfo(zone)
            except (ZoneInfoNotFoundError, ValueError) as error:
                raise ValidationFailed("timezone must be an IANA time zone name.").with_detail(
                    field="timezone", code="INVALID", message="Unknown time zone."
                ) from error
        if (
            fields.get("break_deduction_mode") == "FIXED_FROM_SHIFT"
            and fields.get("punch_evaluation_mode", "FIRST_IN_LAST_OUT") != "FIRST_IN_LAST_OUT"
        ):
            raise ValidationFailed("FIXED_FROM_SHIFT needs the FIRST_IN_LAST_OUT mode.")

    def _new_version(
        self, context: OrgContext, policy: AttendancePolicy, *, number: int, fields: dict[str, Any]
    ) -> AttendancePolicyVersion:
        organization = self._organizations.get(context.organization_id)
        fields = {"timezone": organization.timezone if organization else "UTC", **fields}
        self._assert_version_fields(fields)
        if "default_weekly_off_pattern_id" not in fields:
            raise ValidationFailed("default_weekly_off_pattern_id is required.").with_detail(
                field="default_weekly_off_pattern_id", code="REQUIRED", message="Choose a pattern."
            )
        version = AttendancePolicyVersion(
            id=uuid7(),
            organization_id=context.organization_id,
            policy_id=policy.id,
            version_number=number,
            status="DRAFT",
            created_by=context.user_id,
            **fields,
        )
        self._versions.add(version)
        self._versions.flush()
        return version

    def create_version(
        self, context: OrgContext, *, policy_id: uuid.UUID, fields: dict[str, Any]
    ) -> AttendancePolicyVersion:
        self._manage(context)
        policy = self._policies.lock(policy_id)
        existing = self._versions.for_policies([policy.id])[policy.id]
        if any(v.status == "DRAFT" for v in existing):
            raise AttendancePolicyDraftExists()
        latest = max(existing, key=lambda v: v.version_number)
        carried = {f: getattr(latest, f) for f in VERSION_FIELDS if f != "effective_from"}
        return self._new_version(
            context, policy, number=latest.version_number + 1, fields={**carried, **fields}
        )

    def update_version(
        self,
        context: OrgContext,
        *,
        policy_id: uuid.UUID,
        version_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> AttendancePolicyVersion:
        self._manage(context)
        version = self._version(policy_id, version_id)
        assert_etag(version.row_version, if_match)
        if version.status != "DRAFT":
            raise InvalidStatusTransition("Only a DRAFT policy version can be edited.")
        self._assert_version_fields({f: getattr(version, f) for f in VERSION_FIELDS} | changes)
        for field, value in changes.items():
            setattr(version, field, value)
        version.updated_by = context.user_id
        return version

    def _version(self, policy_id: uuid.UUID, version_id: uuid.UUID) -> AttendancePolicyVersion:
        version = self._versions.require(version_id, resource="policy version")
        if version.policy_id != policy_id:
            raise ValidationFailed("That version does not belong to the policy.")
        return version

    def publish_version(
        self,
        context: OrgContext,
        *,
        policy_id: uuid.UUID,
        version_id: uuid.UUID,
        if_match: int | None,
    ) -> AttendancePolicyVersion:
        self._manage(context)
        self._policies.lock(policy_id)  # serialises ATT-05/06 (no exclusion constraints in MySQL)
        version = self._version(policy_id, version_id)
        assert_etag(version.row_version, if_match)
        if version.status != "DRAFT":
            raise InvalidStatusTransition("Only a DRAFT policy version can be published.")
        effective_from = self._assert_publishable(version)
        today = self._today(context)
        siblings = self._versions.for_policies([policy_id])[policy_id]
        previous = max(
            (v for v in siblings if v.status == "ACTIVE"),
            key=lambda v: v.version_number,
            default=None,
        )
        if previous is not None or version.version_number >= 2:
            if effective_from <= today or (
                previous is not None
                and previous.effective_from is not None
                and effective_from <= previous.effective_from
            ):
                raise PolicyVersionNotFuture()
        elif effective_from <= today and self._versions.any_record_on_or_after(effective_from):
            raise PolicyVersionNotFuture()
        self._assert_no_conflict(version, effective_from)
        if previous is not None:
            previous.effective_to = effective_from
            previous.updated_by = context.user_id
        version.status = "ACTIVE"
        version.published_by = context.user_id
        version.published_at = utc_now()
        version.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.ATTENDANCE_POLICY_VERSION_PUBLISHED,
            entity_type="attendance_policy_version",
            entity_id=version.id,
            after={"policy_id": str(policy_id), "effective_from": effective_from},
        )
        return version

    def _assert_publishable(self, version: AttendancePolicyVersion) -> date:
        problems: list[tuple[str, str]] = []
        if version.effective_from is None:
            problems.append(("effective_from", "Required to publish."))
        if version.default_shift_id is None and not (
            version.flexible_full_day_min_work_minutes
            and version.flexible_half_day_min_work_minutes
            and version.flexible_half_day_min_work_minutes
            < version.flexible_full_day_min_work_minutes
        ):
            problems.append(
                ("flexible_full_day_min_work_minutes", "Required without a default shift.")
            )
        if version.default_shift_id is not None:
            shift = self._shifts.require(version.default_shift_id, resource="shift")
            if shift.status != "ACTIVE":
                problems.append(("default_shift_id", "The shift must be ACTIVE."))
        if problems:
            error = ValidationFailed("The policy version is incomplete.")
            for field, message in problems:
                error.with_detail(field=field, code="REQUIRED", message=message)
            raise error
        return version.effective_from  # type: ignore[return-value]  # checked above

    def _assert_no_conflict(self, version: AttendancePolicyVersion, effective_from: date) -> None:
        """ATT-06: identical applicability tuple and priority with an overlapping interval."""
        for other, _code in self._versions.active_with_codes():
            if other.policy_id == version.policy_id or other.effective_from is None:
                continue
            same = all(getattr(other, f) == getattr(version, f) for f in _APPLIES)
            if (
                same
                and other.priority == version.priority
                and intervals_overlap(
                    effective_from, version.effective_to, other.effective_from, other.effective_to
                )
            ):
                raise AttendancePolicyConflict()

    def archive_version(
        self,
        context: OrgContext,
        *,
        policy_id: uuid.UUID,
        version_id: uuid.UUID,
        reason: str,
        if_match: int | None,
    ) -> AttendancePolicyVersion:
        self._manage(context)
        self._policies.lock(policy_id)
        version = self._version(policy_id, version_id)
        assert_etag(version.row_version, if_match)
        if version.status != "ACTIVE":
            raise InvalidStatusTransition("Only an ACTIVE policy version can be archived.")
        version.status = "ARCHIVED"
        version.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.ATTENDANCE_POLICY_VERSION_ARCHIVED,
            entity_type="attendance_policy_version",
            entity_id=version.id,
            reason=reason,
        )
        return version

    def resolve(self, context: OrgContext, *, employee_id: uuid.UUID, on: date) -> Schedule:
        """SCHEDULE(E, D) with no writes (5.16)."""
        context.require(Permission.ATTENDANCE_VIEW)
        employee = self._employees.require(employee_id, resource="employee")
        schedule = self._schedule.resolver(employee).at(on)
        if schedule.policy_version is None:
            raise NoApplicableAttendancePolicy()
        return schedule

    # ------------------------------------------------------------------ shift assignments

    def list_assignments(
        self, context: OrgContext, employee_id: uuid.UUID
    ) -> list[EmployeeShiftAssignment]:
        context.require(Permission.ATTENDANCE_VIEW)
        self._employees.require(employee_id, resource="employee")
        return list(self._assignments.for_employee(employee_id))

    def create_assignment(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        values: dict[str, Any],
        acknowledged: list[str],
    ) -> tuple[EmployeeShiftAssignment, dict[str, Any] | None]:
        """ATT-08 / ATT-09. Returns the assignment and the boundary adjustment, if any."""
        context.require(Permission.ATTENDANCE_SHIFT_ASSIGN)
        self._require_module(context)
        employee = self._employees.get_for_update(employee_id)  # lock 1: employee
        if employee is None:
            raise ResourceNotFound("employee")
        start: date = values["effective_from"]
        today = self._today(context)
        if values.get("shift_id") is None and values.get("weekly_off_pattern_id") is None:
            raise ValidationFailed("Provide a shift_id or a weekly_off_pattern_id.")
        if values.get("shift_id") is not None:
            shift = self._shifts.require(values["shift_id"], resource="shift")
            if shift.status != "ACTIVE":
                raise ValidationFailed("The shift must be ACTIVE.")
        if employee.joining_date is None or start < employee.joining_date:
            raise ShiftAssignmentDateInvalid("effective_from precedes the joining date.")
        if start < today:
            context.require(Permission.ATTENDANCE_CORRECT)
            reason = (values.get("reason") or "").strip()
            if len(reason) < BACKDATE_REASON_MIN:
                raise ValidationFailed("A backdated assignment needs a reason of 10+ characters.")
            if self._records.has_finalized_from(employee.id, start - timedelta(days=1)):
                raise ShiftAssignmentDateInvalid("Finalized records exist from that date.")
        end = values.get("effective_to")
        if end is not None and end <= start:
            raise ValidationFailed("effective_to must follow effective_from.")

        existing = self._assignments.for_employee(employee.id)
        if any(a.effective_from >= start for a in existing):
            raise ShiftAssignmentOverlap()  # ATT-09: end the later assignment first

        adjustment: dict[str, Any] | None = None
        warnings = WarningCollector(acknowledged)
        containing = next(
            (
                a
                for a in existing
                if a.effective_from < start and (a.effective_to is None or a.effective_to > start)
            ),
            None,
        )
        if containing is not None:
            warnings.add(WarningCode.SHIFT_ASSIGNMENT_TRUNCATES)
        acknowledged_codes = warnings.enforce()
        if containing is not None:
            adjustment = {
                "assignment_id": str(containing.id),
                "effective_to_before": containing.effective_to,
                "effective_to_after": start,
            }
            containing.effective_to = start
            containing.updated_by = context.user_id

        row = EmployeeShiftAssignment(
            id=uuid7(),
            organization_id=context.organization_id,
            employee_id=employee.id,
            shift_id=values.get("shift_id"),
            weekly_off_pattern_id=values.get("weekly_off_pattern_id"),
            effective_from=start,
            effective_to=end,
            reason=values.get("reason"),
            created_by=context.user_id,
        )
        self._assignments.add(row)
        self._assignments.flush()
        self._audit.record_change(
            context,
            action=(
                AuditAction.SHIFT_ASSIGNMENT_BACKDATED
                if start < today
                else AuditAction.SHIFT_ASSIGNMENT_CREATED
            ),
            entity_type="employee_shift_assignment",
            entity_id=row.id,
            employee_id=employee.id,
            after={
                "effective_from": start,
                "shift_id": str(row.shift_id) if row.shift_id else None,
            },
            reason=values.get("reason"),
            acknowledged_warnings=acknowledged_codes,
        )
        # shortcut: ATT-10 re-derivation of punch business dates for backdated assignments.
        return row, adjustment
