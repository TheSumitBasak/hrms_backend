"""Leave ledger, materialized balances and administrative postings (addendum §6.5, §6.13).

The ledger (``leave_transactions``) is authoritative and append-only. Every posting updates the
``leave_balances`` row in the same transaction, and callers hold the balance row's lock
(``SELECT … FOR UPDATE``) before posting, in the §7.3 lock order: employee, then balance rows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in
from app.core.context import OrgContext
from app.core.errors import (
    LeaveTransactionAlreadyReversed,
    LeaveTransactionInvalid,
    LeaveYearClosed,
    ModuleNotEnabled,
    OpeningBalanceExists,
    ResourceNotFound,
    SelfAdjustmentNotAllowed,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.core.warnings import WarningCode, WarningCollector
from app.db.types import uuid7
from app.models.employee import Employee
from app.models.leave import LeaveBalance, LeaveTransaction, LeaveType, TransactionType
from app.repositories.employee import EmployeeRepository
from app.repositories.leave import (
    LeaveBalanceRepository,
    LeaveTransactionRepository,
    LeaveTypeRepository,
)
from app.repositories.organization import (
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.schemas.common import CursorCodec, SortDirection
from app.services.approvals import employee_in_scope
from app.services.leave_rules import leave_year_start, next_leave_year_start

ZERO = Decimal("0.00")
REASON_MIN = 20
_REVERSIBLE = frozenset(
    {
        TransactionType.OPENING_BALANCE.value,
        TransactionType.ALLOCATION.value,
        TransactionType.ACCRUAL.value,
        TransactionType.ADJUSTMENT_CREDIT.value,
        TransactionType.ADJUSTMENT_DEBIT.value,
    }
)
_ADMIN_TYPES = frozenset(
    {
        TransactionType.OPENING_BALANCE.value,
        TransactionType.ADJUSTMENT_CREDIT.value,
        TransactionType.ADJUSTMENT_DEBIT.value,
    }
)


@dataclass(frozen=True, slots=True)
class Posting:
    """One ledger row to write. ``actor_id`` is the user (or the system user for jobs)."""

    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    year_start: date
    transaction_type: str
    quantity_days: Decimal
    effective_date: date
    idempotency_key: str
    actor_id: uuid.UUID
    entitled_quantity_days: Decimal | None = None
    leave_application_id: uuid.UUID | None = None
    application_action_id: uuid.UUID | None = None
    reverses_transaction_id: uuid.UUID | None = None
    year_closure_id: uuid.UUID | None = None
    policy_version_id: uuid.UUID | None = None
    accrual_period_start: date | None = None
    job_run_id: uuid.UUID | None = None
    reason: str | None = None


class LeaveLedger:
    """Posting primitive shared by administrative operations, applications and the jobs."""

    def __init__(
        self, *, balances: LeaveBalanceRepository, transactions: LeaveTransactionRepository
    ) -> None:
        self._balances = balances
        self._transactions = transactions

    def lock_balance(
        self, employee_id: uuid.UUID, leave_type_id: uuid.UUID, year_start: date
    ) -> LeaveBalance:
        return self._balances.lock_or_create(
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            year_start=year_start,
            year_end=next_leave_year_start(year_start),
        )

    def post(
        self, balance: LeaveBalance, posting: Posting, *, closing: bool = False
    ) -> tuple[LeaveTransaction, bool]:
        """Append ``posting`` and update ``balance``. Returns ``(row, created)``.

        An already-posted idempotency key returns the existing row untouched, which is what
        makes jobs and retried requests safe (INT-14). ``closing`` is set only by the year-end
        closure itself (LEV-INV-3).
        """
        existing = self._transactions.by_key(posting.idempotency_key)
        if existing is not None:
            return existing, False
        if balance.closure_status == "CLOSED" and not closing:
            raise LeaveYearClosed()
        row = LeaveTransaction(
            id=uuid7(),
            organization_id=balance.organization_id,
            employee_id=posting.employee_id,
            leave_type_id=posting.leave_type_id,
            leave_year_start=posting.year_start,
            transaction_type=posting.transaction_type,
            quantity_days=posting.quantity_days,
            entitled_quantity_days=posting.entitled_quantity_days,
            effective_date=posting.effective_date,
            idempotency_key=posting.idempotency_key,
            leave_application_id=posting.leave_application_id,
            application_action_id=posting.application_action_id,
            reverses_transaction_id=posting.reverses_transaction_id,
            year_closure_id=posting.year_closure_id,
            policy_version_id=posting.policy_version_id,
            accrual_period_start=posting.accrual_period_start,
            job_run_id=posting.job_run_id,
            reason=posting.reason,
            created_by=posting.actor_id,
        )
        self._transactions.add(row)
        self._transactions.flush()
        if posting.quantity_days > 0:
            balance.credited_days = balance.credited_days + posting.quantity_days
        else:
            balance.debited_days = balance.debited_days - posting.quantity_days
        balance.last_transaction_id = row.id
        return row, True


class LeaveLedgerService:
    """HTTP-facing use cases over the ledger: balances, history, opening balance, adjustments."""

    def __init__(
        self,
        *,
        ledger: LeaveLedger,
        balances: LeaveBalanceRepository,
        transactions: LeaveTransactionRepository,
        types: LeaveTypeRepository,
        employees: EmployeeRepository,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        audit: AuditWriter,
        codec: CursorCodec,
    ) -> None:
        self._codec = codec
        self._ledger = ledger
        self._balances = balances
        self._transactions = transactions
        self._types = types
        self._employees = employees
        self._organizations = organizations
        self._settings = settings
        self._audit = audit

    # ---------------------------------------------------------------- helpers

    def _org_settings(self, context: OrgContext) -> Any:
        return self._settings.require(context.organization_id)

    def leave_type(self, type_id: uuid.UUID) -> LeaveType:
        return self._types.require(type_id, resource="leave type")

    def _today(self, context: OrgContext) -> date:
        organization = self._organizations.get(context.organization_id)
        return today_in(organization.timezone if organization else None)

    def current_year_start(self, context: OrgContext) -> date:
        return leave_year_start(
            self._today(context), self._org_settings(context).leave_year_start_month
        )

    def _visible_employee(self, context: OrgContext, employee_id: uuid.UUID) -> Employee:
        """Own record under SELF, otherwise ``leave.balance_view`` inside the caller's scope."""
        employee = self._employees.get(employee_id)
        if employee is None:
            raise ResourceNotFound("employee")
        if context.is_self(employee_id) and context.has(Permission.SELF_LEAVE):
            return employee
        context.require(Permission.LEAVE_BALANCE_VIEW)
        if not employee_in_scope(context, self._employees, employee):
            raise ResourceNotFound("employee")
        return employee

    # ---------------------------------------------------------------- reads

    def balances(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        year_start: date | None,
        leave_type_id: uuid.UUID | None,
        as_of: date | None,
    ) -> tuple[date, list[tuple[LeaveBalance, LeaveType, Decimal | None]]]:
        """Materialized balances, or the §6.14 posted-basis reconstruction when ``as_of``."""
        self._visible_employee(context, employee_id)
        year = year_start or self.current_year_start(context)
        rows = list(self._balances.for_employee(employee_id, year))
        if leave_type_id:
            rows = [r for r in rows if r.leave_type_id == leave_type_id]
        types = {t.id: t for t in self._types.list_types()}
        out = []
        for row in rows:
            posted_as_of = (
                self._transactions.posted_as_of(employee_id, row.leave_type_id, year, as_of)
                if as_of
                else None
            )
            out.append((row, types[row.leave_type_id], posted_as_of))
        return year, out

    def list_transactions(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        leave_type_id: uuid.UUID | None,
        leave_year_start: date | None,
        transaction_types: list[str] | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[LeaveTransaction], str | None]:
        """Ledger history, keyset-paginated on (effective_date, id) ascending (§6.15)."""
        self._visible_employee(context, employee_id)
        statement = self._transactions.ledger_query(
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            leave_year_start=leave_year_start,
            transaction_types=transaction_types,
        )
        if cursor:
            payload = self._codec.decode(cursor)
            marker = date.fromisoformat(str(payload.sort_value))
            statement = statement.where(
                or_(
                    LeaveTransaction.effective_date > marker,
                    and_(
                        LeaveTransaction.effective_date == marker,
                        LeaveTransaction.id > payload.last_id,
                    ),
                )
            )
        statement = statement.order_by(LeaveTransaction.effective_date, LeaveTransaction.id).limit(
            limit + 1
        )
        rows = list(self._transactions.session.execute(statement).scalars())
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            self._codec.encode(
                sort_field="effective_date",
                sort_value=rows[-1].effective_date,
                last_id=rows[-1].id,
                direction=SortDirection.ASC,
            )
            if more
            else None
        )
        return rows, next_cursor

    def can_see_reasons(self, context: OrgContext) -> bool:
        """§10.5: SELF sees type and quantity but not administrative reasons."""
        return context.has(Permission.LEAVE_BALANCE_VIEW)

    # ---------------------------------------------------------------- administrative postings

    def _guard_adjustment(self, context: OrgContext, employee_id: uuid.UUID) -> Employee:
        context.require(Permission.LEAVE_BALANCE_ADJUST)
        if not self._org_settings(context).enable_leave_module:
            raise ModuleNotEnabled()
        if context.is_self(employee_id):
            raise SelfAdjustmentNotAllowed()
        employee = self._employees.get(employee_id)
        if employee is None:
            raise ResourceNotFound("employee")
        return employee

    def _assert_postable_year(self, context: OrgContext, year_start: date) -> None:
        settings = self._org_settings(context)
        if leave_year_start(year_start, settings.leave_year_start_month) != year_start:
            raise LeaveTransactionInvalid("leave_year_start is not the first day of a leave year.")
        tracking = settings.leave_tracking_start_date
        if tracking is not None and next_leave_year_start(year_start) <= tracking:
            raise LeaveTransactionInvalid(
                "The leave year ends before leave tracking starts (LEV-04)."
            )

    def post_admin_transaction(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        values: dict[str, Any],
        idempotency_key: uuid.UUID,
        acknowledged_warnings: list[str],
    ) -> tuple[LeaveTransaction, LeaveBalance]:
        """§6.13 opening balance and adjustments (``leave.balance_adjust``)."""
        employee = self._guard_adjustment(context, employee_id)
        kind = str(values["transaction_type"])
        if kind not in _ADMIN_TYPES:
            raise LeaveTransactionInvalid("Only opening balances and adjustments can be posted.")
        reason = str(values.get("reason") or "").strip()
        if len(reason) < REASON_MIN:
            raise ValidationFailed(
                f"A reason of at least {REASON_MIN} characters is required."
            ).with_detail(field="reason", code="TOO_SHORT", message="Explain the posting.")
        leave_type = self._types.require(values["leave_type_id"], resource="leave type")
        if not leave_type.tracks_balance:
            raise LeaveTransactionInvalid("The leave type does not track a balance.")
        year_start: date = values["leave_year_start"]
        self._assert_postable_year(context, year_start)
        quantity: Decimal = values["quantity_days"]
        if quantity == 0 or quantity % Decimal("0.01") != 0:
            raise LeaveTransactionInvalid(
                "quantity_days must be non-zero with at most two decimals."
            )
        if kind == "ADJUSTMENT_CREDIT" and quantity < 0:
            raise LeaveTransactionInvalid("A credit must be positive.")
        if kind == "ADJUSTMENT_DEBIT" and quantity > 0:
            raise LeaveTransactionInvalid("A debit must be negative.")
        if not year_start <= values["effective_date"] < next_leave_year_start(year_start):
            raise LeaveTransactionInvalid("effective_date is outside the leave year.")

        key = (
            f"OPN:{employee.id}:{leave_type.id}:{year_start}"
            if kind == "OPENING_BALANCE"
            else f"ADJ:{idempotency_key}"
        )
        balance = self._ledger.lock_balance(employee.id, leave_type.id, year_start)
        if kind == "OPENING_BALANCE" and self._transactions.by_key(key) is not None:
            raise OpeningBalanceExists()

        warnings = WarningCollector(acknowledged_warnings)
        warnings.add_if(
            kind == "ADJUSTMENT_DEBIT" and balance.posted_balance_days + quantity < 0,
            WarningCode.LEAVE_NEGATIVE_AFTER_ADJUSTMENT,
        )
        acknowledged = warnings.enforce()

        row, created = self._ledger.post(
            balance,
            Posting(
                employee_id=employee.id,
                leave_type_id=leave_type.id,
                year_start=year_start,
                transaction_type=kind,
                quantity_days=quantity,
                effective_date=values["effective_date"],
                idempotency_key=key,
                actor_id=context.user_id,
                reason=reason,
            ),
        )
        if created:
            self._audit_posting(context, row, acknowledged)
        return row, self._balances.reload(balance)

    def reverse(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        transaction_id: uuid.UUID,
        reason: str,
    ) -> tuple[LeaveTransaction, LeaveBalance]:
        employee = self._guard_adjustment(context, employee_id)
        reason = reason.strip()
        if len(reason) < REASON_MIN:
            raise ValidationFailed(
                f"A reason of at least {REASON_MIN} characters is required."
            ).with_detail(field="reason", code="TOO_SHORT", message="Explain the reversal.")
        target = self._transactions.get(transaction_id)
        if target is None or target.employee_id != employee.id:
            raise ResourceNotFound("leave transaction")
        if target.transaction_type not in _REVERSIBLE:
            raise LeaveTransactionInvalid("That transaction type cannot be reversed.")
        balance = self._ledger.lock_balance(
            employee.id, target.leave_type_id, target.leave_year_start
        )
        if self._transactions.reversal_of(target.id) is not None:
            raise LeaveTransactionAlreadyReversed()
        row, created = self._ledger.post(
            balance,
            Posting(
                employee_id=employee.id,
                leave_type_id=target.leave_type_id,
                year_start=target.leave_year_start,
                transaction_type=TransactionType.REVERSAL.value,
                quantity_days=-target.quantity_days,
                effective_date=target.effective_date,
                idempotency_key=f"REV:{target.id}",
                actor_id=context.user_id,
                reverses_transaction_id=target.id,
                reason=reason,
            ),
        )
        if created:
            self._audit_posting(context, row, [])
        return row, self._balances.reload(balance)

    def _audit_posting(
        self, context: OrgContext, row: LeaveTransaction, acknowledged: list[str]
    ) -> None:
        # The reason is administrative text: it goes in the audit ``reason`` column, which the
        # redaction rules (§20.3) treat as intentional, not in the diff payload.
        self._audit.record_change(
            context,
            action=AuditAction.LEAVE_TRANSACTION_POSTED,
            entity_type="leave_transaction",
            entity_id=row.id,
            employee_id=row.employee_id,
            after={
                "transaction_type": row.transaction_type,
                "quantity_days": str(row.quantity_days),
                "leave_year_start": row.leave_year_start,
            },
            reason=row.reason,
            acknowledged_warnings=acknowledged,
        )
