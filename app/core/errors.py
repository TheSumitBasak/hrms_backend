"""Domain error codes and the exception hierarchy that renders them.

Every code in §24.3 and §24.5 of the spec appears in :class:`ErrorCode` exactly as written,
plus the handful of codes the spec names inline in its field tables (see ``_INLINE_CODES``
below). ``ERROR_HTTP_STATUS`` is the single mapping from code to HTTP status, so a code can
never be returned with two different statuses from two different call sites.

Raising one of these exceptions is the only way a service reports a business failure; the
handlers in :mod:`app.core.exception_handlers` turn it into the §24.2 envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Self


class ErrorCode(StrEnum):
    """Machine-readable error codes. The string value is what clients receive."""

    # --- 400 -------------------------------------------------------------------
    MALFORMED_REQUEST = "MALFORMED_REQUEST"

    # --- 401 -------------------------------------------------------------------
    UNAUTHENTICATED = "UNAUTHENTICATED"

    # --- 403 -------------------------------------------------------------------
    PERMISSION_DENIED = "PERMISSION_DENIED"
    ORG_ACCESS_DENIED = "ORG_ACCESS_DENIED"
    DOCUMENT_ACCESS_DENIED = "DOCUMENT_ACCESS_DENIED"
    SELF_APPROVAL_NOT_ALLOWED = "SELF_APPROVAL_NOT_ALLOWED"

    # --- 404 -------------------------------------------------------------------
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"

    # --- 409 -------------------------------------------------------------------
    DUPLICATE_EMPLOYEE_CODE = "DUPLICATE_EMPLOYEE_CODE"
    DUPLICATE_IDENTIFIER = "DUPLICATE_IDENTIFIER"
    SALARY_ASSIGNMENT_OVERLAP = "SALARY_ASSIGNMENT_OVERLAP"
    ID_RULE_CONFLICT = "ID_RULE_CONFLICT"
    ID_SEQUENCE_COLLISION_LIMIT = "ID_SEQUENCE_COLLISION_LIMIT"
    CONCURRENT_MODIFICATION = "CONCURRENT_MODIFICATION"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    DOCUMENT_NOT_AVAILABLE = "DOCUMENT_NOT_AVAILABLE"
    DUPLICATE_RESOURCE = "DUPLICATE_RESOURCE"
    """Generic uniqueness conflict for configuration rows (org code, master code, ...).

    §24.4.1 requires "409 on duplicate code/CIN" without naming a code; this is it.
    """

    # --- 412 -------------------------------------------------------------------
    PRECONDITION_FAILED = "PRECONDITION_FAILED"

    # --- 413 / 415 -------------------------------------------------------------
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FILE_TYPE_NOT_ALLOWED = "FILE_TYPE_NOT_ALLOWED"

    # --- 422 -------------------------------------------------------------------
    VALIDATION_FAILED = "VALIDATION_FAILED"
    WARNINGS_REQUIRE_ACKNOWLEDGEMENT = "WARNINGS_REQUIRE_ACKNOWLEDGEMENT"
    INVALID_ORGANIZATION_REFERENCE = "INVALID_ORGANIZATION_REFERENCE"
    NO_APPLICABLE_ID_RULE = "NO_APPLICABLE_ID_RULE"
    TEMPLATE_ARCHIVED = "TEMPLATE_ARCHIVED"
    TEMPLATE_VERSION_NOT_EFFECTIVE = "TEMPLATE_VERSION_NOT_EFFECTIVE"
    TEMPLATE_DRAFT_EXISTS = "TEMPLATE_DRAFT_EXISTS"
    COMPONENT_NOT_OVERRIDABLE = "COMPONENT_NOT_OVERRIDABLE"
    INVALID_STATUS_TRANSITION = "INVALID_STATUS_TRANSITION"
    STATUS_CORRECTION_WINDOW_EXPIRED = "STATUS_CORRECTION_WINDOW_EXPIRED"
    BACKDATE_NOT_ALLOWED = "BACKDATE_NOT_ALLOWED"
    SALARY_AFTER_EMPLOYMENT_END = "SALARY_AFTER_EMPLOYMENT_END"
    SALARY_NOT_EDITABLE = "SALARY_NOT_EDITABLE"
    SALARY_NOT_CANCELLABLE = "SALARY_NOT_CANCELLABLE"
    SALARY_NOT_CORRECTABLE = "SALARY_NOT_CORRECTABLE"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    REPORTING_CYCLE = "REPORTING_CYCLE"
    NOMINEE_SHARE_TOTAL_INVALID = "NOMINEE_SHARE_TOTAL_INVALID"
    AGE_BELOW_ABSOLUTE_MINIMUM = "AGE_BELOW_ABSOLUTE_MINIMUM"
    FIELD_NOT_COLLECTED = "FIELD_NOT_COLLECTED"
    FIELD_READ_ONLY = "FIELD_READ_ONLY"
    CONFLICTING_PERMANENT_ADDRESS = "CONFLICTING_PERMANENT_ADDRESS"
    MASTER_INACTIVE = "MASTER_INACTIVE"
    MASTER_IN_USE_BY_ID_RULE = "MASTER_IN_USE_BY_ID_RULE"
    ID_RULE_IMMUTABLE = "ID_RULE_IMMUTABLE"
    # Codes the spec names inline in field tables rather than in §24.3.
    GSTIN_PAN_MISMATCH = "GSTIN_PAN_MISMATCH"
    """§7.4 writes this as ``E-GSTIN-PAN``; renamed to the prevailing code style (DEC-012)."""

    JOINING_DATE_CONFLICT = "JOINING_DATE_CONFLICT"
    """§10.7 EMP-09."""

    JOB_CHANGE_DATE_INVALID = "JOB_CHANGE_DATE_INVALID"
    """§10.7 EMP-12."""

    # --- 429 -------------------------------------------------------------------
    RATE_LIMITED = "RATE_LIMITED"

    # --- 5xx -------------------------------------------------------------------
    INTERNAL_ERROR = "INTERNAL_ERROR"
    """Not in the spec. Required so unexpected failures still return the §24.2 envelope
    instead of a stack trace (§8 of the brief, OWASP A10)."""

    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    """Returned by the readiness probe and when a dependency is unreachable."""

    # --- job / position architecture ------------------------------------------
    COMPETENCY_IN_USE = "COMPETENCY_IN_USE"
    COMPETENCY_REQUIREMENT_CONFLICT = "COMPETENCY_REQUIREMENT_CONFLICT"
    HEADCOUNT_BELOW_AUTHORIZED = "HEADCOUNT_BELOW_AUTHORIZED"
    HEADCOUNT_BELOW_OCCUPIED = "HEADCOUNT_BELOW_OCCUPIED"
    HEADCOUNT_CHANGE_NOT_APPROVABLE = "HEADCOUNT_CHANGE_NOT_APPROVABLE"
    HEADCOUNT_CHANGE_NOT_EDITABLE = "HEADCOUNT_CHANGE_NOT_EDITABLE"
    HEADCOUNT_CHANGE_REQUIRED = "HEADCOUNT_CHANGE_REQUIRED"
    HEADCOUNT_LIMIT_EXCEEDED = "HEADCOUNT_LIMIT_EXCEEDED"
    HEADCOUNT_PLAN_OVERLAP = "HEADCOUNT_PLAN_OVERLAP"
    INVALID_GRADE_RANGE = "INVALID_GRADE_RANGE"
    JOB_FAMILY_CYCLE = "JOB_FAMILY_CYCLE"
    JOB_FAMILY_IN_USE = "JOB_FAMILY_IN_USE"
    JOB_IN_USE = "JOB_IN_USE"
    MASTER_IN_USE_BY_JOB_ARCHITECTURE = "MASTER_IN_USE_BY_JOB_ARCHITECTURE"
    POSITION_ASSIGNMENT_OVERLAP = "POSITION_ASSIGNMENT_OVERLAP"
    POSITION_GRADE_OUT_OF_RANGE = "POSITION_GRADE_OUT_OF_RANGE"
    POSITION_HEADCOUNT_EXCEEDED = "POSITION_HEADCOUNT_EXCEEDED"
    POSITION_IN_USE = "POSITION_IN_USE"
    POSITION_NOT_ACTIVE = "POSITION_NOT_ACTIVE"
    POSITION_REPORTING_CYCLE = "POSITION_REPORTING_CYCLE"

    # --- onboarding, attendance and leave addendum §16.1 -----------------------
    MODULE_NOT_ENABLED = "MODULE_NOT_ENABLED"
    SELF_SERVICE_NOT_ENABLED = "SELF_SERVICE_NOT_ENABLED"
    NOT_CURRENT_APPROVER = "NOT_CURRENT_APPROVER"
    APPROVER_ALREADY_ACTED = "APPROVER_ALREADY_ACTED"
    INVALID_APPROVAL_TRANSITION = "INVALID_APPROVAL_TRANSITION"
    JOB_ALREADY_RUNNING = "JOB_ALREADY_RUNNING"
    POLICY_VERSION_NOT_FUTURE = "POLICY_VERSION_NOT_FUTURE"
    NO_APPLICABLE_ONBOARDING_TEMPLATE = "NO_APPLICABLE_ONBOARDING_TEMPLATE"
    ONBOARDING_TEMPLATE_CONFLICT = "ONBOARDING_TEMPLATE_CONFLICT"
    ONBOARDING_TEMPLATE_DRAFT_EXISTS = "ONBOARDING_TEMPLATE_DRAFT_EXISTS"
    ONBOARDING_TEMPLATE_VERSION_LOCKED = "ONBOARDING_TEMPLATE_VERSION_LOCKED"
    ONBOARDING_TEMPLATE_INVALID = "ONBOARDING_TEMPLATE_INVALID"
    ONBOARDING_EMPLOYEE_NOT_ELIGIBLE = "ONBOARDING_EMPLOYEE_NOT_ELIGIBLE"
    ONBOARDING_ALREADY_EXISTS = "ONBOARDING_ALREADY_EXISTS"
    INVALID_ONBOARDING_TRANSITION = "INVALID_ONBOARDING_TRANSITION"
    ONBOARDING_MANDATORY_TASKS_INCOMPLETE = "ONBOARDING_MANDATORY_TASKS_INCOMPLETE"
    ONBOARDING_REOPEN_WINDOW_EXPIRED = "ONBOARDING_REOPEN_WINDOW_EXPIRED"
    ONBOARDING_INSTANCE_NOT_OPEN = "ONBOARDING_INSTANCE_NOT_OPEN"
    INVALID_ONBOARDING_TASK_TRANSITION = "INVALID_ONBOARDING_TASK_TRANSITION"
    ONBOARDING_TASK_BLOCKED = "ONBOARDING_TASK_BLOCKED"
    ONBOARDING_TASK_REQUIREMENTS_UNMET = "ONBOARDING_TASK_REQUIREMENTS_UNMET"
    TASK_NOT_WAIVABLE = "TASK_NOT_WAIVABLE"
    SELF_REVIEW_NOT_ALLOWED = "SELF_REVIEW_NOT_ALLOWED"
    ONBOARDING_REQUIRED_FOR_ACTIVATION = "ONBOARDING_REQUIRED_FOR_ACTIVATION"
    ONBOARDING_ACTIVATION_BLOCKED = "ONBOARDING_ACTIVATION_BLOCKED"
    DATA_SUBMISSION_ALREADY_PENDING = "DATA_SUBMISSION_ALREADY_PENDING"
    FIELD_NOT_PERMITTED_IN_SECTION = "FIELD_NOT_PERMITTED_IN_SECTION"
    DOCUMENT_TASK_MISMATCH = "DOCUMENT_TASK_MISMATCH"
    NO_APPLICABLE_ATTENDANCE_POLICY = "NO_APPLICABLE_ATTENDANCE_POLICY"
    ATTENDANCE_POLICY_CONFLICT = "ATTENDANCE_POLICY_CONFLICT"
    ATTENDANCE_POLICY_DRAFT_EXISTS = "ATTENDANCE_POLICY_DRAFT_EXISTS"
    WEEKLY_OFF_PATTERN_IN_USE = "WEEKLY_OFF_PATTERN_IN_USE"
    SHIFT_WINDOW_INVALID = "SHIFT_WINDOW_INVALID"
    SHIFT_IN_USE_IMMUTABLE = "SHIFT_IN_USE_IMMUTABLE"
    SHIFT_ASSIGNMENT_OVERLAP = "SHIFT_ASSIGNMENT_OVERLAP"
    SHIFT_ASSIGNMENT_DATE_INVALID = "SHIFT_ASSIGNMENT_DATE_INVALID"
    EMPLOYEE_NOT_ELIGIBLE_FOR_ATTENDANCE = "EMPLOYEE_NOT_ELIGIBLE_FOR_ATTENDANCE"
    ATTENDANCE_SOURCE_DISABLED = "ATTENDANCE_SOURCE_DISABLED"
    PUNCH_TIME_INVALID = "PUNCH_TIME_INVALID"
    CONFLICTING_ATTENDANCE_EVENT = "CONFLICTING_ATTENDANCE_EVENT"
    ATTENDANCE_RECORD_FINALIZED = "ATTENDANCE_RECORD_FINALIZED"
    ATTENDANCE_RECORD_NOT_FINALIZED = "ATTENDANCE_RECORD_NOT_FINALIZED"
    ATTENDANCE_FINALIZATION_BLOCKED = "ATTENDANCE_FINALIZATION_BLOCKED"
    ATTENDANCE_CONFLICTS_WITH_LEAVE = "ATTENDANCE_CONFLICTS_WITH_LEAVE"
    SELF_CORRECTION_NOT_ALLOWED = "SELF_CORRECTION_NOT_ALLOWED"
    REGULARIZATION_NOT_ENABLED = "REGULARIZATION_NOT_ENABLED"
    REGULARIZATION_WINDOW_EXPIRED = "REGULARIZATION_WINDOW_EXPIRED"
    REGULARIZATION_ALREADY_PENDING = "REGULARIZATION_ALREADY_PENDING"
    REGULARIZATION_ITEMS_INVALID = "REGULARIZATION_ITEMS_INVALID"
    REGULARIZATION_STALE = "REGULARIZATION_STALE"
    LEAVE_YEAR_CHANGE_NOT_ALLOWED = "LEAVE_YEAR_CHANGE_NOT_ALLOWED"
    LEAVE_TYPE_NOT_APPLICABLE = "LEAVE_TYPE_NOT_APPLICABLE"
    LEAVE_POLICY_CONFLICT = "LEAVE_POLICY_CONFLICT"
    LEAVE_POLICY_DRAFT_EXISTS = "LEAVE_POLICY_DRAFT_EXISTS"
    EMPLOYEE_NOT_ELIGIBLE_FOR_LEAVE = "EMPLOYEE_NOT_ELIGIBLE_FOR_LEAVE"
    INVALID_LEAVE_DATE_RANGE = "INVALID_LEAVE_DATE_RANGE"
    LEAVE_SPANS_LEAVE_YEARS = "LEAVE_SPANS_LEAVE_YEARS"
    HALF_DAY_NOT_ALLOWED = "HALF_DAY_NOT_ALLOWED"
    LEAVE_BOUNDARY_ON_NON_WORKING_DAY = "LEAVE_BOUNDARY_ON_NON_WORKING_DAY"
    LEAVE_ZERO_DURATION = "LEAVE_ZERO_DURATION"
    LEAVE_DURATION_LIMIT = "LEAVE_DURATION_LIMIT"
    LEAVE_CONSECUTIVE_LIMIT_EXCEEDED = "LEAVE_CONSECUTIVE_LIMIT_EXCEEDED"
    LEAVE_TIMING_VIOLATION = "LEAVE_TIMING_VIOLATION"
    LEAVE_OVERLAP = "LEAVE_OVERLAP"
    LEAVE_DOCUMENT_REQUIRED = "LEAVE_DOCUMENT_REQUIRED"
    LEAVE_APPLICATION_LIMIT_EXCEEDED = "LEAVE_APPLICATION_LIMIT_EXCEEDED"
    OPTIONAL_HOLIDAY_INVALID = "OPTIONAL_HOLIDAY_INVALID"
    INSUFFICIENT_LEAVE_BALANCE = "INSUFFICIENT_LEAVE_BALANCE"
    LEAVE_CONFLICTS_WITH_ATTENDANCE_OVERRIDE = "LEAVE_CONFLICTS_WITH_ATTENDANCE_OVERRIDE"
    LEAVE_CONFLICTS_WITH_PENDING_REGULARIZATION = "LEAVE_CONFLICTS_WITH_PENDING_REGULARIZATION"
    LEAVE_NOT_CANCELLABLE = "LEAVE_NOT_CANCELLABLE"
    RESTORATION_MODE_REQUIRED = "RESTORATION_MODE_REQUIRED"
    LEAVE_YEAR_CLOSED = "LEAVE_YEAR_CLOSED"
    OPENING_BALANCE_EXISTS = "OPENING_BALANCE_EXISTS"
    LEAVE_TRANSACTION_INVALID = "LEAVE_TRANSACTION_INVALID"
    LEAVE_TRANSACTION_ALREADY_REVERSED = "LEAVE_TRANSACTION_ALREADY_REVERSED"
    SELF_ADJUSTMENT_NOT_ALLOWED = "SELF_ADJUSTMENT_NOT_ALLOWED"


ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.MALFORMED_REQUEST: HTTPStatus.BAD_REQUEST,
    ErrorCode.UNAUTHENTICATED: HTTPStatus.UNAUTHORIZED,
    ErrorCode.PERMISSION_DENIED: HTTPStatus.FORBIDDEN,
    ErrorCode.ORG_ACCESS_DENIED: HTTPStatus.FORBIDDEN,
    ErrorCode.DOCUMENT_ACCESS_DENIED: HTTPStatus.FORBIDDEN,
    ErrorCode.SELF_APPROVAL_NOT_ALLOWED: HTTPStatus.FORBIDDEN,
    ErrorCode.RESOURCE_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ErrorCode.DUPLICATE_EMPLOYEE_CODE: HTTPStatus.CONFLICT,
    ErrorCode.DUPLICATE_IDENTIFIER: HTTPStatus.CONFLICT,
    ErrorCode.SALARY_ASSIGNMENT_OVERLAP: HTTPStatus.CONFLICT,
    ErrorCode.ID_RULE_CONFLICT: HTTPStatus.CONFLICT,
    ErrorCode.ID_SEQUENCE_COLLISION_LIMIT: HTTPStatus.CONFLICT,
    ErrorCode.CONCURRENT_MODIFICATION: HTTPStatus.CONFLICT,
    ErrorCode.IDEMPOTENCY_KEY_REUSED: HTTPStatus.CONFLICT,
    ErrorCode.DOCUMENT_NOT_AVAILABLE: HTTPStatus.CONFLICT,
    ErrorCode.DUPLICATE_RESOURCE: HTTPStatus.CONFLICT,
    ErrorCode.PRECONDITION_FAILED: HTTPStatus.PRECONDITION_FAILED,
    ErrorCode.FILE_TOO_LARGE: HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
    ErrorCode.FILE_TYPE_NOT_ALLOWED: HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
    ErrorCode.VALIDATION_FAILED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.WARNINGS_REQUIRE_ACKNOWLEDGEMENT: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.INVALID_ORGANIZATION_REFERENCE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.NO_APPLICABLE_ID_RULE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.TEMPLATE_ARCHIVED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.TEMPLATE_VERSION_NOT_EFFECTIVE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.TEMPLATE_DRAFT_EXISTS: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.COMPONENT_NOT_OVERRIDABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.INVALID_STATUS_TRANSITION: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.STATUS_CORRECTION_WINDOW_EXPIRED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.BACKDATE_NOT_ALLOWED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.SALARY_AFTER_EMPLOYMENT_END: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.SALARY_NOT_EDITABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.SALARY_NOT_CANCELLABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.SALARY_NOT_CORRECTABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.APPROVAL_REQUIRED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.REPORTING_CYCLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.NOMINEE_SHARE_TOTAL_INVALID: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.AGE_BELOW_ABSOLUTE_MINIMUM: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.FIELD_NOT_COLLECTED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.FIELD_READ_ONLY: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.CONFLICTING_PERMANENT_ADDRESS: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.MASTER_INACTIVE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.MASTER_IN_USE_BY_ID_RULE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ID_RULE_IMMUTABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.GSTIN_PAN_MISMATCH: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.JOINING_DATE_CONFLICT: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.JOB_CHANGE_DATE_INVALID: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.RATE_LIMITED: HTTPStatus.TOO_MANY_REQUESTS,
    ErrorCode.INTERNAL_ERROR: HTTPStatus.INTERNAL_SERVER_ERROR,
    ErrorCode.SERVICE_UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.COMPETENCY_IN_USE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.COMPETENCY_REQUIREMENT_CONFLICT: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.HEADCOUNT_BELOW_AUTHORIZED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.HEADCOUNT_BELOW_OCCUPIED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.HEADCOUNT_CHANGE_NOT_APPROVABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.HEADCOUNT_CHANGE_NOT_EDITABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.HEADCOUNT_CHANGE_REQUIRED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.HEADCOUNT_LIMIT_EXCEEDED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.HEADCOUNT_PLAN_OVERLAP: HTTPStatus.CONFLICT,
    ErrorCode.INVALID_GRADE_RANGE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.JOB_FAMILY_CYCLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.JOB_FAMILY_IN_USE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.JOB_IN_USE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.MASTER_IN_USE_BY_JOB_ARCHITECTURE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.POSITION_ASSIGNMENT_OVERLAP: HTTPStatus.CONFLICT,
    ErrorCode.POSITION_GRADE_OUT_OF_RANGE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.POSITION_HEADCOUNT_EXCEEDED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.POSITION_IN_USE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.POSITION_NOT_ACTIVE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.POSITION_REPORTING_CYCLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.MODULE_NOT_ENABLED: HTTPStatus.FORBIDDEN,
    ErrorCode.SELF_SERVICE_NOT_ENABLED: HTTPStatus.FORBIDDEN,
    ErrorCode.NOT_CURRENT_APPROVER: HTTPStatus.FORBIDDEN,
    ErrorCode.APPROVER_ALREADY_ACTED: HTTPStatus.CONFLICT,
    ErrorCode.INVALID_APPROVAL_TRANSITION: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.JOB_ALREADY_RUNNING: HTTPStatus.CONFLICT,
    ErrorCode.POLICY_VERSION_NOT_FUTURE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.NO_APPLICABLE_ONBOARDING_TEMPLATE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ONBOARDING_TEMPLATE_CONFLICT: HTTPStatus.CONFLICT,
    ErrorCode.ONBOARDING_TEMPLATE_DRAFT_EXISTS: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ONBOARDING_TEMPLATE_VERSION_LOCKED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ONBOARDING_TEMPLATE_INVALID: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ONBOARDING_EMPLOYEE_NOT_ELIGIBLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ONBOARDING_ALREADY_EXISTS: HTTPStatus.CONFLICT,
    ErrorCode.INVALID_ONBOARDING_TRANSITION: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ONBOARDING_MANDATORY_TASKS_INCOMPLETE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ONBOARDING_REOPEN_WINDOW_EXPIRED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ONBOARDING_INSTANCE_NOT_OPEN: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.INVALID_ONBOARDING_TASK_TRANSITION: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ONBOARDING_TASK_BLOCKED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ONBOARDING_TASK_REQUIREMENTS_UNMET: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.TASK_NOT_WAIVABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.SELF_REVIEW_NOT_ALLOWED: HTTPStatus.FORBIDDEN,
    ErrorCode.ONBOARDING_REQUIRED_FOR_ACTIVATION: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ONBOARDING_ACTIVATION_BLOCKED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.DATA_SUBMISSION_ALREADY_PENDING: HTTPStatus.CONFLICT,
    ErrorCode.FIELD_NOT_PERMITTED_IN_SECTION: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.DOCUMENT_TASK_MISMATCH: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.NO_APPLICABLE_ATTENDANCE_POLICY: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ATTENDANCE_POLICY_CONFLICT: HTTPStatus.CONFLICT,
    ErrorCode.ATTENDANCE_POLICY_DRAFT_EXISTS: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.WEEKLY_OFF_PATTERN_IN_USE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.SHIFT_WINDOW_INVALID: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.SHIFT_IN_USE_IMMUTABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.SHIFT_ASSIGNMENT_OVERLAP: HTTPStatus.CONFLICT,
    ErrorCode.SHIFT_ASSIGNMENT_DATE_INVALID: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.EMPLOYEE_NOT_ELIGIBLE_FOR_ATTENDANCE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ATTENDANCE_SOURCE_DISABLED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.PUNCH_TIME_INVALID: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.CONFLICTING_ATTENDANCE_EVENT: HTTPStatus.CONFLICT,
    ErrorCode.ATTENDANCE_RECORD_FINALIZED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ATTENDANCE_RECORD_NOT_FINALIZED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ATTENDANCE_FINALIZATION_BLOCKED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.ATTENDANCE_CONFLICTS_WITH_LEAVE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.SELF_CORRECTION_NOT_ALLOWED: HTTPStatus.FORBIDDEN,
    ErrorCode.REGULARIZATION_NOT_ENABLED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.REGULARIZATION_WINDOW_EXPIRED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.REGULARIZATION_ALREADY_PENDING: HTTPStatus.CONFLICT,
    ErrorCode.REGULARIZATION_ITEMS_INVALID: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.REGULARIZATION_STALE: HTTPStatus.CONFLICT,
    ErrorCode.LEAVE_YEAR_CHANGE_NOT_ALLOWED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.LEAVE_TYPE_NOT_APPLICABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.LEAVE_POLICY_CONFLICT: HTTPStatus.CONFLICT,
    ErrorCode.LEAVE_POLICY_DRAFT_EXISTS: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.EMPLOYEE_NOT_ELIGIBLE_FOR_LEAVE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.INVALID_LEAVE_DATE_RANGE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.LEAVE_SPANS_LEAVE_YEARS: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.HALF_DAY_NOT_ALLOWED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.LEAVE_BOUNDARY_ON_NON_WORKING_DAY: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.LEAVE_ZERO_DURATION: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.LEAVE_DURATION_LIMIT: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.LEAVE_CONSECUTIVE_LIMIT_EXCEEDED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.LEAVE_TIMING_VIOLATION: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.LEAVE_OVERLAP: HTTPStatus.CONFLICT,
    ErrorCode.LEAVE_DOCUMENT_REQUIRED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.LEAVE_APPLICATION_LIMIT_EXCEEDED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.OPTIONAL_HOLIDAY_INVALID: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.INSUFFICIENT_LEAVE_BALANCE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.LEAVE_CONFLICTS_WITH_ATTENDANCE_OVERRIDE: HTTPStatus.CONFLICT,
    ErrorCode.LEAVE_CONFLICTS_WITH_PENDING_REGULARIZATION: HTTPStatus.CONFLICT,
    ErrorCode.LEAVE_NOT_CANCELLABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.RESTORATION_MODE_REQUIRED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.LEAVE_YEAR_CLOSED: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.OPENING_BALANCE_EXISTS: HTTPStatus.CONFLICT,
    ErrorCode.LEAVE_TRANSACTION_INVALID: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.LEAVE_TRANSACTION_ALREADY_REVERSED: HTTPStatus.CONFLICT,
    ErrorCode.SELF_ADJUSTMENT_NOT_ALLOWED: HTTPStatus.FORBIDDEN,
}

RETRYABLE_CODES = frozenset({ErrorCode.CONCURRENT_MODIFICATION, ErrorCode.SERVICE_UNAVAILABLE})
"""§24.7: clients should retry these up to three times with jittered backoff."""


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    """One entry of the ``details`` array in the §24.2 envelope."""

    field: str | None
    code: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class WarningDetail:
    """One entry of the ``warnings`` array (§21.3)."""

    code: str
    field: str | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "field": self.field, "message": self.message}


class EmsError(Exception):
    """Base class for every business failure.

    Carries only client-safe content. Internal diagnostics belong in the log record, never
    in ``message`` (§8 of the brief: no stack traces, no database errors to clients).
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    default_message = "The request could not be completed."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: list[ErrorDetail] | None = None,
        warnings: list[WarningDetail] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.details = details or []
        self.warnings = warnings or []
        self.headers = headers or {}
        super().__init__(self.message)

    @property
    def http_status(self) -> int:
        return ERROR_HTTP_STATUS[self.code]

    @property
    def is_retryable(self) -> bool:
        return self.code in RETRYABLE_CODES

    def with_detail(self, *, field: str | None, code: str, message: str) -> Self:
        """Append a field-level detail. Returns ``self`` so raises stay one expression."""
        self.details.append(ErrorDetail(field=field, code=code, message=message))
        return self


# -------------------------------------------------------------------------------------
# Concrete exceptions. One per code that services raise directly; the rest are raised as
# ``EmsError`` subclasses generated by ``_simple`` to avoid 40 near-identical class bodies.
# -------------------------------------------------------------------------------------


class Unauthenticated(EmsError):
    code = ErrorCode.UNAUTHENTICATED
    default_message = "Authentication is required."

    def __init__(self, message: str | None = None) -> None:
        # RFC 6750: a 401 must say how to authenticate.
        super().__init__(message, headers={"WWW-Authenticate": "Bearer"})


class PermissionDenied(EmsError):
    code = ErrorCode.PERMISSION_DENIED
    default_message = "You do not have permission to perform this action."


class OrgAccessDenied(EmsError):
    code = ErrorCode.ORG_ACCESS_DENIED
    default_message = "You do not have access to this organization."


class ResourceNotFound(EmsError):
    code = ErrorCode.RESOURCE_NOT_FOUND
    default_message = "The requested resource was not found."

    def __init__(self, resource: str = "resource") -> None:
        # Deliberately uniform: §24.3 requires no existence disclosure, so a resource in
        # another organization and a resource that never existed read identically.
        super().__init__(f"The requested {resource} was not found.")


class ValidationFailed(EmsError):
    code = ErrorCode.VALIDATION_FAILED
    default_message = "The request contains invalid field values."


class PreconditionFailed(EmsError):
    code = ErrorCode.PRECONDITION_FAILED
    default_message = (
        "The resource has changed since it was read. Reload it and retry with the current ETag."
    )


class ConcurrentModification(EmsError):
    code = ErrorCode.CONCURRENT_MODIFICATION
    default_message = "The resource is being modified concurrently. Retry the request."


class RateLimited(EmsError):
    code = ErrorCode.RATE_LIMITED
    default_message = "Rate limit exceeded."

    def __init__(
        self, message: str | None = None, *, retry_after_seconds: int | None = None
    ) -> None:
        headers = {"Retry-After": str(retry_after_seconds)} if retry_after_seconds else {}
        super().__init__(message, headers=headers)


class WarningsRequireAcknowledgement(EmsError):
    """§21.3: warnings never block silently and are never ignored silently."""

    code = ErrorCode.WARNINGS_REQUIRE_ACKNOWLEDGEMENT
    default_message = (
        "The request raised warnings that must be acknowledged. Resubmit with the warning "
        "codes in acknowledged_warnings."
    )

    def __init__(self, warnings: list[WarningDetail]) -> None:
        super().__init__(warnings=warnings)


class InternalError(EmsError):
    code = ErrorCode.INTERNAL_ERROR
    default_message = "An internal error occurred."


def _simple(code: ErrorCode, message: str) -> type[EmsError]:
    """Build an :class:`EmsError` subclass for a code that needs no extra behaviour."""
    return type(
        code.value.title().replace("_", ""),
        (EmsError,),
        {"code": code, "default_message": message, "__module__": __name__},
    )


# Services import these by name; each is a real exception class, not a factory call result.
MalformedRequest = _simple(ErrorCode.MALFORMED_REQUEST, "The request body could not be parsed.")
DocumentAccessDenied = _simple(
    ErrorCode.DOCUMENT_ACCESS_DENIED,
    "You do not have permission to access documents in this category.",
)
SelfApprovalNotAllowed = _simple(
    ErrorCode.SELF_APPROVAL_NOT_ALLOWED,
    "An approver may not approve a revision they created or submitted.",
)
DuplicateEmployeeCode = _simple(
    ErrorCode.DUPLICATE_EMPLOYEE_CODE, "That employee code is already registered."
)
DuplicateIdentifier = _simple(
    ErrorCode.DUPLICATE_IDENTIFIER, "That identifier is already recorded for another employee."
)
DuplicateResource = _simple(
    ErrorCode.DUPLICATE_RESOURCE, "A record with that value already exists."
)
SalaryAssignmentOverlap = _simple(
    ErrorCode.SALARY_ASSIGNMENT_OVERLAP,
    "The salary revision overlaps an existing confirmed assignment.",
)
IdRuleConflict = _simple(
    ErrorCode.ID_RULE_CONFLICT,
    "The rule conflicts with another rule of the same scope and priority.",
)
IdSequenceCollisionLimit = _simple(
    ErrorCode.ID_SEQUENCE_COLLISION_LIMIT,
    "Code generation exceeded the collision retry limit.",
)
IdempotencyKeyReused = _simple(
    ErrorCode.IDEMPOTENCY_KEY_REUSED,
    "That Idempotency-Key was already used with a different request body.",
)
DocumentNotAvailable = _simple(
    ErrorCode.DOCUMENT_NOT_AVAILABLE, "The document is not available for download."
)
FileTooLarge = _simple(ErrorCode.FILE_TOO_LARGE, "The file exceeds the category size limit.")
FileTypeNotAllowed = _simple(
    ErrorCode.FILE_TYPE_NOT_ALLOWED, "That file type is not permitted for this category."
)
InvalidOrganizationReference = _simple(
    ErrorCode.INVALID_ORGANIZATION_REFERENCE,
    "A referenced record belongs to a different organization.",
)
NoApplicableIdRule = _simple(
    ErrorCode.NO_APPLICABLE_ID_RULE,
    "No employee ID generation rule applies to this organization, department and date.",
)
TemplateArchived = _simple(ErrorCode.TEMPLATE_ARCHIVED, "The salary template is archived.")
TemplateVersionNotEffective = _simple(
    ErrorCode.TEMPLATE_VERSION_NOT_EFFECTIVE,
    "No active template version is effective on that date.",
)
TemplateDraftExists = _simple(
    ErrorCode.TEMPLATE_DRAFT_EXISTS, "This template already has an open draft version."
)
ComponentNotOverridable = _simple(
    ErrorCode.COMPONENT_NOT_OVERRIDABLE, "That salary component cannot be overridden."
)
InvalidStatusTransition = _simple(
    ErrorCode.INVALID_STATUS_TRANSITION, "That employee status transition is not permitted."
)
StatusCorrectionWindowExpired = _simple(
    ErrorCode.STATUS_CORRECTION_WINDOW_EXPIRED,
    "The status correction window has expired; use rehire instead.",
)
BackdateNotAllowed = _simple(
    ErrorCode.BACKDATE_NOT_ALLOWED, "Backdating this revision is not permitted."
)
SalaryAfterEmploymentEnd = _simple(
    ErrorCode.SALARY_AFTER_EMPLOYMENT_END,
    "A confirmed salary assignment starts after the employment end date.",
)
SalaryNotEditable = _simple(
    ErrorCode.SALARY_NOT_EDITABLE, "Only draft salary assignments can be edited."
)
SalaryNotCancellable = _simple(
    ErrorCode.SALARY_NOT_CANCELLABLE, "Only scheduled salary revisions can be cancelled."
)
SalaryNotCorrectable = _simple(
    ErrorCode.SALARY_NOT_CORRECTABLE, "Only active or expired assignments can be corrected."
)
ApprovalRequired = _simple(
    ErrorCode.APPROVAL_REQUIRED, "This revision must be approved before it can be confirmed."
)
ReportingCycle = _simple(
    ErrorCode.REPORTING_CYCLE, "That reporting manager would create a reporting cycle."
)
NomineeShareTotalInvalid = _simple(
    ErrorCode.NOMINEE_SHARE_TOTAL_INVALID, "Nominee shares must total exactly 100.00."
)
AgeBelowAbsoluteMinimum = _simple(
    ErrorCode.AGE_BELOW_ABSOLUTE_MINIMUM,
    "The employee is below the absolute minimum employment age.",
)
FieldNotCollected = _simple(
    ErrorCode.FIELD_NOT_COLLECTED,
    "That field is not collected by this organization's data policy.",
)
FieldReadOnly = _simple(
    ErrorCode.FIELD_READ_ONLY, "That field is system-generated and cannot be supplied."
)
ConflictingPermanentAddress = _simple(
    ErrorCode.CONFLICTING_PERMANENT_ADDRESS,
    "Permanent address fields cannot be supplied when it is marked same as current.",
)
MasterInactive = _simple(
    ErrorCode.MASTER_INACTIVE, "That record is inactive and cannot be newly assigned."
)
MasterInUseByIdRule = _simple(
    ErrorCode.MASTER_IN_USE_BY_ID_RULE,
    "An active employee ID rule references this department; end or archive the rule first.",
)
IdRuleImmutable = _simple(
    ErrorCode.ID_RULE_IMMUTABLE, "This rule has issued codes or has started; that field is locked."
)
GstinPanMismatch = _simple(
    ErrorCode.GSTIN_PAN_MISMATCH,
    "Characters 3-12 of the GSTIN must equal the organization PAN.",
)
JoiningDateConflict = _simple(
    ErrorCode.JOINING_DATE_CONFLICT,
    "The joining date change conflicts with existing salary assignments.",
)
JobChangeDateInvalid = _simple(
    ErrorCode.JOB_CHANGE_DATE_INVALID, "That job change effective date is not permitted."
)
ServiceUnavailable = _simple(ErrorCode.SERVICE_UNAVAILABLE, "A required dependency is unavailable.")

CompetencyInUse = _simple(
    ErrorCode.COMPETENCY_IN_USE, "The competency is referenced by a job or position requirement."
)
CompetencyRequirementConflict = _simple(
    ErrorCode.COMPETENCY_REQUIREMENT_CONFLICT,
    "The competency requirement conflicts with an existing mapping.",
)
HeadcountBelowAuthorized = _simple(
    ErrorCode.HEADCOUNT_BELOW_AUTHORIZED,
    "The change would leave authorized headcount below the planned level.",
)
HeadcountBelowOccupied = _simple(
    ErrorCode.HEADCOUNT_BELOW_OCCUPIED,
    "The authorized headcount cannot be set below current occupancy.",
)
HeadcountChangeNotApprovable = _simple(
    ErrorCode.HEADCOUNT_CHANGE_NOT_APPROVABLE,
    "That headcount change request cannot be approved in its current state.",
)
HeadcountChangeNotEditable = _simple(
    ErrorCode.HEADCOUNT_CHANGE_NOT_EDITABLE,
    "That headcount change request can no longer be edited.",
)
HeadcountChangeRequired = _simple(
    ErrorCode.HEADCOUNT_CHANGE_REQUIRED,
    "Authorized FTE or headcount changes must go through a headcount change request.",
)
HeadcountLimitExceeded = _simple(
    ErrorCode.HEADCOUNT_LIMIT_EXCEEDED, "The headcount plan limit would be exceeded."
)
HeadcountPlanOverlap = _simple(
    ErrorCode.HEADCOUNT_PLAN_OVERLAP,
    "An active headcount plan already covers that scope and period.",
)
InvalidGradeRange = _simple(ErrorCode.INVALID_GRADE_RANGE, "The grade range is invalid.")
JobFamilyCycle = _simple(ErrorCode.JOB_FAMILY_CYCLE, "That parent would create a job family cycle.")
JobFamilyInUse = _simple(
    ErrorCode.JOB_FAMILY_IN_USE, "The job family is referenced by jobs or child families."
)
JobInUse = _simple(ErrorCode.JOB_IN_USE, "The job is referenced by positions.")
MasterInUseByJobArchitecture = _simple(
    ErrorCode.MASTER_IN_USE_BY_JOB_ARCHITECTURE,
    "The master record is referenced by jobs or positions.",
)
PositionAssignmentOverlap = _simple(
    ErrorCode.POSITION_ASSIGNMENT_OVERLAP,
    "The position assignment overlaps an existing assignment.",
)
PositionGradeOutOfRange = _simple(
    ErrorCode.POSITION_GRADE_OUT_OF_RANGE, "The grade is outside the job's permitted grade range."
)
PositionHeadcountExceeded = _simple(
    ErrorCode.POSITION_HEADCOUNT_EXCEEDED,
    "The position has no remaining FTE or incumbent capacity.",
)
PositionInUse = _simple(
    ErrorCode.POSITION_IN_USE, "The position has active assignments or dependents."
)
PositionNotActive = _simple(
    ErrorCode.POSITION_NOT_ACTIVE, "The position is not in a state that permits this action."
)
PositionReportingCycle = _simple(
    ErrorCode.POSITION_REPORTING_CYCLE, "That reporting position would create a reporting cycle."
)


SelfServiceNotEnabled = _simple(
    ErrorCode.SELF_SERVICE_NOT_ENABLED,
    "Self endpoint with the setting disabled or no linked employee (§11)",
)
NotCurrentApprover = _simple(
    ErrorCode.NOT_CURRENT_APPROVER,
    "Actor is not the current approver and lacks the HR permission (INT-32)",
)
ApproverAlreadyActed = _simple(
    ErrorCode.APPROVER_ALREADY_ACTED, "Level-2 actor equals the level-1 actor (INT-34)"
)
InvalidApprovalTransition = _simple(
    ErrorCode.INVALID_APPROVAL_TRANSITION,
    "Action on a terminal request or not allowed at its status (ATT-21, §6.11)",
)
JobAlreadyRunning = _simple(
    ErrorCode.JOB_ALREADY_RUNNING, "Manual trigger while a run is RUNNING (§14.2)"
)
PolicyVersionNotFuture = _simple(
    ErrorCode.POLICY_VERSION_NOT_FUTURE,
    "Retroactive attendance or leave policy version (ATT-05, LEV-05)",
)
NoApplicableOnboardingTemplate = _simple(
    ErrorCode.NO_APPLICABLE_ONBOARDING_TEMPLATE, "No template version matches (§4.4)"
)
OnboardingTemplateConflict = _simple(
    ErrorCode.ONBOARDING_TEMPLATE_CONFLICT, "Conflicting ACTIVE versions (§4.3)"
)
OnboardingTemplateDraftExists = _simple(
    ErrorCode.ONBOARDING_TEMPLATE_DRAFT_EXISTS, "Second open DRAFT for a template"
)
OnboardingTemplateVersionLocked = _simple(
    ErrorCode.ONBOARDING_TEMPLATE_VERSION_LOCKED, "Editing a non-DRAFT version"
)
OnboardingTemplateInvalid = _simple(
    ErrorCode.ONBOARDING_TEMPLATE_INVALID, "Publish validation failure (details list problems)"
)
OnboardingEmployeeNotEligible = _simple(
    ErrorCode.ONBOARDING_EMPLOYEE_NOT_ELIGIBLE,
    "ONB-01 failure, or completion or reopen with an ineligible employee status",
)
OnboardingAlreadyExists = _simple(ErrorCode.ONBOARDING_ALREADY_EXISTS, "ONB-02")
InvalidOnboardingTransition = _simple(
    ErrorCode.INVALID_ONBOARDING_TRANSITION, "Instance transition not allowed (§4.2)"
)
OnboardingMandatoryTasksIncomplete = _simple(
    ErrorCode.ONBOARDING_MANDATORY_TASKS_INCOMPLETE, "Completion with open mandatory tasks"
)
OnboardingReopenWindowExpired = _simple(
    ErrorCode.ONBOARDING_REOPEN_WINDOW_EXPIRED, "Reopen beyond the window"
)
OnboardingInstanceNotOpen = _simple(
    ErrorCode.ONBOARDING_INSTANCE_NOT_OPEN, "Task action on a non-IN_PROGRESS instance"
)
InvalidOnboardingTaskTransition = _simple(
    ErrorCode.INVALID_ONBOARDING_TASK_TRANSITION, "Task transition not in §4.8"
)
OnboardingTaskBlocked = _simple(ErrorCode.ONBOARDING_TASK_BLOCKED, "Prerequisites not closed")
OnboardingTaskRequirementsUnmet = _simple(
    ErrorCode.ONBOARDING_TASK_REQUIREMENTS_UNMET, "Type completeness not satisfied"
)
TaskNotWaivable = _simple(ErrorCode.TASK_NOT_WAIVABLE, "Waiving a non-waivable task")
SelfReviewNotAllowed = _simple(
    ErrorCode.SELF_REVIEW_NOT_ALLOWED, "Reviewer equals submitter (§4.8, ONB-12)"
)
OnboardingRequiredForActivation = _simple(
    ErrorCode.ONBOARDING_REQUIRED_FOR_ACTIVATION,
    "Activation without an instance when required (§4.9)",
)
OnboardingActivationBlocked = _simple(
    ErrorCode.ONBOARDING_ACTIVATION_BLOCKED, "Activation with open blocking tasks (§4.9)"
)
DataSubmissionAlreadyPending = _simple(ErrorCode.DATA_SUBMISSION_ALREADY_PENDING, "ONB-10")
FieldNotPermittedInSection = _simple(ErrorCode.FIELD_NOT_PERMITTED_IN_SECTION, "ONB-11")
DocumentTaskMismatch = _simple(
    ErrorCode.DOCUMENT_TASK_MISMATCH, "Upload or link incompatible with the task (§4.11)"
)
NoApplicableAttendancePolicy = _simple(ErrorCode.NO_APPLICABLE_ATTENDANCE_POLICY, "ATT-07")
AttendancePolicyConflict = _simple(ErrorCode.ATTENDANCE_POLICY_CONFLICT, "ATT-06")
AttendancePolicyDraftExists = _simple(
    ErrorCode.ATTENDANCE_POLICY_DRAFT_EXISTS, "Second open DRAFT for a policy"
)
WeeklyOffPatternInUse = _simple(
    ErrorCode.WEEKLY_OFF_PATTERN_IN_USE, "Editing the days of a referenced pattern (§5.2)"
)
ShiftWindowInvalid = _simple(ErrorCode.SHIFT_WINDOW_INVALID, "ATT-03")
ShiftInUseImmutable = _simple(ErrorCode.SHIFT_IN_USE_IMMUTABLE, "ATT-04")
ShiftAssignmentOverlap = _simple(ErrorCode.SHIFT_ASSIGNMENT_OVERLAP, "ATT-09")
ShiftAssignmentDateInvalid = _simple(ErrorCode.SHIFT_ASSIGNMENT_DATE_INVALID, "ATT-08")
EmployeeNotEligibleForAttendance = _simple(ErrorCode.EMPLOYEE_NOT_ELIGIBLE_FOR_ATTENDANCE, "ATT-01")
AttendanceSourceDisabled = _simple(ErrorCode.ATTENDANCE_SOURCE_DISABLED, "ATT-16")
PunchTimeInvalid = _simple(
    ErrorCode.PUNCH_TIME_INVALID, "Supplied punch_at more than 5 minutes in the future"
)
ConflictingAttendanceEvent = _simple(
    ErrorCode.CONFLICTING_ATTENDANCE_EVENT, "Integration replay with a different payload (ATT-13)"
)
AttendanceRecordFinalized = _simple(
    ErrorCode.ATTENDANCE_RECORD_FINALIZED,
    "Punch, regularization or approval against a FINALIZED record",
)
AttendanceRecordNotFinalized = _simple(
    ErrorCode.ATTENDANCE_RECORD_NOT_FINALIZED, "Reopening an OPEN record"
)
AttendanceFinalizationBlocked = _simple(
    ErrorCode.ATTENDANCE_FINALIZATION_BLOCKED,
    "Per-record finalization skip (sub-codes WINDOW_OPEN, PENDING_REGULARIZATION, INCOMPLETE)",
)
AttendanceConflictsWithLeave = _simple(
    ErrorCode.ATTENDANCE_CONFLICTS_WITH_LEAVE,
    "Override, ON_DUTY or regularization targeting a LEAVE half (ATT-17, ATT-20)",
)
SelfCorrectionNotAllowed = _simple(ErrorCode.SELF_CORRECTION_NOT_ALLOWED, "ATT-18")
RegularizationNotEnabled = _simple(
    ErrorCode.REGULARIZATION_NOT_ENABLED, "Policy disables regularization"
)
RegularizationWindowExpired = _simple(
    ErrorCode.REGULARIZATION_WINDOW_EXPIRED, "Outside the backdate limit or a future date"
)
RegularizationAlreadyPending = _simple(
    ErrorCode.REGULARIZATION_ALREADY_PENDING, "Second pending request for a record"
)
RegularizationItemsInvalid = _simple(
    ErrorCode.REGULARIZATION_ITEMS_INVALID, "Item composition or content invalid"
)
RegularizationStale = _simple(
    ErrorCode.REGULARIZATION_STALE, "Target punches changed since submission (ATT-22)"
)
LeaveYearChangeNotAllowed = _simple(ErrorCode.LEAVE_YEAR_CHANGE_NOT_ALLOWED, "LEV-03")
LeaveTypeNotApplicable = _simple(ErrorCode.LEAVE_TYPE_NOT_APPLICABLE, "No applicable policy (§6.4)")
LeavePolicyConflict = _simple(ErrorCode.LEAVE_POLICY_CONFLICT, "LEV-06")
LeavePolicyDraftExists = _simple(
    ErrorCode.LEAVE_POLICY_DRAFT_EXISTS, "Second open DRAFT for a leave policy"
)
EmployeeNotEligibleForLeave = _simple(ErrorCode.EMPLOYEE_NOT_ELIGIBLE_FOR_LEAVE, "LEV-21 to LEV-23")
InvalidLeaveDateRange = _simple(
    ErrorCode.INVALID_LEAVE_DATE_RANGE, "End before start or invalid half ordering"
)
LeaveSpansLeaveYears = _simple(
    ErrorCode.LEAVE_SPANS_LEAVE_YEARS, "Range crosses a leave-year boundary"
)
HalfDayNotAllowed = _simple(
    ErrorCode.HALF_DAY_NOT_ALLOWED, "Half-day slots when allow_half_day = false"
)
LeaveBoundaryOnNonWorkingDay = _simple(
    ErrorCode.LEAVE_BOUNDARY_ON_NON_WORKING_DAY, "Start or end on a non-counted non-working day"
)
LeaveZeroDuration = _simple(ErrorCode.LEAVE_ZERO_DURATION, "No counted slots")
LeaveDurationLimit = _simple(
    ErrorCode.LEAVE_DURATION_LIMIT, "Below the minimum or above the maximum per application"
)
LeaveConsecutiveLimitExceeded = _simple(
    ErrorCode.LEAVE_CONSECUTIVE_LIMIT_EXCEEDED, "Chain exceeds max_consecutive_days"
)
LeaveTimingViolation = _simple(
    ErrorCode.LEAVE_TIMING_VIOLATION, "Backdate, future or notice limits (LEV-25)"
)
LeaveOverlap = _simple(ErrorCode.LEAVE_OVERLAP, "Slot already ACTIVE for the employee (LEV-26)")
LeaveDocumentRequired = _simple(
    ErrorCode.LEAVE_DOCUMENT_REQUIRED, "Missing required supporting document"
)
LeaveApplicationLimitExceeded = _simple(
    ErrorCode.LEAVE_APPLICATION_LIMIT_EXCEEDED, "max_applications_per_year reached"
)
OptionalHolidayInvalid = _simple(ErrorCode.OPTIONAL_HOLIDAY_INVALID, "LEV-28")
InsufficientLeaveBalance = _simple(ErrorCode.INSUFFICIENT_LEAVE_BALANCE, "LEV-29")
LeaveConflictsWithAttendanceOverride = _simple(
    ErrorCode.LEAVE_CONFLICTS_WITH_ATTENDANCE_OVERRIDE, "LEV-31"
)
LeaveConflictsWithPendingRegularization = _simple(
    ErrorCode.LEAVE_CONFLICTS_WITH_PENDING_REGULARIZATION, "LEV-31"
)
LeaveNotCancellable = _simple(
    ErrorCode.LEAVE_NOT_CANCELLABLE,
    "No ACTIVE slots in scope, or the employee attempts to cancel slots dated today or earlier",
)
RestorationModeRequired = _simple(
    ErrorCode.RESTORATION_MODE_REQUIRED, "Cancellation into a CLOSED year without restoration_mode"
)
LeaveYearClosed = _simple(ErrorCode.LEAVE_YEAR_CLOSED, "Posting into a CLOSED year (LEV-INV-3)")
OpeningBalanceExists = _simple(
    ErrorCode.OPENING_BALANCE_EXISTS, "Second opening balance for the same employee, type and year"
)
LeaveTransactionInvalid = _simple(
    ErrorCode.LEAVE_TRANSACTION_INVALID, "Wrong sign, type, year or a non-tracking leave type"
)
LeaveTransactionAlreadyReversed = _simple(
    ErrorCode.LEAVE_TRANSACTION_ALREADY_REVERSED, "Second reversal of the same transaction"
)
SelfAdjustmentNotAllowed = _simple(ErrorCode.SELF_ADJUSTMENT_NOT_ALLOWED, "§6.13")


ModuleNotEnabled = _simple(
    ErrorCode.MODULE_NOT_ENABLED, "This module is not enabled for the organization."
)


__all__ = [
    "ERROR_HTTP_STATUS",
    "RETRYABLE_CODES",
    "AgeBelowAbsoluteMinimum",
    "ApprovalRequired",
    "ApproverAlreadyActed",
    "AttendanceConflictsWithLeave",
    "AttendanceFinalizationBlocked",
    "AttendancePolicyConflict",
    "AttendancePolicyDraftExists",
    "AttendanceRecordFinalized",
    "AttendanceRecordNotFinalized",
    "AttendanceSourceDisabled",
    "BackdateNotAllowed",
    "CompetencyInUse",
    "CompetencyRequirementConflict",
    "ComponentNotOverridable",
    "ConcurrentModification",
    "ConflictingAttendanceEvent",
    "ConflictingPermanentAddress",
    "DataSubmissionAlreadyPending",
    "DocumentAccessDenied",
    "DocumentNotAvailable",
    "DocumentTaskMismatch",
    "DuplicateEmployeeCode",
    "DuplicateIdentifier",
    "DuplicateResource",
    "EmployeeNotEligibleForAttendance",
    "EmployeeNotEligibleForLeave",
    "EmsError",
    "ErrorCode",
    "ErrorDetail",
    "FieldNotCollected",
    "FieldNotPermittedInSection",
    "FieldReadOnly",
    "FileTooLarge",
    "FileTypeNotAllowed",
    "GstinPanMismatch",
    "HalfDayNotAllowed",
    "HeadcountBelowAuthorized",
    "HeadcountBelowOccupied",
    "HeadcountChangeNotApprovable",
    "HeadcountChangeNotEditable",
    "HeadcountChangeRequired",
    "HeadcountLimitExceeded",
    "HeadcountPlanOverlap",
    "IdRuleConflict",
    "IdRuleImmutable",
    "IdSequenceCollisionLimit",
    "IdempotencyKeyReused",
    "InsufficientLeaveBalance",
    "InternalError",
    "InvalidApprovalTransition",
    "InvalidGradeRange",
    "InvalidLeaveDateRange",
    "InvalidOnboardingTaskTransition",
    "InvalidOnboardingTransition",
    "InvalidOrganizationReference",
    "InvalidStatusTransition",
    "JobAlreadyRunning",
    "JobChangeDateInvalid",
    "JobFamilyCycle",
    "JobFamilyInUse",
    "JobInUse",
    "JoiningDateConflict",
    "LeaveApplicationLimitExceeded",
    "LeaveBoundaryOnNonWorkingDay",
    "LeaveConflictsWithAttendanceOverride",
    "LeaveConflictsWithPendingRegularization",
    "LeaveConsecutiveLimitExceeded",
    "LeaveDocumentRequired",
    "LeaveDurationLimit",
    "LeaveNotCancellable",
    "LeaveOverlap",
    "LeavePolicyConflict",
    "LeavePolicyDraftExists",
    "LeaveSpansLeaveYears",
    "LeaveTimingViolation",
    "LeaveTransactionAlreadyReversed",
    "LeaveTransactionInvalid",
    "LeaveTypeNotApplicable",
    "LeaveYearChangeNotAllowed",
    "LeaveYearClosed",
    "LeaveZeroDuration",
    "MalformedRequest",
    "MasterInUseByIdRule",
    "MasterInUseByJobArchitecture",
    "MasterInactive",
    "ModuleNotEnabled",
    "NoApplicableAttendancePolicy",
    "NoApplicableIdRule",
    "NoApplicableOnboardingTemplate",
    "NomineeShareTotalInvalid",
    "NotCurrentApprover",
    "OnboardingActivationBlocked",
    "OnboardingAlreadyExists",
    "OnboardingEmployeeNotEligible",
    "OnboardingInstanceNotOpen",
    "OnboardingMandatoryTasksIncomplete",
    "OnboardingReopenWindowExpired",
    "OnboardingRequiredForActivation",
    "OnboardingTaskBlocked",
    "OnboardingTaskRequirementsUnmet",
    "OnboardingTemplateConflict",
    "OnboardingTemplateDraftExists",
    "OnboardingTemplateInvalid",
    "OnboardingTemplateVersionLocked",
    "OpeningBalanceExists",
    "OptionalHolidayInvalid",
    "OrgAccessDenied",
    "PermissionDenied",
    "PolicyVersionNotFuture",
    "PositionAssignmentOverlap",
    "PositionGradeOutOfRange",
    "PositionHeadcountExceeded",
    "PositionInUse",
    "PositionNotActive",
    "PositionReportingCycle",
    "PreconditionFailed",
    "PunchTimeInvalid",
    "RateLimited",
    "RegularizationAlreadyPending",
    "RegularizationItemsInvalid",
    "RegularizationNotEnabled",
    "RegularizationStale",
    "RegularizationWindowExpired",
    "ReportingCycle",
    "ResourceNotFound",
    "RestorationModeRequired",
    "SalaryAfterEmploymentEnd",
    "SalaryAssignmentOverlap",
    "SalaryNotCancellable",
    "SalaryNotCorrectable",
    "SalaryNotEditable",
    "SelfAdjustmentNotAllowed",
    "SelfApprovalNotAllowed",
    "SelfCorrectionNotAllowed",
    "SelfReviewNotAllowed",
    "SelfServiceNotEnabled",
    "ServiceUnavailable",
    "ShiftAssignmentDateInvalid",
    "ShiftAssignmentOverlap",
    "ShiftInUseImmutable",
    "ShiftWindowInvalid",
    "StatusCorrectionWindowExpired",
    "TaskNotWaivable",
    "TemplateArchived",
    "TemplateDraftExists",
    "TemplateVersionNotEffective",
    "Unauthenticated",
    "ValidationFailed",
    "WarningDetail",
    "WarningsRequireAcknowledgement",
    "WeeklyOffPatternInUse",
]
