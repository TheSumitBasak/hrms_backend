"""Audit event construction and the §20.3 redaction rules.

The guarantee under test: an audit row describes a change without becoming a second,
permanent copy of the personal data. Audit tables are append-only, so a leak committed here
cannot be corrected by an update.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.audit.actions import (
    PLATFORM_ACTIONS,
    REASON_REQUIRED_ACTIONS,
    SALARY_SCOPED_ACTIONS,
    AuditAction,
)
from app.audit.events import (
    MASKED_IDENTIFIER_FIELDS,
    NEVER_AUDITED_FIELDS,
    AuditEvent,
    assert_audit_payload_is_safe,
    build_change_set,
    format_actor_roles,
)
from app.core.masking import AUDIT_REDACTED
from app.core.permissions import Role

pytestmark = pytest.mark.unit

ACTOR = uuid.UUID("0192c3a4-0000-7000-8000-0000000000f1")
ENTITY = uuid.UUID("0192c3a4-0000-7000-8000-0000000000e1")
ORG = uuid.UUID("0192c3a4-0000-7000-8000-00000000000a")


class TestActionVocabulary:
    SPEC_SAMPLE = (
        "EMPLOYEE_CREATED",
        "EMPLOYEE_STATUS_CORRECTED",
        "EMPLOYEE_ORG_LINKED",
        "AGE_EXCEPTION_DECIDED",
        "IDENTIFIER_REVEALED",
        "BANK_ACCOUNT_REVEALED",
        "RESTRICTED_PERSONAL_VIEWED",
        "BREAK_GLASS_GRANTED",
        "BREAK_GLASS_ACCESS",
        "DUPLICATE_OVERRIDE",
        "BANK_PRIMARY_CHANGED",
        "NOMINATION_SET_REPLACED",
        "DOCUMENT_QUARANTINED",
        "DOCUMENT_VERSION_ADDED",
        "SALARY_BOUNDARY_ADJUSTED",
        "SALARY_REVISION_CORRECTED",
        "EMPLOYEE_CODE_REASSIGNED",
        "EMPLOYEE_CODE_RETIRED",
        "ID_RULE_PUBLISHED",
        "SETTINGS_UPDATED",
        "IDENTIFIER_POLICY_UPDATED",
        "ORG_ACCESS_REVOKED",
        "ROLE_PERMISSIONS_CHANGED",
        "LOGIN_FAILURE",
        "MFA_RESET",
    )

    @pytest.mark.parametrize("action", SPEC_SAMPLE)
    def test_spec_action_exists(self, action: str) -> None:
        assert AuditAction(action)

    def test_identifier_searched_is_present(self) -> None:
        """§26.2 mandates it even though the §20.2 table omits it (DEC-016)."""
        assert AuditAction.IDENTIFIER_SEARCHED

    def test_salary_scope_covers_every_salary_action(self) -> None:
        """§20.3: audit views of salary events require salary.view."""
        assert AuditAction.SALARY_REVISION_CONFIRMED in SALARY_SCOPED_ACTIONS
        assert AuditAction.SALARY_BOUNDARY_ADJUSTED in SALARY_SCOPED_ACTIONS
        assert AuditAction.EMPLOYEE_CREATED not in SALARY_SCOPED_ACTIONS

    def test_platform_actions_are_what_an_ssa_may_see(self) -> None:
        """§5.3 note 3: organization creation, role definitions, break-glass grants."""
        assert AuditAction.ORG_CREATED in PLATFORM_ACTIONS
        assert AuditAction.ROLE_PERMISSIONS_CHANGED in PLATFORM_ACTIONS
        assert AuditAction.BREAK_GLASS_GRANTED in PLATFORM_ACTIONS
        # Employee data events are not visible to an SSA outside break-glass.
        assert AuditAction.EMPLOYEE_UPDATED not in PLATFORM_ACTIONS
        assert AuditAction.IDENTIFIER_REVEALED not in PLATFORM_ACTIONS

    def test_action_values_fit_the_column(self) -> None:
        """§20.1 types ``action`` as VARCHAR(60)."""
        assert all(len(action.value) <= 60 for action in AuditAction)


class TestReasonEnforcement:
    """§20.1: a reason is mandatory for reveals, overrides, corrections and reassignments."""

    @pytest.mark.parametrize("action", sorted(REASON_REQUIRED_ACTIONS))
    def test_missing_reason_is_refused(self, action: AuditAction) -> None:
        with pytest.raises(ValueError, match="requires a reason"):
            AuditEvent(
                action=action,
                entity_type="employee",
                entity_id=ENTITY,
                actor_user_id=ACTOR,
                actor_role_codes="HRA",
            )

    @pytest.mark.parametrize("action", sorted(REASON_REQUIRED_ACTIONS))
    def test_blank_reason_is_refused(self, action: AuditAction) -> None:
        with pytest.raises(ValueError, match="requires a reason"):
            AuditEvent(
                action=action,
                entity_type="employee",
                entity_id=ENTITY,
                actor_user_id=ACTOR,
                actor_role_codes="HRA",
                reason="   ",
            )

    def test_reason_supplied_is_accepted(self) -> None:
        event = AuditEvent(
            action=AuditAction.IDENTIFIER_REVEALED,
            entity_type="employee_identifier",
            entity_id=ENTITY,
            actor_user_id=ACTOR,
            actor_role_codes="HRA",
            reason="Payroll onboarding verification",
        )
        assert event.reason == "Payroll onboarding verification"

    def test_ordinary_action_needs_no_reason(self) -> None:
        assert AuditEvent(
            action=AuditAction.EMPLOYEE_CREATED,
            entity_type="employee",
            entity_id=ENTITY,
            actor_user_id=ACTOR,
            actor_role_codes="HRE",
        )


class TestActorRoles:
    def test_roles_are_sorted(self) -> None:
        """§20.1: "Roles active at time of action, sorted"."""
        formatted = format_actor_roles(frozenset({Role.HR_EXECUTIVE, Role.HR_ADMIN}))
        assert formatted == "HRA,HRE"

    def test_single_role(self) -> None:
        assert format_actor_roles(frozenset({Role.HR_ADMIN})) == "HRA"

    def test_fits_the_column(self) -> None:
        """§20.1 types ``actor_role_codes`` as VARCHAR(100)."""
        assert len(format_actor_roles(frozenset(Role))) <= 100


class TestChangeSetRedaction:
    def test_ordinary_fields_are_recorded_as_is(self) -> None:
        changes = build_change_set(
            {"designation_id": "old", "work_mode": "ON_SITE"},
            {"designation_id": "new", "work_mode": "HYBRID"},
        )
        assert sorted(changes.changed_fields) == ["designation_id", "work_mode"]
        assert changes.before_values["work_mode"] == "ON_SITE"
        assert changes.after_values["work_mode"] == "HYBRID"

    def test_unchanged_fields_are_omitted(self) -> None:
        changes = build_change_set({"a": 1, "b": 2}, {"a": 1, "b": 3})
        assert changes.changed_fields == ["b"]

    def test_identical_mappings_produce_an_empty_change_set(self) -> None:
        assert build_change_set({"a": 1}, {"a": 1}).is_empty

    def test_identifiers_are_recorded_masked(self) -> None:
        """§20.3: "recorded as field name + masked before/after (last 4)"."""
        changes = build_change_set({"pan": "ABCPE1234F"}, {"pan": "ABCPE9999Z"})
        assert changes.before_values["pan"] == "******234F"
        assert changes.after_values["pan"] == "******999Z"

    def test_full_aadhaar_never_reaches_an_audit_payload(self) -> None:
        """AC-27."""
        aadhaar = "234567890124"
        changes = build_change_set({"aadhaar_number": None}, {"aadhaar_number": aadhaar})
        rendered = repr(changes.after_values)
        assert aadhaar not in rendered
        assert changes.after_values["aadhaar_number"] == "XXXX XXXX 0124"

    @pytest.mark.parametrize("field_name", sorted(MASKED_IDENTIFIER_FIELDS))
    def test_every_identifier_field_is_masked(self, field_name: str) -> None:
        changes = build_change_set({field_name: None}, {field_name: "ABCDE1234567890F1234"})
        recorded = str(changes.after_values[field_name])
        assert "*" in recorded or "X" in recorded

    def test_bank_account_number_is_redacted_entirely(self) -> None:
        changes = build_change_set(
            {"account_number": "30111111111"}, {"account_number": "30123456789"}
        )
        assert changes.before_values["account_number"] == AUDIT_REDACTED
        assert changes.after_values["account_number"] == AUDIT_REDACTED

    def test_personal_contact_values_are_redacted(self) -> None:
        changes = build_change_set(
            {"personal_mobile": "+919876543210"}, {"personal_mobile": "+919812345678"}
        )
        assert changes.after_values["personal_mobile"] == AUDIT_REDACTED

    def test_remarks_content_is_redacted_but_the_change_is_recorded(self) -> None:
        changes = build_change_set({"remarks": "old note"}, {"remarks": "sensitive note"})
        assert changes.changed_fields == ["remarks"]
        assert changes.after_values["remarks"] == AUDIT_REDACTED

    @pytest.mark.parametrize("field_name", sorted(NEVER_AUDITED_FIELDS))
    def test_forbidden_fields_are_dropped_completely(self, field_name: str) -> None:
        """§20.3: passwords, tokens, keys, ciphertext and file contents never appear."""
        changes = build_change_set({field_name: "before"}, {field_name: "after"})
        assert field_name not in changes.changed_fields
        assert field_name not in changes.before_values
        assert field_name not in changes.after_values

    def test_raw_bytes_are_never_recorded(self) -> None:
        changes = build_change_set({"some_blob": b"\x00\x01"}, {"some_blob": b"\x02\x03"})
        assert changes.after_values["some_blob"] == AUDIT_REDACTED

    def test_salary_values_are_permitted(self) -> None:
        """§20.3 explicitly allows salary values in before/after; reading them is gated."""
        changes = build_change_set({"value_amount": "600000.00"}, {"value_amount": "720000.00"})
        assert changes.after_values["value_amount"] == "720000.00"

    def test_field_restriction_limits_the_diff(self) -> None:
        """A PATCH records only the fields the caller submitted."""
        changes = build_change_set({"a": 1, "b": 2}, {"a": 9, "b": 9}, fields=["a"])
        assert changes.changed_fields == ["a"]

    def test_uuid_and_datetime_are_serialisable(self) -> None:
        changes = build_change_set(
            {"branch_id": None, "updated_at": None},
            {"branch_id": ENTITY, "updated_at": datetime(2026, 10, 28, tzinfo=UTC)},
        )
        assert changes.after_values["branch_id"] == str(ENTITY)
        assert changes.after_values["updated_at"].startswith("2026-10-28")

    def test_none_handling(self) -> None:
        changes = build_change_set({"grade_id": "g1"}, {"grade_id": None})
        assert changes.before_values["grade_id"] == "g1"
        assert changes.after_values["grade_id"] is None


class TestPayloadSafetyNet:
    """The writer's last-line check, because an append-only leak cannot be undone."""

    def test_clean_payload_passes(self) -> None:
        assert_audit_payload_is_safe({"work_mode": "HYBRID", "pan": "******234F"})

    @pytest.mark.parametrize("field_name", ["password_hash", "value_encrypted", "storage_key"])
    def test_forbidden_field_raises(self, field_name: str) -> None:
        with pytest.raises(ValueError, match="must not contain"):
            assert_audit_payload_is_safe({field_name: "anything"})

    def test_raw_bytes_raise(self) -> None:
        with pytest.raises(ValueError, match="raw bytes"):
            assert_audit_payload_is_safe({"blob": b"\x00"})

    def test_unmasked_identifier_raises(self) -> None:
        with pytest.raises(ValueError, match="unmasked"):
            assert_audit_payload_is_safe({"pan": "ABCPE1234F"})

    def test_masked_identifier_passes(self) -> None:
        assert_audit_payload_is_safe({"aadhaar_number": "XXXX XXXX 0124"})
