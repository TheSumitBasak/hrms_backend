"""Leave types, policies, policy versions and policy resolution (addendum §6.2-§6.4)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in, utc_now
from app.core.context import OrgContext
from app.core.errors import (
    DuplicateResource,
    InvalidStatusTransition,
    LeavePolicyConflict,
    LeavePolicyDraftExists,
    LeaveTypeNotApplicable,
    ModuleNotEnabled,
    PolicyVersionNotFuture,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.employee import Employee
from app.models.leave import (
    LeaveApplication,
    LeavePolicy,
    LeavePolicyVersion,
    LeaveTransaction,
    LeaveType,
    PolicyVersionStatus,
)
from app.repositories.employee import EmployeeRepository
from app.repositories.leave import (
    LeavePolicyRepository,
    LeavePolicyVersionRepository,
    LeaveTypeRepository,
)
from app.repositories.organization import (
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.services.organizations import assert_etag
from app.services.versioning import select_version

_VIEW = (Permission.LEAVE_VIEW, Permission.SELF_LEAVE, Permission.LEAVE_TYPE_MANAGE)
_APPLIES = (
    "branch_id",
    "department_id",
    "designation_id",
    "grade_id",
    "employment_type_id",
    "employee_category_id",
)
_VERSION_EDITABLE = frozenset(
    {
        "effective_from",
        "priority",
        *(f"applies_{name}" for name in _APPLIES),
        "applies_gender",
        "allocation_method",
        "annual_entitlement_days",
        "accrual_frequency",
        "accrual_timing",
        "joining_cutoff_day",
        "upfront_proration",
        "rounding_mode",
        "max_balance_days",
        "accrue_during_inactive",
        "carry_forward_enabled",
        "max_carry_forward_days",
        "negative_balance_year_end",
        "allow_negative_balance",
        "max_negative_balance_days",
        "reserve_pending_applications",
        "allow_half_day",
        "min_days_per_application",
        "max_days_per_application",
        "max_consecutive_days",
        "min_service_days",
        "allowed_during_probation",
        "allowed_during_notice",
        "min_notice_days",
        "max_backdate_days",
        "max_future_days",
        "count_weekly_offs",
        "count_holidays",
        "document_required_above_days",
        "max_applications_per_year",
        "approval_chain",
        "employee_cancellation_allowed",
        "change_note",
    }
)
_HALF_STEP_FIELDS = (
    "min_days_per_application",
    "max_days_per_application",
    "max_consecutive_days",
)


def _snapshot(row: Any, fields: list[str]) -> dict[str, Any]:
    return {f: getattr(row, f) for f in fields}


class LeaveConfigService:
    def __init__(
        self,
        *,
        types: LeaveTypeRepository,
        policies: LeavePolicyRepository,
        versions: LeavePolicyVersionRepository,
        employees: EmployeeRepository,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        audit: AuditWriter,
    ) -> None:
        self._types = types
        self._policies = policies
        self._versions = versions
        self._employees = employees
        self._organizations = organizations
        self._settings = settings
        self._audit = audit

    # ---------------------------------------------------------------- guards

    def _today(self, context: OrgContext) -> date:
        organization = self._organizations.get(context.organization_id)
        return today_in(organization.timezone if organization else None)

    def _require_module(self, context: OrgContext) -> None:
        if not self._settings.require(context.organization_id).enable_leave_module:
            raise ModuleNotEnabled()

    def _manage(self, context: OrgContext) -> None:
        context.require(Permission.LEAVE_TYPE_MANAGE)
        self._require_module(context)

    # ---------------------------------------------------------------- leave types

    def list_types(self, context: OrgContext, *, status: str | None) -> list[LeaveType]:
        context.require_any(*_VIEW)
        return list(self._types.list_types(status=status))

    def get_type(self, context: OrgContext, type_id: uuid.UUID) -> LeaveType:
        context.require_any(*_VIEW)
        return self._types.require(type_id, resource="leave type")

    def create_type(self, context: OrgContext, *, values: dict[str, Any]) -> LeaveType:
        self._manage(context)
        code = str(values["leave_type_code"])
        if self._types.code_exists(code):
            raise DuplicateResource("A leave type with that code already exists.")
        self._assert_type_rules(values)
        row = LeaveType(
            id=uuid7(),
            organization_id=context.organization_id,
            created_by=context.user_id,
            **values,
        )
        self._types.add(row)
        self._types.flush()
        self._audit.record_change(
            context,
            action=AuditAction.LEAVE_TYPE_CREATED,
            entity_type="leave_type",
            entity_id=row.id,
            after=_snapshot(
                row, ["leave_type_code", "leave_type_name", "leave_category", "tracks_balance"]
            ),
        )
        return row

    @staticmethod
    def _assert_type_rules(values: dict[str, Any]) -> None:
        if values.get("leave_category") == "OPTIONAL_HOLIDAY" and not values.get(
            "tracks_balance", True
        ):
            raise ValidationFailed(
                "An optional-holiday leave type must track a balance."
            ).with_detail(
                field="tracks_balance", code="INVALID", message="Must be true for OPTIONAL_HOLIDAY."
            )

    def update_type(
        self,
        context: OrgContext,
        *,
        type_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> LeaveType:
        self._manage(context)
        row = self._types.require(type_id, resource="leave type")
        assert_etag(row.row_version, if_match)
        immutable = {"leave_category", "tracks_balance"} & changes.keys()
        if any(changes[f] != getattr(row, f) for f in immutable) and self._types.is_referenced(
            row.id
        ):
            raise InvalidStatusTransition(
                "Category and balance tracking are locked once the type is in use."
            )
        self._assert_type_rules({**_snapshot(row, ["leave_category", "tracks_balance"]), **changes})
        before = {f: getattr(row, f) for f in changes}
        for field, value in changes.items():
            setattr(row, field, value)
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.LEAVE_TYPE_UPDATED,
            entity_type="leave_type",
            entity_id=row.id,
            before=before,
            after=dict(changes),
        )
        return row

    def set_type_status(
        self, context: OrgContext, *, type_id: uuid.UUID, status: str, reason: str
    ) -> LeaveType:
        self._manage(context)
        row = self._types.require(type_id, resource="leave type")
        if row.status == status:
            raise InvalidStatusTransition(f"The leave type is already {status}.")
        before = row.status
        row.status = status
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.LEAVE_TYPE_STATUS_CHANGED,
            entity_type="leave_type",
            entity_id=row.id,
            before={"status": before},
            after={"status": status},
            reason=reason,
        )
        return row

    # ---------------------------------------------------------------- policies

    def list_policies(
        self, context: OrgContext, *, leave_type_id: uuid.UUID | None, status: str | None
    ) -> tuple[list[LeavePolicy], dict[uuid.UUID, list[LeavePolicyVersion]]]:
        context.require_any(Permission.LEAVE_VIEW, Permission.LEAVE_TYPE_MANAGE)
        rows = list(self._policies.list_policies(leave_type_id=leave_type_id, status=status))
        return rows, self._versions.for_policies([r.id for r in rows])

    def get_policy(
        self, context: OrgContext, policy_id: uuid.UUID
    ) -> tuple[LeavePolicy, list[LeavePolicyVersion]]:
        context.require_any(Permission.LEAVE_VIEW, Permission.LEAVE_TYPE_MANAGE)
        policy = self._policies.require(policy_id, resource="leave policy")
        return policy, self._versions.for_policies([policy.id])[policy.id]

    def create_policy(
        self, context: OrgContext, *, values: dict[str, Any], version: dict[str, Any]
    ) -> tuple[LeavePolicy, list[LeavePolicyVersion]]:
        self._manage(context)
        leave_type = self._types.require(values["leave_type_id"], resource="leave type")
        if self._policies.code_exists(values["policy_code"]):
            raise DuplicateResource("A leave policy with that code already exists.")
        policy = LeavePolicy(
            id=uuid7(),
            organization_id=context.organization_id,
            created_by=context.user_id,
            **values,
        )
        self._policies.add(policy)
        self._policies.flush()
        draft = self._new_version(context, policy, leave_type, number=1, fields=version)
        self._audit.record_change(
            context,
            action=AuditAction.LEAVE_POLICY_CREATED,
            entity_type="leave_policy",
            entity_id=policy.id,
            after={"policy_code": policy.policy_code, "leave_type_id": str(policy.leave_type_id)},
        )
        return policy, [draft]

    def update_policy(
        self,
        context: OrgContext,
        *,
        policy_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> LeavePolicy:
        self._manage(context)
        policy = self._policies.require(policy_id, resource="leave policy")
        assert_etag(policy.row_version, if_match)
        before = {f: getattr(policy, f) for f in changes}
        for field, value in changes.items():
            setattr(policy, field, value)
        policy.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.LEAVE_POLICY_UPDATED,
            entity_type="leave_policy",
            entity_id=policy.id,
            before=before,
            after=dict(changes),
        )
        return policy

    def _new_version(
        self,
        context: OrgContext,
        policy: LeavePolicy,
        leave_type: LeaveType,
        *,
        number: int,
        fields: dict[str, Any],
    ) -> LeavePolicyVersion:
        self._assert_version_fields(fields, tracks_balance=leave_type.tracks_balance)
        version = LeavePolicyVersion(
            id=uuid7(),
            organization_id=context.organization_id,
            policy_id=policy.id,
            leave_type_id=policy.leave_type_id,
            version_number=number,
            status=PolicyVersionStatus.DRAFT.value,
            created_by=context.user_id,
            **fields,
        )
        self._versions.add(version)
        self._versions.flush()
        return version

    def create_version(
        self, context: OrgContext, *, policy_id: uuid.UUID, fields: dict[str, Any]
    ) -> LeavePolicyVersion:
        self._manage(context)
        policy = self._policies.lock(policy_id)
        existing = self._versions.for_policies([policy.id])[policy.id]
        if any(v.status == PolicyVersionStatus.DRAFT.value for v in existing):
            raise LeavePolicyDraftExists()
        leave_type = self._types.require(policy.leave_type_id, resource="leave type")
        return self._new_version(
            context,
            policy,
            leave_type,
            number=max(v.version_number for v in existing) + 1,
            fields=fields,
        )

    def update_version(
        self,
        context: OrgContext,
        *,
        policy_id: uuid.UUID,
        version_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> LeavePolicyVersion:
        self._manage(context)
        version = self._version(policy_id, version_id)
        assert_etag(version.row_version, if_match)
        if version.status != PolicyVersionStatus.DRAFT.value:
            raise InvalidStatusTransition("Only a DRAFT policy version can be edited.")
        leave_type = self._types.require(version.leave_type_id, resource="leave type")
        merged = {f: getattr(version, f) for f in _VERSION_EDITABLE} | changes
        self._assert_version_fields(merged, tracks_balance=leave_type.tracks_balance)
        for field, value in changes.items():
            setattr(version, field, value)
        version.updated_by = context.user_id
        return version

    def _version(self, policy_id: uuid.UUID, version_id: uuid.UUID) -> LeavePolicyVersion:
        version = self._versions.require(version_id, resource="policy version")
        if version.policy_id != policy_id:
            raise ValidationFailed("That version does not belong to the policy.")
        return version

    @staticmethod
    def _assert_version_fields(fields: dict[str, Any], *, tracks_balance: bool) -> None:
        """Cross-field rules of §6.3 that apply to a DRAFT as well as to a published version."""
        unknown = set(fields) - _VERSION_EDITABLE
        if unknown:
            raise ValidationFailed(f"Unknown policy fields: {', '.join(sorted(unknown))}.")
        method = fields.get("allocation_method", "NONE")
        if not tracks_balance and method != "NONE":
            raise ValidationFailed(
                "A type without balance tracking can only use method NONE."
            ).with_detail(field="allocation_method", code="INVALID", message="Use NONE.")
        for name in _HALF_STEP_FIELDS:
            value = fields.get(name)
            if value is not None and Decimal(value) % Decimal("0.50") != 0:
                raise ValidationFailed(f"{name} must be a multiple of 0.50.").with_detail(
                    field=name, code="NOT_HALF_DAY_MULTIPLE", message="Use steps of 0.50."
                )

    @staticmethod
    def _assert_publishable(version: LeavePolicyVersion) -> date:
        problems: list[tuple[str, str]] = []
        if version.effective_from is None:
            problems.append(("effective_from", "Required to publish."))
        if version.allocation_method != "NONE":
            if not version.annual_entitlement_days:
                problems.append(("annual_entitlement_days", "Required unless allocation is NONE."))
            if version.joining_cutoff_day is None:
                problems.append(("joining_cutoff_day", "Required unless allocation is NONE."))
        if version.allocation_method == "PERIODIC_ACCRUAL" and not (
            version.accrual_frequency and version.accrual_timing
        ):
            problems.append(("accrual_frequency", "Frequency and timing are required for accrual."))
        if version.allocation_method == "ANNUAL_UPFRONT" and not version.upfront_proration:
            problems.append(("upfront_proration", "Required for ANNUAL_UPFRONT."))
        if version.carry_forward_enabled and version.max_carry_forward_days is None:
            problems.append(("max_carry_forward_days", "Required when carry-forward is enabled."))
        if version.allow_negative_balance and not version.max_negative_balance_days:
            problems.append(
                ("max_negative_balance_days", "Required when negative balance is allowed.")
            )
        if problems:
            error = ValidationFailed("The policy version is incomplete.")
            for field, message in problems:
                error.with_detail(field=field, code="REQUIRED", message=message)
            raise error
        return version.effective_from  # type: ignore[return-value]  # checked above

    def publish_version(
        self,
        context: OrgContext,
        *,
        policy_id: uuid.UUID,
        version_id: uuid.UUID,
        if_match: int | None,
    ) -> LeavePolicyVersion:
        self._manage(context)
        # Lock the parent: with no exclusion constraints in MySQL this serialises LEV-05/06.
        self._policies.lock(policy_id)
        version = self._version(policy_id, version_id)
        assert_etag(version.row_version, if_match)
        if version.status != PolicyVersionStatus.DRAFT.value:
            raise InvalidStatusTransition("Only a DRAFT policy version can be published.")
        effective_from = self._assert_publishable(version)
        today = self._today(context)

        siblings = self._versions.for_policies([policy_id])[policy_id]
        previous = max(
            (v for v in siblings if v.status == PolicyVersionStatus.ACTIVE.value),
            key=lambda v: v.version_number,
            default=None,
        )
        if version.version_number >= 2 or previous is not None:
            if effective_from < today or (
                previous is not None
                and previous.effective_from is not None
                and effective_from <= previous.effective_from
            ):
                raise PolicyVersionNotFuture()
        elif effective_from < today and self._type_has_activity(version):
            raise PolicyVersionNotFuture()

        self._assert_no_conflict(version)
        if previous is not None:
            previous.effective_to = version.effective_from
            previous.updated_by = context.user_id
        version.status = PolicyVersionStatus.ACTIVE.value
        version.published_by = context.user_id
        version.published_at = utc_now()
        version.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.LEAVE_POLICY_VERSION_PUBLISHED,
            entity_type="leave_policy_version",
            entity_id=version.id,
            after={
                "policy_id": str(policy_id),
                "version_number": version.version_number,
                "effective_from": version.effective_from,
            },
        )
        return version

    def _type_has_activity(self, version: LeavePolicyVersion) -> bool:
        """LEV-05: version 1 may be backdated only before any ledger or application use."""
        session = self._versions.session
        for model, column in (
            (LeaveTransaction, LeaveTransaction.effective_date),
            (LeaveApplication, LeaveApplication.start_date),
        ):
            statement = select(model.id).where(
                model.organization_id == version.organization_id,
                model.leave_type_id == version.leave_type_id,
                column >= version.effective_from,
            )
            if session.execute(statement.limit(1)).first() is not None:
                return True
        return False

    def _assert_no_conflict(self, version: LeavePolicyVersion) -> None:
        """LEV-06: same type, same applicability tuple and priority, overlapping interval."""
        tuple_fields = [f"applies_{name}" for name in _APPLIES] + ["applies_gender"]
        for other in self._versions.active_for_type(version.leave_type_id):
            if other.policy_id == version.policy_id:
                continue
            same = all(getattr(other, f) == getattr(version, f) for f in tuple_fields)
            if not (same and other.priority == version.priority):
                continue
            if version.effective_from is None or other.effective_from is None:
                continue
            open_end = date.max
            if version.effective_from < (
                other.effective_to or open_end
            ) and other.effective_from < (version.effective_to or open_end):
                raise LeavePolicyConflict()

    def archive_version(
        self,
        context: OrgContext,
        *,
        policy_id: uuid.UUID,
        version_id: uuid.UUID,
        reason: str,
        if_match: int | None,
    ) -> LeavePolicyVersion:
        self._manage(context)
        self._policies.lock(policy_id)
        version = self._version(policy_id, version_id)
        assert_etag(version.row_version, if_match)
        if version.status != PolicyVersionStatus.ACTIVE.value:
            raise InvalidStatusTransition("Only an ACTIVE policy version can be archived.")
        version.status = PolicyVersionStatus.ARCHIVED.value
        version.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.LEAVE_POLICY_VERSION_ARCHIVED,
            entity_type="leave_policy_version",
            entity_id=version.id,
            reason=reason,
        )
        return version

    def version_by_id(self, version_id: uuid.UUID) -> LeavePolicyVersion:
        """The policy version an application snapshotted; internal, no permission check."""
        return self._versions.require(version_id, resource="policy version")

    # ---------------------------------------------------------------- resolution (§6.4)

    def resolve(
        self, context: OrgContext, *, employee_id: uuid.UUID, leave_type_id: uuid.UUID, on: date
    ) -> LeavePolicyVersion:
        context.require(Permission.LEAVE_VIEW)
        employee = self._employees.require(employee_id, resource="employee")
        return self.resolve_for(employee, leave_type_id, on)

    def resolve_for(
        self, employee: Employee, leave_type_id: uuid.UUID, on: date
    ) -> LeavePolicyVersion:
        # shortcut: attributes are the employee's current ones, not job history as of ``on``;
        # upgrade when historical resolution (EMS §10.8) is needed for back-dated runs.
        versions = self._versions.active_for_type(leave_type_id)
        codes = {v.policy_id: self._versions.get_policy_code(v.policy_id) for v in versions}
        chosen = select_version(
            versions,
            employee,
            on,
            attributes=tuple(f"{name}" for name in _APPLIES),
            policy_code=lambda v: codes[v.policy_id],
            extra=lambda v: v.applies_gender in (None, employee.gender),
        )
        if chosen is None:
            raise LeaveTypeNotApplicable()
        return chosen
