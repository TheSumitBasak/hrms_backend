"""Leave repositories (addendum §6, §14.2). Every query starts from the tenant-filtered select."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import Select, case, func, or_, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.models.leave import (
    ApplicationStatus,
    BackgroundJobRun,
    LeaveApplication,
    LeaveApplicationAction,
    LeaveApplicationSlot,
    LeaveBalance,
    LeavePolicy,
    LeavePolicyVersion,
    LeaveTransaction,
    LeaveType,
    LeaveYearClosure,
    PolicyVersionStatus,
)
from app.repositories.base import OrgScopedRepository

ZERO = Decimal("0.00")


class LeaveTypeRepository(OrgScopedRepository[LeaveType]):
    model = LeaveType

    def list_types(self, *, status: str | None = None) -> Sequence[LeaveType]:
        statement = self.select().order_by(LeaveType.display_order, LeaveType.leave_type_code)
        if status:
            statement = statement.where(LeaveType.status == status)
        return list(self._session.execute(statement).scalars())

    def code_exists(self, code: str) -> bool:
        statement = select(LeaveType.id).where(
            LeaveType.organization_id == self._organization_id,
            LeaveType.leave_type_code_normalized == code.upper(),
        )
        return self._session.execute(statement.limit(1)).first() is not None

    def is_referenced(self, leave_type_id: uuid.UUID) -> bool:
        """LEV: category and tracks_balance are immutable once a policy or application uses it."""
        for model in (LeavePolicy, LeaveApplication):
            statement = select(model.id).where(
                model.organization_id == self._organization_id, model.leave_type_id == leave_type_id
            )
            if self._session.execute(statement.limit(1)).first() is not None:
                return True
        return False


class LeavePolicyRepository(OrgScopedRepository[LeavePolicy]):
    model = LeavePolicy

    def list_policies(
        self, *, leave_type_id: uuid.UUID | None = None, status: str | None = None
    ) -> Sequence[LeavePolicy]:
        statement = self.select().order_by(LeavePolicy.policy_code)
        if leave_type_id:
            statement = statement.where(LeavePolicy.leave_type_id == leave_type_id)
        if status:
            statement = statement.where(LeavePolicy.status == status)
        return list(self._session.execute(statement).scalars())

    def code_exists(self, code: str) -> bool:
        statement = select(LeavePolicy.id).where(
            LeavePolicy.organization_id == self._organization_id,
            LeavePolicy.policy_code_normalized == code.upper(),
        )
        return self._session.execute(statement.limit(1)).first() is not None


class LeavePolicyVersionRepository(OrgScopedRepository[LeavePolicyVersion]):
    model = LeavePolicyVersion

    def for_policies(
        self, policy_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[LeavePolicyVersion]]:
        grouped: dict[uuid.UUID, list[LeavePolicyVersion]] = {pid: [] for pid in policy_ids}
        if not policy_ids:
            return grouped
        statement = (
            self.select()
            .where(LeavePolicyVersion.policy_id.in_(policy_ids))
            .order_by(LeavePolicyVersion.version_number)
        )
        for row in self._session.execute(statement).scalars():
            grouped[row.policy_id].append(row)
        return grouped

    def active_for_type(self, leave_type_id: uuid.UUID) -> Sequence[LeavePolicyVersion]:
        """ACTIVE versions of ACTIVE policies of one type (the §6.4 candidate set)."""
        statement = (
            self.select()
            .join(
                LeavePolicy,
                (LeavePolicy.id == LeavePolicyVersion.policy_id)
                & (LeavePolicy.organization_id == LeavePolicyVersion.organization_id),
            )
            .where(
                LeavePolicyVersion.leave_type_id == leave_type_id,
                LeavePolicyVersion.status == PolicyVersionStatus.ACTIVE.value,
                LeavePolicy.status == "ACTIVE",
            )
        )
        return list(self._session.execute(statement).scalars())

    def get_policy_code(self, policy_id: uuid.UUID) -> str:
        return str(
            self._session.execute(
                select(LeavePolicy.policy_code).where(LeavePolicy.id == policy_id)
            ).scalar_one()
        )


class LeaveBalanceRepository(OrgScopedRepository[LeaveBalance]):
    model = LeaveBalance

    def lock_or_create(
        self,
        *,
        employee_id: uuid.UUID,
        leave_type_id: uuid.UUID,
        year_start: date,
        year_end: date,
    ) -> LeaveBalance:
        """Insert-if-absent on the unique key, then ``SELECT … FOR UPDATE`` (§6.5).

        The session does not autoflush, and ``refresh`` discards unflushed changes, so pending
        updates to other balance rows are flushed first (found by the employment-end test).
        """
        self._session.flush()
        insert = mysql_insert(LeaveBalance).values(
            id=uuid.uuid4(),
            organization_id=self._organization_id,
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            leave_year_start=year_start,
            leave_year_end=year_end,
            credited_days=ZERO,
            debited_days=ZERO,
            reserved_days=ZERO,
            closure_status="OPEN",
            row_version=1,
        )
        # ``id = id`` is a no-op update: absorbs the duplicate without hiding other errors.
        self._session.execute(insert.on_duplicate_key_update(id=LeaveBalance.__table__.c.id))
        statement = (
            self.select()
            .where(
                LeaveBalance.employee_id == employee_id,
                LeaveBalance.leave_type_id == leave_type_id,
                LeaveBalance.leave_year_start == year_start,
            )
            .with_for_update()
        )
        balance = self._session.execute(statement).scalar_one()
        self._session.refresh(balance)
        return balance

    def reload(self, balance: LeaveBalance) -> LeaveBalance:
        """Flush, then re-read: ``posted_balance_days`` / ``available_days`` are DB-generated."""
        self._session.flush()
        self._session.refresh(balance)
        return balance

    def find(
        self, employee_id: uuid.UUID, leave_type_id: uuid.UUID, year_start: date
    ) -> LeaveBalance | None:
        statement = self.select().where(
            LeaveBalance.employee_id == employee_id,
            LeaveBalance.leave_type_id == leave_type_id,
            LeaveBalance.leave_year_start == year_start,
        )
        return self._session.execute(statement).scalar_one_or_none()

    def for_employee(self, employee_id: uuid.UUID, year_start: date) -> Sequence[LeaveBalance]:
        statement = self.select().where(
            LeaveBalance.employee_id == employee_id, LeaveBalance.leave_year_start == year_start
        )
        return list(self._session.execute(statement).scalars())

    def ledger_sums(
        self, employee_id: uuid.UUID, leave_type_id: uuid.UUID, year_start: date
    ) -> tuple[Decimal, Decimal]:
        """(credited, debited) recomputed from the ledger: the LEV-INV-1 reference."""
        quantity = LeaveTransaction.quantity_days
        statement = select(
            func.coalesce(func.sum(case((quantity > 0, quantity), else_=0)), 0),
            func.coalesce(func.sum(case((quantity < 0, -quantity), else_=0)), 0),
        ).where(
            LeaveTransaction.organization_id == self._organization_id,
            LeaveTransaction.employee_id == employee_id,
            LeaveTransaction.leave_type_id == leave_type_id,
            LeaveTransaction.leave_year_start == year_start,
        )
        credited, debited = self._session.execute(statement).one()
        return Decimal(credited), Decimal(debited)

    def reserved_sum(
        self, employee_id: uuid.UUID, leave_type_id: uuid.UUID, year_start: date
    ) -> Decimal:
        """LEV-INV-2 reference: Σ requested_days of reserving SUBMITTED applications."""
        statement = select(func.coalesce(func.sum(LeaveApplication.requested_days), 0)).where(
            LeaveApplication.organization_id == self._organization_id,
            LeaveApplication.employee_id == employee_id,
            LeaveApplication.leave_type_id == leave_type_id,
            LeaveApplication.leave_year_start == year_start,
            LeaveApplication.status == ApplicationStatus.SUBMITTED.value,
            LeaveApplication.reserves_balance.is_(True),
        )
        return Decimal(self._session.execute(statement).scalar_one())

    def has_any_transaction(self) -> bool:
        statement = select(LeaveTransaction.id).where(
            LeaveTransaction.organization_id == self._organization_id
        )
        return self._session.execute(statement.limit(1)).first() is not None


class LeaveTransactionRepository(OrgScopedRepository[LeaveTransaction]):
    model = LeaveTransaction

    def by_key(self, idempotency_key: str) -> LeaveTransaction | None:
        statement = self.select().where(LeaveTransaction.idempotency_key == idempotency_key)
        return self._session.execute(statement).scalar_one_or_none()

    def posted_as_of(
        self, employee_id: uuid.UUID, leave_type_id: uuid.UUID, year_start: date, as_of: date
    ) -> Decimal:
        """§6.14 posted balance on effective date ``as_of``, as known now."""
        statement = select(func.coalesce(func.sum(LeaveTransaction.quantity_days), 0)).where(
            LeaveTransaction.organization_id == self._organization_id,
            LeaveTransaction.employee_id == employee_id,
            LeaveTransaction.leave_type_id == leave_type_id,
            LeaveTransaction.leave_year_start == year_start,
            LeaveTransaction.effective_date <= as_of,
        )
        return Decimal(self._session.execute(statement).scalar_one())

    def reversal_of(self, transaction_id: uuid.UUID) -> LeaveTransaction | None:
        statement = self.select().where(LeaveTransaction.reverses_transaction_id == transaction_id)
        return self._session.execute(statement).scalar_one_or_none()

    def count_for_version(
        self,
        employee_id: uuid.UUID,
        leave_type_id: uuid.UUID,
        year_start: date,
        version_id: uuid.UUID,
    ) -> tuple[int, Decimal]:
        """(n posted, Σ entitled) of ACCRUALs under one policy version, for §6.6."""
        statement = select(
            func.count(), func.coalesce(func.sum(LeaveTransaction.entitled_quantity_days), 0)
        ).where(
            LeaveTransaction.organization_id == self._organization_id,
            LeaveTransaction.employee_id == employee_id,
            LeaveTransaction.leave_type_id == leave_type_id,
            LeaveTransaction.leave_year_start == year_start,
            LeaveTransaction.transaction_type == "ACCRUAL",
            LeaveTransaction.policy_version_id == version_id,
        )
        count, entitled = self._session.execute(statement).one()
        return int(count), Decimal(entitled)

    def ledger_query(
        self,
        *,
        employee_id: uuid.UUID,
        leave_type_id: uuid.UUID | None,
        leave_year_start: date | None,
        transaction_types: Sequence[str] | None,
    ) -> Select[tuple[LeaveTransaction]]:
        statement = self.select().where(LeaveTransaction.employee_id == employee_id)
        if leave_type_id:
            statement = statement.where(LeaveTransaction.leave_type_id == leave_type_id)
        if leave_year_start:
            statement = statement.where(LeaveTransaction.leave_year_start == leave_year_start)
        if transaction_types:
            statement = statement.where(LeaveTransaction.transaction_type.in_(transaction_types))
        return statement


class LeaveApplicationRepository(OrgScopedRepository[LeaveApplication]):
    model = LeaveApplication

    def lock_application(self, application_id: uuid.UUID) -> LeaveApplication:
        return self.lock(application_id)

    def submitted_count_in_year(
        self, employee_id: uuid.UUID, leave_type_id: uuid.UUID, year_start: date
    ) -> int:
        """LEV-27: SUBMITTED and APPROVED applications in the leave year."""
        statement = select(func.count()).where(
            LeaveApplication.organization_id == self._organization_id,
            LeaveApplication.employee_id == employee_id,
            LeaveApplication.leave_type_id == leave_type_id,
            LeaveApplication.leave_year_start == year_start,
            LeaveApplication.status.in_(("SUBMITTED", "APPROVED")),
        )
        return int(self._session.execute(statement).scalar_one())

    def adjacent_chain_days(
        self, employee_id: uuid.UUID, leave_type_id: uuid.UUID
    ) -> Sequence[LeaveApplication]:
        statement = self.select().where(
            LeaveApplication.employee_id == employee_id,
            LeaveApplication.leave_type_id == leave_type_id,
            LeaveApplication.status.in_(("SUBMITTED", "APPROVED")),
        )
        return list(self._session.execute(statement).scalars())

    def has_submitted(
        self, employee_id: uuid.UUID, leave_type_id: uuid.UUID, year_start: date
    ) -> bool:
        statement = select(LeaveApplication.id).where(
            LeaveApplication.organization_id == self._organization_id,
            LeaveApplication.employee_id == employee_id,
            LeaveApplication.leave_type_id == leave_type_id,
            LeaveApplication.leave_year_start == year_start,
            LeaveApplication.status == "SUBMITTED",
        )
        return self._session.execute(statement.limit(1)).first() is not None

    def list_query(
        self,
        *,
        employee_id: uuid.UUID | None = None,
        statuses: Sequence[str] | None = None,
        leave_type_id: uuid.UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        approver_user_id: uuid.UUID | None = None,
        hr_queue: bool = False,
        employee_ids: Sequence[uuid.UUID] | None = None,
    ) -> Select[tuple[LeaveApplication]]:
        statement = self.select()
        if employee_id:
            statement = statement.where(LeaveApplication.employee_id == employee_id)
        if employee_ids is not None:
            statement = statement.where(LeaveApplication.employee_id.in_(employee_ids))
        if statuses:
            statement = statement.where(LeaveApplication.status.in_(statuses))
        if leave_type_id:
            statement = statement.where(LeaveApplication.leave_type_id == leave_type_id)
        if from_date:
            statement = statement.where(LeaveApplication.end_date >= from_date)
        if to_date:
            statement = statement.where(LeaveApplication.start_date <= to_date)
        approver = []
        if approver_user_id:
            approver.append(LeaveApplication.current_approver_user_id == approver_user_id)
        if hr_queue:
            approver.append(LeaveApplication.current_approver_type == "HR_QUEUE")
        if approver:
            statement = statement.where(or_(*approver))
        return statement


class LeaveSlotRepository(OrgScopedRepository[LeaveApplicationSlot]):
    model = LeaveApplicationSlot

    def for_application(
        self, application_id: uuid.UUID, *, status: str | None = None
    ) -> Sequence[LeaveApplicationSlot]:
        statement = (
            self.select()
            .where(LeaveApplicationSlot.application_id == application_id)
            .order_by(LeaveApplicationSlot.slot_date, LeaveApplicationSlot.half)
        )
        if status:
            statement = statement.where(LeaveApplicationSlot.slot_status == status)
        return list(self._session.execute(statement).scalars())

    def active_in_range(
        self, employee_id: uuid.UUID, start: date, end: date
    ) -> Sequence[LeaveApplicationSlot]:
        statement = self.select().where(
            LeaveApplicationSlot.employee_id == employee_id,
            LeaveApplicationSlot.slot_status == "ACTIVE",
            LeaveApplicationSlot.slot_date.between(start, end),
        )
        return list(self._session.execute(statement).scalars())

    def release(self, application_id: uuid.UUID, status: str) -> None:
        self._session.execute(
            update(LeaveApplicationSlot)
            .where(
                LeaveApplicationSlot.organization_id == self._organization_id,
                LeaveApplicationSlot.application_id == application_id,
                LeaveApplicationSlot.slot_status == "ACTIVE",
            )
            .values(slot_status=status)
        )


class LeaveActionRepository(OrgScopedRepository[LeaveApplicationAction]):
    model = LeaveApplicationAction

    def for_application(self, application_id: uuid.UUID) -> Sequence[LeaveApplicationAction]:
        statement = (
            self.select()
            .where(LeaveApplicationAction.application_id == application_id)
            .order_by(LeaveApplicationAction.occurred_at, LeaveApplicationAction.id)
        )
        return list(self._session.execute(statement).scalars())

    def actors(self, application_id: uuid.UUID, action: str) -> set[uuid.UUID]:
        statement = select(LeaveApplicationAction.actor_user_id).where(
            LeaveApplicationAction.organization_id == self._organization_id,
            LeaveApplicationAction.application_id == application_id,
            LeaveApplicationAction.action == action,
        )
        return set(self._session.execute(statement).scalars())


class LeaveYearClosureRepository(OrgScopedRepository[LeaveYearClosure]):
    model = LeaveYearClosure

    def find(
        self, employee_id: uuid.UUID, leave_type_id: uuid.UUID, year_start: date
    ) -> LeaveYearClosure | None:
        statement = self.select().where(
            LeaveYearClosure.employee_id == employee_id,
            LeaveYearClosure.leave_type_id == leave_type_id,
            LeaveYearClosure.leave_year_start == year_start,
        )
        return self._session.execute(statement).scalar_one_or_none()


class BackgroundJobRunRepository(OrgScopedRepository[BackgroundJobRun]):
    model = BackgroundJobRun

    def next_attempt(self, job_code: str, run_key: str) -> int:
        statement = select(func.coalesce(func.max(BackgroundJobRun.attempt_no), 0)).where(
            BackgroundJobRun.organization_id == self._organization_id,
            BackgroundJobRun.job_code == job_code,
            BackgroundJobRun.run_key == run_key,
        )
        return int(self._session.execute(statement).scalar_one()) + 1

    def by_run_key(self, job_code: str, run_key: str) -> BackgroundJobRun | None:
        statement = (
            self.select()
            .where(BackgroundJobRun.job_code == job_code, BackgroundJobRun.run_key == run_key)
            .order_by(BackgroundJobRun.attempt_no.desc())
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def running(self, job_code: str) -> BackgroundJobRun | None:
        statement = self.select().where(
            BackgroundJobRun.job_code == job_code, BackgroundJobRun.status == "RUNNING"
        )
        return self._session.execute(statement).scalar_one_or_none()

    def list_runs(self, job_code: str | None = None) -> Sequence[BackgroundJobRun]:
        statement = self.select().order_by(BackgroundJobRun.started_at.desc()).limit(100)
        if job_code:
            statement = statement.where(BackgroundJobRun.job_code == job_code)
        return list(self._session.execute(statement).scalars())
