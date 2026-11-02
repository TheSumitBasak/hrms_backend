"""The §5.3 permission matrix, §5.4 scoping and field sets.

These tests encode the matrix independently of the implementation: each assertion names the
spec cell it checks, so a regression in `app/core/permissions.py` fails against §5.3 rather
than against itself.
"""

from __future__ import annotations

import pytest

from app.core.permissions import (
    ALL_PERMISSIONS,
    BREAK_GLASS_MAX_MINUTES,
    BREAK_GLASS_ROLE,
    DM_MAX_REPORTING_DEPTH,
    MINIMAL_PROFILE_FIELDS,
    ORG_ADMIN_GRANTABLE_ROLES,
    REPORTING_CYCLE_MAX_DEPTH,
    ROLE_CONFIGURABLE_PERMISSIONS,
    ROLE_PERMISSIONS,
    WORK_PROFILE_FIELDS,
    Permission,
    Role,
    configurable_permissions_for,
    permissions_for_role,
    resolve_permissions,
)

pytestmark = pytest.mark.unit


class TestSuperAdminHasNoEmployeeData:
    """§5.1/§5.3 note 1: SSA has **no employee data access by default**."""

    EMPLOYEE_DATA_PERMISSIONS = (
        Permission.EMPLOYEE_LIST,
        Permission.EMPLOYEE_VIEW,
        Permission.EMPLOYEE_VIEW_PERSONAL,
        Permission.EMPLOYEE_CREATE,
        Permission.EMPLOYEE_EDIT,
        Permission.IDENTITY_VIEW_MASKED,
        Permission.IDENTITY_REVEAL,
        Permission.BANK_VIEW_MASKED,
        Permission.BANK_REVEAL,
        Permission.SALARY_VIEW,
        Permission.DOCUMENT_DOWNLOAD,
    )

    @pytest.mark.parametrize("permission", EMPLOYEE_DATA_PERMISSIONS)
    def test_ssa_lacks_employee_data_permission(self, permission: Permission) -> None:
        resolved = resolve_permissions(frozenset({Role.SYSTEM_SUPER_ADMIN}))
        assert not resolved.has(permission), f"SSA must not hold {permission} by default (§5.3)"

    def test_ssa_cannot_gain_employee_data_through_configurable_cells(self) -> None:
        """No `C` cell exists for SSA, so enabling everything changes nothing."""
        resolved = resolve_permissions(
            frozenset({Role.SYSTEM_SUPER_ADMIN}), enabled_configurable=ALL_PERMISSIONS
        )
        for permission in self.EMPLOYEE_DATA_PERMISSIONS:
            assert not resolved.has(permission)

    def test_ssa_holds_platform_permissions(self) -> None:
        resolved = resolve_permissions(frozenset({Role.SYSTEM_SUPER_ADMIN}))
        assert resolved.has_all(
            Permission.ORG_CREATE,
            Permission.ORG_ARCHIVE,
            Permission.ORG_MANAGE,
            Permission.USER_MANAGE,
            Permission.AUDIT_VIEW,
        )

    def test_break_glass_is_a_bounded_hra_grant(self) -> None:
        """§5.3 note 1: access comes from a time-bound grant, not a wider SSA role."""
        assert BREAK_GLASS_ROLE is Role.HR_ADMIN
        assert BREAK_GLASS_MAX_MINUTES == 240


class TestMatrixCells:
    """Spot-checks of each §5.3 row that distinguishes two roles."""

    def test_hra_can_reveal_identifiers_and_bank(self) -> None:
        hra = resolve_permissions(frozenset({Role.HR_ADMIN}))
        assert hra.has_all(Permission.IDENTITY_REVEAL, Permission.BANK_REVEAL)

    def test_hre_cannot_reveal_identifiers_or_bank(self) -> None:
        """§5.1: HRE has "no reveal of full identifiers or bank numbers"."""
        hre = resolve_permissions(frozenset({Role.HR_EXECUTIVE}))
        assert hre.has(Permission.IDENTITY_VIEW_MASKED)
        assert hre.has(Permission.BANK_VIEW_MASKED)
        assert not hre.has(Permission.IDENTITY_REVEAL)
        assert not hre.has(Permission.BANK_REVEAL)

    def test_hre_cannot_reveal_even_when_every_c_cell_is_enabled(self) -> None:
        hre = resolve_permissions(
            frozenset({Role.HR_EXECUTIVE}), enabled_configurable=ALL_PERMISSIONS
        )
        assert not hre.has(Permission.IDENTITY_REVEAL)
        assert not hre.has(Permission.BANK_REVEAL)

    def test_only_hra_holds_restricted_status_transitions(self) -> None:
        """§5.3: terminate / absconding / status correction is HRA only."""
        for role in Role:
            resolved = resolve_permissions(frozenset({role}), enabled_configurable=ALL_PERMISSIONS)
            expected = role is Role.HR_ADMIN
            assert resolved.has(Permission.EMPLOYEE_STATUS_RESTRICTED) is expected

    def test_only_hra_can_archive_employees(self) -> None:
        for role in Role:
            resolved = resolve_permissions(frozenset({role}), enabled_configurable=ALL_PERMISSIONS)
            assert resolved.has(Permission.EMPLOYEE_ARCHIVE) is (role is Role.HR_ADMIN)

    def test_only_hra_overrides_duplicate_identifiers(self) -> None:
        for role in Role:
            resolved = resolve_permissions(frozenset({role}), enabled_configurable=ALL_PERMISSIONS)
            assert resolved.has(Permission.IDENTITY_DUPLICATE_OVERRIDE) is (role is Role.HR_ADMIN)

    def test_salary_confirm_cancel_backdate_correct_are_hra_only(self) -> None:
        """§5.3: confirm/cancel and "Backdate / correct revisions" are N for every other role."""
        hra_only = (
            Permission.SALARY_REVISION_CONFIRM,
            Permission.SALARY_REVISION_CANCEL,
            Permission.SALARY_REVISION_BACKDATE,
            Permission.SALARY_REVISION_CORRECT,
        )
        for role in Role:
            resolved = resolve_permissions(frozenset({role}), enabled_configurable=ALL_PERMISSIONS)
            for permission in hra_only:
                assert resolved.has(permission) is (role is Role.HR_ADMIN), (
                    f"{role} must{'' if role is Role.HR_ADMIN else ' not'} hold {permission}"
                )

    def test_oa_manages_configuration_but_not_employee_personal_data(self) -> None:
        oa = resolve_permissions(frozenset({Role.ORGANIZATION_ADMIN}))
        assert oa.has_all(
            Permission.ORG_MANAGE,
            Permission.MASTER_MANAGE,
            Permission.SALARY_TEMPLATE_MANAGE,
            Permission.IDRULE_MANAGE,
            Permission.EMPLOYEE_LIST,
            Permission.EMPLOYEE_VIEW,
        )
        # §5.1: "Non-sensitive employee read access."
        assert not oa.has(Permission.EMPLOYEE_VIEW_PERSONAL)
        assert not oa.has(Permission.EMPLOYEE_CREATE)
        assert not oa.has(Permission.SALARY_VIEW)
        assert not oa.has(Permission.IDENTITY_VIEW_MASKED)

    def test_rdr_downloads_restricted_documents_but_cannot_edit(self) -> None:
        rdr = resolve_permissions(frozenset({Role.RESTRICTED_DOCUMENT_REVIEWER}))
        assert rdr.has_all(
            Permission.DOCUMENT_DOWNLOAD,
            Permission.DOCUMENT_DOWNLOAD_RESTRICTED,
            Permission.DOCUMENT_VERIFY,
            Permission.IDENTITY_VIEW_MASKED,
            Permission.BGV_VIEW,
        )
        assert not rdr.has(Permission.DOCUMENT_UPLOAD)
        assert not rdr.has(Permission.DOCUMENT_ARCHIVE)
        assert not rdr.has(Permission.EMPLOYEE_EDIT)
        assert not rdr.has(Permission.SALARY_VIEW)

    def test_rv_and_dm_are_read_only(self) -> None:
        for role in (Role.READ_ONLY_VIEWER, Role.DEPARTMENT_MANAGER):
            resolved = resolve_permissions(frozenset({role}))
            employee_data = {p for p in resolved.permissions if p.value.startswith("employee.")}
            assert employee_data == {Permission.EMPLOYEE_LIST, Permission.EMPLOYEE_VIEW}
            # Addendum §10.3: no write permission of the new modules for these roles.
            writes = {
                Permission.ONBOARDING_MANAGE,
                Permission.ATTENDANCE_CORRECT,
                Permission.LEAVE_BALANCE_ADJUST,
                Permission.HOLIDAY_MANAGE,
            }
            assert not (resolved.permissions & writes)

    def test_self_role_confers_no_permission_code(self) -> None:
        """§5.6 is handled by a scope check, not by a permission (§5.3 omits SELF)."""
        assert resolve_permissions(frozenset({Role.EMPLOYEE_SELF})).permissions == frozenset()


class TestConfigurableCells:
    """`C` cells are off unless the organization enables them (DEC-004)."""

    def test_rdr_reveal_is_off_by_default(self) -> None:
        rdr = resolve_permissions(frozenset({Role.RESTRICTED_DOCUMENT_REVIEWER}))
        assert not rdr.has(Permission.IDENTITY_REVEAL)

    def test_rdr_reveal_can_be_enabled(self) -> None:
        rdr = resolve_permissions(
            frozenset({Role.RESTRICTED_DOCUMENT_REVIEWER}),
            enabled_configurable=frozenset({Permission.IDENTITY_REVEAL}),
        )
        assert rdr.has(Permission.IDENTITY_REVEAL)

    def test_hre_salary_view_is_configurable(self) -> None:
        """§5.3 and Q-22: HREs do not see salary by default."""
        assert not resolve_permissions(frozenset({Role.HR_EXECUTIVE})).has(Permission.SALARY_VIEW)
        enabled = resolve_permissions(
            frozenset({Role.HR_EXECUTIVE}),
            enabled_configurable=frozenset({Permission.SALARY_VIEW}),
        )
        assert enabled.has(Permission.SALARY_VIEW)

    def test_enabling_a_cell_for_one_role_does_not_leak_to_another(self) -> None:
        """`SALARY_VIEW` is a C cell for HRE only; enabling it must not widen RV."""
        rv = resolve_permissions(
            frozenset({Role.READ_ONLY_VIEWER}),
            enabled_configurable=frozenset({Permission.SALARY_VIEW}),
        )
        assert not rv.has(Permission.SALARY_VIEW)

    def test_override_cannot_grant_a_permission_outside_the_matrix(self) -> None:
        """A stale or hostile override row must not widen a role beyond §5.3."""
        for role in Role:
            resolved = permissions_for_role(role, enabled_configurable=ALL_PERMISSIONS)
            allowed = ROLE_PERMISSIONS.get(role, frozenset()) | ROLE_CONFIGURABLE_PERMISSIONS.get(
                role, frozenset()
            )
            assert resolved <= allowed

    def test_configurable_and_granted_sets_are_disjoint(self) -> None:
        """A cell cannot be both Y and C."""
        for role in Role:
            granted = ROLE_PERMISSIONS.get(role, frozenset())
            assert not (granted & configurable_permissions_for(role)), (
                f"{role} lists the same permission as both Y and C"
            )


class TestRoleUnionAndScoping:
    def test_permissions_are_the_union_of_roles(self) -> None:
        """§5.1: effective permissions are the union of the roles granted."""
        combined = resolve_permissions(frozenset({Role.HR_EXECUTIVE, Role.READ_ONLY_VIEWER}))
        assert combined.permissions == (
            ROLE_PERMISSIONS[Role.HR_EXECUTIVE] | ROLE_PERMISSIONS[Role.READ_ONLY_VIEWER]
        )

    def test_dm_alone_is_reporting_scoped(self) -> None:
        assert resolve_permissions(frozenset({Role.DEPARTMENT_MANAGER})).is_reporting_scoped

    def test_dm_plus_unscoped_role_is_not_scoped(self) -> None:
        """A DM who is also an RV sees the organization; the union rule widens the scope."""
        combined = resolve_permissions(frozenset({Role.DEPARTMENT_MANAGER, Role.READ_ONLY_VIEWER}))
        assert not combined.is_reporting_scoped

    def test_no_roles_is_not_scoped_and_holds_nothing(self) -> None:
        empty = resolve_permissions(frozenset())
        assert empty.permissions == frozenset()
        assert not empty.is_reporting_scoped

    def test_depth_limits_match_spec(self) -> None:
        assert DM_MAX_REPORTING_DEPTH == 20  # §5.4, Q-23
        assert REPORTING_CYCLE_MAX_DEPTH == 50  # §10.7 EMP-06

    def test_oa_cannot_grant_ssa(self) -> None:
        """§5.3 note 4."""
        assert Role.SYSTEM_SUPER_ADMIN not in ORG_ADMIN_GRANTABLE_ROLES
        assert Role.EMPLOYEE_SELF not in ORG_ADMIN_GRANTABLE_ROLES


class TestFieldAllowlists:
    """§5.4 field sets. "Visible fields: ... Nothing else"."""

    def test_dm_and_rv_see_the_work_profile_only(self) -> None:
        for role in (Role.DEPARTMENT_MANAGER, Role.READ_ONLY_VIEWER):
            resolved = resolve_permissions(frozenset({role}))
            assert resolved.employee_field_allowlist == WORK_PROFILE_FIELDS

    def test_rdr_sees_the_minimal_profile(self) -> None:
        resolved = resolve_permissions(frozenset({Role.RESTRICTED_DOCUMENT_REVIEWER}))
        assert resolved.employee_field_allowlist == MINIMAL_PROFILE_FIELDS

    def test_work_profile_excludes_personal_and_sensitive_fields(self) -> None:
        forbidden = {
            "date_of_birth",
            "personal_mobile",
            "personal_email",
            "father_name",
            "religion",
            "social_category",
            "disability_status",
            "remarks",
            "aadhaar_number",
            "pan",
        }
        assert not (WORK_PROFILE_FIELDS & forbidden)

    def test_minimal_profile_is_a_subset_of_the_work_profile(self) -> None:
        assert MINIMAL_PROFILE_FIELDS <= WORK_PROFILE_FIELDS

    def test_hra_has_no_allowlist_restriction(self) -> None:
        assert resolve_permissions(frozenset({Role.HR_ADMIN})).employee_field_allowlist is None

    def test_wider_role_wins_when_roles_are_combined(self) -> None:
        """§5.1 union rule: RDR plus RV yields the work profile, not the minimal profile."""
        combined = resolve_permissions(
            frozenset({Role.RESTRICTED_DOCUMENT_REVIEWER, Role.READ_ONLY_VIEWER})
        )
        assert combined.employee_field_allowlist == WORK_PROFILE_FIELDS


class TestMatrixIntegrity:
    def test_every_role_appears_in_the_matrix(self) -> None:
        assert set(ROLE_PERMISSIONS) == set(Role)

    def test_every_permission_is_held_by_at_least_one_role(self) -> None:
        """A permission no role can hold would be dead configuration."""
        reachable: set[Permission] = set()
        for role in Role:
            reachable |= permissions_for_role(role, enabled_configurable=ALL_PERMISSIONS)
        assert reachable == set(Permission)

    def test_permission_values_are_the_spec_codes(self) -> None:
        """§5.2 codes are the wire format; a typo here is an authorization bug."""
        assert Permission.EMPLOYEE_VIEW_PERSONAL.value == "employee.view_personal"
        assert Permission.SENSITIVE_RESTRICTED_PERSONAL.value == "sensitive.restricted_personal"
        assert Permission.DOCUMENT_DOWNLOAD_RESTRICTED.value == "document.download_restricted"
        assert Permission.SALARY_REVISION_BACKDATE.value == "salary.revision_backdate"

    def test_permission_set_is_immutable(self) -> None:
        resolved = resolve_permissions(frozenset({Role.READ_ONLY_VIEWER}))
        with pytest.raises((AttributeError, TypeError)):
            resolved.permissions = frozenset(Permission)  # type: ignore[misc]


class TestPayrollMatrix:
    """Payroll addendum 16.1 to 16.3."""

    def test_existing_roles_gain_no_payroll_permission_by_default(self) -> None:
        payroll = {p for p in Permission if p.value.split(".")[0] in {"payroll", "loan", "tax"}}
        for role in (Role.HR_ADMIN, Role.HR_EXECUTIVE, Role.DEPARTMENT_MANAGER):
            assert not ROLE_PERMISSIONS[role] & payroll

    def test_department_manager_never_reaches_payroll_even_when_enabled(self) -> None:
        reachable = permissions_for_role(
            Role.DEPARTMENT_MANAGER, enabled_configurable=ALL_PERMISSIONS
        )
        assert not any(
            p.value.split(".")[0] in {"payroll", "loan", "tax", "fnf"} for p in reachable
        )

    def test_no_single_role_can_both_calculate_and_approve(self) -> None:
        """Maker-checker (PAY-40): the processor and the approver are different roles."""
        for role in Role:
            held = permissions_for_role(role, enabled_configurable=ALL_PERMISSIONS)
            assert not {Permission.PAYROLL_CALCULATE, Permission.PAYROLL_APPROVE} <= held

    def test_system_super_admin_holds_only_the_rule_permissions(self) -> None:
        held = ROLE_PERMISSIONS[Role.SYSTEM_SUPER_ADMIN]
        assert Permission.STATUTORY_RULE_PREPARE in held
        assert Permission.STATUTORY_RULE_PUBLISH in held
        assert not {p for p in held if p.value.startswith(("payroll.", "tax.", "loan."))}

    def test_auditor_is_read_only(self) -> None:
        held = permissions_for_role(Role.PAYROLL_AUDITOR, enabled_configurable=ALL_PERMISSIONS)
        writes = {p for p in held if p.value.endswith(("_manage", "_create", ".approve", ".lock"))}
        assert not writes

    def test_only_finance_can_manage_payments(self) -> None:
        holders = {
            role
            for role in Role
            if Permission.PAYROLL_PAYMENT_MANAGE
            in permissions_for_role(role, enabled_configurable=ALL_PERMISSIONS)
        }
        assert holders == {Role.FINANCE_USER}

    def test_alumni_see_only_their_own_payslips_and_tax_documents(self) -> None:
        assert ROLE_PERMISSIONS[Role.EMPLOYEE_ALUMNI] == frozenset(
            {Permission.SELF_PAYSLIP, Permission.SELF_TAX_DOCUMENTS}
        )

    def test_payroll_self_service_is_an_organization_opt_in(self) -> None:
        assert Permission.SELF_PAYROLL not in ROLE_PERMISSIONS[Role.EMPLOYEE_SELF]
        assert Permission.SELF_PAYROLL in configurable_permissions_for(Role.EMPLOYEE_SELF)

    def test_role_codes_fit_the_column(self) -> None:
        assert all(2 <= len(role.value) <= 20 for role in Role)
