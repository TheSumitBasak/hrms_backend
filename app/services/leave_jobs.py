"""LEAVE_ACCRUAL and LEAVE_YEAR_END jobs (addendum §6.6, §6.7, §14).

One organization per runner, one transaction per ``(employee, leave type)``. Every posting uses
a deterministic ledger key (ALC/ACR/CFO/CFI/LAP/NWO) and every closure a unique row, so a rerun,
a manual run on the same date or two overlapping runners cannot double-credit or double-close.
Run-level locking (``background_job_runs``) only avoids wasted work; item-level idempotency is
what guarantees correctness (§14.2).
"""

from __future__ import annotations

import uuid
from contextlib import suppress
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.actions import AuditAction
from app.audit.events import AuditEvent, ChangeSet
from app.core.clock import utc_now
from app.core.errors import EmsError, LeaveTypeNotApplicable
from app.db.types import uuid7
from app.models.employee import Employee, EmployeeStatus
from app.models.leave import (
    BackgroundJobRun,
    JobCode,
    LeaveBalance,
    LeavePolicyVersion,
    LeaveType,
    LeaveYearClosure,
    TransactionType,
)
from app.repositories.employee import EmployeeRepository
from app.repositories.leave import (
    LeaveApplicationRepository,
    LeaveBalanceRepository,
    LeavePolicyRepository,
    LeavePolicyVersionRepository,
    LeaveTransactionRepository,
    LeaveTypeRepository,
    LeaveYearClosureRepository,
)
from app.repositories.organization import (
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.services.employee_status import status_on
from app.services.job_runs import JobBase, RunTally, system_user_id  # noqa: F401
from app.services.leave_config import LeaveConfigService
from app.services.leave_ledger import LeaveLedger, Posting
from app.services.leave_rules import (
    FREQUENCY_PERIODS,
    accrual_periods,
    apply_cap,
    cumulative_accrual,
    leave_year_start,
    next_leave_year_start,
    upfront_entitlement,
)

_ELIGIBLE_STATUSES = (
    EmployeeStatus.ACTIVE.value,
    EmployeeStatus.ON_NOTICE.value,
    EmployeeStatus.INACTIVE.value,
)
ZERO = Decimal("0.00")


class LeaveJobs(JobBase):
    def __init__(self, session: Session, organization_id: uuid.UUID) -> None:
        super().__init__(session, organization_id)
        self._employees = EmployeeRepository(session, organization_id)
        self._types = LeaveTypeRepository(session, organization_id)
        self._balances = LeaveBalanceRepository(session, organization_id)
        self._transactions = LeaveTransactionRepository(session, organization_id)
        self._closures = LeaveYearClosureRepository(session, organization_id)
        self._applications = LeaveApplicationRepository(session, organization_id)
        self._versions = LeavePolicyVersionRepository(session, organization_id)
        self._ledger = LeaveLedger(balances=self._balances, transactions=self._transactions)
        self._config = LeaveConfigService(
            types=self._types,
            policies=LeavePolicyRepository(session, organization_id),
            versions=self._versions,
            employees=self._employees,
            organizations=OrganizationRepository(session),
            settings=OrganizationSettingsRepository(session),
            audit=self._audit,
        )

    # ------------------------------------------------------------------ run bookkeeping

    # ------------------------------------------------------------------ candidate selection

    def _employee_ids(self) -> list[uuid.UUID]:
        statement = (
            select(Employee.id)
            .where(
                Employee.organization_id == self._org,
                Employee.archived_at.is_(None),
                Employee.joining_date.is_not(None),
                Employee.status.in_(_ELIGIBLE_STATUSES),
            )
            .order_by(Employee.id)
        )
        return list(self._session.execute(statement).scalars())

    def _status_on(self, employee: Employee, day: date) -> str | None:
        return status_on(self._session, self._org, employee, day, today=self.today())

    def _eligible(self, employee: Employee, version: LeavePolicyVersion, anchor: date) -> bool:
        status = self._status_on(employee, anchor)
        if status in (EmployeeStatus.ACTIVE.value, EmployeeStatus.ON_NOTICE.value):
            return True
        return status == EmployeeStatus.INACTIVE.value and version.accrue_during_inactive

    # ------------------------------------------------------------------ accrual

    def run_accrual(
        self, as_of: date, *, run_key: str, trigger: str, requested_by: uuid.UUID
    ) -> BackgroundJobRun:
        run = self.start_run(
            JobCode.LEAVE_ACCRUAL,
            run_key=run_key,
            trigger=trigger,
            requested_by=requested_by,
            parameters={"as_of_date": as_of.isoformat()},
        )
        tally = RunTally()
        types = [t for t in self._types.list_types(status="ACTIVE") if t.tracks_balance]
        self._session.rollback()  # end the read transaction before the per-item ones
        for employee_id in self._employee_ids():
            for leave_type in types:
                tally.processed += 1
                try:
                    posted = self._accrue_item(run, employee_id, leave_type, as_of)
                    self._heartbeat(run)
                    self._session.commit()
                    tally.succeeded += 1 if posted else 0
                    tally.skipped += 0 if posted else 1
                except EmsError as error:
                    self._session.rollback()
                    tally.failed += 1
                    tally.failures.append(
                        {"employee_id": str(employee_id), "error_code": error.code.value}
                    )
        run = self._session.merge(run)
        self._finish(run, tally, AuditAction.LEAVE_ACCRUAL_RUN_COMPLETED)
        return run

    def _accrue_item(
        self, run: BackgroundJobRun, employee_id: uuid.UUID, leave_type: LeaveType, as_of: date
    ) -> int:
        employee = self._employees.get_for_update(employee_id)
        if employee is None or employee.joining_date is None:
            return 0
        month = self.settings.leave_year_start_month
        year = leave_year_start(as_of, month)
        tracking = self.settings.leave_tracking_start_date
        if tracking is None or next_leave_year_start(year) <= tracking:
            return 0
        # Ordering: Y+1 waits until Y is CLOSED for this (employee, type) (§6.6).
        previous = leave_year_start(year - timedelta(days=1), month)
        prior_balance = self._balances.find(employee.id, leave_type.id, previous)
        if prior_balance is not None and prior_balance.closure_status != "CLOSED":
            return 0

        posted = 0
        with suppress(LeaveTypeNotApplicable):  # no ANNUAL_UPFRONT version applies: nothing due
            posted += self._allocate_upfront(run, employee, leave_type, year, as_of, tracking)
        for frequency in FREQUENCY_PERIODS:
            posted += self._accrue_periods(
                run, employee, leave_type, year, as_of, tracking, frequency
            )
        return posted

    def _post(
        self,
        run: BackgroundJobRun,
        balance: LeaveBalance,
        employee: Employee,
        leave_type: LeaveType,
        year: date,
        **posting: Any,
    ) -> bool:
        _, created = self._ledger.post(
            balance,
            Posting(
                employee_id=employee.id,
                leave_type_id=leave_type.id,
                year_start=year,
                actor_id=run.requested_by,
                job_run_id=run.id,
                **posting,
            ),
        )
        self._balances.reload(balance)
        return created

    def _allocate_upfront(  # noqa: PLR0917
        self,
        run: BackgroundJobRun,
        employee: Employee,
        leave_type: LeaveType,
        year: date,
        as_of: date,
        tracking: date,
    ) -> int:
        assert employee.joining_date is not None  # noqa: S101 - filtered by _employee_ids
        anchor = max(year, employee.joining_date)
        if as_of < anchor or anchor < tracking:
            return 0
        version = self._config.resolve_for(employee, leave_type.id, anchor)
        if version.allocation_method != "ANNUAL_UPFRONT" or not self._ending_ok(employee, anchor):
            return 0
        if not self._eligible(employee, version, anchor):
            return 0
        balance = self._ledger.lock_balance(employee.id, leave_type.id, year)
        entitled = upfront_entitlement(
            version.annual_entitlement_days or ZERO,
            version.upfront_proration or "NONE",
            employee.joining_date,
            year,
            version.joining_cutoff_day or 15,
            version.rounding_mode,
        )
        quantity = apply_cap(entitled, version.max_balance_days, balance.posted_balance_days)
        created = self._post(
            run,
            balance,
            employee,
            leave_type,
            year,
            transaction_type=TransactionType.ALLOCATION.value,
            quantity_days=quantity,
            entitled_quantity_days=entitled,
            effective_date=anchor,
            idempotency_key=f"ALC:{employee.id}:{leave_type.id}:{year}",
            policy_version_id=version.id,
        )
        return 1 if created else 0

    @staticmethod
    def _ending_ok(employee: Employee, anchor: date) -> bool:
        return employee.employment_end_date is None or employee.employment_end_date >= anchor

    def _accrue_periods(  # noqa: PLR0917
        self,
        run: BackgroundJobRun,
        employee: Employee,
        leave_type: LeaveType,
        year: date,
        as_of: date,
        tracking: date,
        frequency: str,
    ) -> int:
        if employee.joining_date is None:
            return 0
        posted = 0
        per_year = FREQUENCY_PERIODS[frequency]
        # The layout depends on a version's frequency; the cutoff day is read per period below.
        for period in accrual_periods(year, frequency, 15):
            if period.start < tracking:
                continue
            probe = self._probe_version(employee, leave_type, period.start, period.last_day)
            if probe is None or probe.accrual_frequency != frequency:
                continue
            anchor = period.start if probe.accrual_timing == "PERIOD_START" else period.last_day
            effective = max(anchor, employee.joining_date)
            if as_of < effective:
                break  # periods are processed in ascending order
            cutoff = accrual_periods(year, frequency, probe.joining_cutoff_day or 15)[
                period.number - 1
            ].cutoff
            version = self._config.resolve_for(employee, leave_type.id, effective)
            if (
                version.id != probe.id
                or employee.joining_date > cutoff
                or not self._ending_ok(employee, anchor)
                or not self._eligible(employee, version, effective)
            ):
                continue
            balance = self._ledger.lock_balance(employee.id, leave_type.id, year)
            count, prior = self._transactions.count_for_version(
                employee.id, leave_type.id, year, version.id
            )
            entitled = cumulative_accrual(
                version.annual_entitlement_days or ZERO,
                per_year,
                count,
                prior,
                version.rounding_mode,
            )
            quantity = apply_cap(entitled, version.max_balance_days, balance.posted_balance_days)
            created = self._post(
                run,
                balance,
                employee,
                leave_type,
                year,
                transaction_type=TransactionType.ACCRUAL.value,
                quantity_days=quantity,
                entitled_quantity_days=entitled,
                effective_date=effective,
                idempotency_key=f"ACR:{employee.id}:{leave_type.id}:{period.start}",
                policy_version_id=version.id,
                accrual_period_start=period.start,
            )
            posted += 1 if created else 0
        return posted

    def _probe_version(
        self, employee: Employee, leave_type: LeaveType, start: date, last_day: date
    ) -> LeavePolicyVersion | None:
        """The PERIODIC_ACCRUAL version applying inside the period, used only to read its layout."""
        for day in (max(start, employee.joining_date or start), last_day):
            try:
                version = self._config.resolve_for(employee, leave_type.id, day)
            except LeaveTypeNotApplicable:
                continue
            if version.allocation_method == "PERIODIC_ACCRUAL":
                return version
        return None

    # ------------------------------------------------------------------ year end

    def run_year_end(
        self, as_of: date, *, run_key: str, trigger: str, requested_by: uuid.UUID
    ) -> BackgroundJobRun:
        run = self.start_run(
            JobCode.LEAVE_YEAR_END,
            run_key=run_key,
            trigger=trigger,
            requested_by=requested_by,
            parameters={"as_of_date": as_of.isoformat()},
        )
        tally = RunTally()
        month = self.settings.leave_year_start_month
        statement = select(
            LeaveBalance.employee_id, LeaveBalance.leave_type_id, LeaveBalance.leave_year_start
        ).where(
            LeaveBalance.organization_id == self._org,
            LeaveBalance.closure_status == "OPEN",
            LeaveBalance.leave_year_end <= as_of,
        )
        pending = sorted(self._session.execute(statement).all(), key=lambda r: (r[2], str(r[0])))
        self._session.rollback()
        for employee_id, type_id, year in pending:
            tally.processed += 1
            try:
                closed = self._close_item(run, employee_id, type_id, year, month)
                self._heartbeat(run)
                self._session.commit()
                tally.succeeded += 1 if closed else 0
                tally.skipped += 0 if closed else 1
            except EmsError as error:
                self._session.rollback()
                tally.failed += 1
                tally.failures.append(
                    {"employee_id": str(employee_id), "error_code": error.code.value}
                )
        run = self._session.merge(run)
        self._finish(run, tally, AuditAction.LEAVE_ACCRUAL_RUN_COMPLETED)
        return run

    def _close_item(
        self,
        run: BackgroundJobRun,
        employee_id: uuid.UUID,
        type_id: uuid.UUID,
        year: date,
        month: int,
    ) -> bool:
        """Close one ``(employee, type, year)`` in one transaction (§6.7). False when deferred."""
        employee = self._employees.get_for_update(employee_id)
        if employee is None:
            return False
        balance = self._ledger.lock_balance(employee_id, type_id, year)  # Y then Y+1: ascending
        if balance.closure_status == "CLOSED":
            return False
        next_year = next_leave_year_start(year)
        next_balance = self._ledger.lock_balance(employee_id, type_id, next_year)
        closure = self._closures.find(employee_id, type_id, year)

        if self._has_submitted(employee_id, type_id, year):
            self._upsert_closure(closure, employee_id, type_id, year, run, deferred=True)
            self._audit_closure(run, employee_id, year, AuditAction.LEAVE_YEAR_CLOSURE_DEFERRED)
            return False

        version = self._closing_version(employee, type_id, year, next_year)
        posted = balance.posted_balance_days
        carry, lapse, written_off = self._year_end_split(posted, version)

        closure = self._upsert_closure(
            closure,
            employee_id,
            type_id,
            year,
            run,
            deferred=False,
            amounts=(posted, carry, lapse, written_off),
            version=version,
        )
        self._self_close(run, balance, employee, type_id, year, closure, carry, lapse, written_off)
        if carry != 0:
            self._ledger.post(
                next_balance,
                Posting(
                    employee_id=employee_id,
                    leave_type_id=type_id,
                    year_start=next_year,
                    transaction_type=TransactionType.CARRY_FORWARD_IN.value,
                    quantity_days=carry,
                    effective_date=next_year,
                    idempotency_key=f"CFI:{closure.id}",
                    actor_id=run.requested_by,
                    year_closure_id=closure.id,
                    policy_version_id=version.id if version else None,
                    job_run_id=run.id,
                ),
            )
        balance.closure_status = "CLOSED"
        self._balances.reload(balance)
        self._audit_closure(run, employee_id, year, AuditAction.LEAVE_YEAR_CLOSED)
        return True

    def _has_submitted(self, employee_id: uuid.UUID, type_id: uuid.UUID, year: date) -> bool:
        return self._applications.has_submitted(employee_id, type_id, year)

    def _closing_version(
        self, employee: Employee, type_id: uuid.UUID, year: date, next_year: date
    ) -> LeavePolicyVersion | None:
        """§6.7 step 3: the version on the last day of Y, else the latest that applied in Y."""
        last_day = next_year - timedelta(days=1)
        try:
            return self._config.resolve_for(employee, type_id, last_day)
        except LeaveTypeNotApplicable:
            pass
        applied = [
            v
            for v in self._versions.active_for_type(type_id)
            if v.effective_from is not None
            and v.effective_from < next_year
            and (v.effective_to is None or v.effective_to > year)
        ]
        return max(applied, key=lambda v: v.effective_from or year, default=None)

    @staticmethod
    def _year_end_split(
        posted: Decimal, version: LeavePolicyVersion | None
    ) -> tuple[Decimal, Decimal, Decimal]:
        """§6.7 step 4: ``(carry-forward, lapse, write-off)`` for a closing balance."""
        if posted > 0:
            carry = ZERO
            if version is not None and version.carry_forward_enabled:
                carry = min(posted, version.max_carry_forward_days or ZERO)
            return carry, posted - carry, ZERO
        if posted < 0:
            if version is not None and version.negative_balance_year_end == "WRITE_OFF":
                return ZERO, ZERO, -posted
            return posted, ZERO, ZERO
        return ZERO, ZERO, ZERO

    def _upsert_closure(
        self,
        closure: LeaveYearClosure | None,
        employee_id: uuid.UUID,
        type_id: uuid.UUID,
        year: date,
        run: BackgroundJobRun,
        *,
        deferred: bool,
        amounts: tuple[Decimal, Decimal, Decimal, Decimal] | None = None,
        version: LeavePolicyVersion | None = None,
    ) -> LeaveYearClosure:
        if closure is None:
            closure = LeaveYearClosure(
                id=uuid7(),
                organization_id=self._org,
                employee_id=employee_id,
                leave_type_id=type_id,
                leave_year_start=year,
                status="DEFERRED",
                created_by=run.requested_by,
            )
            self._session.add(closure)
        closure.job_run_id = run.id
        closure.processed_at = utc_now()
        if deferred:
            closure.status = "DEFERRED"
            closure.deferred_reason = "PENDING_APPLICATIONS"
        elif amounts is not None:
            closure.status = "CLOSED"
            closure.deferred_reason = None
            (
                closure.closing_posted_balance,
                closure.carried_forward_days,
                closure.lapsed_days,
                closure.written_off_days,
            ) = amounts
            closure.policy_version_id = version.id if version else None
        self._session.flush()
        return closure

    def _self_close(  # noqa: PLR0917
        self,
        run: BackgroundJobRun,
        balance: LeaveBalance,
        employee: Employee,
        type_id: uuid.UUID,
        year: date,
        closure: LeaveYearClosure,
        carry: Decimal,
        lapse: Decimal,
        written_off: Decimal,
    ) -> None:
        """Steps 5: the postings into Y that bring its posted balance to zero."""
        last_day = next_leave_year_start(year) - timedelta(days=1)
        plan = (
            (TransactionType.CARRY_FORWARD_OUT, -carry, "CFO", carry != 0),
            (TransactionType.LAPSE, -lapse, "LAP", lapse > 0),
            (TransactionType.NEGATIVE_BALANCE_WRITE_OFF, written_off, "NWO", written_off > 0),
        )
        for kind, quantity, prefix, due in plan:
            if not due:
                continue
            self._ledger.post(
                balance,
                Posting(
                    employee_id=employee.id,
                    leave_type_id=type_id,
                    year_start=year,
                    transaction_type=kind.value,
                    quantity_days=quantity,
                    effective_date=last_day,
                    idempotency_key=f"{prefix}:{closure.id}",
                    actor_id=run.requested_by,
                    year_closure_id=closure.id,
                    policy_version_id=closure.policy_version_id,
                    job_run_id=run.id,
                ),
                closing=True,
            )

    def _audit_closure(
        self, run: BackgroundJobRun, employee_id: uuid.UUID, year: date, action: AuditAction
    ) -> None:
        self._audit.record(
            AuditEvent(
                action=action,
                entity_type="leave_year_closure",
                entity_id=uuid7(),
                actor_user_id=run.requested_by,
                actor_role_codes="SYSTEM",
                organization_id=self._org,
                employee_id=employee_id,
                changes=ChangeSet(
                    changed_fields=["leave_year_start"],
                    before_values={},
                    after_values={"leave_year_start": year.isoformat()},
                ),
            )
        )
