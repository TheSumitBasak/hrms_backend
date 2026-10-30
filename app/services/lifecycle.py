"""Employee lifecycle side effects on leave and attendance (addendum 7.5, INT-36).

The EMS employee service calls these inside the transaction that changes the employee, so a
manager change, an employment end or an archive and its cross-module effects commit together.
Each hook is a no-op while the corresponding module is disabled.
"""

from __future__ import annotations

from datetime import date

from app.core.context import OrgContext
from app.models.employee import Employee
from app.services.attendance_regularization import RegularizationService
from app.services.leave_applications import LeaveApplicationService
from app.services.onboarding import OnboardingService

EMPLOYMENT_END = "EMPLOYMENT_END"
EMPLOYEE_ARCHIVED = "EMPLOYEE_ARCHIVED"


class LifecycleHooks:
    def __init__(
        self,
        *,
        leave: LeaveApplicationService,
        regularization: RegularizationService,
        onboarding: OnboardingService,
    ) -> None:
        self._leave = leave
        self._regularization = regularization
        self._onboarding = onboarding

    def on_manager_changed(self, context: OrgContext, employee: Employee) -> None:
        self._leave.reroute_for_manager_change(context, employee)
        self._regularization.reroute_for_manager_change(context, employee)

    def on_employment_end(self, context: OrgContext, employee: Employee, end_date: date) -> None:
        self._leave.system_cancel_for_employee(
            context, employee, reason_code=EMPLOYMENT_END, end_date=end_date
        )
        self._regularization.system_cancel_for_employee(
            context, employee, reason_code=EMPLOYMENT_END, end_date=end_date
        )

    def assert_can_activate(self, context: OrgContext, employee: Employee) -> None:
        self._onboarding.assert_can_activate(context, employee)

    def after_activation(self, context: OrgContext, employee: Employee) -> None:
        self._onboarding.after_activation(context, employee)

    def on_joining_date_changed(self, context: OrgContext, employee: Employee) -> None:
        self._onboarding.recompute_due_dates(context, employee)

    def on_separated(self, context: OrgContext, employee: Employee) -> None:
        self._onboarding.cancel_for_employee(context, employee, source="EMPLOYEE_SEPARATED")

    def on_archived(self, context: OrgContext, employee: Employee) -> None:
        self._onboarding.cancel_for_employee(context, employee, source="EMPLOYEE_ARCHIVED")
        self._leave.system_cancel_for_employee(
            context, employee, reason_code=EMPLOYEE_ARCHIVED, end_date=None
        )
        self._regularization.system_cancel_for_employee(
            context, employee, reason_code=EMPLOYEE_ARCHIVED, end_date=None
        )
