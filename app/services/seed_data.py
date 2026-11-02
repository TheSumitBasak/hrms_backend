"""The reference and configuration data the specification says to seed.

Two scopes:

**Platform** (:func:`seed_platform`) — the §5 roles and permissions, the §21.2 identifier
validation configuration, and the §23.2 ``states`` list with GST state codes. Idempotent, so
it can run on every deployment.

**Per organization** (:func:`seed_organization`) — what §24.4.1 requires organization creation
to seed: "settings, lookups, categories, employment types, policies". Runs inside the
organization-creation transaction, so an organization never exists in a half-configured state.

Nothing here invents business data. Every value is either named in the spec or, for the
`SEED_*` suggestions of §16.3, explicitly optional and deletable before use. **No
realistic personal data is created** (§9 of the brief): there are no sample employees.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.masking import IdentifierType
from app.core.permissions import ALL_PERMISSIONS, ROLE_PERMISSIONS, Permission
from app.db.types import uuid7
from app.models.document import ALLOWED_MIME_TYPES, DocumentCategory, DocumentCategoryMimeType
from app.models.master import EmploymentType, EmploymentTypeBase, LookupList, LookupValue
from app.models.org_config import (
    DEFAULT_IDENTIFIER_REQUIREMENTS,
    DEFAULT_NOMINATION_CONFIGS,
    ChecksumAlgorithm,
    IdentifierRequirementPolicy,
    IdentifierTypeConfig,
    NominationType,
    NominationTypeConfig,
    NormalizationStrategy,
)
from app.models.organization import Organization, OrganizationSettings
from app.models.platform import Permission as PermissionRow
from app.models.platform import Role as RoleRow
from app.models.platform import RolePermission
from app.models.reference import State
from app.models.salary import (
    BASIC_COMPONENT_CODE,
    AmountPeriod,
    ComponentCategory,
    ComponentType,
    SalaryComponent,
    ValueType,
)
from app.repositories.platform import SYSTEM_ROLE_DEFINITIONS
from app.validation.rules import PATTERNS, RULES, RuleId, Severity

# ---------------------------------------------------------------------------- §9.3 lookups

DEFAULT_LOOKUP_VALUES: dict[LookupList, tuple[str, ...]] = {
    LookupList.GENDER: ("MALE", "FEMALE", "TRANSGENDER", "OTHER", "UNDISCLOSED"),
    LookupList.MARITAL_STATUS: (
        "SINGLE",
        "MARRIED",
        "DIVORCED",
        "WIDOWED",
        "SEPARATED",
        "UNDISCLOSED",
    ),
    LookupList.BLOOD_GROUP: (
        "A_POS",
        "A_NEG",
        "B_POS",
        "B_NEG",
        "AB_POS",
        "AB_NEG",
        "O_POS",
        "O_NEG",
        "UNKNOWN",
    ),
    LookupList.RELATIONSHIP: (
        "SPOUSE",
        "FATHER",
        "MOTHER",
        "SON",
        "DAUGHTER",
        "BROTHER",
        "SISTER",
        "FATHER_IN_LAW",
        "MOTHER_IN_LAW",
        "GRANDPARENT",
        "GUARDIAN",
        "FRIEND",
        "OTHER",
    ),
    LookupList.QUALIFICATION_LEVEL: (
        "SECONDARY_10",
        "HIGHER_SECONDARY_12",
        "ITI",
        "DIPLOMA",
        "GRADUATE",
        "POST_GRADUATE",
        "DOCTORATE",
        "PROFESSIONAL_CERTIFICATION",
        "OTHER",
    ),
    LookupList.STUDY_MODE: ("FULL_TIME", "PART_TIME", "DISTANCE", "ONLINE", "OTHER"),
    LookupList.SOURCE_OF_HIRE: (
        "DIRECT",
        "REFERRAL",
        "JOB_PORTAL",
        "CAMPUS",
        "AGENCY",
        "SOCIAL_MEDIA",
        "INTERNAL_TRANSFER",
        "REHIRE",
        "OTHER",
    ),
    # §9.3: the disability categories are "to be confirmed by legal review" (§25.8 item 9),
    # so only OTHER is seeded. An organization that enables disability collection defines the
    # categories its counsel approves.
    LookupList.DISABILITY_TYPE: ("OTHER",),
    LookupList.BANK_ACCOUNT_TYPE: ("SAVINGS", "SALARY", "CURRENT", "NRE", "NRO", "OTHER"),
    LookupList.INDUSTRY: (
        "IT_SERVICES",
        "MANUFACTURING",
        "BFSI",
        "HEALTHCARE",
        "RETAIL",
        "EDUCATION",
        "CONSTRUCTION",
        "LOGISTICS",
        "OTHER",
    ),
    LookupList.BGV_STATUS: (
        "NOT_INITIATED",
        "IN_PROGRESS",
        "CLEAR",
        "DISCREPANCY",
        "UNABLE_TO_VERIFY",
    ),
    LookupList.STATUS_REASON: (
        "RESIGNATION",
        "TERMINATION_MISCONDUCT",
        "TERMINATION_PERFORMANCE",
        "CONTRACT_END",
        "RETIREMENT_SUPERANNUATION",
        "VOLUNTARY_RETIREMENT",
        "DEATH",
        "INTER_ORG_TRANSFER",
        "SUSPENSION",
        "SABBATICAL",
        "ABSENCE_UNAUTHORIZED",
        "RESIGNATION_WITHDRAWN",
        "RETURN_TO_WORK",
        "DATA_CORRECTION",
        "OTHER",
    ),
    LookupList.JOB_CHANGE_TYPE: (
        "JOINING",
        "TRANSFER",
        "PROMOTION",
        "REDESIGNATION",
        "MANAGER_CHANGE",
        "RESTRUCTURE",
        "CORRECTION",
        "OTHER",
    ),
    LookupList.SALARY_REVISION_REASON: (
        "NEW_JOINER",
        "ANNUAL_REVISION",
        "PROMOTION",
        "MARKET_CORRECTION",
        "ROLE_CHANGE",
        "CONFIRMATION",
        "STRUCTURE_CHANGE",
        "DATA_CORRECTION",
        "OTHER",
    ),
    # §9.3: RELIGION and SOCIAL_CATEGORY are "empty by default" and usable only once the
    # corresponding setting is enabled with a recorded purpose (§25.2, DEC-036).
    LookupList.RELIGION: (),
    LookupList.SOCIAL_CATEGORY: (),
    # §9.3: DOCUMENT_TYPE values are per-category sub-types; seeded from the category codes
    # below so a document always has a valid default type (§15.3).
    LookupList.DOCUMENT_TYPE: (),
    # JA §9.5: a competency cannot be created until at least one category exists, so the
    # seven suggested values ship with the organization rather than leaving the first
    # competency create to fail on an empty list.
    LookupList.COMPETENCY_CATEGORY: (
        "TECHNICAL",
        "FUNCTIONAL",
        "BEHAVIOURAL",
        "LEADERSHIP",
        "COMPLIANCE",
        "LANGUAGE",
        "OTHER",
    ),
}

UNDEACTIVATABLE_LOOKUP_VALUES: frozenset[tuple[LookupList, str]] = frozenset(
    {(LookupList.GENDER, "UNDISCLOSED")}
)
"""§9.3: ``UNDISCLOSED`` in the GENDER list cannot be deactivated — there must always be a
value a person can choose without disclosing anything."""

FIXED_LOOKUP_LISTS: frozenset[LookupList] = frozenset({LookupList.BLOOD_GROUP})
"""§9.3 marks BLOOD_GROUP "No (system fixed)" — values cannot be added or edited."""


def _humanize(code: str) -> str:
    """Turn a seeded ``value_code`` into a default label.

    Labels are editable afterwards (§9.3); this only has to be a reasonable starting point.
    """
    special = {
        "A_POS": "A+",
        "A_NEG": "A-",
        "B_POS": "B+",
        "B_NEG": "B-",
        "AB_POS": "AB+",
        "AB_NEG": "AB-",
        "O_POS": "O+",
        "O_NEG": "O-",
        "ITI": "ITI",
        "BFSI": "BFSI",
        "IT_SERVICES": "IT services",
        "NRE": "NRE",
        "NRO": "NRO",
        "SECONDARY_10": "Secondary (class 10)",
        "HIGHER_SECONDARY_12": "Higher secondary (class 12)",
        "POSH_POLICY_ACK": "POSH policy acknowledgement",
        "CODE_OF_CONDUCT_ACK": "Code of conduct acknowledgement",
        "NDA": "Non-disclosure agreement",
        "BGV_DOCUMENT": "Background verification document",
        "PAN_PROOF": "PAN proof",
        "AADHAAR_PROOF": "Aadhaar proof",
    }
    if code in special:
        return special[code]
    return code.replace("_", " ").capitalize()


# ---------------------------------------------------------------------------- §15.2 categories


@dataclass(frozen=True, slots=True)
class CategorySeed:
    """One row of the §15.2 seeded-category table."""

    code: str
    restricted: bool
    verification: bool
    expiry: bool
    multiple_current: bool
    mime_types: tuple[str, ...] = ("application/pdf", "image/jpeg", "image/png")
    max_size_bytes: int | None = None


_IMAGE_ONLY = ("image/jpeg", "image/png")

SEEDED_DOCUMENT_CATEGORIES: tuple[CategorySeed, ...] = (
    # §15.2: PHOTOGRAPH is JPEG/PNG only and capped at 2 MB (NFR-18).
    CategorySeed("PHOTOGRAPH", False, False, False, False, _IMAGE_ONLY, 2 * 1024 * 1024),
    CategorySeed("PAN_PROOF", True, True, False, False),
    CategorySeed("AADHAAR_PROOF", True, True, False, False),
    CategorySeed("ADDRESS_PROOF", True, True, True, True),
    CategorySeed("PASSPORT", True, True, True, False),
    CategorySeed("VOTER_ID", True, True, False, False),
    CategorySeed("DRIVING_LICENCE", True, True, True, False),
    CategorySeed("RESUME", False, False, False, True),
    CategorySeed("OFFER_LETTER", False, False, False, True),
    CategorySeed("APPOINTMENT_LETTER", False, False, False, True),
    CategorySeed("EMPLOYMENT_AGREEMENT", False, False, True, True),
    CategorySeed("NDA", False, False, True, True),
    CategorySeed("CODE_OF_CONDUCT_ACK", False, False, False, True),
    CategorySeed("POSH_POLICY_ACK", False, False, False, True),
    CategorySeed("EDUCATION_CERTIFICATE", False, True, False, True),
    CategorySeed("EXPERIENCE_LETTER", False, True, False, True),
    CategorySeed("RELIEVING_LETTER", False, True, False, True),
    CategorySeed("PREVIOUS_PAYSLIP", True, False, False, True),
    CategorySeed("BANK_PROOF", True, True, False, True),
    CategorySeed("MEDICAL_FITNESS", True, False, True, True),
    CategorySeed("BGV_DOCUMENT", True, False, False, True),
    CategorySeed("NOMINATION_FORM", True, False, False, True),
    CategorySeed("SALARY_REVISION_LETTER", True, False, False, True),
    CategorySeed("SALARY_STRUCTURE_DOCUMENT", True, False, False, True),
    CategorySeed("OTHER", False, False, True, True),
    # Addendum §9.3
    CategorySeed("LEAVE_SUPPORTING_DOCUMENT", True, False, False, True),
    CategorySeed("ATTENDANCE_EVIDENCE", False, False, False, True),
    CategorySeed("ONBOARDING_TASK_EVIDENCE", False, False, False, True),
)
"""The twenty-five categories of the §15.2 table, with its exact restricted/verification/
expiry/multiple-current flags."""

SALARY_SCOPED_CATEGORY_PREFIX = "SALARY_"
"""§15.2: "SALARY_* categories additionally require salary.view to list or download"."""


# ---------------------------------------------------------------------------- §23.2 states

INDIAN_STATES: tuple[tuple[str, str], ...] = (
    ("01", "Jammu and Kashmir"),
    ("02", "Himachal Pradesh"),
    ("03", "Punjab"),
    ("04", "Chandigarh"),
    ("05", "Uttarakhand"),
    ("06", "Haryana"),
    ("07", "Delhi"),
    ("08", "Rajasthan"),
    ("09", "Uttar Pradesh"),
    ("10", "Bihar"),
    ("11", "Sikkim"),
    ("12", "Arunachal Pradesh"),
    ("13", "Nagaland"),
    ("14", "Manipur"),
    ("15", "Mizoram"),
    ("16", "Tripura"),
    ("17", "Meghalaya"),
    ("18", "Assam"),
    ("19", "West Bengal"),
    ("20", "Jharkhand"),
    ("21", "Odisha"),
    ("22", "Chhattisgarh"),
    ("23", "Madhya Pradesh"),
    ("24", "Gujarat"),
    ("26", "Dadra and Nagar Haveli and Daman and Diu"),
    ("27", "Maharashtra"),
    ("28", "Andhra Pradesh"),
    ("29", "Karnataka"),
    ("30", "Goa"),
    ("31", "Lakshadweep"),
    ("32", "Kerala"),
    ("33", "Tamil Nadu"),
    ("34", "Puducherry"),
    ("35", "Andaman and Nicobar Islands"),
    ("36", "Telangana"),
    ("37", "Ladakh"),
    ("38", "Other Territory"),
)
"""GST state codes. Code 25 is absent: it was the former Daman and Diu, merged into 26.
Seeded rather than hardcoded in validators (ADR-006), so §7.1's "must match states reference"
stays configuration."""


# ---------------------------------------------------------------------------- §21.2 config

_CHECKSUMS: dict[RuleId, ChecksumAlgorithm] = {
    RuleId.AADHAAR: ChecksumAlgorithm.VERHOEFF,
    RuleId.GSTIN: ChecksumAlgorithm.GSTIN_MOD36,
}

_NORMALIZATIONS: dict[RuleId, NormalizationStrategy] = {
    RuleId.AADHAAR: NormalizationStrategy.DIGITS_ONLY,
    RuleId.UAN: NormalizationStrategy.DIGITS_ONLY,
    RuleId.ESIC_IP: NormalizationStrategy.DIGITS_ONLY,
    RuleId.ESIC_EMP: NormalizationStrategy.DIGITS_ONLY,
    RuleId.BANKACC: NormalizationStrategy.DIGITS_ONLY,
    RuleId.PFMID: NormalizationStrategy.UPPER_STRIP_SLASH,
    RuleId.PF_EST: NormalizationStrategy.UPPER_STRIP_SLASH,
}

IDENTIFIER_CONFIG_RULES: tuple[tuple[str, RuleId], ...] = (
    ("PAN", RuleId.PAN),
    ("AADHAAR", RuleId.AADHAAR),
    ("UAN", RuleId.UAN),
    ("PF_MEMBER_ID", RuleId.PFMID),
    ("PREVIOUS_PF_MEMBER_ID", RuleId.PFMID),
    ("ESIC_IP", RuleId.ESIC_IP),
    ("PASSPORT", RuleId.PASSPORT),
    ("VOTER_ID", RuleId.EPIC),
    ("DRIVING_LICENSE", RuleId.DL_IN),
    # §21.2 covers "employee and organization identifier types".
    ("TAN", RuleId.TAN),
    ("CIN", RuleId.CIN),
    ("LLPIN", RuleId.LLPIN),
    ("GSTIN", RuleId.GSTIN),
    ("UDYAM", RuleId.UDYAM),
    ("PF_ESTABLISHMENT", RuleId.PF_EST),
    ("ESIC_EMPLOYER", RuleId.ESIC_EMP),
    ("IFSC", RuleId.IFSC),
    ("BANK_ACCOUNT", RuleId.BANKACC),
)
"""Maps each configurable identifier type to the §21.1 rule that supplies its default pattern,
so the seeded configuration and the in-code rule dictionary cannot drift apart."""


# ---------------------------------------------------------------------------- platform seed


@dataclass(frozen=True, slots=True)
class SeedReport:
    """What a seed run created. Nothing is reported as created unless it was inserted."""

    roles: int = 0
    permissions: int = 0
    role_permissions: int = 0
    states: int = 0
    identifier_configs: int = 0

    def total(self) -> int:
        return (
            self.roles
            + self.permissions
            + self.role_permissions
            + self.states
            + self.identifier_configs
        )


def seed_platform(session: Session) -> SeedReport:
    """Seed roles, permissions, their links, states and identifier configuration.

    Idempotent: existing rows are left untouched, so this can run on every deployment. It does
    **not** create any user — §19 of the brief forbids creating a privileged administrator
    with a known password, so the first account is an explicit operator action.
    """
    created_permissions = _seed_permissions(session)
    created_roles = _seed_roles(session)
    session.flush()
    created_links = _seed_role_permissions(session)
    created_states = _seed_states(session)
    created_configs = _seed_identifier_type_configs(session)
    return SeedReport(
        roles=created_roles,
        permissions=created_permissions,
        role_permissions=created_links,
        states=created_states,
        identifier_configs=created_configs,
    )


def _seed_permissions(session: Session) -> int:
    existing = set(session.execute(select(PermissionRow.permission_code)).scalars())
    created = 0
    for permission in sorted(ALL_PERMISSIONS, key=lambda item: item.value):
        if permission.value in existing:
            continue
        session.add(
            PermissionRow(
                id=uuid7(),
                permission_code=permission.value,
                description=_permission_description(permission),
            )
        )
        created += 1
    return created


def _permission_description(permission: Permission) -> str:
    """A readable description for the §24.4.8 permission catalogue."""
    area, _, action = permission.value.partition(".")
    return f"{action.replace('_', ' ').capitalize()} ({area})"


def _seed_roles(session: Session) -> int:
    existing = set(session.execute(select(RoleRow.role_code)).scalars())
    created = 0
    for role, (name, description) in SYSTEM_ROLE_DEFINITIONS.items():
        if role.value in existing:
            continue
        session.add(
            RoleRow(
                id=uuid7(),
                role_code=role.value,
                name=name,
                description=description,
                is_system=True,
            )
        )
        created += 1
    return created


def _seed_role_permissions(session: Session) -> int:
    """Link each role to the permissions the §5.3 matrix grants it by default.

    Only the ``Y`` cells. The ``C`` cells are deliberately **not** linked here: they are
    per-organization opt-ins stored in ``organization_role_permissions`` (DEC-004), and
    seeding them platform-wide would hand every organization a permission the spec says is
    off by default.
    """
    role_ids: dict[str, uuid.UUID] = {
        row.role_code: row.id
        for row in session.execute(select(RoleRow.role_code, RoleRow.id)).all()
    }
    permission_ids: dict[str, uuid.UUID] = {
        row.permission_code: row.id
        for row in session.execute(select(PermissionRow.permission_code, PermissionRow.id)).all()
    }
    existing_links = set(
        session.execute(select(RolePermission.role_id, RolePermission.permission_id)).all()
    )

    created = 0
    for role, permissions in ROLE_PERMISSIONS.items():
        role_id = role_ids.get(role.value)
        if role_id is None:
            continue
        for permission in permissions:
            permission_id = permission_ids.get(permission.value)
            if permission_id is None or (role_id, permission_id) in existing_links:
                continue
            session.add(RolePermission(role_id=role_id, permission_id=permission_id))
            created += 1
    return created


def _seed_states(session: Session) -> int:
    existing = set(session.execute(select(State.code)).scalars())
    created = 0
    for code, name in INDIAN_STATES:
        if code in existing:
            continue
        session.add(State(code=code, name=name, status="ACTIVE"))
        created += 1
    return created


def _seed_identifier_type_configs(session: Session) -> int:
    existing = set(session.execute(select(IdentifierTypeConfig.identifier_type)).scalars())
    created = 0
    for identifier_type, rule_id in IDENTIFIER_CONFIG_RULES:
        if identifier_type in existing:
            continue
        rule = RULES[rule_id]
        pattern = PATTERNS.get(rule_id)
        # Country-dependent rules (passport, postal code) have no single pattern; their
        # validators stay in code and the config row records only severity and masking.
        pattern_text = ".*" if pattern is None else pattern.pattern
        session.add(
            IdentifierTypeConfig(
                identifier_type=identifier_type,
                pattern=pattern_text[:200],
                normalization=_NORMALIZATIONS.get(rule_id, NormalizationStrategy.UPPER_STRIP).value,
                checksum_algorithm=_CHECKSUMS.get(rule_id, ChecksumAlgorithm.NONE).value,
                severity=Severity.WARNING.value
                if rule.severity is Severity.WARNING
                else Severity.ERROR.value,
                mask_visible_chars=4,
            )
        )
        created += 1
    return created


# ---------------------------------------------------------------------------- org seed


@dataclass(frozen=True, slots=True)
class OrganizationSeedReport:
    """What organization creation seeded (§24.4.1)."""

    lookup_values: int = 0
    document_categories: int = 0
    employment_types: int = 0
    identifier_policies: int = 0
    nomination_configs: int = 0
    basic_component: int = 0
    settings_created: bool = False


def seed_organization(session: Session, organization_id: uuid.UUID) -> OrganizationSeedReport:
    """Seed everything a new organization needs to be usable (§24.4.1).

    Called inside the organization-creation transaction, so an organization never exists
    without its settings, lookups, categories, employment types and policies.
    """
    settings_created = _seed_settings(session, organization_id)
    lookups = _seed_lookups(session, organization_id)
    categories, document_types = _seed_document_categories(session, organization_id)
    lookups += _seed_document_type_lookups(session, organization_id, document_types)
    employment_types = _seed_employment_types(session, organization_id)
    policies = _seed_identifier_policies(session, organization_id)
    nominations = _seed_nomination_configs(session, organization_id)
    basic = _seed_basic_component(session, organization_id)
    return OrganizationSeedReport(
        lookup_values=lookups,
        document_categories=categories,
        employment_types=employment_types,
        identifier_policies=policies,
        nomination_configs=nominations,
        basic_component=basic,
        settings_created=settings_created,
    )


def _seed_basic_component(session: Session, organization_id: uuid.UUID) -> int:
    """The system Basic component: always present, never deletable, base of every ``%``."""
    exists = session.execute(
        select(SalaryComponent.id).where(
            SalaryComponent.organization_id == organization_id,
            SalaryComponent.component_code_normalized == BASIC_COMPONENT_CODE,
        )
    ).first()
    if exists is not None:
        return 0
    currency = session.execute(
        select(Organization.default_currency).where(Organization.id == organization_id)
    ).scalar_one()
    session.add(
        SalaryComponent(
            id=uuid7(),
            organization_id=organization_id,
            component_code=BASIC_COMPONENT_CODE,
            component_name="Basic",
            display_name="Basic",
            component_type=ComponentType.EARNING.value,
            component_category=ComponentCategory.EARNINGS.value,
            value_type=ValueType.FIXED_AMOUNT.value,
            amount_period=AmountPeriod.MONTHLY.value,
            currency=currency,
            is_required=True,
            is_system=True,
            display_order=0,
            status="ACTIVE",
        )
    )
    return 1


def _seed_settings(session: Session, organization_id: uuid.UUID) -> bool:
    existing = session.get(OrganizationSettings, organization_id)
    if existing is not None:
        return False
    # Every default comes from the §7.5 table; the privacy-relevant ones default to the most
    # restrictive option the spec allows (§25.2).
    session.add(OrganizationSettings(organization_id=organization_id))
    return True


def _seed_lookups(session: Session, organization_id: uuid.UUID) -> int:
    existing = set(
        session.execute(
            select(LookupValue.list_code, LookupValue.value_code).where(
                LookupValue.organization_id == organization_id
            )
        ).all()
    )
    created = 0
    for list_code, values in DEFAULT_LOOKUP_VALUES.items():
        for order, value_code in enumerate(values, start=1):
            if (list_code.value, value_code) in existing:
                continue
            session.add(
                LookupValue(
                    id=uuid7(),
                    organization_id=organization_id,
                    list_code=list_code.value,
                    value_code=value_code,
                    label=_humanize(value_code),
                    display_order=order * 10,
                    status="ACTIVE",
                    is_system_default=True,
                )
            )
            created += 1
    return created


def _seed_document_categories(
    session: Session, organization_id: uuid.UUID
) -> tuple[int, list[str]]:
    existing = set(
        session.execute(
            select(DocumentCategory.category_code).where(
                DocumentCategory.organization_id == organization_id
            )
        ).scalars()
    )
    created = 0
    codes: list[str] = []
    for seed in SEEDED_DOCUMENT_CATEGORIES:
        codes.append(seed.code)
        if seed.code in existing:
            continue
        category = DocumentCategory(
            id=uuid7(),
            organization_id=organization_id,
            category_code=seed.code,
            category_name=_humanize(seed.code),
            is_restricted=seed.restricted,
            requires_verification=seed.verification,
            supports_expiry=seed.expiry,
            allows_multiple_current=seed.multiple_current,
            max_file_size_bytes=seed.max_size_bytes or 10 * 1024 * 1024,
            is_mandatory_for_activation=False,
            status="ACTIVE",
            is_system_default=True,
        )
        session.add(category)
        session.flush()
        for mime_type in seed.mime_types:
            if mime_type not in ALLOWED_MIME_TYPES:
                continue
            session.add(
                DocumentCategoryMimeType(document_category_id=category.id, mime_type=mime_type)
            )
        created += 1
    return created, codes


def _seed_document_type_lookups(
    session: Session, organization_id: uuid.UUID, category_codes: list[str]
) -> int:
    """Seed ``DOCUMENT_TYPE`` values from the category codes (§9.3, §15.3).

    §15.3 defaults a document's type to its category code, so every category needs a matching
    lookup value or the default would reference a value that does not exist.
    """
    existing = set(
        session.execute(
            select(LookupValue.value_code).where(
                LookupValue.organization_id == organization_id,
                LookupValue.list_code == LookupList.DOCUMENT_TYPE.value,
            )
        ).scalars()
    )
    created = 0
    for order, code in enumerate(category_codes, start=1):
        if code in existing:
            continue
        session.add(
            LookupValue(
                id=uuid7(),
                organization_id=organization_id,
                list_code=LookupList.DOCUMENT_TYPE.value,
                value_code=code,
                label=_humanize(code),
                display_order=order * 10,
                status="ACTIVE",
                is_system_default=True,
            )
        )
        created += 1
    return created


def _seed_employment_types(session: Session, organization_id: uuid.UUID) -> int:
    """§9.2: "Seeded: one row per base type"."""
    existing = set(
        session.execute(
            select(EmploymentType.code).where(EmploymentType.organization_id == organization_id)
        ).scalars()
    )
    created = 0
    for order, base_type in enumerate(EmploymentTypeBase, start=1):
        if base_type.value in existing:
            continue
        session.add(
            EmploymentType(
                id=uuid7(),
                organization_id=organization_id,
                code=base_type.value,
                name=_humanize(base_type.value),
                base_type=base_type.value,
                # §9.2: "Default true for FIXED_TERM, false otherwise".
                requires_end_date=base_type is EmploymentTypeBase.FIXED_TERM,
                display_order=order * 10,
                status="ACTIVE",
            )
        )
        created += 1
    return created


def _seed_identifier_policies(session: Session, organization_id: uuid.UUID) -> int:
    """§11.3 seeded defaults: Aadhaar, voter ID and driving licence are NOT_COLLECTED."""
    existing = set(
        session.execute(
            select(IdentifierRequirementPolicy.identifier_type).where(
                IdentifierRequirementPolicy.organization_id == organization_id
            )
        ).scalars()
    )
    created = 0
    for identifier_type in IdentifierType:
        if identifier_type.value in existing:
            continue
        requirement = DEFAULT_IDENTIFIER_REQUIREMENTS[identifier_type]
        session.add(
            IdentifierRequirementPolicy(
                id=uuid7(),
                organization_id=organization_id,
                identifier_type=identifier_type.value,
                requirement=requirement.value,
                # No purpose statement: the three restricted types are seeded NOT_COLLECTED,
                # and the CHECK constraint only demands a purpose once that changes.
                purpose_statement=None,
            )
        )
        created += 1
    return created


def _seed_nomination_configs(session: Session, organization_id: uuid.UUID) -> int:
    """§13.3 defaults: PF and gratuity require full allocation and a date of birth."""
    existing = set(
        session.execute(
            select(NominationTypeConfig.nomination_type).where(
                NominationTypeConfig.organization_id == organization_id
            )
        ).scalars()
    )
    created = 0
    for nomination_type in NominationType:
        if nomination_type.value in existing:
            continue
        requires_full, requires_dob = DEFAULT_NOMINATION_CONFIGS[nomination_type]
        session.add(
            NominationTypeConfig(
                id=uuid7(),
                organization_id=organization_id,
                nomination_type=nomination_type.value,
                requires_full_allocation=requires_full,
                requires_dob=requires_dob,
                max_nominees=None,
            )
        )
        created += 1
    return created


__all__ = [
    "DEFAULT_LOOKUP_VALUES",
    "FIXED_LOOKUP_LISTS",
    "IDENTIFIER_CONFIG_RULES",
    "INDIAN_STATES",
    "SALARY_SCOPED_CATEGORY_PREFIX",
    "SEEDED_DOCUMENT_CATEGORIES",
    "UNDEACTIVATABLE_LOOKUP_VALUES",
    "CategorySeed",
    "OrganizationSeedReport",
    "SeedReport",
    "seed_organization",
    "seed_platform",
]
