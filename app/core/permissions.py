"""Roles, permission codes and the §5.3 permission matrix.

This module is the **single source of truth** for authorization. Endpoints and services
declare the :class:`Permission` they need; they never branch on a role code, except for the
three places where the spec itself scopes by role (the Department Manager reporting tree and
the Read-only Viewer / Restricted Document Reviewer field sets, §5.4).

The matrix encodes §5.3 exactly:

* ``Y`` -> the role's entry in :data:`ROLE_PERMISSIONS`
* ``C`` -> the role's entry in :data:`ROLE_CONFIGURABLE_PERMISSIONS`; granted only when an
  Organization Admin enables it for that organization (stored in
  ``organization_role_permissions``, DEC-004)
* ``S`` -> granted but restricted to a subset of employees by :data:`SCOPED_ROLES`
* ``N`` -> absent from both sets
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Permission(StrEnum):
    """§5.2 permission codes. Values are the wire format stored in ``permissions``."""

    ORG_CREATE = "org.create"
    ORG_ARCHIVE = "org.archive"
    ORG_MANAGE = "org.manage"
    MASTER_MANAGE = "master.manage"

    EMPLOYEE_LIST = "employee.list"
    EMPLOYEE_VIEW = "employee.view"
    EMPLOYEE_VIEW_PERSONAL = "employee.view_personal"
    EMPLOYEE_CREATE = "employee.create"
    EMPLOYEE_EDIT = "employee.edit"
    EMPLOYEE_STATUS = "employee.status"
    EMPLOYEE_STATUS_RESTRICTED = "employee.status_restricted"
    EMPLOYEE_ARCHIVE = "employee.archive"
    EMPLOYEE_REMARKS = "employee.remarks"

    SENSITIVE_RESTRICTED_PERSONAL = "sensitive.restricted_personal"

    IDENTITY_VIEW_MASKED = "identity.view_masked"
    IDENTITY_REVEAL = "identity.reveal"
    IDENTITY_EDIT = "identity.edit"
    IDENTITY_DUPLICATE_OVERRIDE = "identity.duplicate_override"

    BANK_VIEW_MASKED = "bank.view_masked"
    BANK_REVEAL = "bank.reveal"
    BANK_EDIT = "bank.edit"
    BANK_VERIFY = "bank.verify"

    BGV_VIEW = "bgv.view"

    SALARY_VIEW = "salary.view"
    SALARY_TEMPLATE_MANAGE = "salary.template_manage"
    SALARY_REVISION_CREATE = "salary.revision_create"
    SALARY_REVISION_CONFIRM = "salary.revision_confirm"
    SALARY_REVISION_CANCEL = "salary.revision_cancel"
    SALARY_REVISION_APPROVE = "salary.revision_approve"
    SALARY_REVISION_BACKDATE = "salary.revision_backdate"
    SALARY_REVISION_CORRECT = "salary.revision_correct"
    SALARY_CUSTOM_COMPONENT = "salary.custom_component"

    IDRULE_MANAGE = "idrule.manage"
    EMPLOYEE_CODE_MANUAL = "employee.code_manual"
    EMPLOYEE_CODE_REASSIGN = "employee.code_reassign"

    DOCUMENT_UPLOAD = "document.upload"
    DOCUMENT_DOWNLOAD = "document.download"
    DOCUMENT_DOWNLOAD_RESTRICTED = "document.download_restricted"
    DOCUMENT_VERIFY = "document.verify"
    DOCUMENT_ARCHIVE = "document.archive"

    AUDIT_VIEW = "audit.view"
    USER_MANAGE = "user.manage"

    POSITION_VIEW = "position.view"
    POSITION_MANAGE = "position.manage"
    HEADCOUNT_VIEW = "headcount.view"
    HEADCOUNT_MANAGE = "headcount.manage"
    HEADCOUNT_APPROVE = "headcount.approve"

    # --- Onboarding, attendance and leave addendum §10.2 ---
    ONBOARDING_TEMPLATE_MANAGE = "onboarding.template_manage"
    ONBOARDING_VIEW = "onboarding.view"
    ONBOARDING_INITIATE = "onboarding.initiate"
    ONBOARDING_MANAGE = "onboarding.manage"
    ONBOARDING_TASK_EXECUTE = "onboarding.task_execute"
    ONBOARDING_TASK_REVIEW = "onboarding.task_review"
    ONBOARDING_TASK_WAIVE = "onboarding.task_waive"
    ONBOARDING_COMPLETE = "onboarding.complete"
    ONBOARDING_CANCEL = "onboarding.cancel"
    ONBOARDING_REOPEN = "onboarding.reopen"
    ONBOARDING_SELF_ACCESS_MANAGE = "onboarding.self_access_manage"
    HOLIDAY_MANAGE = "holiday.manage"
    ATTENDANCE_POLICY_MANAGE = "attendance.policy_manage"
    ATTENDANCE_SHIFT_ASSIGN = "attendance.shift_assign"
    ATTENDANCE_VIEW = "attendance.view"
    ATTENDANCE_PUNCH_MANAGE = "attendance.punch_manage"
    ATTENDANCE_IMPORT = "attendance.import"
    ATTENDANCE_FINALIZE = "attendance.finalize"
    ATTENDANCE_CORRECT = "attendance.correct"
    ATTENDANCE_REGULARIZE_APPROVE = "attendance.regularize_approve"
    ATTENDANCE_REGULARIZE_APPROVE_HR = "attendance.regularize_approve_hr"
    LEAVE_TYPE_MANAGE = "leave.type_manage"
    LEAVE_VIEW = "leave.view"
    LEAVE_BALANCE_VIEW = "leave.balance_view"
    LEAVE_APPLY_ON_BEHALF = "leave.apply_on_behalf"
    LEAVE_APPROVE = "leave.approve"
    LEAVE_APPROVE_HR = "leave.approve_hr"
    LEAVE_CANCEL_ANY = "leave.cancel_any"
    LEAVE_BALANCE_ADJUST = "leave.balance_adjust"
    LEAVE_ADMIN_CORRECT = "leave.admin_correct"
    LEAVE_PROCESS_RUN = "leave.process_run"
    SELF_ONBOARDING = "self.onboarding"
    SELF_ATTENDANCE = "self.attendance"
    SELF_LEAVE = "self.leave"


class Role(StrEnum):
    """§5.1 roles. Values are the ``role_code`` stored in ``roles``."""

    SYSTEM_SUPER_ADMIN = "SSA"
    ORGANIZATION_ADMIN = "OA"
    HR_ADMIN = "HRA"
    HR_EXECUTIVE = "HRE"
    DEPARTMENT_MANAGER = "DM"
    READ_ONLY_VIEWER = "RV"
    RESTRICTED_DOCUMENT_REVIEWER = "RDR"
    EMPLOYEE_SELF = "SELF"
    ONBOARDING_TASK_OWNER = "OTO"
    ATTENDANCE_INTEGRATION_CLIENT = "AIC"


# -------------------------------------------------------------------------------------
# §5.3 matrix — "Y" cells
# -------------------------------------------------------------------------------------

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    # SSA deliberately holds *no* employee-data permission. §5.3 note 1: employee data is
    # reachable only through a time-bound, audited break-glass grant, which issues a
    # temporary HRA-equivalent grant rather than widening this set.
    Role.SYSTEM_SUPER_ADMIN: frozenset(
        {
            Permission.ORG_CREATE,
            Permission.ORG_ARCHIVE,
            Permission.ORG_MANAGE,
            Permission.AUDIT_VIEW,  # note 3: platform-level events only
            Permission.USER_MANAGE,
        }
    ),
    Role.ORGANIZATION_ADMIN: frozenset(
        {
            Permission.ORG_MANAGE,
            Permission.MASTER_MANAGE,
            Permission.EMPLOYEE_LIST,
            Permission.EMPLOYEE_VIEW,
            Permission.SALARY_TEMPLATE_MANAGE,
            Permission.IDRULE_MANAGE,
            Permission.EMPLOYEE_CODE_MANUAL,
            Permission.EMPLOYEE_CODE_REASSIGN,
            Permission.AUDIT_VIEW,
            Permission.USER_MANAGE,  # note 4: restricted to non-SSA roles in its own orgs
            Permission.POSITION_VIEW,
            Permission.POSITION_MANAGE,
            Permission.HEADCOUNT_VIEW,
            Permission.HEADCOUNT_MANAGE,
            Permission.HEADCOUNT_APPROVE,
        }
    ),
    Role.HR_ADMIN: frozenset(
        {
            Permission.POSITION_VIEW,
            Permission.HEADCOUNT_VIEW,
            Permission.EMPLOYEE_LIST,
            Permission.EMPLOYEE_VIEW,
            Permission.EMPLOYEE_VIEW_PERSONAL,
            Permission.EMPLOYEE_CREATE,
            Permission.EMPLOYEE_EDIT,
            Permission.EMPLOYEE_STATUS,
            Permission.EMPLOYEE_STATUS_RESTRICTED,
            Permission.EMPLOYEE_ARCHIVE,
            Permission.EMPLOYEE_REMARKS,
            Permission.SENSITIVE_RESTRICTED_PERSONAL,
            Permission.IDENTITY_VIEW_MASKED,
            Permission.IDENTITY_REVEAL,
            Permission.IDENTITY_EDIT,
            Permission.IDENTITY_DUPLICATE_OVERRIDE,
            Permission.BANK_VIEW_MASKED,
            Permission.BANK_REVEAL,
            Permission.BANK_EDIT,
            Permission.BANK_VERIFY,
            Permission.BGV_VIEW,
            Permission.SALARY_VIEW,
            Permission.SALARY_TEMPLATE_MANAGE,
            Permission.SALARY_REVISION_CREATE,
            Permission.SALARY_REVISION_CONFIRM,
            Permission.SALARY_REVISION_CANCEL,
            Permission.SALARY_REVISION_APPROVE,
            Permission.SALARY_REVISION_BACKDATE,
            Permission.SALARY_REVISION_CORRECT,
            Permission.SALARY_CUSTOM_COMPONENT,
            Permission.EMPLOYEE_CODE_REASSIGN,
            Permission.DOCUMENT_UPLOAD,
            Permission.DOCUMENT_DOWNLOAD,
            Permission.DOCUMENT_DOWNLOAD_RESTRICTED,
            Permission.DOCUMENT_VERIFY,
            Permission.DOCUMENT_ARCHIVE,
            Permission.AUDIT_VIEW,
        }
    ),
    Role.HR_EXECUTIVE: frozenset(
        {
            Permission.POSITION_VIEW,
            Permission.EMPLOYEE_LIST,
            Permission.EMPLOYEE_VIEW,
            Permission.EMPLOYEE_VIEW_PERSONAL,
            Permission.EMPLOYEE_CREATE,
            Permission.EMPLOYEE_EDIT,
            Permission.EMPLOYEE_STATUS,
            Permission.IDENTITY_VIEW_MASKED,
            Permission.IDENTITY_EDIT,
            Permission.BANK_VIEW_MASKED,
            Permission.BANK_EDIT,
            Permission.DOCUMENT_UPLOAD,
            Permission.DOCUMENT_DOWNLOAD,
        }
    ),
    # "S" cells: granted, but every employee lookup is narrowed to the reporting tree.
    Role.DEPARTMENT_MANAGER: frozenset(
        {
            Permission.EMPLOYEE_LIST,
            Permission.EMPLOYEE_VIEW,
        }
    ),
    Role.READ_ONLY_VIEWER: frozenset(
        {
            Permission.EMPLOYEE_LIST,
            Permission.EMPLOYEE_VIEW,
        }
    ),
    Role.RESTRICTED_DOCUMENT_REVIEWER: frozenset(
        {
            Permission.EMPLOYEE_LIST,
            Permission.EMPLOYEE_VIEW,
            Permission.IDENTITY_VIEW_MASKED,
            Permission.BGV_VIEW,
            Permission.DOCUMENT_DOWNLOAD,
            Permission.DOCUMENT_DOWNLOAD_RESTRICTED,
            Permission.DOCUMENT_VERIFY,
        }
    ),
    # §5.6: self-view is not in the §5.3 matrix. It grants no permission code; access is
    # decided by the self-view scope check against the caller's own employee record.
    Role.EMPLOYEE_SELF: frozenset(),
}


# -------------------------------------------------------------------------------------
# §5.3 matrix — "C" cells (off by default, enablable per organization by an OA)
# -------------------------------------------------------------------------------------

ROLE_CONFIGURABLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ORGANIZATION_ADMIN: frozenset({Permission.SALARY_REVISION_APPROVE}),
    Role.HR_ADMIN: frozenset(
        {
            Permission.MASTER_MANAGE,
            Permission.IDRULE_MANAGE,
            Permission.EMPLOYEE_CODE_MANUAL,
            Permission.POSITION_MANAGE,
            Permission.HEADCOUNT_MANAGE,
            Permission.HEADCOUNT_APPROVE,
        }
    ),
    Role.HR_EXECUTIVE: frozenset(
        {
            Permission.EMPLOYEE_REMARKS,
            Permission.BANK_VERIFY,
            Permission.BGV_VIEW,
            Permission.SALARY_VIEW,
            Permission.SALARY_REVISION_CREATE,
            Permission.DOCUMENT_VERIFY,
            # Not in the §5.3 matrix: salary.custom_component appears only in §5.2 and
            # §17.3. Granted to HRA by default and configurable for HRE, the two roles that
            # can create revisions at all. See DECISIONS.md DEC-038.
            Permission.SALARY_CUSTOM_COMPONENT,
        }
    ),
    Role.RESTRICTED_DOCUMENT_REVIEWER: frozenset({Permission.IDENTITY_REVEAL}),
}

# -------------------------------------------------------------------------------------
# Onboarding, attendance and leave addendum §10.3. "A" (assigned) and "S" (scoped) cells are
# held permissions; the owning service narrows them to assigned items or the DM reporting tree
# (addendum §10.4), exactly as EMS §5.4 narrows the DM employee permissions.
# -------------------------------------------------------------------------------------

_ADDENDUM_Y: dict[Role, frozenset[Permission]] = {
    Role.ORGANIZATION_ADMIN: frozenset(
        {
            Permission.ONBOARDING_TEMPLATE_MANAGE,
            Permission.ONBOARDING_VIEW,
            Permission.ONBOARDING_SELF_ACCESS_MANAGE,
            Permission.HOLIDAY_MANAGE,
            Permission.ATTENDANCE_POLICY_MANAGE,
            Permission.ATTENDANCE_SHIFT_ASSIGN,
            Permission.LEAVE_TYPE_MANAGE,
            Permission.LEAVE_PROCESS_RUN,
        }
    ),
    Role.HR_ADMIN: frozenset(
        {
            Permission.ONBOARDING_TEMPLATE_MANAGE,
            Permission.ONBOARDING_VIEW,
            Permission.ONBOARDING_INITIATE,
            Permission.ONBOARDING_MANAGE,
            Permission.ONBOARDING_TASK_EXECUTE,
            Permission.ONBOARDING_TASK_REVIEW,
            Permission.ONBOARDING_TASK_WAIVE,
            Permission.ONBOARDING_COMPLETE,
            Permission.ONBOARDING_CANCEL,
            Permission.ONBOARDING_REOPEN,
            Permission.ONBOARDING_SELF_ACCESS_MANAGE,
            Permission.HOLIDAY_MANAGE,
            Permission.ATTENDANCE_POLICY_MANAGE,
            Permission.ATTENDANCE_SHIFT_ASSIGN,
            Permission.ATTENDANCE_VIEW,
            Permission.ATTENDANCE_PUNCH_MANAGE,
            Permission.ATTENDANCE_FINALIZE,
            Permission.ATTENDANCE_CORRECT,
            Permission.ATTENDANCE_REGULARIZE_APPROVE,
            Permission.ATTENDANCE_REGULARIZE_APPROVE_HR,
            Permission.LEAVE_TYPE_MANAGE,
            Permission.LEAVE_VIEW,
            Permission.LEAVE_BALANCE_VIEW,
            Permission.LEAVE_APPLY_ON_BEHALF,
            Permission.LEAVE_APPROVE,
            Permission.LEAVE_APPROVE_HR,
            Permission.LEAVE_CANCEL_ANY,
            Permission.LEAVE_BALANCE_ADJUST,
            Permission.LEAVE_ADMIN_CORRECT,
            Permission.LEAVE_PROCESS_RUN,
        }
    ),
    Role.HR_EXECUTIVE: frozenset(
        {
            Permission.ONBOARDING_VIEW,
            Permission.ONBOARDING_INITIATE,
            Permission.ONBOARDING_MANAGE,
            Permission.ONBOARDING_TASK_EXECUTE,
            Permission.ONBOARDING_TASK_REVIEW,
            Permission.ONBOARDING_COMPLETE,
            Permission.ATTENDANCE_SHIFT_ASSIGN,
            Permission.ATTENDANCE_VIEW,
            Permission.ATTENDANCE_PUNCH_MANAGE,
            Permission.ATTENDANCE_REGULARIZE_APPROVE,
            Permission.LEAVE_VIEW,
            Permission.LEAVE_BALANCE_VIEW,
            Permission.LEAVE_APPLY_ON_BEHALF,
            Permission.LEAVE_APPROVE,
        }
    ),
    Role.DEPARTMENT_MANAGER: frozenset(
        {
            Permission.ONBOARDING_VIEW,
            Permission.ONBOARDING_TASK_EXECUTE,
            Permission.ONBOARDING_TASK_REVIEW,
            Permission.ATTENDANCE_VIEW,
            Permission.ATTENDANCE_REGULARIZE_APPROVE,
            Permission.LEAVE_VIEW,
            Permission.LEAVE_APPROVE,
        }
    ),
    Role.RESTRICTED_DOCUMENT_REVIEWER: frozenset(
        {
            Permission.ONBOARDING_VIEW,
        }
    ),
    Role.ONBOARDING_TASK_OWNER: frozenset(
        {
            Permission.ONBOARDING_VIEW,
            Permission.ONBOARDING_TASK_EXECUTE,
            Permission.ONBOARDING_TASK_REVIEW,
        }
    ),
    Role.ATTENDANCE_INTEGRATION_CLIENT: frozenset(
        {
            Permission.ATTENDANCE_IMPORT,
        }
    ),
}

_ADDENDUM_C: dict[Role, frozenset[Permission]] = {
    Role.HR_EXECUTIVE: frozenset(
        {
            Permission.ONBOARDING_TEMPLATE_MANAGE,
            Permission.ONBOARDING_TASK_WAIVE,
            Permission.ONBOARDING_CANCEL,
            Permission.ONBOARDING_SELF_ACCESS_MANAGE,
            Permission.HOLIDAY_MANAGE,
            Permission.ATTENDANCE_FINALIZE,
            Permission.ATTENDANCE_REGULARIZE_APPROVE_HR,
            Permission.LEAVE_APPROVE_HR,
            Permission.LEAVE_CANCEL_ANY,
        }
    ),
    Role.ORGANIZATION_ADMIN: frozenset(
        {
            Permission.ATTENDANCE_VIEW,
            Permission.LEAVE_VIEW,
        }
    ),
    Role.READ_ONLY_VIEWER: frozenset(
        {
            Permission.ATTENDANCE_VIEW,
            Permission.LEAVE_VIEW,
        }
    ),
    Role.DEPARTMENT_MANAGER: frozenset(
        {
            Permission.LEAVE_BALANCE_VIEW,
        }
    ),
    Role.EMPLOYEE_SELF: frozenset(
        {
            Permission.SELF_ONBOARDING,
            Permission.SELF_ATTENDANCE,
            Permission.SELF_LEAVE,
        }
    ),
}


def _merge(
    base: dict[Role, frozenset[Permission]], extra: dict[Role, frozenset[Permission]]
) -> dict[Role, frozenset[Permission]]:
    merged = dict(base)
    for role, permissions in extra.items():
        merged[role] = merged.get(role, frozenset()) | permissions
    return merged


ROLE_PERMISSIONS = _merge(ROLE_PERMISSIONS, _ADDENDUM_Y)
ROLE_CONFIGURABLE_PERMISSIONS = _merge(ROLE_CONFIGURABLE_PERMISSIONS, _ADDENDUM_C)


SCOPED_ROLES: frozenset[Role] = frozenset({Role.DEPARTMENT_MANAGER})
"""Roles whose employee access is narrowed to a subset of the organization (§5.4)."""

DM_MAX_REPORTING_DEPTH = 20
"""§5.4 and Q-23: a DM sees reportees down to twenty levels."""

REPORTING_CYCLE_MAX_DEPTH = 50
"""§10.7 EMP-06: cycle detection walks up to fifty levels."""

BREAK_GLASS_MAX_MINUTES = 240
"""§5.3 note 1 and §24.4.8: a break-glass grant lasts at most four hours."""

BREAK_GLASS_MIN_REASON_LENGTH = 20
"""§5.3 note 1: the reason must be at least twenty characters."""

BREAK_GLASS_ROLE = Role.HR_ADMIN
"""A break-glass grant is an ordinary, expiring HRA grant, so every downstream permission
check and audit entry works unchanged; only its provenance differs (DEC-007)."""

ORG_ADMIN_GRANTABLE_ROLES: frozenset[Role] = frozenset(
    {
        Role.ORGANIZATION_ADMIN,
        Role.HR_ADMIN,
        Role.HR_EXECUTIVE,
        Role.DEPARTMENT_MANAGER,
        Role.READ_ONLY_VIEWER,
        Role.RESTRICTED_DOCUMENT_REVIEWER,
        Role.ONBOARDING_TASK_OWNER,
        Role.ATTENDANCE_INTEGRATION_CLIENT,
    }
)
"""§5.3 note 4: an OA may grant these roles, never SSA, and only in its own organizations."""

MFA_REQUIRED_ROLES: frozenset[Role] = frozenset(
    {
        Role.SYSTEM_SUPER_ADMIN,
        Role.ORGANIZATION_ADMIN,
        Role.HR_ADMIN,
        Role.RESTRICTED_DOCUMENT_REVIEWER,
    }
)
"""NFR-12. Enforced by the identity provider; the flag is surfaced for the access review."""


# -------------------------------------------------------------------------------------
# §5.4 field sets
# -------------------------------------------------------------------------------------

WORK_PROFILE_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "employee_code",
        "display_name",
        "photograph_document_id",
        "designation_id",
        "department_id",
        "sub_department_id",
        "branch_id",
        "work_location_id",
        "work_mode",
        "official_email",
        "joining_date",
        "status",
        "reporting_manager_employee_id",
    }
)
"""Exactly the fields §5.4 allows a Department Manager and a Read-only Viewer to see
("Nothing else"). §5.4 also lists "official phone (if any)", for which §10.5 defines no
column on ``employees``; see DECISIONS.md DEC-039."""

MINIMAL_PROFILE_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "employee_code",
        "display_name",
        "department_id",
        "designation_id",
        "status",
    }
)
"""§5.4: the minimal profile a Restricted Document Reviewer sees alongside documents."""

RESTRICTED_FIELD_PERMISSIONS: dict[str, Permission] = {
    "remarks": Permission.EMPLOYEE_REMARKS,
    "religion": Permission.SENSITIVE_RESTRICTED_PERSONAL,
    "social_category": Permission.SENSITIVE_RESTRICTED_PERSONAL,
    "disability_status": Permission.SENSITIVE_RESTRICTED_PERSONAL,
    "disability_type": Permission.SENSITIVE_RESTRICTED_PERSONAL,
    "disability_percentage": Permission.SENSITIVE_RESTRICTED_PERSONAL,
}
"""Per-field permission gates for ``employees`` columns flagged ``[R]`` in §10.4.

Fields listed here are **omitted** from a response when the caller lacks the permission,
never returned as ``null`` (§5.5)."""

PERSONAL_CONTACT_FIELDS: frozenset[str] = frozenset(
    {
        "date_of_birth",
        "personal_mobile",
        "alternate_mobile",
        "personal_email",
        "father_name",
        "mother_name",
        "spouse_name",
        "marital_status",
        "blood_group",
        "emergency_contact_name",
        "emergency_contact_relationship",
        "emergency_contact_phone",
        "emergency_contact_alternate_phone",
        "current_address",
        "permanent_address",
    }
)
"""Requires ``employee.view_personal`` (§5.2)."""


@dataclass(frozen=True, slots=True)
class PermissionSet:
    """The caller's effective permissions inside one organization.

    Built by :func:`resolve_permissions` from the caller's active grants for that
    organization and the organization's configurable-permission overrides. Immutable, so it
    can be passed freely into serialisers without risk of widening.
    """

    roles: frozenset[Role]
    permissions: frozenset[Permission]

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    def has_any(self, *permissions: Permission) -> bool:
        return any(p in self.permissions for p in permissions)

    def has_all(self, *permissions: Permission) -> bool:
        return all(p in self.permissions for p in permissions)

    @property
    def is_reporting_scoped(self) -> bool:
        """True when every role held is reporting-tree scoped (§5.4).

        A user who also holds an unscoped role (for example DM plus RV) is not scoped, since
        §5.1 makes effective permissions the union of the roles granted.
        """
        return bool(self.roles) and self.roles <= SCOPED_ROLES

    @property
    def employee_field_allowlist(self) -> frozenset[str] | None:
        """Field allowlist for employee payloads, or ``None`` for no allowlist restriction.

        §5.4 caps DM and RV at the work profile and RDR at the minimal profile. When a user
        holds several roles the widest applicable set wins, consistent with §5.1's union
        rule.
        """
        if self.has(Permission.EMPLOYEE_VIEW_PERSONAL) or self.has(Permission.EMPLOYEE_EDIT):
            return None
        narrow_roles = {
            Role.DEPARTMENT_MANAGER,
            Role.READ_ONLY_VIEWER,
            Role.RESTRICTED_DOCUMENT_REVIEWER,
        }
        if not self.roles or not self.roles <= narrow_roles:
            return None
        if self.roles & {Role.DEPARTMENT_MANAGER, Role.READ_ONLY_VIEWER}:
            return WORK_PROFILE_FIELDS
        return MINIMAL_PROFILE_FIELDS


def permissions_for_role(
    role: Role, *, enabled_configurable: frozenset[Permission]
) -> frozenset[Permission]:
    """Permissions a single role confers, given the organization's enabled ``C`` cells."""
    base = ROLE_PERMISSIONS.get(role, frozenset())
    configurable = ROLE_CONFIGURABLE_PERMISSIONS.get(role, frozenset())
    return base | (configurable & enabled_configurable)


def resolve_permissions(
    roles: frozenset[Role],
    *,
    enabled_configurable: frozenset[Permission] = frozenset(),
) -> PermissionSet:
    """Union the permissions of every role the caller holds in one organization (§5.1).

    ``enabled_configurable`` carries the ``C`` cells the organization has switched on. A
    permission that is not a ``C`` cell for a given role is never granted by this argument,
    so a stale or over-broad override row cannot widen a role beyond the §5.3 matrix.
    """
    granted: set[Permission] = set()
    for role in roles:
        granted |= permissions_for_role(role, enabled_configurable=enabled_configurable)
    return PermissionSet(roles=roles, permissions=frozenset(granted))


def configurable_permissions_for(role: Role) -> frozenset[Permission]:
    """The ``C`` cells an OA may switch on for ``role``."""
    return ROLE_CONFIGURABLE_PERMISSIONS.get(role, frozenset())


ALL_PERMISSIONS: frozenset[Permission] = frozenset(Permission)
ALL_ROLES: frozenset[Role] = frozenset(Role)

__all__ = [
    "ALL_PERMISSIONS",
    "ALL_ROLES",
    "BREAK_GLASS_MAX_MINUTES",
    "BREAK_GLASS_MIN_REASON_LENGTH",
    "BREAK_GLASS_ROLE",
    "DM_MAX_REPORTING_DEPTH",
    "MFA_REQUIRED_ROLES",
    "MINIMAL_PROFILE_FIELDS",
    "ORG_ADMIN_GRANTABLE_ROLES",
    "PERSONAL_CONTACT_FIELDS",
    "REPORTING_CYCLE_MAX_DEPTH",
    "RESTRICTED_FIELD_PERMISSIONS",
    "ROLE_CONFIGURABLE_PERMISSIONS",
    "ROLE_PERMISSIONS",
    "SCOPED_ROLES",
    "WORK_PROFILE_FIELDS",
    "Permission",
    "PermissionSet",
    "Role",
    "configurable_permissions_for",
    "permissions_for_role",
    "resolve_permissions",
]
