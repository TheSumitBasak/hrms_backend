"""Payroll settings, pay groups and the period calendar (payroll addendum §4, §15.2)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in
from app.core.context import OrgContext
from app.core.errors import (
    DuplicateResource,
    ModuleNotEnabled,
    PayrollCurrencyNotSupported,
    ResourceNotFound,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.core.warnings import WarningCode, WarningCollector
from app.db.types import uuid7
from app.models.organization import Organization, OrganizationSettings
from app.models.payroll import PayGroup, PayrollPeriod, PayrollSettings
from app.payroll.periods import add_months, derive_period, first_of_month
from app.repositories.organization import OrganizationRepository, OrganizationSettingsRepository
from app.repositories.payroll import (
    PayGroupRepository,
    PayrollPeriodRepository,
    PayrollSettingsRepository,
)
from app.services.organizations import assert_etag

PERIODS_AHEAD = 13
"""§4.4: the calendar job generates periods this many months ahead."""

_MODULE_FLAGS = (
    "enable_payroll_module",
    "enable_payroll_self_service",
    "enable_payroll_alumni_access",
)
_SETTINGS_FIELDS = (
    "payroll_go_live_period",
    "default_proration_basis",
    "component_rounding_default",
    "net_pay_rounding",
    "lop_requires_finalized_attendance",
    "lop_lookback_periods",
    "arrear_lookback_periods",
    "salary_hold_default_on_notice",
    "deduction_limit_percent",
    "maker_checker_mode",
    "one_time_tds_method",
    "bonus_accounting_year_start_month",
    "gratuity_provision_enabled",
    "overtime_pay_enabled",
    "maternity_pay_method",
    "payslip_zero_pay_generate",
    "alumni_access_months",
    "fnf_wage_payment_working_days",
)
_GROUP_CYCLE_FIELDS = ("input_window_mode", "input_cycle_start_day")


class PayrollConfigService:
    def __init__(
        self,
        *,
        settings: PayrollSettingsRepository,
        groups: PayGroupRepository,
        periods: PayrollPeriodRepository,
        organizations: OrganizationRepository,
        org_settings: OrganizationSettingsRepository,
        audit: AuditWriter,
    ) -> None:
        self._settings = settings
        self._groups = groups
        self._periods = periods
        self._organizations = organizations
        self._org_settings = org_settings
        self._audit = audit

    # ------------------------------------------------------------------ settings

    def get_settings(self, context: OrgContext) -> tuple[PayrollSettings, OrganizationSettings]:
        context.require(Permission.PAYROLL_VIEW)
        row = self._settings.current()
        if row is None:
            raise ResourceNotFound("payroll settings")
        return row, self._org_settings.require(context.organization_id)

    def replace_settings(
        self, context: OrgContext, values: dict[str, Any], *, if_match: int | None
    ) -> tuple[PayrollSettings, OrganizationSettings]:
        """``PUT Org/payroll-settings``: create on first call, replace afterwards.

        PCFG-01 / PCFG-03: the module can be switched on only with a settings row (this call
        writes it) and an INR organization.
        """
        context.require(Permission.PAYROLL_CONFIG_MANAGE)
        org_settings = self._org_settings.require(context.organization_id)
        if values["enable_payroll_module"]:
            organization = self._organization(context.organization_id)
            if organization.default_currency != "INR":
                raise PayrollCurrencyNotSupported()

        row = self._settings.current()
        opening = values["opening_ytd_required"]
        if opening is None:
            opening = values["payroll_go_live_period"].month != 4
        changes = {field: values[field] for field in _SETTINGS_FIELDS} | {
            "opening_ytd_required": opening
        }
        if row is None:
            row = PayrollSettings(
                id=uuid7(),
                organization_id=context.organization_id,
                created_by=context.user_id,
                **changes,
            )
            self._settings.add(row)
            before: dict[str, Any] = {}
        else:
            assert_etag(row.row_version, if_match)
            # shortcut: go-live is only immutable once a run exists (§4.2); the runs table
            # arrives with the processing engine, which adds the check here.
            before = {field: getattr(row, field) for field in changes}
            for field, value in changes.items():
                setattr(row, field, value)
            row.updated_by = context.user_id

        for flag in _MODULE_FLAGS:
            before[flag] = getattr(org_settings, flag)
            setattr(org_settings, flag, values[flag])
        # shortcut: disabling with runs in flight must return 422 PAYROLL_RUNS_IN_PROGRESS
        # (§4.1); there are no runs yet, so there is nothing to check.

        self._audit.record_change(
            context,
            action=AuditAction.PAYROLL_SETTINGS_UPDATED,
            entity_type="payroll_settings",
            entity_id=context.organization_id,
            before=before,
            after={**changes, **{flag: values[flag] for flag in _MODULE_FLAGS}},
            fields=[*changes, *_MODULE_FLAGS],
        )
        self._settings.flush()
        if org_settings.enable_payroll_module:
            self.generate_periods(context.organization_id, created_by=context.user_id)
        return row, org_settings

    # ---------------------------------------------------------------- pay groups

    def list_groups(
        self, context: OrgContext, *, include_inactive: bool = False
    ) -> Sequence[PayGroup]:
        context.require(Permission.PAYROLL_VIEW)
        return self._groups.list_groups(include_inactive=include_inactive)

    def get_group(self, context: OrgContext, group_id: uuid.UUID) -> PayGroup:
        context.require(Permission.PAYROLL_VIEW)
        return self._groups.require(group_id, resource="pay group")

    def create_group(self, context: OrgContext, values: dict[str, Any]) -> PayGroup:
        context.require(Permission.PAYROLL_CONFIG_MANAGE)
        self._require_module(context)
        if self._groups.get_by_code(values["pay_group_code"]) is not None:
            raise DuplicateResource("A pay group with that code already exists.").with_detail(
                field="pay_group_code", code="DUPLICATE", message="Pay group codes are unique."
            )
        group = PayGroup(id=uuid7(), status="ACTIVE", created_by=context.user_id, **values)
        self._groups.add(group)
        self._groups.flush()
        self._audit.record_change(
            context,
            action=AuditAction.PAY_GROUP_CREATED,
            entity_type="pay_group",
            entity_id=group.id,
            after={"pay_group_code": group.pay_group_code},
            fields=["pay_group_code"],
        )
        self.generate_periods(context.organization_id, created_by=context.user_id)
        return group

    def update_group(
        self,
        context: OrgContext,
        group_id: uuid.UUID,
        changes: dict[str, Any],
        *,
        if_match: int | None,
    ) -> PayGroup:
        context.require(Permission.PAYROLL_CONFIG_MANAGE)
        self._require_module(context)
        group = self._groups.require(group_id, resource="pay group")
        assert_etag(group.row_version, if_match)
        if any(field in changes for field in _GROUP_CYCLE_FIELDS):
            mode = changes.get("input_window_mode", group.input_window_mode)
            day = (
                changes.get("input_cycle_start_day")
                if "input_window_mode" in changes
                else changes.get("input_cycle_start_day", group.input_cycle_start_day)
            )
            if (mode == "OFFSET_CYCLE") != (day is not None):
                raise ValidationFailed(
                    "input_cycle_start_day is required for OFFSET_CYCLE and not allowed otherwise."
                )
        before = {field: getattr(group, field) for field in changes}
        for field, value in changes.items():
            setattr(group, field, value)
        group.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.PAY_GROUP_UPDATED,
            entity_type="pay_group",
            entity_id=group.id,
            before=before,
            after=dict(changes),
            fields=list(changes),
        )
        return group

    # ------------------------------------------------------------------- periods

    def list_periods(
        self,
        context: OrgContext,
        *,
        pay_group_id: uuid.UUID | None,
        start: date | None,
        end: date | None,
    ) -> Sequence[PayrollPeriod]:
        context.require(Permission.PAYROLL_VIEW)
        return self._periods.list_periods(pay_group_id=pay_group_id, start=start, end=end)

    def update_period(
        self,
        context: OrgContext,
        period_id: uuid.UUID,
        *,
        target_payment_date: date,
        acknowledged_warnings: list[str],
        if_match: int | None,
    ) -> PayrollPeriod:
        context.require(Permission.PAYROLL_CONFIG_MANAGE)
        self._require_module(context)
        period = self._periods.require(period_id, resource="payroll period")
        assert_etag(period.row_version, if_match)
        if period.status != "OPEN":
            raise ValidationFailed("Only an OPEN period can be edited.")
        warnings = WarningCollector(acknowledged_warnings)
        warnings.add_if(
            period.payment_due_date is not None and target_payment_date > period.payment_due_date,
            WarningCode.PAY_PAYDATE_LATE,
            due_date=period.payment_due_date,
        )
        warnings.enforce()
        before = {"target_payment_date": period.target_payment_date}
        period.target_payment_date = target_payment_date
        period.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.PAYROLL_PERIOD_UPDATED,
            entity_type="payroll_period",
            entity_id=period.id,
            before=before,
            after={"target_payment_date": target_payment_date},
            fields=["target_payment_date"],
        )
        return period

    def generate_periods(
        self, organization_id: uuid.UUID, *, created_by: uuid.UUID, today: date | None = None
    ) -> int:
        """Create the missing periods from go-live to ``PERIODS_AHEAD`` months out (§4.4).

        Idempotent: existing months are skipped, so the daily job and every settings or
        pay-group change can call it. Returns the number of periods created.
        """
        settings = self._settings.current()
        org_settings = self._org_settings.require(organization_id)
        if settings is None or not org_settings.enable_payroll_module:
            return 0
        organization = self._organization(organization_id)
        timezone = organization.timezone
        current = first_of_month(today or today_in(timezone))
        last = add_months(current, PERIODS_AHEAD)
        created = 0
        for group in self._groups.list_groups():
            existing = self._periods.existing_months(group.id)
            month = settings.payroll_go_live_period
            while month <= last:
                if month not in existing:
                    self._periods.add(self._new_period(group, month, timezone, created_by))
                    created += 1
                month = add_months(month, 1)
        self._periods.flush()
        return created

    # ------------------------------------------------------------------ internals

    def _new_period(
        self, group: PayGroup, month: date, timezone: str, created_by: uuid.UUID
    ) -> PayrollPeriod:
        dates = derive_period(
            month,
            input_window_mode=group.input_window_mode,
            input_cycle_start_day=group.input_cycle_start_day,
            input_cutoff_offset_days=group.input_cutoff_offset_days,
            target_payment_day=group.target_payment_day,
            pay_in_period=group.pay_in_period,
            timezone=timezone,
            # shortcut: the wage-payment due date comes from a published DUE_DATES rule
            # (PCFG-20, MW-10). The rule framework is not built yet, so it stays null.
            wage_payment_day=None,
        )
        return PayrollPeriod(
            id=uuid7(),
            organization_id=group.organization_id,
            pay_group_id=group.id,
            period_month=month,
            period_start=dates.period_start,
            period_end=dates.period_end,
            tax_year_start=dates.tax_year_start,
            input_window_start=dates.input_window_start,
            input_window_end=dates.input_window_end,
            input_cutoff_at=dates.input_cutoff_at,
            payment_due_date=dates.payment_due_date,
            target_payment_date=dates.target_payment_date,
            status="OPEN",
            created_by=created_by,
        )

    def _organization(self, organization_id: uuid.UUID) -> Organization:
        organization = self._organizations.get(organization_id)
        if organization is None:
            raise ResourceNotFound("organization")
        return organization

    def _require_module(self, context: OrgContext) -> None:
        if not self._org_settings.require(context.organization_id).enable_payroll_module:
            raise ModuleNotEnabled()


__all__ = ["PayrollConfigService"]
