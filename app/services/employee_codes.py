"""Employee code rendering, rule selection and atomic issuance (§19)."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import (
    YearFormat as ClockYearFormat,
)
from app.core.clock import (
    render_year_token,
    sequence_scope_key,
    today_in,
    utc_now,
)
from app.core.context import OrgContext
from app.core.errors import (
    DuplicateEmployeeCode,
    DuplicateResource,
    ErrorDetail,
    IdRuleConflict,
    IdRuleImmutable,
    IdSequenceCollisionLimit,
    NoApplicableIdRule,
    ResourceNotFound,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.employee import Employee
from app.models.employee_code import (
    MAX_RENDERED_CODE_LENGTH,
    SELECTABLE_RULE_STATUSES,
    SEQUENCE_COLLISION_LIMIT,
    CodeRegistryStatus,
    CodeSource,
    EmployeeCodeRegistry,
    EmployeeIdGenerationRule,
    IdRuleStatus,
)
from app.repositories.employee import EmployeeRepository
from app.repositories.employee_code import (
    EmployeeCodeRegistryRepository,
    EmployeeIdSequenceRepository,
    IdRuleRepository,
)
from app.repositories.master import DepartmentRepository
from app.repositories.organization import (
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.services.organizations import assert_etag
from app.validation.normalizers import normalize_code

TOKENS = frozenset({"ORG", "DEPT", "PREFIX", "SUFFIX", "YEAR", "SEQ", "SEP"})
TOKEN_PATTERN = re.compile(r"\{([A-Z]+)\}")
LITERAL_PATTERN = re.compile(r"^[A-Z0-9/_-]*$")
CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9/_-]{0,29}$")

IMMUTABLE_AFTER_PUBLISH = frozenset(
    {
        "format_pattern",
        "prefix",
        "suffix",
        "separator",
        "year_format",
        "sequence_reset_policy",
        "sequence_group_code",
        "sequence_start",
        "sequence_increment",
        "sequence_padding_length",
        "department_id",
        "priority",
        "effective_from",
    }
)
"""§19.3 fields locked once a rule has issued a code or its start date has passed."""


class PatternError(ValueError):
    """A format pattern that cannot be rendered."""


def parse_pattern(pattern: str) -> list[tuple[str, str]]:
    """Split a format into ``("literal", text)`` and ``("token", NAME)`` parts."""
    parts: list[tuple[str, str]] = []
    position = 0
    for match in TOKEN_PATTERN.finditer(pattern):
        literal = pattern[position : match.start()]
        if literal:
            if not LITERAL_PATTERN.match(literal):
                raise PatternError("Literals may contain only A-Z, 0-9, '-' , '/' and '_'.")
            parts.append(("literal", literal))
        token = match.group(1)
        if token not in TOKENS:
            raise PatternError(f"Unknown token {{{token}}}.")
        parts.append(("token", token))
        position = match.end()
    trailing = pattern[position:]
    if trailing:
        if not LITERAL_PATTERN.match(trailing):
            raise PatternError("Literals may contain only A-Z, 0-9, '-', '/' and '_'.")
        parts.append(("literal", trailing))
    return parts


def validate_pattern(
    pattern: str,
    *,
    prefix: str | None,
    suffix: str | None,
    separator: str | None,
    year_format: str | None,
) -> list[ErrorDetail]:
    """Apply the §19.1 token rules, returning field-level errors."""
    try:
        parts = parse_pattern(pattern)
    except PatternError as error:
        return [ErrorDetail("format_pattern", "INVALID_PATTERN", str(error))]
    tokens = [value for kind, value in parts if kind == "token"]

    details: list[ErrorDetail] = []
    if tokens.count("SEQ") != 1:
        details.append(
            ErrorDetail("format_pattern", "SEQ_COUNT", "{SEQ} must appear exactly once.")
        )
    if "PREFIX" in tokens and not prefix:
        details.append(ErrorDetail("prefix", "REQUIRED", "{PREFIX} requires a prefix value."))
    if "SUFFIX" in tokens and not suffix:
        details.append(ErrorDetail("suffix", "REQUIRED", "{SUFFIX} requires a suffix value."))
    if "SEP" in tokens and not separator:
        details.append(ErrorDetail("separator", "REQUIRED", "{SEP} requires a separator."))
    if ("YEAR" in tokens) != (year_format is not None):
        details.append(
            ErrorDetail(
                "year_format",
                "YEAR_FORMAT",
                "year_format is required if and only if the pattern contains {YEAR}.",
            )
        )
    return details


def render_code(
    *,
    pattern: str,
    organization_code: str,
    department_code: str | None,
    prefix: str | None,
    suffix: str | None,
    separator: str | None,
    year_format: str | None,
    financial_year_start_month: int,
    basis_date: date,
    sequence: int,
    padding: int,
) -> str:
    """Render one code from a validated pattern (§19.2)."""
    rendered: list[str] = []
    for kind, value in parse_pattern(pattern):
        if kind == "literal":
            rendered.append(value)
            continue
        match value:
            case "ORG":
                rendered.append(organization_code)
            case "DEPT":
                if not department_code:
                    raise PatternError("{DEPT} requires the employee to have a department.")
                rendered.append(department_code)
            case "PREFIX":
                rendered.append(prefix or "")
            case "SUFFIX":
                rendered.append(suffix or "")
            case "SEP":
                rendered.append(separator or "")
            case "YEAR":
                rendered.append(
                    render_year_token(
                        basis_date,
                        ClockYearFormat(str(year_format)),
                        financial_year_start_month=financial_year_start_month,
                    )
                )
            case "SEQ":
                rendered.append(str(sequence).zfill(padding))
    code = "".join(rendered).upper()
    if len(code) > MAX_RENDERED_CODE_LENGTH or not CODE_PATTERN.match(code):
        raise PatternError("The rendered code does not match the §19.1 code format.")
    return code


class EmployeeCodeService:
    """Issue, preview and reassign employee codes (§19, §24.4.7)."""

    def __init__(
        self,
        *,
        rules: IdRuleRepository,
        sequences: EmployeeIdSequenceRepository,
        registry: EmployeeCodeRegistryRepository,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        departments: DepartmentRepository,
        employees: EmployeeRepository,
        audit: AuditWriter,
    ) -> None:
        self._rules = rules
        self._sequences = sequences
        self._registry = registry
        self._organizations = organizations
        self._settings = settings
        self._departments = departments
        self._employees = employees
        self._audit = audit

    # ---------------------------------------------------------------- rule reads

    def list_rules(
        self,
        context: OrgContext,
        *,
        department_id: uuid.UUID | None = None,
        status: Sequence[str] | None = None,
    ) -> Sequence[EmployeeIdGenerationRule]:
        context.require_any(Permission.IDRULE_MANAGE, Permission.EMPLOYEE_CREATE)
        rules = self._rules.list_rules(
            department_id=department_id, status=list(status) if status else None
        )
        return [rule for rule in rules if rule.status != IdRuleStatus.ARCHIVED.value]

    def get_rule(self, context: OrgContext, rule_id: uuid.UUID) -> EmployeeIdGenerationRule:
        context.require_any(Permission.IDRULE_MANAGE, Permission.EMPLOYEE_CREATE)
        return self._rules.require(rule_id, resource="ID rule")

    def last_issued_sequence(self, rule: EmployeeIdGenerationRule) -> int | None:
        organization = self._organizations.get(rule.organization_id)
        if organization is None:
            return None
        scope = sequence_scope_key(
            today_in(organization.timezone),
            rule.sequence_reset_policy,
            financial_year_start_month=organization.financial_year_start_month,
        )
        sequence = self._sequences.find(rule.sequence_group_code, scope)
        return sequence.last_issued_value if sequence else None

    # ---------------------------------------------------------------- rule writes

    def create_rule(
        self, context: OrgContext, *, values: dict[str, Any]
    ) -> EmployeeIdGenerationRule:
        context.require(Permission.IDRULE_MANAGE)
        self._validate_rule_values(values)
        code = normalize_code(str(values["rule_code"])) or str(values["rule_code"])
        if self._rules.get_by_code(code) is not None:
            raise DuplicateResource("An ID rule with that code already exists.").with_detail(
                field="rule_code", code="DUPLICATE", message="Rule codes are unique."
            )
        if values.get("department_id") is not None:
            self._departments.require(values["department_id"], resource="department")

        rule = EmployeeIdGenerationRule(
            id=uuid7(),
            rule_name=str(values["rule_name"]),
            rule_code=code,
            department_id=values.get("department_id"),
            prefix=values.get("prefix"),
            suffix=values.get("suffix"),
            format_pattern=str(values["format_pattern"]),
            separator=values.get("separator"),
            year_format=values.get("year_format"),
            sequence_reset_policy=str(values.get("sequence_reset_policy") or "NEVER"),
            sequence_group_code=normalize_code(str(values.get("sequence_group_code") or code)),
            sequence_start=int(values.get("sequence_start", 1)),
            sequence_increment=int(values.get("sequence_increment", 1)),
            sequence_padding_length=int(values.get("sequence_padding_length", 4)),
            effective_from=values["effective_from"],
            effective_to=values.get("effective_to"),
            priority=int(values.get("priority", 100)),
            status=IdRuleStatus.DRAFT.value,
            created_by=context.user_id,
        )
        self._rules.add(rule)
        self._rules.flush()
        self._audit.record_change(
            context,
            action=AuditAction.ID_RULE_CREATED,
            entity_type="employee_id_generation_rule",
            entity_id=rule.id,
            after=_rule_values(rule),
            fields=list(_rule_values(rule)),
        )
        return rule

    def update_rule(
        self,
        context: OrgContext,
        *,
        rule_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> EmployeeIdGenerationRule:
        context.require(Permission.IDRULE_MANAGE)
        rule = self._rules.require(rule_id, resource="ID rule")
        assert_etag(rule.row_version, if_match)

        if self._is_locked(rule):
            locked = sorted(set(changes) & IMMUTABLE_AFTER_PUBLISH)
            if locked:
                raise IdRuleImmutable(
                    f"This rule has issued codes or has started; {', '.join(locked)} is locked."
                )
        merged = {field: getattr(rule, field) for field in _RULE_FIELDS}
        merged.update(changes)
        self._validate_rule_values(merged)

        before = {field: getattr(rule, field) for field in changes}
        for field, value in changes.items():
            if field in _RULE_FIELDS:
                setattr(rule, field, value)
        rule.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.ID_RULE_UPDATED,
            entity_type="employee_id_generation_rule",
            entity_id=rule.id,
            before=before,
            after={field: getattr(rule, field) for field in changes if field in _RULE_FIELDS},
            fields=list(changes),
        )
        return rule

    def publish_rule(self, context: OrgContext, *, rule_id: uuid.UUID) -> EmployeeIdGenerationRule:
        context.require(Permission.IDRULE_MANAGE)
        rule = self._rules.require(rule_id, resource="ID rule")
        if rule.status == IdRuleStatus.ARCHIVED.value:
            raise ValidationFailed("An archived rule cannot be published.")
        self._validate_rule_values({field: getattr(rule, field) for field in _RULE_FIELDS})

        if rule.department_id is not None:
            department: Any = self._departments.require(rule.department_id, resource="department")
            if department.status != "ACTIVE":
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "department_id",
                            "INACTIVE",
                            "A rule cannot be published for an inactive department.",
                        )
                    ]
                )

        conflicts = self._conflicting_rules(rule)
        if conflicts:
            raise IdRuleConflict(
                "Another rule with the same department scope, priority and overlapping "
                "effective dates already exists."
            )

        organization = self._organizations.get(context.organization_id)
        today = today_in(organization.timezone if organization else None)
        rule.status = _derive_status(rule.effective_from, rule.effective_to, today)
        rule.published_at = utc_now()
        rule.published_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.ID_RULE_PUBLISHED,
            entity_type="employee_id_generation_rule",
            entity_id=rule.id,
            after={"status": rule.status, "published_at": rule.published_at},
            fields=["status"],
        )
        return rule

    def archive_rule(
        self, context: OrgContext, *, rule_id: uuid.UUID, reason: str
    ) -> EmployeeIdGenerationRule:
        context.require(Permission.IDRULE_MANAGE)
        rule = self._rules.require(rule_id, resource="ID rule")
        before = {"status": rule.status}
        rule.status = IdRuleStatus.ARCHIVED.value
        rule.archived_at = utc_now()
        rule.archived_by = context.user_id
        rule.archive_reason = reason
        self._audit.record_change(
            context,
            action=AuditAction.ID_RULE_ARCHIVED,
            entity_type="employee_id_generation_rule",
            entity_id=rule.id,
            before=before,
            after={"status": rule.status},
            fields=["status"],
            reason=reason,
        )
        return rule

    # ---------------------------------------------------------------- preview and issuance

    def preview(
        self,
        context: OrgContext,
        *,
        department_id: uuid.UUID | None,
        basis_date: date | None,
    ) -> tuple[EmployeeIdGenerationRule, int, str]:
        context.require_any(Permission.IDRULE_MANAGE, Permission.EMPLOYEE_CREATE)
        organization = self._organization(context)
        basis = basis_date or today_in(organization.timezone)
        rule = self._select_rule(department_id=department_id, basis_date=basis)
        sequence = self._sequences.find(
            rule.sequence_group_code,
            sequence_scope_key(
                basis,
                rule.sequence_reset_policy,
                financial_year_start_month=organization.financial_year_start_month,
            ),
        )
        last_issued = (
            sequence.last_issued_value
            if sequence
            else rule.sequence_start - rule.sequence_increment
        )
        next_value = last_issued + rule.sequence_increment
        code = self._render(rule, organization, department_id, basis, next_value)
        return rule, next_value, code

    def claim_manual(
        self, context: OrgContext, *, employee: Employee, code: str
    ) -> EmployeeCodeRegistry:
        """Register a manual code (§19.8): no rule, no sequence, uniqueness checked."""
        normalized = code.strip().upper()
        if not CODE_PATTERN.match(normalized):
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "employee_code", "INVALID", "The code does not match the code pattern."
                    )
                ]
            )
        if self._registry.get_by_code(normalized) is not None:
            raise DuplicateEmployeeCode()
        entry = EmployeeCodeRegistry(
            id=uuid7(),
            employee_code_normalized=normalized,
            employee_id=employee.id,
            source=CodeSource.MANUAL.value,
            status=CodeRegistryStatus.ASSIGNED.value,
            issued_by=context.user_id,
        )
        self._registry.add(entry)
        self._audit.record_change(
            context,
            action=AuditAction.EMPLOYEE_CODE_MANUAL,
            entity_type="employee",
            entity_id=employee.id,
            employee_id=employee.id,
            after={"employee_code": normalized},
            fields=["employee_code"],
        )
        return entry

    def issue_code(
        self,
        context: OrgContext,
        *,
        employee: Employee,
        basis_date: date,
    ) -> tuple[str, EmployeeIdGenerationRule]:
        """Issue a code inside the caller's transaction (§19.6).

        Locks only the sequence row, so the lock is held briefly and the employee transaction
        can still do its own writes.
        """
        organization = self._organization(context)
        rule = self._select_rule(department_id=employee.department_id, basis_date=basis_date)
        scope_key = sequence_scope_key(
            basis_date,
            rule.sequence_reset_policy,
            financial_year_start_month=organization.financial_year_start_month,
        )
        candidate = self._sequences.allocate_next(
            sequence_group_code=rule.sequence_group_code,
            scope_key=scope_key,
            sequence_start=rule.sequence_start,
            sequence_increment=rule.sequence_increment,
        )
        for _ in range(SEQUENCE_COLLISION_LIMIT):
            code = self._render(rule, organization, employee.department_id, basis_date, candidate)
            if self._registry.get_by_code(code) is None:
                self._registry.add(
                    EmployeeCodeRegistry(
                        id=uuid7(),
                        employee_code_normalized=code.upper(),
                        employee_id=employee.id,
                        source=CodeSource.GENERATED.value,
                        rule_id=rule.id,
                        sequence_group_code=rule.sequence_group_code,
                        scope_key=scope_key,
                        sequence_value=candidate,
                        status=CodeRegistryStatus.ASSIGNED.value,
                        issued_by=context.user_id,
                    )
                )
                return code, rule
            candidate += rule.sequence_increment
        raise IdSequenceCollisionLimit(
            "A thousand consecutive candidate codes were already registered."
        )

    def reassign_code(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        mode: str,
        new_code: str | None,
        reason: str,
        basis_date: date,
    ) -> tuple[Employee, str]:
        context.require(Permission.EMPLOYEE_CODE_REASSIGN)
        settings = self._settings.require(context.organization_id)
        employee = self._employees.lock(employee_id)

        if mode == "MANUAL":
            if not settings.allow_manual_employee_code or not context.has(
                Permission.EMPLOYEE_CODE_MANUAL
            ):
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "mode",
                            "NOT_ALLOWED",
                            "Manual codes are disabled or the caller lacks employee.code_manual.",
                        )
                    ]
                )
            if not new_code or not CODE_PATTERN.match(new_code):
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "new_code", "INVALID", "The code does not match the code pattern."
                        )
                    ]
                )
            code = new_code.upper()
            if self._registry.get_by_code(code) is not None:
                raise DuplicateEmployeeCode()
            self._registry.add(
                EmployeeCodeRegistry(
                    id=uuid7(),
                    employee_code_normalized=code,
                    employee_id=employee.id,
                    source=CodeSource.MANUAL.value,
                    status=CodeRegistryStatus.ASSIGNED.value,
                    issued_by=context.user_id,
                )
            )
        else:
            code, _ = self.issue_code(context, employee=employee, basis_date=basis_date)

        previous = employee.employee_code
        for entry in self._registry.list_for_employee(employee.id):
            if entry.status == CodeRegistryStatus.ASSIGNED.value and (
                entry.employee_code_normalized != code.upper()
            ):
                self._registry.retire(entry, reason=reason, user_id=context.user_id)
        employee.employee_code = code
        self._audit.record_change(
            context,
            action=AuditAction.EMPLOYEE_CODE_REASSIGNED,
            entity_type="employee",
            entity_id=employee.id,
            employee_id=employee.id,
            before={"employee_code": previous},
            after={"employee_code": code},
            fields=["employee_code"],
            reason=reason,
        )
        return employee, code

    def list_sequences(self, context: OrgContext) -> Sequence[Any]:
        context.require(Permission.IDRULE_MANAGE)
        return self._sequences.list_sequences()

    def search_registry(
        self,
        context: OrgContext,
        *,
        code: str | None = None,
        employee_id: uuid.UUID | None = None,
        status: str | None = None,
    ) -> Sequence[EmployeeCodeRegistry]:
        context.require(Permission.IDRULE_MANAGE)
        return self._registry.search(code=code, employee_id=employee_id, status=status)

    def code_history(
        self, context: OrgContext, employee_id: uuid.UUID
    ) -> Sequence[EmployeeCodeRegistry]:
        context.require(Permission.EMPLOYEE_LIST)
        self._employees.require(employee_id, resource="employee")
        return self._registry.list_for_employee(employee_id)

    # ---------------------------------------------------------------- internals

    def _organization(self, context: OrgContext):  # type: ignore[no-untyped-def]
        organization = self._organizations.get(context.organization_id)
        if organization is None or organization.archived_at is not None:
            raise ResourceNotFound("organization")
        return organization

    def _select_rule(
        self, *, department_id: uuid.UUID | None, basis_date: date
    ) -> EmployeeIdGenerationRule:
        candidates = [
            rule
            for rule in self._rules.list_rules(status=None)
            if rule.status in {status.value for status in SELECTABLE_RULE_STATUSES}
            and rule.effective_from <= basis_date
            and (rule.effective_to is None or basis_date < rule.effective_to)
            and (rule.department_id == department_id or rule.department_id is None)
        ]
        if not candidates:
            raise NoApplicableIdRule(
                "No employee ID rule applies to this department and basis date."
            )
        candidates.sort(
            key=lambda rule: (
                rule.department_id is None,
                rule.priority,
                -rule.effective_from.toordinal(),
                rule.rule_code,
            )
        )
        return candidates[0]

    def _render(
        self,
        rule: EmployeeIdGenerationRule,
        organization: Any,
        department_id: uuid.UUID | None,
        basis_date: date,
        sequence: int,
    ) -> str:
        department_code = None
        if department_id is not None:
            department: Any = self._departments.require(department_id, resource="department")
            department_code = department.code
        try:
            return render_code(
                pattern=rule.format_pattern,
                organization_code=organization.organization_code,
                department_code=department_code,
                prefix=rule.prefix,
                suffix=rule.suffix,
                separator=rule.separator,
                year_format=rule.year_format,
                financial_year_start_month=organization.financial_year_start_month,
                basis_date=basis_date,
                sequence=sequence,
                padding=rule.sequence_padding_length,
            )
        except PatternError as error:
            raise ValidationFailed(
                details=[ErrorDetail("format_pattern", "RENDER_FAILED", str(error))]
            ) from error

    def _is_locked(self, rule: EmployeeIdGenerationRule) -> bool:
        organization = self._organizations.get(rule.organization_id)
        today = today_in(organization.timezone if organization else None)
        return self._registry.count_for_rule(rule.id) > 0 or rule.effective_from <= today

    def _conflicting_rules(
        self, rule: EmployeeIdGenerationRule
    ) -> Sequence[EmployeeIdGenerationRule]:
        return [
            other
            for other in self._rules.list_rules(status=None)
            if other.id != rule.id
            and other.status not in {IdRuleStatus.DRAFT.value, IdRuleStatus.ARCHIVED.value}
            and other.department_id == rule.department_id
            and other.priority == rule.priority
            and other.effective_from < (rule.effective_to or date.max)
            and (other.effective_to is None or rule.effective_from < other.effective_to)
        ]

    @staticmethod
    def _validate_rule_values(values: dict[str, Any]) -> None:
        details = validate_pattern(
            str(values["format_pattern"]),
            prefix=values.get("prefix"),
            suffix=values.get("suffix"),
            separator=values.get("separator"),
            year_format=values.get("year_format"),
        )
        if (
            values.get("effective_to") is not None
            and values.get("effective_from") is not None
            and values["effective_to"] <= values["effective_from"]
        ):
            details.append(
                ErrorDetail(
                    "effective_to", "INVALID_INTERVAL", "effective_to must be after effective_from."
                )
            )
        if details:
            raise ValidationFailed(details=details)


_RULE_FIELDS = (
    "rule_name",
    "rule_code",
    "department_id",
    "prefix",
    "suffix",
    "format_pattern",
    "separator",
    "year_format",
    "sequence_reset_policy",
    "sequence_group_code",
    "sequence_start",
    "sequence_increment",
    "sequence_padding_length",
    "effective_from",
    "effective_to",
    "priority",
)


def _derive_status(effective_from: date, effective_to: date | None, today: date) -> str:
    if effective_from > today:
        return IdRuleStatus.SCHEDULED.value
    if effective_to is not None and effective_to <= today:
        return IdRuleStatus.EXPIRED.value
    return IdRuleStatus.ACTIVE.value


def _rule_values(rule: EmployeeIdGenerationRule) -> dict[str, object]:
    return {
        "rule_name": rule.rule_name,
        "rule_code": rule.rule_code,
        "department_id": rule.department_id,
        "format_pattern": rule.format_pattern,
        "effective_from": rule.effective_from,
        "effective_to": rule.effective_to,
        "priority": rule.priority,
        "status": rule.status,
    }


__all__ = [
    "EmployeeCodeService",
    "PatternError",
    "parse_pattern",
    "render_code",
    "validate_pattern",
]
