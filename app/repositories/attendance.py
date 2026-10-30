"""Attendance repositories (addendum 5.3-5.9); queries start from the tenant-filtered select."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.models.attendance import (
    AttendancePolicy,
    AttendancePolicyVersion,
    AttendancePunch,
    AttendanceRecord,
    AttendanceRecordRevision,
    AttendanceRegularizationAction,
    AttendanceRegularizationItem,
    AttendanceRegularizationRequest,
    EmployeeShiftAssignment,
    Shift,
)
from app.models.leave import LeaveApplication, LeaveApplicationSlot
from app.repositories.base import OrgScopedRepository


class ShiftRepository(OrgScopedRepository[Shift]):
    model = Shift

    def list_shifts(self, *, status: str | None = None) -> Sequence[Shift]:
        statement = self.select().order_by(Shift.shift_name, Shift.id)
        if status:
            statement = statement.where(Shift.status == status)
        return list(self._session.execute(statement).scalars())

    def code_exists(self, code: str) -> bool:
        statement = select(Shift.id).where(
            Shift.organization_id == self._organization_id,
            Shift.shift_code_normalized == code.upper(),
        )
        return self._session.execute(statement.limit(1)).first() is not None

    def is_referenced_by_record(self, shift_id: uuid.UUID) -> bool:
        statement = select(AttendanceRecord.id).where(
            AttendanceRecord.organization_id == self._organization_id,
            AttendanceRecord.shift_id == shift_id,
        )
        return self._session.execute(statement.limit(1)).first() is not None


class AttendancePolicyRepository(OrgScopedRepository[AttendancePolicy]):
    model = AttendancePolicy

    def list_policies(self, *, status: str | None = None) -> Sequence[AttendancePolicy]:
        statement = self.select().order_by(AttendancePolicy.policy_code)
        if status:
            statement = statement.where(AttendancePolicy.status == status)
        return list(self._session.execute(statement).scalars())

    def code_exists(self, code: str) -> bool:
        statement = select(AttendancePolicy.id).where(
            AttendancePolicy.organization_id == self._organization_id,
            AttendancePolicy.policy_code_normalized == code.upper(),
        )
        return self._session.execute(statement.limit(1)).first() is not None


class AttendancePolicyVersionRepository(OrgScopedRepository[AttendancePolicyVersion]):
    model = AttendancePolicyVersion

    def for_policies(
        self, policy_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[AttendancePolicyVersion]]:
        grouped: dict[uuid.UUID, list[AttendancePolicyVersion]] = {pid: [] for pid in policy_ids}
        if not policy_ids:
            return grouped
        statement = (
            self.select()
            .where(AttendancePolicyVersion.policy_id.in_(policy_ids))
            .order_by(AttendancePolicyVersion.version_number)
        )
        for row in self._session.execute(statement).scalars():
            grouped[row.policy_id].append(row)
        return grouped

    def active_with_codes(self) -> list[tuple[AttendancePolicyVersion, str]]:
        """ACTIVE versions of ACTIVE policies, with the policy code for tie-breaking (ATT-07)."""
        statement = (
            select(AttendancePolicyVersion, AttendancePolicy.policy_code)
            .join(
                AttendancePolicy,
                (AttendancePolicy.id == AttendancePolicyVersion.policy_id)
                & (AttendancePolicy.organization_id == AttendancePolicyVersion.organization_id),
            )
            .where(
                AttendancePolicyVersion.organization_id == self._organization_id,
                AttendancePolicyVersion.status == "ACTIVE",
                AttendancePolicy.status == "ACTIVE",
            )
        )
        return [(v, code) for v, code in self._session.execute(statement).all()]

    def any_record_on_or_after(self, day: date) -> bool:
        """ATT-05: version 1 may start in the past only while no record exists from that date."""
        statement = select(AttendanceRecord.id).where(
            AttendanceRecord.organization_id == self._organization_id,
            AttendanceRecord.business_date >= day,
        )
        return self._session.execute(statement.limit(1)).first() is not None


class ShiftAssignmentRepository(OrgScopedRepository[EmployeeShiftAssignment]):
    model = EmployeeShiftAssignment

    def for_employee(self, employee_id: uuid.UUID) -> Sequence[EmployeeShiftAssignment]:
        statement = (
            self.select()
            .where(EmployeeShiftAssignment.employee_id == employee_id)
            .order_by(EmployeeShiftAssignment.effective_from)
        )
        return list(self._session.execute(statement).scalars())


class AttendanceRecordRepository(OrgScopedRepository[AttendanceRecord]):
    model = AttendanceRecord

    def find(self, employee_id: uuid.UUID, business_date: date) -> AttendanceRecord | None:
        statement = self.select().where(
            AttendanceRecord.employee_id == employee_id,
            AttendanceRecord.business_date == business_date,
        )
        return self._session.execute(statement).scalar_one_or_none()

    def lock_find(self, employee_id: uuid.UUID, business_date: date) -> AttendanceRecord | None:
        statement = (
            self.select()
            .where(
                AttendanceRecord.employee_id == employee_id,
                AttendanceRecord.business_date == business_date,
            )
            .with_for_update()
        )
        found = self._session.execute(statement).scalar_one_or_none()
        if found is not None:
            self._session.refresh(found)
        return found

    def insert_if_absent(self, values: dict[str, object]) -> None:
        """Insert-if-absent on the unique key; the caller then reads it with ``lock_find``."""
        insert = mysql_insert(AttendanceRecord).values(
            id=uuid.uuid4(), organization_id=self._organization_id, row_version=1, **values
        )
        self._session.execute(insert.on_duplicate_key_update(id=AttendanceRecord.__table__.c.id))

    def approved_leave_halves(self, employee_id: uuid.UUID, day: date) -> dict[str, uuid.UUID]:
        """INT-11: counted ACTIVE slots of APPROVED applications on ``day``, by half."""
        statement = (
            select(LeaveApplicationSlot.half, LeaveApplicationSlot.application_id)
            .join(
                LeaveApplication,
                (LeaveApplication.id == LeaveApplicationSlot.application_id)
                & (LeaveApplication.organization_id == LeaveApplicationSlot.organization_id),
            )
            .where(
                LeaveApplicationSlot.organization_id == self._organization_id,
                LeaveApplicationSlot.employee_id == employee_id,
                LeaveApplicationSlot.slot_date == day,
                LeaveApplicationSlot.slot_status == "ACTIVE",
                LeaveApplicationSlot.counted.is_(True),
                LeaveApplication.status == "APPROVED",
            )
        )
        return dict(self._session.execute(statement).tuples().all())

    def open_before(self, day: date) -> Sequence[AttendanceRecord]:
        """OPEN records strictly before ``day`` (candidates for automatic finalization)."""
        statement = (
            self.select()
            .where(AttendanceRecord.record_state == "OPEN", AttendanceRecord.business_date < day)
            .order_by(AttendanceRecord.business_date, AttendanceRecord.employee_id)
        )
        return list(self._session.execute(statement).scalars())

    def range_query(
        self, employee_id: uuid.UUID, start: date, end: date
    ) -> Select[tuple[AttendanceRecord]]:
        return (
            self.select()
            .where(
                AttendanceRecord.employee_id == employee_id,
                AttendanceRecord.business_date.between(start, end),
            )
            .order_by(AttendanceRecord.business_date)
        )

    def in_range(
        self, employee_id: uuid.UUID, start: date, end: date
    ) -> Sequence[AttendanceRecord]:
        return list(self._session.execute(self.range_query(employee_id, start, end)).scalars())

    def has_finalized_from(self, employee_id: uuid.UUID, day: date) -> bool:
        statement = select(AttendanceRecord.id).where(
            AttendanceRecord.organization_id == self._organization_id,
            AttendanceRecord.employee_id == employee_id,
            AttendanceRecord.business_date >= day,
            AttendanceRecord.record_state == "FINALIZED",
        )
        return self._session.execute(statement.limit(1)).first() is not None


class PunchRepository(OrgScopedRepository[AttendancePunch]):
    model = AttendancePunch

    def for_record(
        self, record_id: uuid.UUID, *, include_voided: bool = True
    ) -> Sequence[AttendancePunch]:
        statement = (
            self.select()
            .where(AttendancePunch.attendance_record_id == record_id)
            .order_by(AttendancePunch.punch_at, AttendancePunch.id)
        )
        if not include_voided:
            statement = statement.where(AttendancePunch.punch_status != "VOIDED")
        return list(self._session.execute(statement).scalars())

    def valid_for_record(self, record_id: uuid.UUID) -> Sequence[AttendancePunch]:
        statement = (
            self.select()
            .where(
                AttendancePunch.attendance_record_id == record_id,
                AttendancePunch.punch_status == "VALID",
            )
            .order_by(AttendancePunch.punch_at, AttendancePunch.id)
        )
        return list(self._session.execute(statement).scalars())

    def nearby_valid(
        self, employee_id: uuid.UUID, punch_type: str, low: datetime, high: datetime
    ) -> AttendancePunch | None:
        """ATT-13: an existing VALID punch of the same type inside the duplicate window."""
        statement = (
            self.select()
            .where(
                AttendancePunch.employee_id == employee_id,
                AttendancePunch.punch_type == punch_type,
                AttendancePunch.punch_status == "VALID",
                AttendancePunch.punch_at.between(low, high),
            )
            .order_by(AttendancePunch.punch_at, AttendancePunch.id)
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def by_external_event(self, source_code: str, event_id: str) -> AttendancePunch | None:
        statement = self.select().where(
            AttendancePunch.integration_source_code == source_code,
            AttendancePunch.external_event_id == event_id,
        )
        return self._session.execute(statement).scalar_one_or_none()

    def count_for_employee(self, employee_id: uuid.UUID) -> int:
        statement = select(func.count()).where(
            AttendancePunch.organization_id == self._organization_id,
            AttendancePunch.employee_id == employee_id,
        )
        return int(self._session.execute(statement).scalar_one())

    def list_query(
        self, employee_id: uuid.UUID, start: date, end: date, *, include_voided: bool
    ) -> Sequence[AttendancePunch]:
        statement = (
            self.select()
            .where(
                AttendancePunch.employee_id == employee_id,
                AttendancePunch.business_date.between(start, end),
            )
            .order_by(AttendancePunch.punch_at, AttendancePunch.id)
        )
        if not include_voided:
            statement = statement.where(AttendancePunch.punch_status != "VOIDED")
        return list(self._session.execute(statement).scalars())


class RevisionRepository(OrgScopedRepository[AttendanceRecordRevision]):
    model = AttendanceRecordRevision

    def for_record(self, record_id: uuid.UUID) -> Sequence[AttendanceRecordRevision]:
        statement = (
            self.select()
            .where(AttendanceRecordRevision.attendance_record_id == record_id)
            .order_by(AttendanceRecordRevision.revision_no)
        )
        return list(self._session.execute(statement).scalars())


class RegularizationRequestRepository(OrgScopedRepository[AttendanceRegularizationRequest]):
    model = AttendanceRegularizationRequest

    def pending_for_record(self, record_id: uuid.UUID) -> AttendanceRegularizationRequest | None:
        statement = self.select().where(
            AttendanceRegularizationRequest.attendance_record_id == record_id,
            AttendanceRegularizationRequest.status == "SUBMITTED",
        )
        return self._session.execute(statement).scalar_one_or_none()

    def listing(
        self,
        *,
        employee_id: uuid.UUID | None,
        employee_ids: Sequence[uuid.UUID] | None,
        statuses: Sequence[str] | None,
        approver_user_id: uuid.UUID | None,
        hr_queue: bool,
        limit: int = 100,
    ) -> Sequence[AttendanceRegularizationRequest]:
        model = AttendanceRegularizationRequest
        statement = self.select().order_by(model.submitted_at.desc(), model.id.desc()).limit(limit)
        if employee_id:
            statement = statement.where(model.employee_id == employee_id)
        if employee_ids is not None:
            statement = statement.where(model.employee_id.in_(employee_ids))
        if statuses:
            statement = statement.where(model.status.in_(statuses))
        approver = []
        if approver_user_id:
            approver.append(model.current_approver_user_id == approver_user_id)
        if hr_queue:
            approver.append(model.current_approver_type == "HR_QUEUE")
        if approver:
            statement = statement.where(or_(*approver))
        return list(self._session.execute(statement).scalars())


class RegularizationItemRepository(OrgScopedRepository[AttendanceRegularizationItem]):
    model = AttendanceRegularizationItem

    def for_request(self, request_id: uuid.UUID) -> Sequence[AttendanceRegularizationItem]:
        statement = (
            self.select()
            .where(AttendanceRegularizationItem.request_id == request_id)
            .order_by(AttendanceRegularizationItem.created_at, AttendanceRegularizationItem.id)
        )
        return list(self._session.execute(statement).scalars())


class RegularizationActionRepository(OrgScopedRepository[AttendanceRegularizationAction]):
    model = AttendanceRegularizationAction

    def for_request(self, request_id: uuid.UUID) -> Sequence[AttendanceRegularizationAction]:
        statement = (
            self.select()
            .where(AttendanceRegularizationAction.request_id == request_id)
            .order_by(AttendanceRegularizationAction.occurred_at, AttendanceRegularizationAction.id)
        )
        return list(self._session.execute(statement).scalars())

    def actors(self, request_id: uuid.UUID, action: str) -> set[uuid.UUID]:
        statement = select(AttendanceRegularizationAction.actor_user_id).where(
            AttendanceRegularizationAction.organization_id == self._organization_id,
            AttendanceRegularizationAction.request_id == request_id,
            AttendanceRegularizationAction.action == action,
        )
        return set(self._session.execute(statement).scalars())
