"""Masked rendering of sensitive values (§11.6, §14.1, §15.3).

Masks are built **only** from the stored ``value_last_four`` column. No ciphertext is opened
to render a mask (§11.6: "no decryption occurs for masked display"), which means a caller
holding ``identity.view_masked`` never causes a key to be used at all.

Because the plaintext length is unknown at mask time, each identifier type declares the total
width its mask should present. Fixed-format Indian identifiers have a known width; for
variable-length values (non-Indian passports, driving licences) a fixed filler width is used
and documented below.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

DEFAULT_VISIBLE_CHARS = 4
"""§21.2 ``mask_visible_chars`` defaults to 4 and is configurable from 0 to 4."""

MAX_VISIBLE_CHARS = 4

UNKNOWN_MASK = "****"
"""Rendered when no last-four fragment is stored, so a response never leaks "empty means
absent" as a different shape from "present but hidden"."""


class IdentifierType(StrEnum):
    """§11.1 ``identifier_type`` enum."""

    PAN = "PAN"
    AADHAAR = "AADHAAR"
    UAN = "UAN"
    PF_MEMBER_ID = "PF_MEMBER_ID"
    PREVIOUS_PF_MEMBER_ID = "PREVIOUS_PF_MEMBER_ID"
    ESIC_IP = "ESIC_IP"
    PASSPORT = "PASSPORT"
    VOTER_ID = "VOTER_ID"
    DRIVING_LICENSE = "DRIVING_LICENSE"


@dataclass(frozen=True, slots=True)
class MaskFormat:
    """How one value type renders when masked.

    ``total_width`` is the length of the rendered mask including the visible tail.
    ``fill`` is the character used for the hidden part. ``group`` inserts a space every
    ``group`` characters, which Aadhaar needs to render as ``XXXX XXXX 0124``.
    """

    total_width: int
    fill: str = "*"
    group: int | None = None

    def render(self, last_four: str | None, visible_chars: int = DEFAULT_VISIBLE_CHARS) -> str:
        visible = max(0, min(visible_chars, MAX_VISIBLE_CHARS))
        tail = (last_four or "")[-visible:] if visible else ""
        if visible and not tail:
            return UNKNOWN_MASK
        hidden_width = max(self.total_width - len(tail), 0)
        masked = self.fill * hidden_width + tail
        if self.group:
            return " ".join(masked[i : i + self.group] for i in range(0, len(masked), self.group))
        return masked


# Widths come straight from the §11.6 examples. PASSPORT uses the Indian 8-character format
# (`****4567`); longer foreign passport numbers render at the same width rather than
# disclosing their true length, which the mask is not meant to reveal. DRIVING_LICENSE has no
# example in §11.6 beyond "last 4 visible", so it uses the same conservative 8-wide mask.
MASK_FORMATS: dict[IdentifierType, MaskFormat] = {
    IdentifierType.PAN: MaskFormat(total_width=10),
    IdentifierType.AADHAAR: MaskFormat(total_width=12, fill="X", group=4),
    IdentifierType.UAN: MaskFormat(total_width=12),
    IdentifierType.PF_MEMBER_ID: MaskFormat(total_width=22),
    IdentifierType.PREVIOUS_PF_MEMBER_ID: MaskFormat(total_width=22),
    IdentifierType.ESIC_IP: MaskFormat(total_width=10),
    IdentifierType.PASSPORT: MaskFormat(total_width=8),
    IdentifierType.VOTER_ID: MaskFormat(total_width=10),
    IdentifierType.DRIVING_LICENSE: MaskFormat(total_width=8),
}

BANK_ACCOUNT_MASK = MaskFormat(total_width=11, fill="X")
"""§14.1: ``XXXXXXX6789``."""

DOCUMENT_NUMBER_MASK = MaskFormat(total_width=8)
"""§15.3: "masked display last 4"; no example given, so the conservative 8-wide mask."""


def mask_identifier(
    identifier_type: IdentifierType | str,
    last_four: str | None,
    *,
    visible_chars: int = DEFAULT_VISIBLE_CHARS,
) -> str:
    """Render the §11.6 mask for an identifier."""
    key = (
        identifier_type
        if isinstance(identifier_type, IdentifierType)
        else IdentifierType(identifier_type)
    )
    return MASK_FORMATS[key].render(last_four, visible_chars)


def mask_bank_account(last_four: str | None) -> str:
    """Render the §14.1 bank account mask."""
    return BANK_ACCOUNT_MASK.render(last_four)


def mask_document_number(last_four: str | None) -> str:
    """Render the §15.3 document number mask."""
    return DOCUMENT_NUMBER_MASK.render(last_four)


def last_four_of(normalized_value: str) -> str:
    """The stored ``value_last_four`` fragment: the final four characters.

    Values shorter than four characters are left-padded so the column is always four
    characters wide, matching ``CHAR(4)`` in §11.1. Normalisation (§21.1) must already have
    stripped spaces and separators, or the fragment will not match a later search.
    """
    return normalized_value[-4:].rjust(4, "0") if normalized_value else ""


AUDIT_REDACTED = "***changed***"
"""§20.3: the placeholder recorded in audit payloads for a sensitive field change."""


def audit_safe_value(
    value: str | None,
    *,
    identifier_type: IdentifierType | str | None = None,
) -> str | None:
    """Reduce a sensitive value to something safe for an audit record (§20.3).

    Identifier changes are recorded as the field name plus a masked before/after; anything
    else sensitive is recorded as :data:`AUDIT_REDACTED`. Full PAN, Aadhaar, bank account,
    passport, UAN, ESIC and PF values must never reach an audit row.
    """
    if value is None:
        return None
    if identifier_type is not None:
        return mask_identifier(identifier_type, last_four_of(value))
    return AUDIT_REDACTED


__all__ = [
    "AUDIT_REDACTED",
    "BANK_ACCOUNT_MASK",
    "DEFAULT_VISIBLE_CHARS",
    "DOCUMENT_NUMBER_MASK",
    "MASK_FORMATS",
    "MAX_VISIBLE_CHARS",
    "UNKNOWN_MASK",
    "IdentifierType",
    "MaskFormat",
    "audit_safe_value",
    "last_four_of",
    "mask_bank_account",
    "mask_document_number",
    "mask_identifier",
]
