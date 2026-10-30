"""Branches, organization masters, lookups and custom statuses (§8, §9, §10.2)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.context import OrgContext
from app.core.errors import (
    DuplicateResource,
    ErrorDetail,
    MasterInUseByIdRule,
    ResourceNotFound,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.core.warnings import WarningCode, WarningCollector
from app.db.types import uuid7
from app.models.branch import Branch
from app.models.master import (
    EmployeeCustomStatus,
    EmploymentTypeBase,
    LookupList,
    LookupValue,
)
from app.models.org_config import OrganizationRegistration, RegistrationType
from app.repositories.branch import BranchRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.employee_code import IdRuleRepository
from app.repositories.master import (
    MASTER_REPOSITORIES,
    CustomStatusRepository,
    LookupRepository,
    master_repository,
)
from app.repositories.organization import (
    OrganizationRegistrationRepository,
    OrganizationRepository,
)
from app.repositories.reference import StateRepository
from app.services.organizations import assert_etag
from app.validation.normalizers import normalize_registration_number
from app.validation.rules import RuleId, check, validate_email

MASTER_TYPE_FIELDS: dict[str, frozenset[str]] = {
    "departments": frozenset({"head_employee_id"}),
    "sub-departments": frozenset({"department_id"}),
    "designations": frozenset({"default_grade_id"}),
    "grades": frozenset({"rank"}),
    "business-units": frozenset({"head_employee_id"}),
    "cost-centers": frozenset({"business_unit_id"}),
    "employment-types": frozenset({"base_type", "requires_end_date"}),
    "work-locations": frozenset(
        {
            "branch_id",
            "is_remote",
            "address_line_1",
            "address_line_2",
            "address_city",
            "address_district",
            "address_state",
            "address_state_code",
            "address_country",
            "address_postal_code",
        }
    ),
    "employee-categories": frozenset(),
}
"""§9.2: which extra fields each master accepts. A field outside its type's set is rejected."""

COMMON_MASTER_FIELDS = frozenset({"code", "name", "description", "display_order"})

REQUIRED_MASTER_FIELDS: dict[str, frozenset[str]] = {
    "sub-departments": frozenset({"department_id"}),
    "grades": frozenset({"rank"}),
    "employment-types": frozenset({"base_type"}),
}


class CatalogueService:
    """The organization configuration catalogue (§24.4.2)."""

    def __init__(
        self,
        *,
        branches: BranchRepository,
        masters: Any,
        lookups: LookupRepository,
        custom_statuses: CustomStatusRepository,
        registrations: OrganizationRegistrationRepository,
        organizations: OrganizationRepository,
        states: StateRepository,
        employees: EmployeeRepository,
        id_rules: IdRuleRepository,
        audit: AuditWriter,
    ) -> None:
        self._branches = branches
        self._masters = masters
        self._lookups = lookups
        self._custom_statuses = custom_statuses
        self._registrations = registrations
        self._organizations = organizations
        self._states = states
        self._employees = employees
        self._id_rules = id_rules
        self._audit = audit

    # ---------------------------------------------------------------- branches

    def list_branches(
        self, context: OrgContext, *, status: str | None = None, query: str | None = None
    ) -> Sequence[Branch]:
        context.require(Permission.EMPLOYEE_LIST)
        return self._branches.list_branches(status=status, query=query)

    def get_branch(self, context: OrgContext, branch_id: uuid.UUID) -> Branch:
        context.require(Permission.EMPLOYEE_LIST)
        return self._branches.require(branch_id, resource="branch")

    def create_branch(
        self,
        context: OrgContext,
        *,
        values: dict[str, Any],
        acknowledged_warnings: list[str],
    ) -> tuple[Branch, list[str]]:
        context.require(Permission.MASTER_MANAGE)
        if self._branches.get_by_code(str(values["branch_code"])) is not None:
            raise DuplicateResource("A branch with that code already exists.").with_detail(
                field="branch_code", code="DUPLICATE", message="Branch codes are unique."
            )
        warnings = WarningCollector(acknowledged_warnings)
        warnings.add_if(
            self._branches.name_exists(str(values["branch_name"])),
            WarningCode.BRANCH_NAME_DUPLICATE,
        )
        self._validate_address(values)
        acknowledged = warnings.enforce()

        branch = Branch(
            id=uuid7(),
            branch_code=str(values["branch_code"]),
            branch_name=str(values["branch_name"]),
            address_line_1=str(values["address_line_1"]),
            address_line_2=_str_or_none(values.get("address_line_2")),
            city=str(values["city"]),
            district=_str_or_none(values.get("district")),
            state=str(values["state"]),
            state_code=_str_or_none(values.get("state_code")),
            country=str(values.get("country") or "IN"),
            postal_code=str(values["postal_code"]),
            official_phone=_str_or_none(values.get("official_phone")),
            official_email=_str_or_none(values.get("official_email")),
            status="ACTIVE",
            created_by=context.user_id,
        )
        self._branches.add(branch)
        self._branches.flush()
        self._write_branch_registrations(context, branch, values)
        self._audit.record_change(
            context,
            action=AuditAction.BRANCH_CREATED,
            entity_type="branch",
            entity_id=branch.id,
            after={
                "branch_code": branch.branch_code,
                "branch_name": branch.branch_name,
                "status": branch.status,
            },
            fields=["branch_code", "branch_name", "status"],
            acknowledged_warnings=acknowledged,
        )
        return branch, acknowledged

    def update_branch(
        self,
        context: OrgContext,
        *,
        branch_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
        acknowledged_warnings: list[str],
    ) -> tuple[Branch, list[str]]:
        context.require(Permission.MASTER_MANAGE)
        branch = self._branches.require(branch_id, resource="branch")
        assert_etag(branch.row_version, if_match)

        warnings = WarningCollector(acknowledged_warnings)
        if "branch_name" in changes:
            warnings.add_if(
                self._branches.name_exists(str(changes["branch_name"])),
                WarningCode.BRANCH_NAME_DUPLICATE,
            )
        merged = {column: getattr(branch, column) for column in _BRANCH_FIELDS}
        merged.update({key: value for key, value in changes.items() if key in _BRANCH_FIELDS})
        self._validate_address(merged)
        if "state" in changes or "country" in changes:
            changes["state_code"] = merged.get("state_code")
        acknowledged = warnings.enforce()

        before = {field: getattr(branch, field) for field in changes}
        for field, value in changes.items():
            if field in _BRANCH_FIELDS:
                setattr(branch, field, value)
        branch.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.BRANCH_UPDATED,
            entity_type="branch",
            entity_id=branch.id,
            before=before,
            after={field: getattr(branch, field) for field in changes if field in _BRANCH_FIELDS},
            fields=[field for field in changes if field in _BRANCH_FIELDS],
            acknowledged_warnings=acknowledged,
        )
        return branch, acknowledged

    def set_branch_status(
        self,
        context: OrgContext,
        *,
        branch_id: uuid.UUID,
        active: bool,
        reason: str,
        acknowledged_warnings: list[str],
    ) -> tuple[Branch, list[str]]:
        context.require(Permission.MASTER_MANAGE)
        branch = self._branches.require(branch_id, resource="branch")
        target = "ACTIVE" if active else "INACTIVE"
        if branch.status == target:
            raise ValidationFailed(f"The branch is already {target.lower()}.")

        warnings = WarningCollector(acknowledged_warnings)
        if not active:
            count = self._employees.count_assigned_to_branch(branch.id)
            warnings.add_if(
                count > 0,
                WarningCode.BRANCH_DEACTIVATION_IN_USE,
                employee_count=count,
            )
        acknowledged = warnings.enforce()

        before = {"status": branch.status}
        branch.status = target
        branch.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.BRANCH_STATUS_CHANGED,
            entity_type="branch",
            entity_id=branch.id,
            before=before,
            after={"status": target},
            fields=["status"],
            reason=reason,
            acknowledged_warnings=acknowledged,
        )
        return branch, acknowledged

    def _write_branch_registrations(
        self, context: OrgContext, branch: Branch, values: dict[str, Any]
    ) -> None:
        """Persist the three §8.1 convenience fields into ``organization_registrations``.

        They are stored once there, with a ``branch_id``, and exposed on the branch as derived
        read-only fields (DEC-015). Nothing is duplicated onto the branch row.
        """
        organization = self._organizations.get(context.organization_id)
        organisation_pan = (organization.pan if organization else None) or None
        convenience: tuple[tuple[str, RegistrationType], ...] = (
            ("gstin", RegistrationType.GSTIN),
            ("shops_establishments_registration_number", RegistrationType.SHOPS_ESTABLISHMENTS),
            ("factory_license_number", RegistrationType.FACTORY_LICENSE),
        )
        for field, registration_type in convenience:
            raw = values.get(field)
            if not raw:
                continue
            number = normalize_registration_number(str(raw)) or str(raw).upper()
            rule = RuleId.GSTIN if registration_type is RegistrationType.GSTIN else RuleId.REGNO
            outcome = check(rule, number)
            if not outcome.ok:
                raise ValidationFailed(
                    details=[ErrorDetail(field, rule.value, outcome.message or "Invalid number.")]
                )
            state_code = branch.state_code
            if registration_type is RegistrationType.GSTIN:
                if state_code and number[:2] != state_code:
                    raise ValidationFailed(
                        details=[
                            ErrorDetail(
                                field,
                                "MISMATCH",
                                "The GSTIN state code must equal the branch state.",
                            )
                        ]
                    )
                if organisation_pan and number[2:12] != organisation_pan.upper():
                    raise ValidationFailed(
                        details=[
                            ErrorDetail(
                                field,
                                "MISMATCH",
                                "Characters 3-12 of the GSTIN must equal the organization PAN.",
                            )
                        ]
                    )
            duplicates = self._registrations.find_duplicates(
                registration_type=registration_type.value,
                registration_number=number,
                state_code=state_code,
                registration_label=None,
                system_wide=registration_type in _SYSTEM_WIDE,
                state_unique=registration_type in _STATE_UNIQUE,
                label_unique=False,
            )
            if duplicates:
                raise DuplicateResource(
                    "A registration with that number already exists."
                ).with_detail(field=field, code="DUPLICATE", message="Number already registered.")
            self._registrations.add(
                OrganizationRegistration(
                    id=uuid7(),
                    branch_id=branch.id,
                    registration_type=registration_type.value,
                    registration_number=number,
                    state_code=state_code,
                    status="ACTIVE",
                    created_by=context.user_id,
                )
            )

    def _validate_address(self, values: dict[str, Any]) -> None:
        country = str(values.get("country") or "IN")
        postal = values.get("postal_code")
        outcome = check(RuleId.POSTAL, str(postal) if postal else None, country=country)
        if not outcome.ok:
            raise ValidationFailed(
                details=[ErrorDetail("postal_code", RuleId.POSTAL.value, outcome.message)]
            )
        if country == "IN":
            state = self._states.get_by_name(str(values.get("state") or ""))
            if state is None:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "state",
                            "UNKNOWN_STATE",
                            "Must match an Indian state or union territory.",
                        )
                    ]
                )
            supplied = values.get("state_code")
            if supplied and str(supplied) != state.code:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "state_code",
                            "MISMATCH",
                            f"The GST state code for {state.name} is {state.code}.",
                        )
                    ]
                )
            values["state_code"] = state.code
        phone = values.get("official_phone")
        if phone:
            phone_outcome = check(RuleId.PHONE, str(phone))
            if not phone_outcome.ok:
                raise ValidationFailed(
                    details=[
                        ErrorDetail("official_phone", RuleId.PHONE.value, phone_outcome.message)
                    ]
                )
        email = values.get("official_email")
        if email:
            email_outcome = validate_email(str(email))
            if not email_outcome.ok:
                raise ValidationFailed(
                    details=[
                        ErrorDetail("official_email", RuleId.EMAIL.value, email_outcome.message)
                    ]
                )

    # ---------------------------------------------------------------- masters

    def list_masters(
        self,
        context: OrgContext,
        *,
        table_name: str,
        status: str | None = None,
        query: str | None = None,
    ) -> Sequence[Any]:
        context.require(Permission.EMPLOYEE_LIST)
        repository = self._master_repository(table_name)
        rows: Sequence[Any] = repository.list_masters(status=status, query=query)
        return rows

    def get_master(self, context: OrgContext, *, table_name: str, entity_id: uuid.UUID) -> Any:
        context.require(Permission.EMPLOYEE_LIST)
        return self._master_repository(table_name).require(entity_id, resource="master record")

    def create_master(
        self,
        context: OrgContext,
        *,
        table_name: str,
        values: dict[str, Any],
        acknowledged_warnings: list[str],
    ) -> tuple[Any, list[str]]:
        context.require(Permission.MASTER_MANAGE)
        repository = self._master_repository(table_name)
        self._validate_master_fields(table_name, values, creating=True)

        if repository.get_by_code(str(values["code"])) is not None:
            raise DuplicateResource("A record with that code already exists.").with_detail(
                field="code", code="DUPLICATE", message="Codes are unique within a master type."
            )
        warnings = WarningCollector(acknowledged_warnings)
        warnings.add_if(
            repository.name_exists(str(values["name"])),
            WarningCode.MASTER_NAME_DUPLICATE,
        )
        acknowledged = warnings.enforce()

        entity = repository.model(
            id=uuid7(),
            code=str(values["code"]),
            name=str(values["name"]),
            description=_str_or_none(values.get("description")),
            display_order=values.get("display_order"),
            status="ACTIVE",
            created_by=context.user_id,
            **self._master_extra_values(table_name, values),
        )
        repository.add(entity)
        repository.flush()
        self._audit.record_change(
            context,
            action=AuditAction.MASTER_CREATED,
            entity_type=table_name.rstrip("s"),
            entity_id=entity.id,
            after={"code": entity.code, "name": entity.name, "status": entity.status},
            fields=["code", "name", "status"],
            acknowledged_warnings=acknowledged,
        )
        return entity, acknowledged

    def update_master(
        self,
        context: OrgContext,
        *,
        table_name: str,
        entity_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
        acknowledged_warnings: list[str],
    ) -> tuple[Any, list[str]]:
        context.require(Permission.MASTER_MANAGE)
        repository = self._master_repository(table_name)
        entity = repository.require(entity_id, resource="master record")
        assert_etag(entity.row_version, if_match)
        self._validate_master_fields(table_name, changes, creating=False)

        warnings = WarningCollector(acknowledged_warnings)
        if "name" in changes:
            warnings.add_if(
                repository.name_exists(str(changes["name"])),
                WarningCode.MASTER_NAME_DUPLICATE,
            )
        acknowledged = warnings.enforce()

        updatable = {"name", "description", "display_order"} | MASTER_TYPE_FIELDS[table_name]
        applied = {key: value for key, value in changes.items() if key in updatable}
        before = {field: getattr(entity, field) for field in applied}
        for field, value in applied.items():
            setattr(entity, field, value)
        entity.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.MASTER_UPDATED,
            entity_type=table_name.rstrip("s"),
            entity_id=entity.id,
            before=before,
            after={field: getattr(entity, field) for field in applied},
            fields=list(applied),
            acknowledged_warnings=acknowledged,
        )
        return entity, acknowledged

    def set_master_status(
        self,
        context: OrgContext,
        *,
        table_name: str,
        entity_id: uuid.UUID,
        active: bool,
        reason: str,
        acknowledged_warnings: list[str],
    ) -> tuple[Any, list[str]]:
        context.require(Permission.MASTER_MANAGE)
        repository = self._master_repository(table_name)
        entity = repository.require(entity_id, resource="master record")
        target = "ACTIVE" if active else "INACTIVE"
        if entity.status == target:
            raise ValidationFailed(f"The record is already {target.lower()}.")

        if not active and table_name == "departments":
            in_use = self._id_rules.unarchived_rules_for_department(entity_id)
            if in_use:
                raise MasterInUseByIdRule(
                    "This department is used by an employee ID rule. End or archive the rule "
                    "before deactivating the department."
                )

        acknowledged = WarningCollector(acknowledged_warnings).enforce()
        before = {"status": entity.status}
        entity.status = target
        entity.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.MASTER_STATUS_CHANGED,
            entity_type=table_name.rstrip("s"),
            entity_id=entity.id,
            before=before,
            after={"status": target},
            fields=["status"],
            reason=reason,
            acknowledged_warnings=acknowledged,
        )
        return entity, acknowledged

    def _master_repository(self, table_name: str) -> Any:
        if table_name not in MASTER_REPOSITORIES:
            raise ResourceNotFound("master type")
        return master_repository(table_name, self._masters.session, self._masters.organization_id)

    @staticmethod
    def _validate_master_fields(table_name: str, values: dict[str, Any], *, creating: bool) -> None:
        allowed = COMMON_MASTER_FIELDS | MASTER_TYPE_FIELDS[table_name]
        unsupported = sorted(set(values) - allowed)
        if unsupported:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        field,
                        "NOT_APPLICABLE",
                        f"That field does not apply to {table_name}.",
                    )
                    for field in unsupported
                ]
            )
        if creating:
            details = [
                ErrorDetail(field, "REQUIRED", f"{field} is required.")
                for field in REQUIRED_MASTER_FIELDS.get(table_name, frozenset())
                if values.get(field) is None
            ]
            if details:
                raise ValidationFailed(details=details)
        if table_name == "work-locations" and creating:
            is_remote = bool(values.get("is_remote"))
            has_branch = values.get("branch_id") is not None
            has_address = bool(values.get("address_line_1") and values.get("address_city"))
            if not is_remote and not has_branch and not has_address:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "address_line_1",
                            "REQUIRED",
                            "A work location needs a branch, an address, or is_remote=true.",
                        )
                    ]
                )

    @staticmethod
    def _master_extra_values(table_name: str, values: dict[str, Any]) -> dict[str, Any]:
        extra = {field: values.get(field) for field in MASTER_TYPE_FIELDS[table_name]}
        if table_name == "employment-types" and extra.get("requires_end_date") is None:
            extra["requires_end_date"] = (
                extra.get("base_type") == EmploymentTypeBase.FIXED_TERM.value
            )
        if table_name == "work-locations" and extra.get("is_remote") is None:
            extra["is_remote"] = False
        return {key: value for key, value in extra.items() if value is not None}

    # ---------------------------------------------------------------- lookups

    def list_lookups(
        self, context: OrgContext, *, list_code: str | None = None, status: str | None = None
    ) -> Sequence[LookupValue]:
        context.require(Permission.EMPLOYEE_LIST)
        return self._lookups.list_values(list_code=list_code, status=status)

    def create_lookup(self, context: OrgContext, *, values: dict[str, Any]) -> LookupValue:
        context.require(Permission.MASTER_MANAGE)
        list_code = str(values["list_code"])
        value_code = str(values["value_code"])
        if list_code not in {member.value for member in LookupList}:
            raise ValidationFailed(
                details=[ErrorDetail("list_code", "UNKNOWN_LIST", "Unknown lookup list.")]
            )
        if self._lookups.get_value(list_code, value_code) is not None:
            raise DuplicateResource("That value already exists in this list.").with_detail(
                field="value_code", code="DUPLICATE", message="Value codes are unique per list."
            )
        lookup = LookupValue(
            id=uuid7(),
            list_code=list_code,
            value_code=value_code,
            label=str(values["label"]),
            display_order=int(values.get("display_order") or 0),
            status="ACTIVE",
            is_system_default=False,
            created_by=context.user_id,
        )
        self._lookups.add(lookup)
        self._audit.record_change(
            context,
            action=AuditAction.LOOKUP_CREATED,
            entity_type="lookup_value",
            entity_id=lookup.id,
            after={"list_code": list_code, "value_code": value_code, "label": lookup.label},
            fields=["list_code", "value_code", "label"],
        )
        return lookup

    def get_lookup(self, context: OrgContext, *, list_code: str, value_code: str) -> LookupValue:
        context.require(Permission.EMPLOYEE_LIST)
        lookup = self._lookups.get_value(list_code, value_code)
        if lookup is None:
            raise ResourceNotFound("lookup value")
        return lookup

    def update_lookup(
        self,
        context: OrgContext,
        *,
        list_code: str,
        value_code: str,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> LookupValue:
        context.require(Permission.MASTER_MANAGE)
        lookup = self._lookups.get_value(list_code, value_code)
        if lookup is None:
            raise ResourceNotFound("lookup value")
        assert_etag(lookup.row_version, if_match)

        if changes.get("status") == "INACTIVE" and (
            lookup.list_code == LookupList.GENDER.value and lookup.value_code == "UNDISCLOSED"
        ):
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "status",
                        "NOT_ALLOWED",
                        "UNDISCLOSED must remain selectable for every employee.",
                    )
                ]
            )

        before = {field: getattr(lookup, field) for field in changes}
        for field, value in changes.items():
            setattr(lookup, field, value)
        lookup.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.LOOKUP_UPDATED,
            entity_type="lookup_value",
            entity_id=lookup.id,
            before=before,
            after={field: getattr(lookup, field) for field in changes},
            fields=list(changes),
        )
        return lookup

    # ---------------------------------------------------------------- custom statuses

    def list_custom_statuses(self, context: OrgContext) -> Sequence[EmployeeCustomStatus]:
        context.require(Permission.MASTER_MANAGE)
        return self._custom_statuses.list_statuses()

    def create_custom_status(
        self, context: OrgContext, *, values: dict[str, Any]
    ) -> EmployeeCustomStatus:
        context.require(Permission.MASTER_MANAGE)
        code = str(values["code"])
        if self._custom_statuses.get_by_code(code) is not None:
            raise DuplicateResource("A custom status with that code already exists.").with_detail(
                field="code", code="DUPLICATE", message="Status codes are unique."
            )
        status = EmployeeCustomStatus(
            id=uuid7(),
            code=code,
            label=str(values["label"]),
            base_behaviour=str(values["base_behaviour"]),
            status="ACTIVE",
            created_by=context.user_id,
        )
        self._custom_statuses.add(status)
        self._audit.record_change(
            context,
            action=AuditAction.MASTER_CREATED,
            entity_type="employee_custom_status",
            entity_id=status.id,
            after={"code": code, "label": status.label, "base_behaviour": status.base_behaviour},
            fields=["code", "label", "base_behaviour"],
        )
        return status

    def update_custom_status(
        self,
        context: OrgContext,
        *,
        code: str,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> EmployeeCustomStatus:
        context.require(Permission.MASTER_MANAGE)
        status = self._custom_statuses.get_by_code(code)
        if status is None:
            raise ResourceNotFound("custom status")
        assert_etag(status.row_version, if_match)
        before = {field: getattr(status, field) for field in changes}
        for field, value in changes.items():
            setattr(status, field, value)
        status.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.MASTER_UPDATED,
            entity_type="employee_custom_status",
            entity_id=status.id,
            before=before,
            after={field: getattr(status, field) for field in changes},
            fields=list(changes),
        )
        return status


_BRANCH_FIELDS = (
    "branch_name",
    "address_line_1",
    "address_line_2",
    "city",
    "district",
    "state",
    "state_code",
    "country",
    "postal_code",
    "official_phone",
    "official_email",
)

_SYSTEM_WIDE = {
    RegistrationType.GSTIN,
    RegistrationType.PF_ESTABLISHMENT,
    RegistrationType.ESIC_EMPLOYER,
}
_STATE_UNIQUE = {
    RegistrationType.ESIC_SUB_CODE,
    RegistrationType.PT_REGISTRATION,
    RegistrationType.PT_ENROLMENT,
    RegistrationType.LWF,
}


def _str_or_none(value: object) -> str | None:
    return None if value is None else str(value)


__all__ = ["CatalogueService"]
