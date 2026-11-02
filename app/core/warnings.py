"""Warning registry and the §21.3 acknowledgement protocol.

The spec scatters warning codes across §7-§15 and never consolidates them. This module is
the single registry (DEC-018). Sixteen codes are named explicitly in the spec; a further ten
situations are described as "warning" without a code, and are given conventional codes here
marked ``spec_named=False``.

Protocol (§21.3): a write that raises warnings returns ``422
WARNINGS_REQUIRE_ACKNOWLEDGEMENT`` listing them. The client resubmits with
``acknowledged_warnings: ["W-..."]``; acknowledged codes are recorded in the audit entry.
Warnings never block silently and are never ignored silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.errors import WarningDetail, WarningsRequireAcknowledgement


class WarningCode(StrEnum):
    """Every warning the system can raise."""

    # --- named in the spec -----------------------------------------------------
    ORG_NAME_DUPLICATE = "W-ORG-NAME"
    PAN_TYPE_MISMATCH = "W-PAN-TYPE"
    CIN_TYPE_MISMATCH = "W-CIN-TYPE"
    AGE_HIGH = "W-AGE-HIGH"
    CONFIRMATION_EARLY = "W-CONFIRM-EARLY"
    EMERGENCY_CONTACT_SAME = "W-EMERGENCY-SAME"
    PF_ESTABLISHMENT_MISMATCH = "W-PF-EST-MISMATCH"
    PASSPORT_EXPIRED = "W-PASSPORT-EXPIRED"
    INTERNATIONAL_WORKER_NATIONALITY = "W-IW-NATIONALITY"
    DUPLICATE_IDENTIFIER_REHIRE = "W-DUP-REHIRE"
    DUPLICATE_IDENTIFIER_GROUP = "W-DUP-GROUP"
    EDUCATION_DUPLICATE = "W-EDU-DUP"
    BANK_ACCOUNT_HOLDER_NAME = "W-BANK-NAME"
    BANK_ACCOUNT_DUPLICATE = "W-BANK-DUP"
    DOCUMENT_DUPLICATE = "W-DOC-DUP"
    MANAGER_SEPARATED = "W-MANAGER-SEPARATED"

    # --- described in the spec without a code (conventional names, DEC-018) ----
    ORG_PAN_DUPLICATE = "W-ORG-PAN-DUP"
    BRANCH_NAME_DUPLICATE = "W-BRANCH-NAME-DUP"
    MASTER_NAME_DUPLICATE = "W-MASTER-NAME-DUP"
    BRANCH_DEACTIVATION_IN_USE = "W-BRANCH-IN-USE"
    MASTER_DEACTIVATION_IN_USE = "W-MASTER-IN-USE"
    OFFER_LETTER_AFTER_JOINING = "W-OFFER-AFTER-JOINING"
    APPOINTMENT_BEFORE_OFFER = "W-APPOINTMENT-BEFORE-OFFER"
    EDUCATION_PASSING_YEAR_EARLY = "W-EDU-YEAR-EARLY"
    PREVIOUS_EMPLOYMENT_EARLY = "W-PREV-EMP-EARLY"
    NOMINATION_BEFORE_JOINING = "W-NOM-BEFORE-JOINING"
    ID_RULE_ARCHIVE_LEAVES_GAP = "W-IDRULE-GAP"
    ORG_TIMEZONE_CHANGE = "W-ORG-TZ-CHANGE"
    SEQUENCE_CAPACITY = "W-SEQ-CAPACITY"

    # --- position / job architecture (JA-SPEC-001 §14.5) ----------------------
    JOB_TITLE_DUPLICATE = "W-JOB-TITLE-DUPLICATE"
    POSITION_MANAGER_DIFFERS = "W-POSITION-MANAGER-DIFFERS"

    # --- onboarding, attendance and leave addendum §16.2 -----------------------
    ONB_TEMPLATE_MISMATCH = "W-ONB-TEMPLATE-MISMATCH"
    ONB_OWNER_FALLBACK = "W-ONB-OWNER-FALLBACK"
    ONB_REOPEN_AFTER_ACTIVATION = "W-ONB-REOPEN-AFTER-ACTIVATION"
    SHIFT_ASSIGNMENT_TRUNCATES = "W-SHIFT-ASSIGNMENT-TRUNCATES"
    ATT_FINALIZED_RECORDS_AFFECTED = "W-ATT-FINALIZED-RECORDS-AFFECTED"
    LEAVE_PUNCH_CONFLICT = "W-LEAVE-PUNCH-CONFLICT"
    LEAVE_NEGATIVE_BALANCE = "W-LEAVE-NEGATIVE-BALANCE"
    LEAVE_NEGATIVE_AFTER_ADJUSTMENT = "W-LEAVE-NEGATIVE-AFTER-ADJUSTMENT"
    PAY_PAYDATE_LATE = "W-PAY-PAYDATE-LATE"


@dataclass(frozen=True, slots=True)
class WarningSpec:
    """Definition of a warning: its default field, message template and provenance."""

    code: WarningCode
    field: str | None
    template: str
    section: str
    spec_named: bool = True

    def build(self, **values: object) -> WarningDetail:
        """Render this warning.

        ``values`` fills the message template and may override ``field``. Values must never
        include sensitive plaintext: warning messages reach clients and audit records
        (§20.3), so callers pass codes, counts and last-four fragments only.
        """
        field_override = values.pop("field", None)
        return WarningDetail(
            code=self.code.value,
            field=str(field_override) if field_override is not None else self.field,
            message=self.template.format(**values),
        )


_SPECS: tuple[WarningSpec, ...] = (
    WarningSpec(
        WarningCode.ORG_NAME_DUPLICATE,
        "legal_name",
        "Another organization already uses this legal name.",
        "§7.2",
    ),
    WarningSpec(
        WarningCode.ORG_PAN_DUPLICATE,
        "pan",
        "Another organization already records this PAN. Permitted for proprietorships.",
        "§7.2",
        spec_named=False,
    ),
    WarningSpec(
        WarningCode.PAN_TYPE_MISMATCH,
        "pan",
        "The 4th character of the PAN is '{found}'; '{expected}' is expected for entity type "
        "{entity_type}.",
        "§7.3",
    ),
    WarningSpec(
        WarningCode.CIN_TYPE_MISMATCH,
        "cin",
        "The CIN ownership segment is '{found}'; '{expected}' is expected for entity type "
        "{entity_type}.",
        "§7.3",
    ),
    WarningSpec(
        WarningCode.BRANCH_NAME_DUPLICATE,
        "branch_name",
        "Another active branch in this organization already uses this name.",
        "§8.1",
        spec_named=False,
    ),
    WarningSpec(
        WarningCode.BRANCH_DEACTIVATION_IN_USE,
        "status",
        "{employee_count} active employees are currently assigned to this branch. They are "
        "not reassigned automatically.",
        "§8.2",
        spec_named=False,
    ),
    WarningSpec(
        WarningCode.MASTER_NAME_DUPLICATE,
        "name",
        "Another active record in this list already uses this name.",
        "§9.1",
        spec_named=False,
    ),
    WarningSpec(
        WarningCode.MASTER_DEACTIVATION_IN_USE,
        "status",
        "{employee_count} employees currently reference this record. Existing assignments "
        "remain valid; new assignments will be rejected.",
        "§9.4 / E-20",
        spec_named=False,
    ),
    WarningSpec(
        WarningCode.AGE_HIGH,
        "date_of_birth",
        "The computed age is {age} years, which is unusually high.",
        "§10.4",
    ),
    WarningSpec(
        WarningCode.EMERGENCY_CONTACT_SAME,
        "emergency_contact_phone",
        "The emergency contact number is the same as the employee's personal mobile number.",
        "§10.5",
    ),
    WarningSpec(
        WarningCode.CONFIRMATION_EARLY,
        "confirmation_date",
        "The confirmation date is before the probation end date.",
        "§10.6",
    ),
    WarningSpec(
        WarningCode.OFFER_LETTER_AFTER_JOINING,
        "offer_letter_date",
        "The offer letter date is after the joining date.",
        "§10.6",
        spec_named=False,
    ),
    WarningSpec(
        WarningCode.APPOINTMENT_BEFORE_OFFER,
        "appointment_letter_date",
        "The appointment letter date is before the offer letter date.",
        "§10.6",
        spec_named=False,
    ),
    WarningSpec(
        WarningCode.MANAGER_SEPARATED,
        "reporting_manager_employee_id",
        "The selected reporting manager is no longer employed.",
        "§10.6 / E-19",
    ),
    WarningSpec(
        WarningCode.PF_ESTABLISHMENT_MISMATCH,
        "pf_member_id",
        "The PF member ID does not begin with an active PF establishment code of this "
        "organization.",
        "§11.2",
    ),
    WarningSpec(
        WarningCode.PASSPORT_EXPIRED,
        "passport_expiry_date",
        "The passport expired on {expiry_date}.",
        "§11.2",
    ),
    WarningSpec(
        WarningCode.INTERNATIONAL_WORKER_NATIONALITY,
        "international_worker_status",
        "The employee is marked an international worker but the nationality is India.",
        "§11.4",
    ),
    WarningSpec(
        WarningCode.DUPLICATE_IDENTIFIER_REHIRE,
        None,
        "This identifier matches a previous, non-overlapping employment record "
        "({employee_code}) in this organization. It can be linked as a rehire.",
        "§11.5",
    ),
    WarningSpec(
        WarningCode.DUPLICATE_IDENTIFIER_GROUP,
        None,
        "This identifier matches an employment record in another organization of the same "
        "group ({employee_code}).",
        "§11.5",
    ),
    WarningSpec(
        WarningCode.EDUCATION_DUPLICATE,
        None,
        "An education record with the same qualification level, institution and passing year "
        "already exists.",
        "§12.1",
    ),
    WarningSpec(
        WarningCode.EDUCATION_PASSING_YEAR_EARLY,
        "passing_year",
        "The passing year is less than ten years after the employee's year of birth.",
        "§12.1",
        spec_named=False,
    ),
    WarningSpec(
        WarningCode.PREVIOUS_EMPLOYMENT_EARLY,
        "employment_start_date",
        "This previous employment starts less than fourteen years after the employee's date "
        "of birth.",
        "§12.2",
        spec_named=False,
    ),
    WarningSpec(
        WarningCode.NOMINATION_BEFORE_JOINING,
        "effective_from",
        "The nomination effective date is before the employee's joining date.",
        "§13.2",
        spec_named=False,
    ),
    WarningSpec(
        WarningCode.BANK_ACCOUNT_HOLDER_NAME,
        "account_holder_name",
        "The account holder name does not share any name token with the employee's name.",
        "§14.1",
    ),
    WarningSpec(
        WarningCode.BANK_ACCOUNT_DUPLICATE,
        "account_number",
        "This bank account is already recorded for another employee in this organization "
        "({employee_code}).",
        "§14.1",
    ),
    WarningSpec(
        WarningCode.DOCUMENT_DUPLICATE,
        None,
        "A document with identical content already exists for this employee.",
        "§15.3",
    ),
    WarningSpec(
        WarningCode.ID_RULE_ARCHIVE_LEAVES_GAP,
        None,
        "Archiving this rule leaves no applicable rule for at least one department and date range.",
        "§24.4.7",
        spec_named=False,
    ),
    WarningSpec(
        WarningCode.ORG_TIMEZONE_CHANGE,
        None,
        "Changing this setting alters future {YEAR} tokens and day boundaries. Historical "
        "data is not modified.",
        "§7.7",
        spec_named=False,
    ),
    WarningSpec(
        WarningCode.SEQUENCE_CAPACITY,
        None,
        "Sequence {sequence_group_code} has reached {percent}% of the capacity its padding "
        "width can render.",
        "§27 E-35",
        spec_named=False,
    ),
    WarningSpec(
        WarningCode.JOB_TITLE_DUPLICATE,
        "title",
        "Another active job in the same family already uses this title.",
        "JA §14.5",
    ),
    WarningSpec(
        WarningCode.POSITION_MANAGER_DIFFERS,
        "position_change.position_id",
        "The incumbent of the parent position differs from this employee's "
        "reporting manager. Supply reporting_manager_employee_id to change it.",
        "JA §7.5",
    ),
    WarningSpec(
        WarningCode.ONB_TEMPLATE_MISMATCH,
        "template_version_id",
        "The chosen template's applicability does not match this employee.",
        "Addendum §4.4",
    ),
    WarningSpec(
        WarningCode.ONB_OWNER_FALLBACK,
        "tasks",
        "One or more tasks could not resolve an owner and were assigned to the coordinator.",
        "Addendum §4.7",
    ),
    WarningSpec(
        WarningCode.ONB_REOPEN_AFTER_ACTIVATION,
        "status",
        "Reopening a blocking task after the employee was activated.",
        "Addendum §4.8",
    ),
    WarningSpec(
        WarningCode.SHIFT_ASSIGNMENT_TRUNCATES,
        "effective_from",
        "This assignment shortens the employee's existing shift assignment.",
        "Addendum ATT-09",
    ),
    WarningSpec(
        WarningCode.ATT_FINALIZED_RECORDS_AFFECTED,
        "holiday",
        "This change touches FINALIZED attendance records, which are not altered.",
        "Addendum INT-21",
    ),
    WarningSpec(
        WarningCode.PAY_PAYDATE_LATE,
        "target_payment_date",
        "The target payment date is after the statutory due date ({due_date}).",
        "Payroll addendum MW-10",
    ),
    WarningSpec(
        WarningCode.LEAVE_PUNCH_CONFLICT,
        "from_date",
        "Leave covers halves that already have valid punches.",
        "Addendum LEV-30",
    ),
    WarningSpec(
        WarningCode.LEAVE_NEGATIVE_BALANCE,
        "quantity",
        "This application takes the balance below zero within the allowed limit.",
        "Addendum LEV-30",
    ),
    WarningSpec(
        WarningCode.LEAVE_NEGATIVE_AFTER_ADJUSTMENT,
        "quantity_delta",
        "This administrative debit makes the balance negative.",
        "Addendum §6.13",
    ),
)

WARNINGS: dict[WarningCode, WarningSpec] = {spec.code: spec for spec in _SPECS}


def warning(code: WarningCode, **values: object) -> WarningDetail:
    """Build a :class:`WarningDetail` for ``code``."""
    return WARNINGS[code].build(**values)


class WarningCollector:
    """Accumulates warnings during a write and enforces the §21.3 protocol.

    A service collects warnings while validating, then calls :meth:`enforce` once. Any
    warning the caller did not acknowledge aborts the write with ``422
    WARNINGS_REQUIRE_ACKNOWLEDGEMENT``; acknowledged ones are returned for the audit record.
    """

    __slots__ = ("_acknowledged", "_raised")

    def __init__(self, acknowledged: list[str] | None = None) -> None:
        self._acknowledged = {code.strip().upper() for code in (acknowledged or [])}
        self._raised: list[WarningDetail] = []

    def add(self, code: WarningCode, **values: object) -> None:
        """Record that ``code`` applies to this request."""
        self._raised.append(warning(code, **values))

    def add_if(self, condition: bool, code: WarningCode, **values: object) -> None:
        """Record ``code`` only when ``condition`` holds. Keeps validators free of ``if``s."""
        if condition:
            self.add(code, **values)

    @property
    def raised(self) -> list[WarningDetail]:
        return list(self._raised)

    @property
    def unacknowledged(self) -> list[WarningDetail]:
        return [w for w in self._raised if w.code.upper() not in self._acknowledged]

    def enforce(self) -> list[str]:
        """Abort unless every raised warning was acknowledged.

        Returns the acknowledged codes that actually applied, for the audit record. Codes the
        client acknowledged that were not raised are ignored rather than rejected, so a
        client may safely re-send the acknowledgements it received previously.
        """
        outstanding = self.unacknowledged
        if outstanding:
            raise WarningsRequireAcknowledgement(outstanding)
        return [w.code for w in self._raised]


__all__ = [
    "WARNINGS",
    "WarningCode",
    "WarningCollector",
    "WarningSpec",
    "warning",
]
