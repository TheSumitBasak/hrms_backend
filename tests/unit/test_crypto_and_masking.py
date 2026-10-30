"""Field encryption, blind indexes and masked rendering (§11.6, §14.1, §23.7)."""

from __future__ import annotations

import uuid

import pytest

from app.core.crypto import (
    AES_KEY_BYTES,
    BLIND_INDEX_BYTES,
    BlindIndexer,
    DecryptionError,
    FieldCipher,
    FieldContext,
    constant_time_equals,
    derive_subkey,
    generate_master_key,
    generate_opaque_token,
    hash_opaque_token,
    sha256_hex,
)
from app.core.masking import (
    AUDIT_REDACTED,
    UNKNOWN_MASK,
    IdentifierType,
    audit_safe_value,
    last_four_of,
    mask_bank_account,
    mask_document_number,
    mask_identifier,
)
from app.integrations.keys import EnvKeyProvider, KmsKeyProvider

pytestmark = pytest.mark.unit

ORG_A = uuid.UUID("0192c3a4-0000-7000-8000-00000000000a")
ORG_B = uuid.UUID("0192c3a4-0000-7000-8000-00000000000b")
GROUP_A = uuid.UUID("0192c3a4-0000-7000-8000-0000000000aa")
GROUP_B = uuid.UUID("0192c3a4-0000-7000-8000-0000000000bb")
EMP_1 = uuid.UUID("0192c3a4-0000-7000-8000-000000000001")
EMP_2 = uuid.UUID("0192c3a4-0000-7000-8000-000000000002")


@pytest.fixture
def keys() -> EnvKeyProvider:
    """Deterministic non-production key material, generated per test run."""
    return EnvKeyProvider(
        encryption_master_key=b"\x01" * AES_KEY_BYTES,
        blind_index_master_key=b"\x02" * AES_KEY_BYTES,
        key_version=1,
    )


@pytest.fixture
def cipher(keys: EnvKeyProvider) -> FieldCipher:
    return FieldCipher(keys)


def pan_context(organization_id: uuid.UUID = ORG_A, employee_id: uuid.UUID = EMP_1) -> FieldContext:
    return FieldContext(
        organization_id=organization_id,
        entity_type="employee_identifier",
        entity_id=employee_id,
        field="PAN",
    )


class TestKeyProviderConstruction:
    def test_rejects_identical_master_keys(self) -> None:
        """§23.7: blind-index keys must be separate from encryption keys."""
        with pytest.raises(ValueError, match="must differ"):
            EnvKeyProvider(
                encryption_master_key=b"\x01" * AES_KEY_BYTES,
                blind_index_master_key=b"\x01" * AES_KEY_BYTES,
                key_version=1,
            )

    @pytest.mark.parametrize("length", [16, 31, 33, 64])
    def test_rejects_wrong_key_length(self, length: int) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            EnvKeyProvider(
                encryption_master_key=b"\x01" * length,
                blind_index_master_key=b"\x02" * AES_KEY_BYTES,
                key_version=1,
            )

    def test_rejects_zero_key_version(self) -> None:
        with pytest.raises(ValueError, match="key_version"):
            EnvKeyProvider(
                encryption_master_key=b"\x01" * AES_KEY_BYTES,
                blind_index_master_key=b"\x02" * AES_KEY_BYTES,
                key_version=0,
            )

    def test_per_organization_keys_differ(self, keys: EnvKeyProvider) -> None:
        """§23.7: one DEK per organization."""
        assert keys.data_key(ORG_A, 1) != keys.data_key(ORG_B, 1)

    def test_key_versions_produce_different_keys(self, keys: EnvKeyProvider) -> None:
        assert keys.data_key(ORG_A, 1) != keys.data_key(ORG_A, 2)

    def test_data_and_blind_keys_differ_for_the_same_scope(self, keys: EnvKeyProvider) -> None:
        assert keys.data_key(ORG_A, 1) != keys.blind_index_key(ORG_A, 1)

    def test_keys_are_stable_across_calls(self, keys: EnvKeyProvider) -> None:
        assert keys.data_key(ORG_A, 1) == keys.data_key(ORG_A, 1)

    def test_derive_subkey_rejects_short_master(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            derive_subkey(b"short", info=b"x")

    def test_generated_master_key_is_correct_length(self) -> None:
        assert len(generate_master_key()) == AES_KEY_BYTES

    def test_kms_provider_fails_loudly_rather_than_falling_back(self) -> None:
        """A production deployment missing its KMS client must not appear to work."""
        provider = KmsKeyProvider(kms_key_id="alias/ems", key_version=1)
        with pytest.raises(NotImplementedError, match="KMS"):
            provider.data_key(ORG_A, 1)
        with pytest.raises(NotImplementedError, match="KMS"):
            provider.blind_index_key(GROUP_A, 1)


class TestFieldEncryption:
    def test_round_trip(self, cipher: FieldCipher) -> None:
        blob, version = cipher.encrypt("ABCPE1234F", pan_context())
        assert version == 1
        assert cipher.decrypt(blob, pan_context(), version) == "ABCPE1234F"

    def test_ciphertext_does_not_contain_plaintext(self, cipher: FieldCipher) -> None:
        blob, _ = cipher.encrypt("ABCPE1234F", pan_context())
        assert b"ABCPE1234F" not in blob

    def test_same_value_encrypts_differently_each_time(self, cipher: FieldCipher) -> None:
        """A random nonce per write means ciphertext equality cannot be used to compare
        values; that is what the blind index is for."""
        first, _ = cipher.encrypt("ABCPE1234F", pan_context())
        second, _ = cipher.encrypt("ABCPE1234F", pan_context())
        assert first != second

    def test_unicode_round_trip(self, cipher: FieldCipher) -> None:
        """NFR-20: Unicode including Indic scripts."""
        context = FieldContext(ORG_A, "employee_document", EMP_1, "document_number")
        blob, version = cipher.encrypt("राजेश-१२३", context)
        assert cipher.decrypt(blob, context, version) == "राजेश-१२३"

    def test_tampered_ciphertext_is_rejected(self, cipher: FieldCipher) -> None:
        blob, version = cipher.encrypt("ABCPE1234F", pan_context())
        tampered = bytearray(blob)
        tampered[-1] ^= 0x01
        with pytest.raises(DecryptionError):
            cipher.decrypt(bytes(tampered), pan_context(), version)

    def test_truncated_ciphertext_is_rejected(self, cipher: FieldCipher) -> None:
        with pytest.raises(DecryptionError, match="too short"):
            cipher.decrypt(b"\x00" * 8, pan_context(), 1)

    def test_wrong_key_version_is_rejected(self, cipher: FieldCipher) -> None:
        blob, _ = cipher.encrypt("ABCPE1234F", pan_context())
        with pytest.raises(DecryptionError):
            cipher.decrypt(blob, pan_context(), 2)

    def test_needs_rotation_flags_old_versions(self, cipher: FieldCipher) -> None:
        assert cipher.needs_rotation(0)
        assert not cipher.needs_rotation(1)


class TestAadBindsCiphertextToItsLocation:
    """The security property behind tenant-safe ciphertext: a blob is useless elsewhere."""

    def test_ciphertext_cannot_be_moved_to_another_organization(self, cipher: FieldCipher) -> None:
        blob, version = cipher.encrypt("ABCPE1234F", pan_context(organization_id=ORG_A))
        with pytest.raises(DecryptionError):
            cipher.decrypt(blob, pan_context(organization_id=ORG_B), version)

    def test_ciphertext_cannot_be_moved_to_another_employee(self, cipher: FieldCipher) -> None:
        blob, version = cipher.encrypt("ABCPE1234F", pan_context(employee_id=EMP_1))
        with pytest.raises(DecryptionError):
            cipher.decrypt(blob, pan_context(employee_id=EMP_2), version)

    def test_ciphertext_cannot_be_moved_to_another_field(self, cipher: FieldCipher) -> None:
        """A PAN blob pasted into the UAN column must not decrypt."""
        blob, version = cipher.encrypt("ABCPE1234F", pan_context())
        uan_context = FieldContext(ORG_A, "employee_identifier", EMP_1, "UAN")
        with pytest.raises(DecryptionError):
            cipher.decrypt(blob, uan_context, version)

    def test_ciphertext_cannot_be_moved_to_another_entity_type(self, cipher: FieldCipher) -> None:
        blob, version = cipher.encrypt("ABCPE1234F", pan_context())
        other = FieldContext(ORG_A, "employee_bank_account", EMP_1, "PAN")
        with pytest.raises(DecryptionError):
            cipher.decrypt(blob, other, version)

    def test_aad_fields_cannot_be_shifted_by_embedded_separators(self) -> None:
        """A field name containing the separator must not forge another context's AAD."""
        first = FieldContext(ORG_A, "employee_identifier", EMP_1, "PAN|extra")
        second = FieldContext(ORG_A, "employee_identifier|PAN", EMP_1, "extra")
        assert first.aad() != second.aad()


class TestBlindIndex:
    def test_digest_length(self, keys: EnvKeyProvider) -> None:
        digest = BlindIndexer(keys).compute("ABCPE1234F", scope_id=GROUP_A, identifier_type="PAN")
        assert len(digest) == BLIND_INDEX_BYTES

    def test_same_value_same_scope_matches(self, keys: EnvKeyProvider) -> None:
        indexer = BlindIndexer(keys)
        first = indexer.compute("ABCPE1234F", scope_id=GROUP_A, identifier_type="PAN")
        second = indexer.compute("ABCPE1234F", scope_id=GROUP_A, identifier_type="PAN")
        assert BlindIndexer.matches(first, second)

    def test_different_values_do_not_match(self, keys: EnvKeyProvider) -> None:
        indexer = BlindIndexer(keys)
        first = indexer.compute("ABCPE1234F", scope_id=GROUP_A, identifier_type="PAN")
        second = indexer.compute("ABCPE1234G", scope_id=GROUP_A, identifier_type="PAN")
        assert not BlindIndexer.matches(first, second)

    def test_cross_group_matching_is_impossible(self, keys: EnvKeyProvider) -> None:
        """§11.5 final column: other org, no common group -> "Not checked (different
        blind-index key)". The keys differ, so the digests cannot collide."""
        indexer = BlindIndexer(keys)
        in_group_a = indexer.compute("ABCPE1234F", scope_id=GROUP_A, identifier_type="PAN")
        in_group_b = indexer.compute("ABCPE1234F", scope_id=GROUP_B, identifier_type="PAN")
        assert not BlindIndexer.matches(in_group_a, in_group_b)

    def test_same_digits_as_different_identifier_types_do_not_collide(
        self, keys: EnvKeyProvider
    ) -> None:
        indexer = BlindIndexer(keys)
        as_uan = indexer.compute("100123456789", scope_id=GROUP_A, identifier_type="UAN")
        as_aadhaar = indexer.compute("100123456789", scope_id=GROUP_A, identifier_type="AADHAAR")
        assert not BlindIndexer.matches(as_uan, as_aadhaar)

    def test_key_rotation_changes_the_digest(self, keys: EnvKeyProvider) -> None:
        """§23.7: blind-index key rotation requires full recomputation."""
        indexer = BlindIndexer(keys)
        v1 = indexer.compute("ABCPE1234F", scope_id=GROUP_A, identifier_type="PAN", key_version=1)
        v2 = indexer.compute("ABCPE1234F", scope_id=GROUP_A, identifier_type="PAN", key_version=2)
        assert v1 != v2

    def test_empty_value_rejected(self, keys: EnvKeyProvider) -> None:
        with pytest.raises(ValueError, match="empty value"):
            BlindIndexer(keys).compute("", scope_id=GROUP_A, identifier_type="PAN")


class TestMaskFormats:
    """§11.6 masked display table, verified against the spec's own examples."""

    @pytest.mark.parametrize(
        ("identifier_type", "value", "expected"),
        [
            (IdentifierType.PAN, "ABCPE1234F", "******234F"),
            (IdentifierType.AADHAAR, "234567890124", "XXXX XXXX 0124"),
            (IdentifierType.UAN, "100123456789", "********6789"),
            (IdentifierType.PF_MEMBER_ID, "GJAHD00123450000012345", "******************2345"),
            (IdentifierType.ESIC_IP, "3112345678", "******5678"),
            (IdentifierType.PASSPORT, "K1234567", "****4567"),
        ],
    )
    def test_spec_examples(
        self, identifier_type: IdentifierType, value: str, expected: str
    ) -> None:
        assert mask_identifier(identifier_type, last_four_of(value)) == expected

    def test_aadhaar_mask_never_exposes_more_than_four_digits(self) -> None:
        """§11.6: full Aadhaar must never appear anywhere."""
        masked = mask_identifier(IdentifierType.AADHAAR, last_four_of("234567890124"))
        digits = [character for character in masked if character.isdigit()]
        assert len(digits) == 4
        assert "234567890124" not in masked

    def test_voter_id_and_licence_show_last_four(self) -> None:
        assert mask_identifier(IdentifierType.VOTER_ID, last_four_of("ABC1234567")).endswith("4567")
        assert mask_identifier(
            IdentifierType.DRIVING_LICENSE, last_four_of("GJ0120110012345")
        ).endswith("2345")

    def test_bank_account_mask(self) -> None:
        """§14.1: XXXXXXX6789."""
        assert mask_bank_account(last_four_of("30123456789")) == "XXXXXXX6789"

    def test_document_number_mask_shows_last_four(self) -> None:
        assert mask_document_number(last_four_of("CERT-00098765")).endswith("8765")

    def test_missing_last_four_renders_a_generic_mask(self) -> None:
        assert mask_identifier(IdentifierType.PAN, None) == UNKNOWN_MASK
        assert mask_bank_account("") == UNKNOWN_MASK

    def test_visible_chars_is_configurable_down_to_zero(self) -> None:
        """§21.2 mask_visible_chars ranges 0-4."""
        fully_hidden = mask_identifier(IdentifierType.PAN, "234F", visible_chars=0)
        assert fully_hidden == "*" * 10
        assert not any(character.isalnum() for character in fully_hidden)

    def test_visible_chars_cannot_exceed_four(self) -> None:
        masked = mask_identifier(IdentifierType.PAN, "234F", visible_chars=99)
        assert masked == "******234F"

    def test_last_four_pads_short_values(self) -> None:
        """value_last_four is CHAR(4) in §11.1."""
        assert len(last_four_of("12")) == 4

    def test_every_identifier_type_has_a_mask_format(self) -> None:
        for identifier_type in IdentifierType:
            assert mask_identifier(identifier_type, "1234")


class TestAuditSafeValues:
    """§20.3: audit records must not contain full sensitive identifiers."""

    def test_identifier_change_is_recorded_masked(self) -> None:
        recorded = audit_safe_value("ABCPE1234F", identifier_type=IdentifierType.PAN)
        assert recorded == "******234F"
        assert recorded != "ABCPE1234F"

    def test_aadhaar_is_never_recorded_in_full(self) -> None:
        recorded = audit_safe_value("234567890124", identifier_type=IdentifierType.AADHAAR)
        assert recorded is not None
        assert "234567890124" not in recorded

    def test_other_sensitive_values_are_redacted_entirely(self) -> None:
        assert audit_safe_value("30123456789") == AUDIT_REDACTED

    def test_none_stays_none(self) -> None:
        assert audit_safe_value(None) is None


class TestTokenHelpers:
    def test_opaque_tokens_are_unique_and_long(self) -> None:
        tokens = {generate_opaque_token() for _ in range(64)}
        assert len(tokens) == 64
        assert all(len(token) >= 40 for token in tokens)

    def test_token_hash_is_stable_and_hides_the_token(self) -> None:
        token = generate_opaque_token()
        digest = hash_opaque_token(token)
        assert digest == hash_opaque_token(token)
        assert token not in digest
        assert len(digest) == 64

    def test_constant_time_equals(self) -> None:
        assert constant_time_equals("abc", "abc")
        assert not constant_time_equals("abc", "abd")

    def test_sha256_hex_matches_known_empty_digest(self) -> None:
        assert sha256_hex(b"") == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
