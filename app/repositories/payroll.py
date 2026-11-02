"""Repositories for payroll configuration (payroll addendum §4)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import select

from app.models.payroll import (
    PayGroup,
    PayrollPeriod,
    PayrollSettings,
    StatutoryRegistrationBranch,
    StatutoryRegistrationSetting,
)
from app.repositories.base import OrgScopedRepository


class PayrollSettingsRepository(OrgScopedRepository[PayrollSettings]):
    model = PayrollSettings

    def current(self) -> PayrollSettings | None:
        return self._session.execute(self.select()).scalar_one_or_none()


class PayGroupRepository(OrgScopedRepository[PayGroup]):
    model = PayGroup

    def list_groups(self, *, include_inactive: bool = False) -> Sequence[PayGroup]:
        statement = self.select().order_by(PayGroup.pay_group_code_normalized)
        if not include_inactive:
            statement = statement.where(PayGroup.status == "ACTIVE")
        return list(self._session.execute(statement).scalars())

    def get_by_code(self, code: str) -> PayGroup | None:
        statement = self.select().where(PayGroup.pay_group_code_normalized == code.strip().upper())
        return self._session.execute(statement).scalar_one_or_none()


class PayrollPeriodRepository(OrgScopedRepository[PayrollPeriod]):
    model = PayrollPeriod

    def list_periods(
        self,
        *,
        pay_group_id: uuid.UUID | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> Sequence[PayrollPeriod]:
        statement = self.select().order_by(PayrollPeriod.period_month, PayrollPeriod.pay_group_id)
        if pay_group_id is not None:
            statement = statement.where(PayrollPeriod.pay_group_id == pay_group_id)
        if start is not None:
            statement = statement.where(PayrollPeriod.period_month >= start)
        if end is not None:
            statement = statement.where(PayrollPeriod.period_month <= end)
        return list(self._session.execute(statement).scalars())

    def existing_months(self, pay_group_id: uuid.UUID) -> set[date]:
        statement = select(PayrollPeriod.period_month).where(
            PayrollPeriod.organization_id == self.organization_id,
            PayrollPeriod.pay_group_id == pay_group_id,
        )
        return set(self._session.execute(statement).scalars())


class StatutoryRegistrationSettingRepository(OrgScopedRepository[StatutoryRegistrationSetting]):
    model = StatutoryRegistrationSetting

    def for_registration(self, registration_id: uuid.UUID) -> StatutoryRegistrationSetting | None:
        statement = self.select().where(
            StatutoryRegistrationSetting.registration_id == registration_id
        )
        return self._session.execute(statement).scalar_one_or_none()


class StatutoryRegistrationBranchRepository(OrgScopedRepository[StatutoryRegistrationBranch]):
    model = StatutoryRegistrationBranch

    def for_setting(self, setting_id: uuid.UUID) -> Sequence[StatutoryRegistrationBranch]:
        statement = (
            self.select()
            .where(StatutoryRegistrationBranch.registration_setting_id == setting_id)
            .order_by(
                StatutoryRegistrationBranch.branch_id, StatutoryRegistrationBranch.effective_from
            )
        )
        return list(self._session.execute(statement).scalars())
