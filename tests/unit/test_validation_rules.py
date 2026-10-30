"""The §21.1 validation dictionary, §21.2 normalisation and the §21.2 checksums.

All identifier values here are fictitious, per §1.4 ("All identifier examples in this
document are illustrative"). Checksum-bearing values are constructed from the algorithm so
the tests verify the implementation rather than memorised strings.
"""

from __future__ import annotations

import pytest

from app.validation.checksums import (
    gstin_check_character,
    gstin_checksum_is_valid,
    verhoeff_check_digit,
    verhoeff_is_valid,
)
from app.validation.normalizers import (
    Normalization,
    escape_like_pattern,
    normalize_code,
    normalize_email,
    normalize_identifier,
    normalize_name,
    normalize_phone,
    normalize_registration_number,
    normalize_search_name,
    normalize_text,
)
from app.validation.rules import (
    PATTERNS,
    RULES,
    RuleId,
    Severity,
    check,
    pan_fourth_character,
    validate_email,
    validate_name,
    validate_passport,
    validate_phone,
    validate_postal_code,
    validate_url,
)

pytestmark = pytest.mark.unit


def aadhaar_with_valid_checksum(first_eleven: str) -> str:
    """Build a checksum-valid fictitious Aadhaar from its first eleven digits."""
    return first_eleven + str(verhoeff_check_digit(first_eleven))


def gstin_with_valid_checksum(first_fourteen: str) -> str:
    return first_fourteen + gstin_check_character(first_fourteen)


class TestNormalization:
    """§21.1 preamble: trim, NFC, collapse whitespace."""

    def test_trims_and_collapses_whitespace(self) -> None:
        assert normalize_text("  Rajesh   Kumar  ") == "Rajesh Kumar"

    def test_blank_becomes_none(self) -> None:
        """ "Supplied but blank" and "absent" must not become two different stored states."""
        assert normalize_text("   ") is None
        assert normalize_text("") is None
        assert normalize_text(None) is None

    def test_nfc_composes_equivalent_spellings(self) -> None:
        """A decomposed and a composed spelling must become the same bytes."""
        decomposed = "José"  # e + combining acute
        composed = "José"
        assert normalize_name(decomposed) == normalize_name(composed)

    def test_indic_script_is_preserved(self) -> None:
        """NFR-20: Unicode name support including Indic scripts."""
        assert normalize_name("  राजेश   कुमार ") == "राजेश कुमार"

    def test_email_is_lowercased(self) -> None:
        assert normalize_email("  Rajesh.Patel@ABCTech.Example.IN ") == (
            "rajesh.patel@abctech.example.in"
        )

    def test_code_is_uppercased(self) -> None:
        assert normalize_code(" it-services ") == "IT-SERVICES"

    def test_registration_number_strips_spaces(self) -> None:
        """§7.4: "normalized upper-case, spaces removed"."""
        assert normalize_registration_number(" 24 aabca1234c 1z5 ") == "24AABCA1234C1Z5"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("9876543210", "+919876543210"),
            ("09876543210", "+919876543210"),
            ("+91 98765 43210", "+919876543210"),
            ("919876543210", "+919876543210"),
            ("(987) 654-3210", "+919876543210"),
            ("0091 9876543210", "+919876543210"),
        ],
    )
    def test_indian_phone_normalises_to_e164(self, raw: str, expected: str) -> None:
        assert normalize_phone(raw) == expected

    def test_foreign_phone_keeps_its_country_code(self) -> None:
        assert normalize_phone("+1 415 555 0132") == "+14155550132"

    @pytest.mark.parametrize(
        ("strategy", "raw", "expected"),
        [
            (Normalization.UPPER_STRIP, " abcpe-1234 f ", "ABCPE1234F"),
            (Normalization.DIGITS_ONLY, "2345 6789 0124", "234567890124"),
            (
                Normalization.UPPER_STRIP_SLASH,
                "gj/ahd/0012345/000/0012345",
                "GJAHD00123450000012345",
            ),
        ],
    )
    def test_identifier_strategies(self, strategy: Normalization, raw: str, expected: str) -> None:
        assert normalize_identifier(raw, strategy) == expected

    def test_search_name_is_lowercase_and_skips_missing_parts(self) -> None:
        """§10.4 full_name_normalized."""
        assert normalize_search_name("Rajesh", None, "Patel") == "rajesh patel"
        assert normalize_search_name("  RAJESH ", "Kumar", "Patel") == "rajesh kumar patel"

    def test_like_wildcards_are_escaped(self) -> None:
        """§26.3: % and _ are treated as literals."""
        escaped = escape_like_pattern("100%_raise")
        assert "%" not in escaped.replace(r"\%", "")
        assert "_" not in escaped.replace(r"\_", "")

    def test_asterisk_passes_through_as_a_literal(self) -> None:
        """* has no special meaning in LIKE, so it is already the literal §26.3 requires."""
        assert "*" in escape_like_pattern("a*b")


class TestVerhoeff:
    """§21.2 ``VERHOEFF``, used by V-AADHAAR."""

    def test_check_digit_is_reproducible(self) -> None:
        assert verhoeff_check_digit("23456789012") == verhoeff_check_digit("23456789012")

    def test_generated_number_validates(self) -> None:
        assert verhoeff_is_valid(aadhaar_with_valid_checksum("23456789012"))

    def test_single_digit_error_is_detected(self) -> None:
        """The property Verhoeff exists for: every single-digit substitution is caught."""
        valid = aadhaar_with_valid_checksum("23456789012")
        for position in range(len(valid)):
            for replacement in "0123456789":
                if replacement == valid[position]:
                    continue
                corrupted = valid[:position] + replacement + valid[position + 1 :]
                assert not verhoeff_is_valid(corrupted), f"missed substitution at {position}"

    def test_adjacent_transposition_is_detected(self) -> None:
        """The other property: adjacent transpositions are caught."""
        valid = aadhaar_with_valid_checksum("23456789012")
        for position in range(len(valid) - 1):
            if valid[position] == valid[position + 1]:
                continue
            swapped = list(valid)
            swapped[position], swapped[position + 1] = swapped[position + 1], swapped[position]
            assert not verhoeff_is_valid("".join(swapped))

    def test_non_numeric_is_invalid_not_an_exception(self) -> None:
        assert not verhoeff_is_valid("ABCD")
        assert not verhoeff_is_valid("")

    def test_non_numeric_check_digit_raises(self) -> None:
        with pytest.raises(ValueError, match="digits only"):
            verhoeff_check_digit("12A4")


class TestGstinChecksum:
    """§21.2 ``GSTIN_MOD36``."""

    def test_generated_gstin_validates(self) -> None:
        assert gstin_checksum_is_valid(gstin_with_valid_checksum("24AABCA1234C1Z"))

    def test_wrong_check_character_fails(self) -> None:
        valid = gstin_with_valid_checksum("24AABCA1234C1Z")
        wrong_char = "0" if valid[-1] != "0" else "1"
        assert not gstin_checksum_is_valid(valid[:-1] + wrong_char)

    def test_single_character_error_is_detected(self) -> None:
        valid = gstin_with_valid_checksum("24AABCA1234C1Z")
        detected = 0
        for position in range(14):
            for replacement in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                if replacement == valid[position]:
                    continue
                corrupted = valid[:position] + replacement + valid[14:]
                if not gstin_checksum_is_valid(corrupted):
                    detected += 1
        assert detected > 0

    def test_wrong_length_is_invalid(self) -> None:
        assert not gstin_checksum_is_valid("24AABCA1234C1Z")
        assert not gstin_checksum_is_valid("")

    def test_invalid_characters_raise_on_generation(self) -> None:
        with pytest.raises(ValueError, match="digits and upper-case letters"):
            gstin_check_character("24aabca1234c1z")

    def test_wrong_input_length_raises(self) -> None:
        with pytest.raises(ValueError, match="14 characters"):
            gstin_check_character("24AABC")


class TestNameRule:
    """V-NAME (§21.1) and the §10.4 name rules."""

    @pytest.mark.parametrize(
        "name",
        [
            "Rajesh",
            "Rajesh Kumar Patel",
            "Rajesh K. Patel",
            "O'Brien",
            "Anne-Marie",
            "राजेश कुमार",
            "ಶ್ರೀನಿವಾಸ",
            "José",
            "M. S. Dhoni",
        ],
    )
    def test_accepts_valid_names(self, name: str) -> None:
        assert validate_name(name).ok, f"{name!r} should be a valid name"

    def test_single_name_without_surname_is_valid(self) -> None:
        """§10.4: "No minimum surname requirement" — many Indian names have no surname."""
        assert validate_name("Rajesh").ok

    @pytest.mark.parametrize(
        ("name", "reason"),
        [
            ("Rajesh2", "contains a digit"),
            ("Raj@esh", "contains a symbol"),
            (".Rajesh", "starts with punctuation"),
            ("-Rajesh", "starts with a hyphen"),
            ("Rajesh..Kumar", "consecutive punctuation"),
            ("Rajesh--Kumar", "consecutive punctuation"),
            ("Rajesh.'s", "consecutive punctuation"),
            ("...", "no letters"),
            ("", "empty"),
            (None, "absent"),
        ],
    )
    def test_rejects_invalid_names(self, name: str | None, reason: str) -> None:
        assert not validate_name(name).ok, f"{name!r} should be rejected: {reason}"

    def test_respects_max_length(self) -> None:
        assert validate_name("A" * 100, max_length=100).ok
        assert not validate_name("A" * 101, max_length=100).ok

    def test_returns_the_normalized_value(self) -> None:
        assert validate_name("  Rajesh   Patel ").normalized == "Rajesh Patel"


class TestIdentifierRules:
    def test_pan_accepts_valid_and_normalises(self) -> None:
        outcome = check(RuleId.PAN, " abcpe1234f ")
        assert outcome.ok
        assert outcome.normalized == "ABCPE1234F"

    @pytest.mark.parametrize("value", ["ABCPE1234", "ABCPE12345", "ABCP11234F", "12345678AB"])
    def test_pan_rejects_malformed(self, value: str) -> None:
        assert not check(RuleId.PAN, value).ok

    def test_pan_fourth_character_identifies_holder_type(self) -> None:
        """§7.3/§11.2: P for an individual, C for a company."""
        assert pan_fourth_character("ABCPE1234F") == "P"
        assert pan_fourth_character("AABCA1234C") == "C"
        assert pan_fourth_character("ABC") == ""

    def test_aadhaar_requires_pattern_and_checksum(self) -> None:
        valid = aadhaar_with_valid_checksum("23456789012")
        assert check(RuleId.AADHAAR, valid).ok
        # Right length and leading digit, wrong checksum.
        wrong = valid[:-1] + ("0" if valid[-1] != "0" else "1")
        outcome = check(RuleId.AADHAAR, wrong)
        assert not outcome.ok
        assert "checksum" in outcome.message.lower()

    def test_aadhaar_rejects_leading_zero_or_one(self) -> None:
        """§21.1: first digit must be 2-9."""
        assert not check(RuleId.AADHAAR, "0" + aadhaar_with_valid_checksum("23456789012")[1:]).ok
        assert not check(RuleId.AADHAAR, "1" + aadhaar_with_valid_checksum("23456789012")[1:]).ok

    def test_aadhaar_accepts_spaced_input(self) -> None:
        """§11.2: "input spaces/hyphens stripped"."""
        valid = aadhaar_with_valid_checksum("23456789012")
        spaced = f"{valid[:4]} {valid[4:8]} {valid[8:]}"
        assert check(RuleId.AADHAAR, spaced).ok

    def test_gstin_requires_checksum(self) -> None:
        valid = gstin_with_valid_checksum("24AABCA1234C1Z")
        assert check(RuleId.GSTIN, valid).ok

    @pytest.mark.parametrize(
        ("rule_id", "valid"),
        [
            (RuleId.TAN, "AHMA12345B"),
            (RuleId.CIN, "U72200GJ2015PTC083456"),
            (RuleId.LLPIN, "AAB-1234"),
            (RuleId.UDYAM, "UDYAM-GJ-08-0012345"),
            (RuleId.UAN, "100123456789"),
            (RuleId.PFMID, "GJAHD00123450000012345"),
            (RuleId.PF_EST, "GJAHD0012345"),
            (RuleId.PF_EST, "GJAHD0012345000"),
            (RuleId.ESIC_IP, "3112345678"),
            (RuleId.ESIC_EMP, "12345678901234567"),
            (RuleId.IFSC, "SBIN0001234"),
            (RuleId.BANKACC, "30123456789"),
            (RuleId.REGNO, "GJ/LABOUR/2019/0012"),
        ],
    )
    def test_accepts_spec_examples(self, rule_id: RuleId, valid: str) -> None:
        assert check(rule_id, valid).ok, f"{rule_id.value} should accept {valid!r}"

    @pytest.mark.parametrize(
        ("rule_id", "invalid"),
        [
            (RuleId.TAN, "AHMA1234B"),
            (RuleId.CIN, "X72200GJ2015PTC083456"),
            (RuleId.LLPIN, "AAB1234"),
            (RuleId.UDYAM, "UDYAM-GJ-8-0012345"),
            (RuleId.UAN, "10012345678"),
            (RuleId.PFMID, "GJAHD001234500000123456"),
            (RuleId.ESIC_IP, "311234567"),
            (RuleId.ESIC_EMP, "1234567890123456"),
            (RuleId.IFSC, "SBIN1001234"),
            (RuleId.BANKACC, "12345678"),
        ],
    )
    def test_rejects_malformed(self, rule_id: RuleId, invalid: str) -> None:
        assert not check(rule_id, invalid).ok

    def test_ifsc_fifth_character_must_be_zero(self) -> None:
        assert check(RuleId.IFSC, "SBIN0001234").ok
        assert not check(RuleId.IFSC, "SBINA001234").ok

    def test_bank_account_length_bounds(self) -> None:
        assert check(RuleId.BANKACC, "9" * 9).ok
        assert check(RuleId.BANKACC, "9" * 18).ok
        assert not check(RuleId.BANKACC, "9" * 8).ok
        assert not check(RuleId.BANKACC, "9" * 19).ok


class TestSeverity:
    """§21.1: V-EPIC and V-DL-IN are warnings because older formats remain in circulation."""

    def test_voter_id_mismatch_warns_rather_than_blocks(self) -> None:
        outcome = check(RuleId.EPIC, "AB123456")
        assert not outcome.ok
        assert outcome.severity is Severity.WARNING
        assert outcome.warns
        assert not outcome.blocks

    def test_driving_licence_mismatch_warns(self) -> None:
        outcome = check(RuleId.DL_IN, "GJ-01-2011-001")
        assert outcome.severity is Severity.WARNING
        assert not outcome.blocks

    def test_valid_warning_rule_values_still_pass(self) -> None:
        assert check(RuleId.EPIC, "ABC1234567").ok
        assert check(RuleId.DL_IN, "GJ0120110012345").ok

    def test_error_severity_blocks(self) -> None:
        outcome = check(RuleId.PAN, "nonsense")
        assert outcome.blocks
        assert not outcome.warns

    def test_every_rule_has_a_declared_severity(self) -> None:
        for rule_id in RuleId:
            assert RULES[rule_id].severity in (Severity.ERROR, Severity.WARNING)

    def test_only_epic_and_dl_are_warnings(self) -> None:
        warning_rules = {rid for rid, rule in RULES.items() if rule.severity is Severity.WARNING}
        assert warning_rules == {RuleId.EPIC, RuleId.DL_IN}


class TestCountryDependentRules:
    def test_indian_phone_must_start_six_to_nine(self) -> None:
        assert validate_phone("9876543210").ok
        assert validate_phone("8876543210").ok
        assert not validate_phone("5876543210").ok
        assert not validate_phone("1234567890").ok

    def test_foreign_phone_uses_e164(self) -> None:
        assert validate_phone("+14155550132").ok
        assert not validate_phone("+0123456789").ok

    def test_indian_pin_code(self) -> None:
        assert validate_postal_code("382021").ok
        assert not validate_postal_code("082021").ok
        assert not validate_postal_code("38202").ok

    def test_foreign_postal_code_is_permissive(self) -> None:
        assert validate_postal_code("SW1A 1AA", country="GB").ok
        assert validate_postal_code("94105", country="US").ok

    def test_indian_passport_pattern(self) -> None:
        assert validate_passport("K1234567").ok
        assert not validate_passport("KK123456").ok

    def test_foreign_passport_is_permissive(self) -> None:
        assert validate_passport("X1Y2Z3A4B5", issuing_country="US").ok
        assert not validate_passport("AB12", issuing_country="US").ok


class TestEmailAndUrl:
    @pytest.mark.parametrize(
        "email",
        [
            "rajesh.patel@example.com",
            "hr@abctech.example.in",
            "first+tag@sub.domain.co.in",
            "a@b.co",
        ],
    )
    def test_accepts_valid_emails(self, email: str) -> None:
        assert validate_email(email).ok

    @pytest.mark.parametrize(
        "email",
        ["plainaddress", "no@domain", "@example.com", "user@@example.com", "user@.com", ""],
    )
    def test_rejects_invalid_emails(self, email: str) -> None:
        assert not validate_email(email).ok

    def test_email_length_cap(self) -> None:
        long_local = "a" * 245
        assert not validate_email(f"{long_local}@example.com").ok

    def test_domain_must_contain_a_dot(self) -> None:
        outcome = validate_email("user@localhost")
        assert not outcome.ok
        assert "dot" in outcome.message.lower()

    @pytest.mark.parametrize(
        "url", ["https://abctech.example.in", "http://example.com/path?q=1", "https://a.b.c:8443/x"]
    )
    def test_accepts_valid_urls(self, url: str) -> None:
        assert validate_url(url).ok

    @pytest.mark.parametrize(
        "url", ["ftp://example.com", "example.com", "javascript:alert(1)", "https://nodot", ""]
    )
    def test_rejects_invalid_urls(self, url: str) -> None:
        assert not validate_url(url).ok


class TestRuleRegistryIntegrity:
    def test_every_spec_rule_id_is_registered(self) -> None:
        """All 24 rows of the §21.1 dictionary."""
        assert set(RULES) == set(RuleId)
        assert len(RuleId) == 24

    def test_pattern_rules_have_patterns(self) -> None:
        """Rules without a compiled pattern must have a dedicated validator in `check`."""
        function_backed = {
            RuleId.NAME,
            RuleId.POSTAL,
            RuleId.URL,
            RuleId.PASSPORT,
        }
        for rule_id in RuleId:
            if rule_id in function_backed:
                continue
            assert rule_id in PATTERNS, f"{rule_id.value} has neither a pattern nor a validator"

    def test_check_dispatches_every_rule_without_error(self) -> None:
        for rule_id in RuleId:
            outcome = check(rule_id, "SOMEVALUE123")
            assert outcome.rule_id is rule_id

    def test_absent_value_is_reported_not_crashed(self) -> None:
        for rule_id in RuleId:
            assert not check(rule_id, None).ok
