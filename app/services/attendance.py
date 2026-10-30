"""Punch ingestion, records, finalization and administrative correction (addendum 5.7-5.13).

Every mutating use case locks the employee row first (7.3), so the duplicate check of ATT-13 and
the insert-if-absent of records are serialised per employee. Raw punches are never edited:
corrections void a punch and add another (ATT-12).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in, utc_now
from app.core.context import OrgContext
from app.core.errors import (
    AttendanceConflictsWithLeave,
    AttendanceRecordFinalized,
    AttendanceRecordNotFinalized,
    AttendanceSourceDisabled,
    ConflictingAttendanceEvent,
    EmployeeNotEligibleForAttendance,
    ModuleNotEnabled,
    NoApplicableAttendancePolicy,
    PermissionDenied,
    PunchTimeInvalid,
    ResourceNotFound,
    SelfCorrectionNotAllowed,
    SelfServiceNotEnabled,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.attendance import AttendancePunch, AttendanceRecord
from app.models.employee import Employee
from app.models.master import LookupList
from app.repositories.attendance import (
    AttendanceRecordRepository,
    PunchRepository,
    RevisionRepository,
)
from app.repositories.employee import EmployeeRepository
from app.repositories.master import LookupRepository
from app.repositories.organization import (
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.services.approvals import employee_in_scope
from app.services.attendance_records import RecordEngine, day_window, snapshot
from app.services.attendance_rules import (
    FIRST_HALF,
    SECOND_HALF,
    NoApplicablePolicy,
    assign_business_date,
)
from app.services.employee_status import attendance_eligible
from app.services.organizations import assert_etag
from app.services.schedule import ScheduleService

FUTURE_TOLERANCE = timedelta(minutes=5)
CORRECTION_REASON_MIN = 20
MAX_FINALIZE_DAYS = 31
MAX_EMPLOYEE_RANGE_DAYS = 366
_HALF_NAMES = {FIRST_HALF, SECOND_HALF}


@dataclass(slots=True)
class PunchOutcome:
    punch: AttendancePunch
    record: AttendanceRecord
    outcome: str  # CREATED | DUPLICATE | REPLAYED | HELD | OUT_OF_WINDOW
    duplicate_of: uuid.UUID | None = None


@dataclass(slots=True)
class FinalizeResult:
    finalized: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)


class AttendanceService:
    def __init__(
        self,
        *,
        engine: RecordEngine,
        records: AttendanceRecordRepository,
        punches: PunchRepository,
        revisions: RevisionRepository,
        employees: EmployeeRepository,
        schedule: ScheduleService,
        lookups: LookupRepository,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        audit: AuditWriter,
    ) -> None:
        self._engine = engine
        self._records = records
        self._punches = punches
        self._revisions = revisions
        self._employees = employees
        self._schedule = schedule
        self._lookups = lookups
        self._organizations = organizations
        self._settings = settings
        self._audit = audit

    # Guards.

    def _org_tz_and_today(self, context: OrgContext) -> tuple[ZoneInfo, date]:
        organization = self._organizations.get(context.organization_id)
        name = organization.timezone if organization else "UTC"
        return ZoneInfo(name), today_in(name)

    def _require_module(self, context: OrgContext) -> Any:
        settings = self._settings.require(context.organization_id)
        if not settings.enable_attendance_module:
            raise ModuleNotEnabled()
        return settings

    def own_employee_id(self, context: OrgContext) -> uuid.UUID:
        """11: self-service identity comes from the user link and the SELF grant."""
        context.require(Permission.SELF_ATTENDANCE)
        settings = self._require_module(context)
        if not settings.enable_attendance_self_service:
            raise SelfServiceNotEnabled()
        if context.employee_id is None:
            raise PermissionDenied("This account is not linked to an employee record.")
        return context.employee_id

    def _visible_employee(self, context: OrgContext, employee_id: uuid.UUID) -> Employee:
        employee = self._employees.get(employee_id)
        if employee is None:
            raise ResourceNotFound("employee")
        if context.is_self(employee_id) and context.has(Permission.SELF_ATTENDANCE):
            return employee
        context.require(Permission.ATTENDANCE_VIEW)
        if not employee_in_scope(context, self._employees, employee):
            raise ResourceNotFound("employee")
        return employee

    def _assert_eligible(
        self, context: OrgContext, settings: Any, employee: Employee, day: date, today: date
    ) -> None:
        """ATT-01 (a)-(e); (f) is the policy lookup done by the caller."""
        if not attendance_eligible(
            self._employees.session,
            context.organization_id,
            settings.attendance_tracking_start_date,
            employee,
            day,
            today=today,
        ):
            raise EmployeeNotEligibleForAttendance()

    # Punch ingestion (5.7, 5.8).

    def self_punch(
        self, context: OrgContext, *, punch_type: str, work_arrangement: str | None
    ) -> PunchOutcome:
        employee_id = self.own_employee_id(context)
        return self._ingest(
            context,
            employee_id=employee_id,
            punch_at=None,
            punch_type=punch_type,
            source="SELF_SERVICE",
            work_arrangement=work_arrangement,
            remarks=None,
            integration_source_code=None,
            external_event_id=None,
        )

    def manual_punch(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        punch_at: datetime,
        punch_type: str,
        work_arrangement: str | None,
        remarks: str,
    ) -> PunchOutcome:
        context.require(Permission.ATTENDANCE_PUNCH_MANAGE)
        return self._ingest(
            context,
            employee_id=employee_id,
            punch_at=punch_at,
            punch_type=punch_type,
            source="MANUAL",
            work_arrangement=work_arrangement,
            remarks=remarks,
            integration_source_code=None,
            external_event_id=None,
        )

    def require_import(self, context: OrgContext) -> None:
        """Whole-request authorization for a batch; items are then processed independently."""
        context.require(Permission.ATTENDANCE_IMPORT)
        self._require_module(context)

    def integration_punch(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID | None,
        employee_code: str | None,
        punch_at: datetime,
        punch_type: str,
        integration_source_code: str,
        external_event_id: str,
    ) -> PunchOutcome:
        context.require(Permission.ATTENDANCE_IMPORT)
        if employee_id is None:
            found = self._employees.get_by_code(employee_code or "")
            if found is None:
                raise ResourceNotFound("employee")
            employee_id = found.id
        return self._ingest(
            context,
            employee_id=employee_id,
            punch_at=punch_at,
            punch_type=punch_type,
            source="INTEGRATION",
            work_arrangement=None,
            remarks=None,
            integration_source_code=integration_source_code,
            external_event_id=external_event_id,
        )

    def _ingest(  # noqa: PLR0912 - one parameter per punch column; linear flow
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        punch_at: datetime | None,
        punch_type: str,
        source: str,
        work_arrangement: str | None,
        remarks: str | None,
        integration_source_code: str | None,
        external_event_id: str | None,
    ) -> PunchOutcome:
        settings = self._require_module(context)
        organization_tz, today = self._org_tz_and_today(context)
        employee = self._employees.get_for_update(employee_id)  # lock 1: the employee
        if employee is None:
            raise ResourceNotFound("employee")
        now = utc_now()
        if source == "SELF_SERVICE":
            punch_at = now
        elif punch_at is None or punch_at > now + FUTURE_TOLERANCE:
            raise PunchTimeInvalid()

        if source == "INTEGRATION":
            replay = self._replay(
                employee.id, punch_at, punch_type, integration_source_code, external_event_id
            )
            if replay is not None:
                return replay

        resolver = self._schedule.resolver(employee)
        try:
            located = assign_business_date(
                punch_at, organization_tz, lambda d: day_window(resolver.at(d), organization_tz)
            )
        except NoApplicablePolicy as error:
            raise NoApplicableAttendancePolicy() from error
        schedule = resolver.at(located.business_date)
        version = schedule.policy_version
        if version is None:
            raise NoApplicableAttendancePolicy()
        self._assert_eligible(context, settings, employee, located.business_date, today)
        self._assert_source_enabled(
            source, version, settings, work_arrangement, integration_source_code
        )

        record = self._engine.get_or_create(
            employee.id, located.business_date, schedule, context.user_id
        )
        before = snapshot(record)
        duplicate: AttendancePunch | None = None
        if record.record_state == "FINALIZED":
            if source != "INTEGRATION":
                raise AttendanceRecordFinalized()
            status = "HELD"  # ATT-15: keep the event; HR applies it by correction
        elif not located.in_window:
            status = "OUT_OF_WINDOW"
        else:
            window = timedelta(seconds=version.duplicate_punch_window_seconds)
            duplicate = self._punches.nearby_valid(
                employee.id, punch_type, punch_at - window, punch_at + window
            )
            status = "DUPLICATE" if duplicate is not None else "VALID"

        punch = AttendancePunch(
            id=uuid7(),
            organization_id=context.organization_id,
            employee_id=employee.id,
            punch_at=punch_at,
            punch_type=punch_type,
            source=source,
            work_arrangement=work_arrangement,
            integration_source_code=integration_source_code,
            external_event_id=external_event_id,
            business_date=located.business_date,
            attendance_record_id=record.id,
            punch_status=status,
            duplicate_of_punch_id=duplicate.id if duplicate is not None else None,
            remarks=remarks,
            created_by=context.user_id,
        )
        self._punches.add(punch)
        self._punches.flush()
        if record.record_state == "OPEN":
            self._engine.evaluate_into(record)
            if source == "MANUAL":  # 5.13: SELF / INTEGRATION ingestion writes no revision
                self._engine.revise(
                    record,
                    before=before,
                    change_source="MANUAL_PUNCH",
                    actor_user_id=context.user_id,
                    punch_ids=[punch.id],
                )
        else:
            record.has_held_punch = True
        self._audit.record_change(
            context,
            action=AuditAction.ATTENDANCE_PUNCH_RECORDED,
            entity_type="attendance_punch",
            entity_id=punch.id,
            employee_id=employee.id,
            after={
                "source": source,
                "punch_type": punch_type,
                "business_date": located.business_date,
                "punch_status": status,
            },
        )
        outcome = {"VALID": "CREATED"}.get(status, status)
        return PunchOutcome(punch, record, outcome, punch.duplicate_of_punch_id)

    def _replay(
        self,
        employee_id: uuid.UUID,
        punch_at: datetime,
        punch_type: str,
        source_code: str | None,
        event_id: str | None,
    ) -> PunchOutcome | None:
        """ATT-13: the same external event returns the stored punch, or conflicts if it differs."""
        existing = self._punches.by_external_event(source_code or "", event_id or "")
        if existing is None:
            return None
        same = (
            existing.employee_id == employee_id
            and existing.punch_type == punch_type
            and int(existing.punch_at.timestamp()) == int(punch_at.timestamp())
        )
        if not same:
            raise ConflictingAttendanceEvent()
        record = self._records.require(existing.attendance_record_id, resource="attendance record")
        return PunchOutcome(existing, record, "REPLAYED", existing.duplicate_of_punch_id)

    def _assert_source_enabled(
        self,
        source: str,
        version: Any,
        settings: Any,
        work_arrangement: str | None,
        source_code: str | None,
    ) -> None:
        """ATT-16, against the policy version that governs the resolved business date."""
        if source == "SELF_SERVICE":
            if not (version.self_punch_enabled and settings.enable_attendance_self_service):
                raise AttendanceSourceDisabled()
            if work_arrangement == "REMOTE" and not version.allow_remote_self_punch:
                raise AttendanceSourceDisabled("Remote self punches are not allowed by the policy.")
        elif source == "MANUAL":
            if not version.manual_punch_enabled:
                raise AttendanceSourceDisabled()
        elif source == "INTEGRATION":
            value = self._lookups.get_value(LookupList.ATTENDANCE_SOURCE.value, source_code or "")
            if not version.integration_punch_enabled or value is None or value.status != "ACTIVE":
                raise AttendanceSourceDisabled()

    # Reads.

    def list_records(
        self, context: OrgContext, *, employee_id: uuid.UUID, start: date, end: date
    ) -> list[AttendanceRecord]:
        self._visible_employee(context, employee_id)
        if end < start or (end - start).days >= MAX_EMPLOYEE_RANGE_DAYS:
            raise ValidationFailed("The date range must be 1 to 366 days.")
        return list(self._records.in_range(employee_id, start, end))

    def get_record(
        self, context: OrgContext, employee_id: uuid.UUID, day: date
    ) -> tuple[AttendanceRecord, list[AttendancePunch], int]:
        self._visible_employee(context, employee_id)
        record = self._records.find(employee_id, day)
        if record is None:
            raise ResourceNotFound("attendance record")
        punches = list(self._punches.for_record(record.id))
        return record, punches, record.revision_no

    def list_punches(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        start: date,
        end: date,
        include_voided: bool,
    ) -> list[AttendancePunch]:
        self._visible_employee(context, employee_id)
        if end < start or (end - start).days >= MAX_EMPLOYEE_RANGE_DAYS:
            raise ValidationFailed("The date range must be 1 to 366 days.")
        return list(
            self._punches.list_query(employee_id, start, end, include_voided=include_voided)
        )

    def revisions(self, context: OrgContext, employee_id: uuid.UUID, day: date) -> list[Any]:
        """Full snapshots are for ``attendance.view`` holders; SELF never sees revisions."""
        context.require(Permission.ATTENDANCE_VIEW)
        employee = self._employees.require(employee_id, resource="employee")
        if not employee_in_scope(context, self._employees, employee):
            raise ResourceNotFound("employee")
        record = self._records.find(employee_id, day)
        if record is None:
            raise ResourceNotFound("attendance record")
        return list(self._revisions.for_record(record.id))

    # Finalize and reopen (5.12).

    def finalize(
        self, context: OrgContext, *, employee_id: uuid.UUID, start: date, end: date
    ) -> FinalizeResult:
        context.require(Permission.ATTENDANCE_FINALIZE)
        self._require_module(context)
        if end < start or (end - start).days >= MAX_FINALIZE_DAYS:
            raise ValidationFailed("The date range must be 1 to 31 days.")
        result = FinalizeResult()
        if self._employees.get_for_update(employee_id) is None:
            raise ResourceNotFound("employee")
        for listed in self._records.in_range(employee_id, start, end):
            record = self._records.lock_find(employee_id, listed.business_date)
            if record is None or record.record_state != "OPEN":
                continue
            reason = self._engine.try_finalize(record, actor_user_id=context.user_id, mode="MANUAL")
            if reason is not None:
                result.skipped.append({"record_id": str(record.id), "reason": reason})
                continue
            self._audit.record_change(
                context,
                action=AuditAction.ATTENDANCE_RECORD_FINALIZED,
                entity_type="attendance_record",
                entity_id=record.id,
                employee_id=employee_id,
                after={"business_date": record.business_date},
            )
            result.finalized += 1
        return result

    def reopen(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        day: date,
        reason: str,
        if_match: int | None,
    ) -> AttendanceRecord:
        context.require(Permission.ATTENDANCE_CORRECT)
        self._require_module(context)
        if context.is_self(employee_id):
            raise SelfCorrectionNotAllowed()
        if len(reason.strip()) < CORRECTION_REASON_MIN:
            raise ValidationFailed("A reason of at least 20 characters is required.")
        self._employees.get_for_update(employee_id)
        record = self._records.lock_find(employee_id, day)
        if record is None:
            raise ResourceNotFound("attendance record")
        assert_etag(record.row_version, if_match)
        if record.record_state != "FINALIZED":
            raise AttendanceRecordNotFinalized()
        before = snapshot(record)
        record.record_state = "OPEN"
        record.finalized_at = None
        record.finalized_by = None
        record.finalization_mode = None
        self._engine.evaluate_into(record)
        self._engine.revise(
            record,
            before=before,
            change_source="REOPENED",
            actor_user_id=context.user_id,
            reason=reason.strip(),
        )
        self._audit.record_change(
            context,
            action=AuditAction.ATTENDANCE_RECORD_REOPENED,
            entity_type="attendance_record",
            entity_id=record.id,
            employee_id=employee_id,
            reason=reason.strip(),
        )
        return record

    # Administrative correction (5.13).

    def correct(  # noqa: PLR0912, PLR0915 - one branch per documented operation
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        day: date,
        operations: list[dict[str, Any]],
        reason: str,
        if_match: int | None,
    ) -> AttendanceRecord:
        context.require(Permission.ATTENDANCE_CORRECT)
        settings = self._require_module(context)
        if context.is_self(employee_id):
            raise SelfCorrectionNotAllowed()  # ATT-18
        reason = reason.strip()
        if len(reason) < CORRECTION_REASON_MIN:
            raise ValidationFailed("A reason of at least 20 characters is required.")
        employee = self._employees.get_for_update(employee_id)
        if employee is None:
            raise ResourceNotFound("employee")
        _, today = self._org_tz_and_today(context)
        record = self._records.lock_find(employee_id, day)
        if record is None:
            schedule = self._schedule.resolver(employee).at(day)
            if schedule.policy_version is None:
                raise NoApplicableAttendancePolicy()
            self._assert_eligible(context, settings, employee, day, today)
            record = self._engine.get_or_create(employee_id, day, schedule, context.user_id)
        elif if_match is not None or record.revision_no:
            assert_etag(record.row_version, if_match)
        before = snapshot(record)
        touched: list[uuid.UUID] = []
        now = utc_now()
        for operation in operations:
            if "add_punch" in operation:
                spec = operation["add_punch"]
                if spec["punch_at"] > now + FUTURE_TOLERANCE:
                    raise PunchTimeInvalid()
                punch = AttendancePunch(
                    id=uuid7(),
                    organization_id=context.organization_id,
                    employee_id=employee_id,
                    punch_at=spec["punch_at"],
                    punch_type=spec["punch_type"],
                    source="MANUAL",
                    work_arrangement=spec.get("work_arrangement"),
                    business_date=day,
                    attendance_record_id=record.id,
                    punch_status="VALID",
                    remarks=reason,
                    created_by=context.user_id,
                )
                self._punches.add(punch)
                touched.append(punch.id)
            elif "void_punch_id" in operation:
                punch = self._owned_punch(record, operation["void_punch_id"])
                if punch.punch_status not in ("VALID", "OUT_OF_WINDOW", "HELD"):
                    raise ValidationFailed("That punch cannot be voided.")
                punch.punch_status = "VOIDED"
                punch.voided_at = now
                punch.voided_by = context.user_id
                punch.void_reason = reason[:500]
                punch.void_source = "ADMIN_CORRECTION"
                touched.append(punch.id)
            elif "apply_held_punch_id" in operation:
                punch = self._owned_punch(record, operation["apply_held_punch_id"])
                if punch.punch_status != "HELD":
                    raise ValidationFailed("Only a HELD punch can be applied.")
                punch.punch_status = "VALID"
                touched.append(punch.id)
            elif "set_override" in operation:
                spec = operation["set_override"]
                self._fix_half(record, spec["half"], spec["status"], "OVERRIDE")
            elif "set_on_duty" in operation:
                self._fix_half(record, operation["set_on_duty"]["half"], "PRESENT", "ON_DUTY")
            elif "clear_override" in operation:
                half = operation["clear_override"]["half"]
                prefix = half.lower()
                if getattr(record, f"{prefix}_source") in ("OVERRIDE", "ON_DUTY"):
                    setattr(record, f"{prefix}_source", "EVALUATED")
                    setattr(record, f"{prefix}_status", None)
            else:
                raise ValidationFailed("Unknown correction operation.")
        self._punches.flush()
        self._engine.evaluate_into(record, force_closed=record.record_state == "FINALIZED")
        self._engine.revise(
            record,
            before=before,
            change_source="ADMIN_CORRECTION",
            actor_user_id=context.user_id,
            reason=reason,
            punch_ids=touched,
        )
        self._audit.record_change(
            context,
            action=AuditAction.ATTENDANCE_RECORD_CORRECTED,
            entity_type="attendance_record",
            entity_id=record.id,
            employee_id=employee_id,
            reason=reason,
        )
        return record

    @staticmethod
    def _fix_half(record: AttendanceRecord, half: str, status: str, source: str) -> None:
        if half not in _HALF_NAMES:
            raise ValidationFailed("half must be FIRST_HALF or SECOND_HALF.")
        prefix = half.lower()
        if getattr(record, f"{prefix}_source") == "LEAVE":
            raise AttendanceConflictsWithLeave()  # ATT-17: cancel the leave instead
        if status not in ("PRESENT", "ABSENT"):
            raise ValidationFailed("An override can only be PRESENT or ABSENT.")
        setattr(record, f"{prefix}_status", status)
        setattr(record, f"{prefix}_source", source)

    def _owned_punch(self, record: AttendanceRecord, punch_id: uuid.UUID) -> AttendancePunch:
        punch = self._punches.get(punch_id)
        if punch is None or punch.attendance_record_id != record.id:
            raise ResourceNotFound("punch")
        return punch
