"""The §21.1 validation rule dictionary.

Each ``V-*`` rule from the spec is one entry in :data:`RULES`, carrying its pattern, the
normalisation that must run first, its default severity and any checksum. Services and
schemas call :func:`check` rather than re-deriving a regex, so a pattern exists in exactly
one place and §21.2's configurable overrides have a single point of application.

Severity matters: §21.1 marks V-EPIC and V-DL-IN as ``Warning`` because older formats remain
in circulation, so those must not hard-fail a save. Everything else defaults to ``Error``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
from typing import Final

from app.validation.checksums import gstin_checksum_is_valid, verhoeff_is_valid
from app.validation.normalizers import (
    Normalization,
    normalize_code,
    normalize_email,
    normalize_identifier,
    normalize_name,
    normalize_phone,
    normalize_postal_code,
    normalize_text,
)


class Severity(StrEnum):
    """§21.2 ``severity``."""

    ERROR = "ERROR"
    WARNING = "WARNING"


class RuleId(StrEnum):
    """The ``Rule ID`` column of §21.1."""

    NAME = "V-NAME"
    CODE = "V-CODE"
    EMAIL = "V-EMAIL"
    PHONE = "V-PHONE"
    POSTAL = "V-POSTAL"
    URL = "V-URL"
    PAN = "V-PAN"
    TAN = "V-TAN"
    CIN = "V-CIN"
    LLPIN = "V-LLPIN"
    GSTIN = "V-GSTIN"
    UDYAM = "V-UDYAM"
    AADHAAR = "V-AADHAAR"
    UAN = "V-UAN"
    PFMID = "V-PFMID"
    PF_EST = "V-PF-EST"
    ESIC_IP = "V-ESIC-IP"
    ESIC_EMP = "V-ESIC-EMP"
    PASSPORT = "V-PASSPORT"
    EPIC = "V-EPIC"
    DL_IN = "V-DL-IN"
    IFSC = "V-IFSC"
    BANKACC = "V-BANKACC"
    REGNO = "V-REGNO"


# ---------------------------------------------------------------------------- patterns

PATTERNS: Final[dict[RuleId, re.Pattern[str]]] = {
    RuleId.CODE: re.compile(r"^[A-Z0-9][A-Z0-9_./\-]{0,29}$"),
    # WHATWG "valid e-mail address" production. Length and the "domain contains a dot"
    # requirement are checked separately because the pattern cannot express them.
    RuleId.EMAIL: re.compile(
        r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~\-]+"
        r"@[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
        r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$"
    ),
    RuleId.PHONE: re.compile(r"^\+[1-9][0-9]{7,14}$"),
    RuleId.PAN: re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$"),
    RuleId.TAN: re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$"),
    RuleId.CIN: re.compile(r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$"),
    RuleId.LLPIN: re.compile(r"^[A-Z]{3}-[0-9]{4}$"),
    RuleId.GSTIN: re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"),
    RuleId.UDYAM: re.compile(r"^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$"),
    RuleId.AADHAAR: re.compile(r"^[2-9][0-9]{11}$"),
    RuleId.UAN: re.compile(r"^[0-9]{12}$"),
    RuleId.PFMID: re.compile(r"^[A-Z]{5}[0-9]{7}[0-9]{3}[0-9]{7}$"),
    RuleId.PF_EST: re.compile(r"^[A-Z]{5}[0-9]{7}([0-9]{3})?$"),
    RuleId.ESIC_IP: re.compile(r"^[0-9]{10}$"),
    RuleId.ESIC_EMP: re.compile(r"^[0-9]{17}$"),
    RuleId.EPIC: re.compile(r"^[A-Z]{3}[0-9]{7}$"),
    RuleId.DL_IN: re.compile(r"^[A-Z]{2}[0-9]{2}[0-9]{4}[0-9]{7}$"),
    RuleId.IFSC: re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$"),
    RuleId.BANKACC: re.compile(r"^[0-9]{9,18}$"),
    RuleId.REGNO: re.compile(r"^[A-Z0-9][A-Z0-9 /._\-]{0,49}$"),
}

# Country-dependent patterns, chosen by the caller's country context.
POSTAL_PATTERN_INDIA: Final = re.compile(r"^[1-9][0-9]{5}$")
POSTAL_PATTERN_OTHER: Final = re.compile(r"^[A-Z0-9][A-Z0-9 \-]{1,9}$")
PASSPORT_PATTERN_INDIA: Final = re.compile(r"^[A-Z][0-9]{7}$")
PASSPORT_PATTERN_OTHER: Final = re.compile(r"^[A-Z0-9]{5,20}$")
PHONE_PATTERN_INDIA: Final = re.compile(r"^\+91[6-9][0-9]{9}$")

NAME_PUNCTUATION: Final = frozenset(" .'-")
NAME_ALLOWED_PUNCTUATION: Final = frozenset(".'-")
URL_PATTERN: Final = re.compile(
    r"^https?://[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)+"
    r"(?::\d{1,5})?(?:[/?#][^\s]*)?$"
)

MAX_EMAIL_LENGTH: Final = 254
MAX_URL_LENGTH: Final = 255
INDIA: Final = "IN"


@dataclass(frozen=True, slots=True)
class Outcome:
    """The result of one rule check."""

    rule_id: RuleId
    ok: bool
    severity: Severity
    normalized: str | None
    message: str = ""

    @property
    def blocks(self) -> bool:
        """Whether this outcome must prevent the write."""
        return not self.ok and self.severity is Severity.ERROR

    @property
    def warns(self) -> bool:
        """Whether this outcome is a non-blocking warning (§21.3)."""
        return not self.ok and self.severity is Severity.WARNING


@dataclass(frozen=True, slots=True)
class Rule:
    """One row of the §21.1 dictionary."""

    rule_id: RuleId
    description: str
    severity: Severity = Severity.ERROR
    normalization: Normalization | None = None
    max_length: int | None = None
    tags: frozenset[str] = field(default_factory=frozenset)


RULES: Final[dict[RuleId, Rule]] = {
    RuleId.NAME: Rule(RuleId.NAME, "Person name: Unicode letters and marks, . ' - and spaces"),
    RuleId.CODE: Rule(RuleId.CODE, "Master, rule or component code", max_length=30),
    RuleId.EMAIL: Rule(RuleId.EMAIL, "Email address", max_length=MAX_EMAIL_LENGTH),
    RuleId.PHONE: Rule(RuleId.PHONE, "Phone number in E.164", max_length=16),
    RuleId.POSTAL: Rule(RuleId.POSTAL, "Postal code", max_length=10),
    RuleId.URL: Rule(RuleId.URL, "Website URL", max_length=MAX_URL_LENGTH),
    RuleId.PAN: Rule(RuleId.PAN, "PAN", normalization=Normalization.UPPER_STRIP, max_length=10),
    RuleId.TAN: Rule(RuleId.TAN, "TAN", normalization=Normalization.UPPER_STRIP, max_length=10),
    RuleId.CIN: Rule(RuleId.CIN, "CIN", normalization=Normalization.UPPER_STRIP, max_length=21),
    # LLPIN keeps its hyphen (AAB-1234), so it is upper-cased without separator stripping.
    RuleId.LLPIN: Rule(RuleId.LLPIN, "LLPIN", max_length=8),
    RuleId.GSTIN: Rule(
        RuleId.GSTIN, "GSTIN", normalization=Normalization.UPPER_STRIP, max_length=15
    ),
    RuleId.UDYAM: Rule(RuleId.UDYAM, "Udyam registration number", max_length=19),
    RuleId.AADHAAR: Rule(
        RuleId.AADHAAR,
        "Aadhaar number",
        normalization=Normalization.DIGITS_ONLY,
        max_length=12,
        tags=frozenset({"sensitive"}),
    ),
    RuleId.UAN: Rule(RuleId.UAN, "UAN", normalization=Normalization.DIGITS_ONLY, max_length=12),
    RuleId.PFMID: Rule(
        RuleId.PFMID,
        "PF member ID",
        normalization=Normalization.UPPER_STRIP_SLASH,
        max_length=22,
    ),
    RuleId.PF_EST: Rule(
        RuleId.PF_EST,
        "PF establishment code",
        normalization=Normalization.UPPER_STRIP_SLASH,
        max_length=15,
    ),
    RuleId.ESIC_IP: Rule(
        RuleId.ESIC_IP, "ESIC IP number", normalization=Normalization.DIGITS_ONLY, max_length=10
    ),
    RuleId.ESIC_EMP: Rule(
        RuleId.ESIC_EMP,
        "ESIC employer code",
        normalization=Normalization.DIGITS_ONLY,
        max_length=17,
    ),
    RuleId.PASSPORT: Rule(
        RuleId.PASSPORT,
        "Passport number",
        normalization=Normalization.UPPER_STRIP,
        max_length=20,
    ),
    # §21.1 marks these Warning: older formats remain in circulation.
    RuleId.EPIC: Rule(
        RuleId.EPIC,
        "Voter ID (EPIC)",
        severity=Severity.WARNING,
        normalization=Normalization.UPPER_STRIP,
        max_length=10,
    ),
    RuleId.DL_IN: Rule(
        RuleId.DL_IN,
        "Indian driving licence number",
        severity=Severity.WARNING,
        normalization=Normalization.UPPER_STRIP,
        max_length=20,
    ),
    RuleId.IFSC: Rule(
        RuleId.IFSC, "IFSC code", normalization=Normalization.UPPER_STRIP, max_length=11
    ),
    RuleId.BANKACC: Rule(
        RuleId.BANKACC,
        "Bank account number",
        normalization=Normalization.DIGITS_ONLY,
        max_length=18,
        tags=frozenset({"sensitive"}),
    ),
    RuleId.REGNO: Rule(RuleId.REGNO, "Registration number", max_length=50),
}


# ---------------------------------------------------------------------------- V-NAME


def _is_name_character(character: str) -> bool:
    category = unicodedata.category(character)
    return category.startswith(("L", "M")) or character in NAME_PUNCTUATION


def _name_failure_reason(normalized: str, max_length: int) -> str | None:
    """Why ``normalized`` is not a valid name, or ``None`` when it is."""
    if len(normalized) > max_length:
        return f"Must be at most {max_length} characters."
    if not unicodedata.category(normalized[0]).startswith(("L", "M")):
        return "A name must start with a letter."
    if any(not _is_name_character(character) for character in normalized):
        return "A name may contain only letters, spaces, periods, apostrophes and hyphens."
    if not any(unicodedata.category(character).startswith("L") for character in normalized):
        return "A name must contain at least one letter."
    if any(
        previous in NAME_ALLOWED_PUNCTUATION and current in NAME_ALLOWED_PUNCTUATION
        for previous, current in pairwise(normalized)
    ):
        return "A name may not contain consecutive punctuation."
    return None


def validate_name(value: str | None, *, max_length: int = 150) -> Outcome:
    """V-NAME (§21.1, §10.4).

    Unicode letters and combining marks including Indic scripts, plus spaces, periods,
    apostrophes and hyphens. Must start with a letter or mark, contain at least one letter,
    contain no digits and contain no consecutive punctuation. No minimum surname requirement
    (§10.4: "many Indian names have no surname").
    """
    normalized = normalize_name(value)
    severity = RULES[RuleId.NAME].severity
    if not normalized:
        return Outcome(RuleId.NAME, False, severity, None, "A name is required.")
    reason = _name_failure_reason(normalized, max_length)
    return Outcome(RuleId.NAME, reason is None, severity, normalized, reason or "")


# ---------------------------------------------------------------------------- others


def validate_code(value: str | None) -> Outcome:
    """V-CODE: upper-cased, ``^[A-Z0-9][A-Z0-9_./-]{0,29}$``."""
    normalized = normalize_code(value)
    if not normalized:
        return Outcome(RuleId.CODE, False, Severity.ERROR, None, "A code is required.")
    ok = bool(PATTERNS[RuleId.CODE].match(normalized))
    return Outcome(
        RuleId.CODE,
        ok,
        Severity.ERROR,
        normalized,
        ""
        if ok
        else "A code must be 1-30 characters of A-Z, 0-9, underscore, dot, slash or hyphen, "
        "starting with a letter or digit.",
    )


def validate_email(value: str | None) -> Outcome:
    """V-EMAIL: WHATWG valid email, at most 254 characters, domain containing a dot."""
    normalized = normalize_email(value)
    if not normalized:
        return Outcome(RuleId.EMAIL, False, Severity.ERROR, None, "An email address is required.")
    if len(normalized) > MAX_EMAIL_LENGTH:
        return Outcome(
            RuleId.EMAIL,
            False,
            Severity.ERROR,
            normalized,
            f"Must be at most {MAX_EMAIL_LENGTH} characters.",
        )
    if not PATTERNS[RuleId.EMAIL].match(normalized):
        return Outcome(
            RuleId.EMAIL, False, Severity.ERROR, normalized, "Not a valid email address."
        )
    domain = normalized.rsplit("@", 1)[1]
    if "." not in domain:
        return Outcome(
            RuleId.EMAIL,
            False,
            Severity.ERROR,
            normalized,
            "The email domain must contain a dot.",
        )
    return Outcome(RuleId.EMAIL, True, Severity.ERROR, normalized)


def validate_phone(value: str | None, *, country: str = INDIA) -> Outcome:
    """V-PHONE: ``+91[6-9]XXXXXXXXX`` for India, otherwise E.164."""
    normalized = normalize_phone(value, default_country=country)
    if not normalized:
        return Outcome(RuleId.PHONE, False, Severity.ERROR, None, "A phone number is required.")
    pattern = PHONE_PATTERN_INDIA if normalized.startswith("+91") else PATTERNS[RuleId.PHONE]
    ok = bool(pattern.match(normalized))
    return Outcome(
        RuleId.PHONE,
        ok,
        Severity.ERROR,
        normalized,
        "" if ok else "Not a valid phone number. Indian numbers must be ten digits starting 6-9.",
    )


def validate_postal_code(value: str | None, *, country: str = INDIA) -> Outcome:
    """V-POSTAL: six digits not starting with zero for India, otherwise a loose pattern."""
    normalized = normalize_postal_code(value)
    if not normalized:
        return Outcome(RuleId.POSTAL, False, Severity.ERROR, None, "A postal code is required.")
    pattern = POSTAL_PATTERN_INDIA if country == INDIA else POSTAL_PATTERN_OTHER
    ok = bool(pattern.match(normalized))
    message = (
        "An Indian PIN code must be six digits and must not start with zero."
        if country == INDIA
        else "Not a valid postal code."
    )
    return Outcome(RuleId.POSTAL, ok, Severity.ERROR, normalized, "" if ok else message)


def validate_url(value: str | None) -> Outcome:
    """V-URL: an http or https URL with a valid host, at most 255 characters."""
    normalized = normalize_text(value)
    if not normalized:
        return Outcome(RuleId.URL, False, Severity.ERROR, None, "A URL is required.")
    if len(normalized) > MAX_URL_LENGTH:
        return Outcome(
            RuleId.URL,
            False,
            Severity.ERROR,
            normalized,
            f"Must be at most {MAX_URL_LENGTH} characters.",
        )
    ok = bool(URL_PATTERN.match(normalized))
    return Outcome(
        RuleId.URL,
        ok,
        Severity.ERROR,
        normalized,
        "" if ok else "A URL must start with http:// or https:// and contain a valid host.",
    )


def validate_passport(value: str | None, *, issuing_country: str = INDIA) -> Outcome:
    """V-PASSPORT: the Indian pattern when issued in India, otherwise 5-20 alphanumerics."""
    normalized = normalize_identifier(value, Normalization.UPPER_STRIP)
    if not normalized:
        return Outcome(
            RuleId.PASSPORT, False, Severity.ERROR, None, "A passport number is required."
        )
    pattern = PASSPORT_PATTERN_INDIA if issuing_country == INDIA else PASSPORT_PATTERN_OTHER
    ok = bool(pattern.match(normalized))
    message = (
        "An Indian passport number is one letter followed by seven digits."
        if issuing_country == INDIA
        else "A passport number must be 5-20 letters or digits."
    )
    return Outcome(RuleId.PASSPORT, ok, Severity.ERROR, normalized, "" if ok else message)


# Dispatch tables rather than an if/elif chain: adding a rule means adding an entry, and
# `check` stays a three-line function with no branching on rule identity.
_COUNTRY_VALIDATORS: Final[dict[RuleId, Callable[[str | None, str], Outcome]]] = {
    RuleId.PHONE: lambda value, country: validate_phone(value, country=country),
    RuleId.POSTAL: lambda value, country: validate_postal_code(value, country=country),
    RuleId.PASSPORT: lambda value, country: validate_passport(value, issuing_country=country),
}

_SIMPLE_VALIDATORS: Final[dict[RuleId, Callable[[str | None], Outcome]]] = {
    RuleId.NAME: validate_name,
    RuleId.CODE: validate_code,
    RuleId.EMAIL: validate_email,
    RuleId.URL: validate_url,
}


def check(rule_id: RuleId, value: str | None, *, country: str = INDIA) -> Outcome:
    """Run the rule named by ``rule_id``.

    The single entry point services use, so applying a rule never means re-implementing one.
    Rules whose semantics depend on a country are routed to their dedicated validators;
    everything else is a pattern plus an optional checksum.
    """
    if (country_validator := _COUNTRY_VALIDATORS.get(rule_id)) is not None:
        return country_validator(value, country)
    if (simple_validator := _SIMPLE_VALIDATORS.get(rule_id)) is not None:
        return simple_validator(value)
    return _check_pattern_rule(rule_id, value)


def _check_pattern_rule(rule_id: RuleId, value: str | None) -> Outcome:
    """Pattern-plus-optional-checksum rules."""
    rule = RULES[rule_id]
    normalized = (
        normalize_identifier(value, rule.normalization)
        if rule.normalization is not None
        else normalize_text(value)
    )
    if rule.normalization is None and normalized is not None:
        normalized = normalized.upper()
    if not normalized:
        return Outcome(rule_id, False, rule.severity, None, f"{rule.description} is required.")

    if not PATTERNS[rule_id].match(normalized):
        return Outcome(
            rule_id,
            False,
            rule.severity,
            normalized,
            f"Not a valid {rule.description} ({rule_id.value}).",
        )

    if rule_id is RuleId.AADHAAR and not verhoeff_is_valid(normalized):
        return Outcome(
            rule_id, False, rule.severity, normalized, "The Aadhaar checksum is invalid."
        )
    if rule_id is RuleId.GSTIN and not gstin_checksum_is_valid(normalized):
        return Outcome(rule_id, False, rule.severity, normalized, "The GSTIN checksum is invalid.")

    return Outcome(rule_id, True, rule.severity, normalized)


def pan_fourth_character(pan: str) -> str:
    """The holder-type character of a PAN (§7.3, §11.2).

    ``P`` denotes an individual, ``C`` a company, ``F`` a firm or LLP, ``T`` a trust. Used for
    the §7.3 entity-type expectation (a warning) and the §11.2 employee-PAN check.
    """
    return pan[3] if len(pan) >= 4 else ""


__all__ = [
    "INDIA",
    "MAX_EMAIL_LENGTH",
    "MAX_URL_LENGTH",
    "PATTERNS",
    "RULES",
    "Outcome",
    "Rule",
    "RuleId",
    "Severity",
    "check",
    "pan_fourth_character",
    "validate_code",
    "validate_email",
    "validate_name",
    "validate_passport",
    "validate_phone",
    "validate_postal_code",
    "validate_url",
]
