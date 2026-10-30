"""Shared two-level approval model (addendum §7.1, INT-30..INT-35).

Module-agnostic on purpose: leave applications use it now and attendance regularizations will
reuse it. It owns routing and actor authorization; each module owns its own state transitions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from app.core.context import OrgContext
from app.core.errors import (
    ApproverAlreadyActed,
    NotCurrentApprover,
    OrgAccessDenied,
    SelfApprovalNotAllowed,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.models.employee import SEPARATED_STATUSES, Employee
from app.repositories.employee import EmployeeRepository
from app.repositories.platform import UserRepository
from app.services.access import AccessService

MANAGER = "MANAGER"
HR_QUEUE = "HR_QUEUE"
OVERRIDE_COMMENT_MIN = 20


@dataclass(frozen=True, slots=True)
class Route:
    approver_type: str
    user_id: uuid.UUID | None
    employee_id: uuid.UUID | None
    fell_back: bool = False


class Approvable(Protocol):
    employee_id: uuid.UUID
    submitted_by: uuid.UUID
    current_approver_type: str | None
    current_approver_user_id: uuid.UUID | None


def levels_for(chain: str) -> list[str]:
    """INT-30: approver types per level for a chain."""
    return {
        "MANAGER_ONLY": [MANAGER],
        "HR_ONLY": [HR_QUEUE],
        "MANAGER_THEN_HR": [MANAGER, HR_QUEUE],
    }[chain]


class ApprovalRouter:
    def __init__(
        self,
        *,
        employees: EmployeeRepository,
        users: UserRepository,
        access: AccessService,
        organization_id: uuid.UUID,
    ) -> None:
        self._employees = employees
        self._users = users
        self._access = access
        self._organization_id = organization_id

    def route(
        self, employee: Employee, approver_type: str, *, manager_permissions: tuple[Permission, ...]
    ) -> Route:
        """INT-31: the manager if routable, otherwise the HR queue."""
        if approver_type == HR_QUEUE:
            return Route(HR_QUEUE, None, None)
        manager_id = employee.reporting_manager_employee_id
        manager = self._employees.get(manager_id) if manager_id else None
        if manager is not None and manager.status not in SEPARATED_STATUSES:
            user = self._users.get_active_for_employee(manager.id)
            if user is not None and self._holds_any(user.id, manager_permissions):
                return Route(MANAGER, user.id, manager.id)
        return Route(HR_QUEUE, None, None, fell_back=True)

    def _holds_any(self, user_id: uuid.UUID, permissions: tuple[Permission, ...]) -> bool:
        try:
            resolved = self._access.resolve(user_id, self._organization_id)
        except OrgAccessDenied:
            return False
        return resolved.permissions.has_any(*permissions)


def authorize_decision(
    context: OrgContext,
    request: Approvable,
    *,
    manager_permission: Permission,
    hr_permission: Permission,
    comment: str | None,
) -> bool:
    """INT-32/33: may the caller decide now? Returns True when acting as an HR override."""
    if request.submitted_by == context.user_id or context.is_self(request.employee_id):
        raise SelfApprovalNotAllowed()
    is_hr = context.has(hr_permission)
    if request.current_approver_type == HR_QUEUE:
        if is_hr:
            return False
        raise NotCurrentApprover()
    if request.current_approver_user_id == context.user_id and context.has(manager_permission):
        return False
    if is_hr:
        if not comment or len(comment.strip()) < OVERRIDE_COMMENT_MIN:
            raise ValidationFailed(
                f"An override needs a comment of at least {OVERRIDE_COMMENT_MIN} characters."
            ).with_detail(field="comment", code="REQUIRED", message="Explain the override.")
        return True
    raise NotCurrentApprover()


def assert_distinct_approver(actors: set[uuid.UUID], actor_id: uuid.UUID) -> None:
    """INT-34: the level-2 actor differs from every earlier actor on the request."""
    if actor_id in actors:
        raise ApproverAlreadyActed()


__all__ = [
    "HR_QUEUE",
    "MANAGER",
    "Approvable",
    "ApprovalRouter",
    "Route",
    "assert_distinct_approver",
    "authorize_decision",
    "employee_in_scope",
    "levels_for",
]


def employee_in_scope(
    context: OrgContext, employees: EmployeeRepository, employee: Employee
) -> bool:
    """EMS §5.4 for the new modules: own record, or inside the caller's reporting tree.

    Callers with no reporting scoping (HR roles) see everyone; a Department Manager sees the
    employees reporting to them, directly or through ``reporting_scope_depth`` levels.
    """
    if context.is_self(employee.id) or not context.is_reporting_scoped:
        return True
    if context.employee_id is None:
        return False
    cursor: Employee | None = employee
    for _ in range(context.reporting_scope_depth):
        manager_id = cursor.reporting_manager_employee_id if cursor else None
        if manager_id is None:
            return False
        if manager_id == context.employee_id:
            return True
        cursor = employees.get(manager_id)
    return False
