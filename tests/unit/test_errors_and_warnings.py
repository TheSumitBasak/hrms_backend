"""Error-code contract (§24.3, §24.5) and the warning acknowledgement protocol (§21.3)."""

from __future__ import annotations

from typing import ClassVar

import pytest

from app.core.errors import (
    ERROR_HTTP_STATUS,
    ConcurrentModification,
    EmsError,
    ErrorCode,
    OrgAccessDenied,
    PermissionDenied,
    PreconditionFailed,
    RateLimited,
    ResourceNotFound,
    SalaryAssignmentOverlap,
    Unauthenticated,
    ValidationFailed,
    WarningsRequireAcknowledgement,
)
from app.core.warnings import WARNINGS, WarningCode, WarningCollector, warning

pytestmark = pytest.mark.unit


# The §24.3 and §24.5 tables, transcribed. If the spec and the code disagree, this fails.
SPEC_ERROR_STATUSES: dict[str, int] = {
    "MALFORMED_REQUEST": 400,
    "UNAUTHENTICATED": 401,
    "PERMISSION_DENIED": 403,
    "ORG_ACCESS_DENIED": 403,
    "RESOURCE_NOT_FOUND": 404,
    "DOCUMENT_ACCESS_DENIED": 403,
    "DUPLICATE_EMPLOYEE_CODE": 409,
    "DUPLICATE_IDENTIFIER": 409,
    "SALARY_ASSIGNMENT_OVERLAP": 409,
    "ID_RULE_CONFLICT": 409,
    "ID_SEQUENCE_COLLISION_LIMIT": 409,
    "CONCURRENT_MODIFICATION": 409,
    "PRECONDITION_FAILED": 412,
    "FILE_TOO_LARGE": 413,
    "FILE_TYPE_NOT_ALLOWED": 415,
    "VALIDATION_FAILED": 422,
    "WARNINGS_REQUIRE_ACKNOWLEDGEMENT": 422,
    "INVALID_ORGANIZATION_REFERENCE": 422,
    "NO_APPLICABLE_ID_RULE": 422,
    "TEMPLATE_ARCHIVED": 422,
    "TEMPLATE_VERSION_NOT_EFFECTIVE": 422,
    "COMPONENT_NOT_OVERRIDABLE": 422,
    "INVALID_STATUS_TRANSITION": 422,
    "STATUS_CORRECTION_WINDOW_EXPIRED": 422,
    "BACKDATE_NOT_ALLOWED": 422,
    "SALARY_AFTER_EMPLOYMENT_END": 422,
    "REPORTING_CYCLE": 422,
    "NOMINEE_SHARE_TOTAL_INVALID": 422,
    "AGE_BELOW_ABSOLUTE_MINIMUM": 422,
    "FIELD_NOT_COLLECTED": 422,
    "FIELD_READ_ONLY": 422,
    "CONFLICTING_PERMANENT_ADDRESS": 422,
    "MASTER_INACTIVE": 422,
    "MASTER_IN_USE_BY_ID_RULE": 422,
    "RATE_LIMITED": 429,
    # §24.5
    "IDEMPOTENCY_KEY_REUSED": 409,
    "SALARY_NOT_EDITABLE": 422,
    "SALARY_NOT_CANCELLABLE": 422,
    "SALARY_NOT_CORRECTABLE": 422,
    "APPROVAL_REQUIRED": 422,
    "SELF_APPROVAL_NOT_ALLOWED": 403,
    "ID_RULE_IMMUTABLE": 422,
    "DOCUMENT_NOT_AVAILABLE": 409,
    "TEMPLATE_DRAFT_EXISTS": 422,
}


class TestErrorCodeContract:
    @pytest.mark.parametrize(("code", "status"), sorted(SPEC_ERROR_STATUSES.items()))
    def test_spec_code_exists_with_the_documented_status(self, code: str, status: int) -> None:
        error_code = ErrorCode(code)
        assert ERROR_HTTP_STATUS[error_code] == status

    def test_every_spec_code_is_implemented(self) -> None:
        missing = {code for code in SPEC_ERROR_STATUSES if code not in set(ErrorCode)}
        assert not missing, f"codes from §24.3/§24.5 are missing: {sorted(missing)}"

    def test_every_implemented_code_has_a_status(self) -> None:
        unmapped = [code for code in ErrorCode if code not in ERROR_HTTP_STATUS]
        assert not unmapped, f"codes without an HTTP status: {unmapped}"

    def test_additions_beyond_the_spec_are_only_the_documented_ones(self) -> None:
        """Guards against error codes creeping in without a decision record."""
        documented_additions = {
            ErrorCode.DUPLICATE_RESOURCE,
            ErrorCode.GSTIN_PAN_MISMATCH,
            ErrorCode.JOINING_DATE_CONFLICT,
            ErrorCode.JOB_CHANGE_DATE_INVALID,
            ErrorCode.INTERNAL_ERROR,
            ErrorCode.SERVICE_UNAVAILABLE,
            # Job / position architecture (JA-SPEC-001 error table).
            ErrorCode.COMPETENCY_IN_USE,
            ErrorCode.COMPETENCY_REQUIREMENT_CONFLICT,
            ErrorCode.HEADCOUNT_BELOW_AUTHORIZED,
            ErrorCode.HEADCOUNT_BELOW_OCCUPIED,
            ErrorCode.HEADCOUNT_CHANGE_NOT_APPROVABLE,
            ErrorCode.HEADCOUNT_CHANGE_NOT_EDITABLE,
            ErrorCode.HEADCOUNT_CHANGE_REQUIRED,
            ErrorCode.HEADCOUNT_LIMIT_EXCEEDED,
            ErrorCode.HEADCOUNT_PLAN_OVERLAP,
            ErrorCode.INVALID_GRADE_RANGE,
            ErrorCode.JOB_FAMILY_CYCLE,
            ErrorCode.JOB_FAMILY_IN_USE,
            ErrorCode.JOB_IN_USE,
            ErrorCode.MASTER_IN_USE_BY_JOB_ARCHITECTURE,
            ErrorCode.POSITION_ASSIGNMENT_OVERLAP,
            ErrorCode.POSITION_GRADE_OUT_OF_RANGE,
            ErrorCode.POSITION_HEADCOUNT_EXCEEDED,
            ErrorCode.POSITION_IN_USE,
            ErrorCode.POSITION_NOT_ACTIVE,
            ErrorCode.POSITION_REPORTING_CYCLE,
        }
        # Onboarding, attendance and leave addendum §16.1 codes follow the last JA code.
        order = list(ErrorCode)
        documented_additions |= set(order[order.index(ErrorCode.POSITION_REPORTING_CYCLE) + 1 :])
        extra = {code for code in ErrorCode if code.value not in SPEC_ERROR_STATUSES}
        assert extra == documented_additions


class TestExceptionBehaviour:
    def test_http_status_comes_from_the_mapping(self) -> None:
        assert SalaryAssignmentOverlap().http_status == 409
        assert PermissionDenied().http_status == 403
        assert ValidationFailed().http_status == 422

    def test_unauthenticated_advertises_the_scheme(self) -> None:
        """RFC 6750: a 401 must tell the client how to authenticate."""
        assert Unauthenticated().headers["WWW-Authenticate"] == "Bearer"

    def test_not_found_message_does_not_disclose_existence(self) -> None:
        """§24.3: a resource in another organization reads the same as one that never existed."""
        foreign = ResourceNotFound("employee")
        absent = ResourceNotFound("employee")
        assert foreign.message == absent.message
        assert "organization" not in foreign.message.lower()

    def test_org_access_denied_is_distinct_from_permission_denied(self) -> None:
        """§24.3 separates "no grant for this org" from "lacks a permission"."""
        assert OrgAccessDenied().code is ErrorCode.ORG_ACCESS_DENIED
        assert PermissionDenied().code is ErrorCode.PERMISSION_DENIED

    def test_details_can_be_attached_fluently(self) -> None:
        error = ValidationFailed().with_detail(
            field="effective_from", code="OVERLAP", message="Starts on 2027-01-01."
        )
        assert error.details[0].as_dict() == {
            "field": "effective_from",
            "code": "OVERLAP",
            "message": "Starts on 2027-01-01.",
        }

    def test_retryable_codes(self) -> None:
        """§24.7: clients retry 409 CONCURRENT_MODIFICATION."""
        assert ConcurrentModification().is_retryable
        assert not PreconditionFailed().is_retryable
        assert not SalaryAssignmentOverlap().is_retryable

    def test_rate_limited_sets_retry_after(self) -> None:
        assert RateLimited(retry_after_seconds=120).headers["Retry-After"] == "120"

    def test_all_exceptions_derive_from_the_base(self) -> None:
        for exception in (
            Unauthenticated(),
            PermissionDenied(),
            ResourceNotFound(),
            SalaryAssignmentOverlap(),
            RateLimited(),
        ):
            assert isinstance(exception, EmsError)

    def test_messages_carry_no_internal_detail(self) -> None:
        """§8 of the brief: no stack traces, SQL or internal paths in client messages."""
        forbidden = ("Traceback", "SELECT ", "sqlalchemy", "pymysql", "\\", ".py")
        for code in ErrorCode:
            message = EmsError.default_message if code is ErrorCode.INTERNAL_ERROR else ""
            assert all(token not in message for token in forbidden)


class TestWarningRegistry:
    SPEC_NAMED_CODES: ClassVar[set[str]] = {
        "W-ORG-NAME",
        "W-PAN-TYPE",
        "W-CIN-TYPE",
        "W-AGE-HIGH",
        "W-CONFIRM-EARLY",
        "W-EMERGENCY-SAME",
        "W-PF-EST-MISMATCH",
        "W-PASSPORT-EXPIRED",
        "W-IW-NATIONALITY",
        "W-DUP-REHIRE",
        "W-DUP-GROUP",
        "W-EDU-DUP",
        "W-BANK-NAME",
        "W-BANK-DUP",
        "W-DOC-DUP",
        "W-MANAGER-SEPARATED",
    }

    def test_every_spec_named_warning_is_registered(self) -> None:
        registered = {code.value for code in WarningCode}
        assert registered >= self.SPEC_NAMED_CODES

    def test_spec_named_warnings_are_flagged_as_such(self) -> None:
        """Codes the spec names must not be marked as our own convention."""
        for spec_code in self.SPEC_NAMED_CODES:
            assert WARNINGS[WarningCode(spec_code)].spec_named is True

    def test_every_warning_code_has_a_specification(self) -> None:
        for code in WarningCode:
            assert code in WARNINGS
            assert WARNINGS[code].section

    def test_messages_render_with_their_placeholders(self) -> None:
        detail = warning(
            WarningCode.PAN_TYPE_MISMATCH, found="C", expected="P", entity_type="PROPRIETORSHIP"
        )
        assert detail.code == "W-PAN-TYPE"
        assert detail.field == "pan"
        assert "'C'" in detail.message
        assert "'P'" in detail.message

    def test_duplicate_warning_reveals_only_an_employee_code(self) -> None:
        """§11.5: a warning must not reveal the matched person's identity."""
        detail = warning(WarningCode.DUPLICATE_IDENTIFIER_GROUP, employee_code="ABC-IT-0038")
        assert "ABC-IT-0038" in detail.message
        assert "name" not in detail.message.lower()


class TestWarningAcknowledgementProtocol:
    """§21.3: warnings never block silently and are never ignored silently."""

    def test_no_warnings_passes(self) -> None:
        assert WarningCollector().enforce() == []

    def test_unacknowledged_warning_blocks_the_write(self) -> None:
        collector = WarningCollector()
        collector.add(WarningCode.AGE_HIGH, age=104)
        with pytest.raises(WarningsRequireAcknowledgement) as caught:
            collector.enforce()
        assert caught.value.http_status == 422
        assert caught.value.code is ErrorCode.WARNINGS_REQUIRE_ACKNOWLEDGEMENT
        assert [w.code for w in caught.value.warnings] == ["W-AGE-HIGH"]

    def test_acknowledged_warning_allows_the_write_and_is_returned_for_audit(self) -> None:
        collector = WarningCollector(acknowledged=["W-AGE-HIGH"])
        collector.add(WarningCode.AGE_HIGH, age=104)
        assert collector.enforce() == ["W-AGE-HIGH"]

    def test_acknowledgement_is_case_insensitive(self) -> None:
        collector = WarningCollector(acknowledged=["w-age-high"])
        collector.add(WarningCode.AGE_HIGH, age=104)
        assert collector.enforce() == ["W-AGE-HIGH"]

    def test_partial_acknowledgement_still_blocks(self) -> None:
        collector = WarningCollector(acknowledged=["W-AGE-HIGH"])
        collector.add(WarningCode.AGE_HIGH, age=104)
        collector.add(WarningCode.EMERGENCY_CONTACT_SAME)
        with pytest.raises(WarningsRequireAcknowledgement) as caught:
            collector.enforce()
        assert [w.code for w in caught.value.warnings] == ["W-EMERGENCY-SAME"]

    def test_acknowledging_a_warning_that_did_not_apply_is_harmless(self) -> None:
        """A client may safely resend the acknowledgements it was given previously."""
        collector = WarningCollector(acknowledged=["W-DOC-DUP", "W-AGE-HIGH"])
        collector.add(WarningCode.AGE_HIGH, age=104)
        assert collector.enforce() == ["W-AGE-HIGH"]

    def test_add_if_only_records_when_the_condition_holds(self) -> None:
        collector = WarningCollector()
        collector.add_if(False, WarningCode.AGE_HIGH, age=104)
        assert collector.raised == []
        collector.add_if(True, WarningCode.AGE_HIGH, age=104)
        assert len(collector.raised) == 1

    def test_raised_list_is_a_copy(self) -> None:
        collector = WarningCollector()
        collector.add(WarningCode.AGE_HIGH, age=104)
        collector.raised.clear()
        assert len(collector.raised) == 1
