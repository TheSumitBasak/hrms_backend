"""Input normalisation, applied **before** validation (§21.1, §26.3).

§21.1 opens with: "Normalization is applied before validation: trim; Unicode NFC; collapse
internal whitespace; identifiers upper-cased and stripped of spaces/hyphens unless stated."

Normalisation is not cosmetic here. The stored form is what a unique index compares, what a
blind index digests and what a search matches, so two spellings of the same value must
converge to identical bytes or duplicate detection (§11.5) and exact-match search (§26.2)
both silently fail.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

_WHITESPACE_RUN = re.compile(r"\s+")
_NON_DIGITS = re.compile(r"\D")
_IDENTIFIER_SEPARATORS = re.compile(r"[\s\-]+")
_IDENTIFIER_SEPARATORS_WITH_SLASH = re.compile(r"[\s\-/]+")
_SQL_WILDCARDS = str.maketrans({"%": r"\%", "_": r"\_", "\\": r"\\\\"})


class Normalization(StrEnum):
    """§21.2 ``normalization`` values for configurable identifier validation."""

    UPPER_STRIP = "UPPER_STRIP"
    """Upper-case; remove spaces and hyphens. PAN, TAN, CIN, GSTIN, IFSC, passport."""

    DIGITS_ONLY = "DIGITS_ONLY"
    """Keep digits only. Aadhaar, UAN, ESIC."""

    UPPER_STRIP_SLASH = "UPPER_STRIP_SLASH"
    """Upper-case; remove spaces, hyphens and slashes. PF member and establishment IDs."""


def normalize_text(value: str | None) -> str | None:
    """Trim, NFC-normalise and collapse internal whitespace.

    The baseline for every free-text field. ``None`` and strings that are empty after
    trimming both become ``None``, so "supplied but blank" and "not supplied" cannot diverge
    into two different stored states.
    """
    if value is None:
        return None
    collapsed = _WHITESPACE_RUN.sub(" ", unicodedata.normalize("NFC", value)).strip()
    return collapsed or None


def normalize_name(value: str | None) -> str | None:
    """Normalise a person's name (§10.4 name rules).

    Identical to :func:`normalize_text`. Names support Unicode letters and combining marks
    including Indic scripts, so NFC composition is what makes two visually identical names
    compare equal.
    """
    return normalize_text(value)


def normalize_email(value: str | None) -> str | None:
    """Trim and lower-case an email (§21.1 V-EMAIL).

    Lower-casing the local part is technically lossy under RFC 5321, but the spec stores
    emails lower-case and enforces uniqueness case-insensitively (§10.5, §23.1), so the
    normalised form is the authoritative one.
    """
    normalized = normalize_text(value)
    return normalized.lower().replace(" ", "") if normalized else None


def normalize_code(value: str | None) -> str | None:
    """Upper-case and trim a master, rule or component code (§21.1 V-CODE)."""
    normalized = normalize_text(value)
    return normalized.upper().replace(" ", "") if normalized else None


def normalize_identifier(value: str | None, strategy: Normalization) -> str | None:
    """Apply one of the §21.2 normalisation strategies to an identifier."""
    normalized = normalize_text(value)
    if not normalized:
        return None
    match strategy:
        case Normalization.DIGITS_ONLY:
            stripped = _NON_DIGITS.sub("", normalized)
        case Normalization.UPPER_STRIP:
            stripped = _IDENTIFIER_SEPARATORS.sub("", normalized).upper()
        case Normalization.UPPER_STRIP_SLASH:
            stripped = _IDENTIFIER_SEPARATORS_WITH_SLASH.sub("", normalized).upper()
    return stripped or None


def normalize_phone(value: str | None, *, default_country: str = "IN") -> str | None:
    """Normalise a phone number to E.164 (§21.1 V-PHONE).

    India is the default country, so a bare ten-digit number or one written with a leading
    ``0`` becomes ``+91``-prefixed. Numbers that already carry a ``+`` are left with their
    own country code. Validation of the result is :func:`app.validation.rules.validate_phone`.
    """
    normalized = normalize_text(value)
    if not normalized:
        return None

    compact = re.sub(r"[\s\-()]", "", normalized)
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    if compact.startswith("+"):
        return "+" + _NON_DIGITS.sub("", compact[1:])

    digits = _NON_DIGITS.sub("", compact)
    if default_country != "IN":
        # Without a country context there is nothing safe to prepend; return the digits and
        # let validation reject them rather than guessing a country code.
        return digits or None
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if len(digits) == 10:
        return "+91" + digits
    return "+" + digits if digits else None


def normalize_postal_code(value: str | None) -> str | None:
    """Trim and upper-case a postal code (§21.1 V-POSTAL)."""
    normalized = normalize_text(value)
    return normalized.upper() if normalized else None


def normalize_registration_number(value: str | None) -> str | None:
    """§7.4: registration numbers are "normalized upper-case, spaces removed"."""
    normalized = normalize_text(value)
    return normalized.upper().replace(" ", "") if normalized else None


def normalize_search_name(*parts: str | None) -> str:
    """Build ``employees.full_name_normalized`` (§10.4).

    "Lower-case, NFC-normalized, whitespace collapsed concatenation of name parts." Search
    only — never displayed, and never treated as a business value.
    """
    pieces = [normalize_name(part) for part in parts]
    return " ".join(piece for piece in pieces if piece).lower()


def escape_like_pattern(value: str) -> str:
    """Escape SQL ``LIKE`` wildcards so user input matches literally (§26.3).

    "Wildcard characters (%, _, *) are escaped and treated as literals." Without this, a
    search for ``%`` would scan every row in the organization, and a crafted pattern could
    be used to probe data the caller may not list. ``*`` needs no escaping in ``LIKE`` — it
    carries no special meaning — so it passes through as the literal the spec requires.
    """
    return value.translate(_SQL_WILDCARDS)


__all__ = [
    "Normalization",
    "escape_like_pattern",
    "normalize_code",
    "normalize_email",
    "normalize_identifier",
    "normalize_name",
    "normalize_phone",
    "normalize_postal_code",
    "normalize_registration_number",
    "normalize_search_name",
    "normalize_text",
]
