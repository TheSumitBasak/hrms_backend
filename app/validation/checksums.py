"""Checksum algorithms named by §21.2 ``checksum_algorithm``: ``VERHOEFF``, ``GSTIN_MOD36``.

Both are published, standard algorithms implemented from their specifications. They are a
*format* check only: a value can pass its checksum and still not exist, so neither result is
treated as verification of the underlying document (§11.1 ``verification_status`` is a manual
flag; §1.3 puts external verification out of scope).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------- Verhoeff
# The Verhoeff scheme over the dihedral group D5, used by UIDAI for Aadhaar numbers.

_D5_MULTIPLICATION: tuple[tuple[int, ...], ...] = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)

_D5_INVERSE: tuple[int, ...] = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)

_D5_PERMUTATION: tuple[tuple[int, ...], ...] = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_check_digit(digits: str) -> int:
    """The Verhoeff check digit for ``digits`` (which must exclude the check digit)."""
    if not digits.isdigit():
        raise ValueError("Verhoeff input must contain digits only")
    checksum = 0
    for position, character in enumerate(reversed(digits), start=1):
        checksum = _D5_MULTIPLICATION[checksum][_D5_PERMUTATION[position % 8][int(character)]]
    return _D5_INVERSE[checksum]


def verhoeff_is_valid(value: str) -> bool:
    """Whether ``value``'s trailing digit is a correct Verhoeff check digit.

    Used by V-AADHAAR (§21.1). Returns ``False`` rather than raising for non-numeric input,
    so a caller can run the pattern check and the checksum check independently.
    """
    if len(value) < 2 or not value.isdigit():
        return False
    body, check = value[:-1], int(value[-1])
    return verhoeff_check_digit(body) == check


# ---------------------------------------------------------------------------- GSTIN mod 36
# The GSTN check character: base-36 over 0-9 then A-Z, weights alternating 1 and 2 from the
# left, each product folded by quotient + remainder, summed, then the complement mod 36.

_GSTIN_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_GSTIN_BASE = 36
GSTIN_LENGTH = 15


def gstin_check_character(first_fourteen: str) -> str:
    """The 15th character of a GSTIN, given its first fourteen."""
    if len(first_fourteen) != GSTIN_LENGTH - 1:
        raise ValueError("GSTIN checksum input must be exactly 14 characters")

    total = 0
    for position, character in enumerate(first_fourteen):
        try:
            value = _GSTIN_ALPHABET.index(character)
        except ValueError as exc:
            raise ValueError("GSTIN may contain only digits and upper-case letters") from exc
        weight = 1 if position % 2 == 0 else 2
        product = value * weight
        total += product // _GSTIN_BASE + product % _GSTIN_BASE

    remainder = total % _GSTIN_BASE
    return _GSTIN_ALPHABET[(_GSTIN_BASE - remainder) % _GSTIN_BASE]


def gstin_checksum_is_valid(value: str) -> bool:
    """Whether a 15-character GSTIN carries the correct check character."""
    if len(value) != GSTIN_LENGTH:
        return False
    try:
        return gstin_check_character(value[:-1]) == value[-1]
    except ValueError:
        return False


__all__ = [
    "GSTIN_LENGTH",
    "gstin_check_character",
    "gstin_checksum_is_valid",
    "verhoeff_check_digit",
    "verhoeff_is_valid",
]
