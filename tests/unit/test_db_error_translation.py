"""MySQL error numbers mapped onto §24.3 error codes.

This exists because of a real defect found while testing the schema: the translator originally
dispatched on the SQLAlchemy exception *class*, handling ``IntegrityError`` and
``OperationalError`` separately. MySQL does not split them the way one would assume — a
violated CHECK constraint (3819) and an over-long value (1406) arrive as ``OperationalError``,
while a duplicate key (1062) and a failed foreign key (1452) arrive as ``IntegrityError``. CHECK
violations therefore fell through to the generic handler and would have surfaced to clients as
``500 INTERNAL_ERROR`` instead of the ``422 VALIDATION_FAILED`` the contract promises.

Dispatch is now keyed on the error number, and these tests pin that behaviour.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.errors import EmsError, ErrorCode
from app.db.session import (
    MYSQL_CHECK_CONSTRAINT_VIOLATED,
    MYSQL_DATA_TOO_LONG,
    MYSQL_DEADLOCK,
    MYSQL_DUPLICATE_ENTRY,
    MYSQL_FK_CHILD_EXISTS,
    MYSQL_FK_PARENT_MISSING,
    MYSQL_LOCK_WAIT_TIMEOUT,
    MYSQL_OUT_OF_RANGE,
    translate_database_error,
)

pytestmark = pytest.mark.unit


class _FakeDbapiError(Exception):
    """Stands in for a pymysql exception, which carries ``(errno, message)`` in ``args``."""

    def __init__(self, errno: int, message: str) -> None:
        super().__init__(errno, message)
        self.args = (errno, message)


def integrity_error(errno: int, message: str = "constraint failed") -> IntegrityError:
    return IntegrityError("INSERT ...", {}, _FakeDbapiError(errno, message))


def operational_error(errno: int, message: str = "operation failed") -> OperationalError:
    return OperationalError("INSERT ...", {}, _FakeDbapiError(errno, message))


class TestTranslationByErrorNumber:
    """The same error number maps to the same domain error regardless of wrapper class."""

    CASES: ClassVar[list[tuple[int, ErrorCode]]] = [
        (MYSQL_DUPLICATE_ENTRY, ErrorCode.DUPLICATE_RESOURCE),
        (MYSQL_FK_PARENT_MISSING, ErrorCode.INVALID_ORGANIZATION_REFERENCE),
        (MYSQL_FK_CHILD_EXISTS, ErrorCode.VALIDATION_FAILED),
        (MYSQL_CHECK_CONSTRAINT_VIOLATED, ErrorCode.VALIDATION_FAILED),
        (MYSQL_DATA_TOO_LONG, ErrorCode.VALIDATION_FAILED),
        (MYSQL_OUT_OF_RANGE, ErrorCode.VALIDATION_FAILED),
        (MYSQL_LOCK_WAIT_TIMEOUT, ErrorCode.CONCURRENT_MODIFICATION),
        (MYSQL_DEADLOCK, ErrorCode.CONCURRENT_MODIFICATION),
    ]

    @pytest.mark.parametrize(("errno", "expected"), CASES)
    def test_as_integrity_error(self, errno: int, expected: ErrorCode) -> None:
        assert translate_database_error(integrity_error(errno)).code is expected

    @pytest.mark.parametrize(("errno", "expected"), CASES)
    def test_as_operational_error(self, errno: int, expected: ErrorCode) -> None:
        """The regression guard: a CHECK violation arrives here, and must not escape."""
        assert translate_database_error(operational_error(errno)).code is expected

    def test_check_violation_is_a_422_not_a_500(self) -> None:
        """The specific defect this module exists for."""
        translated = translate_database_error(
            operational_error(MYSQL_CHECK_CONSTRAINT_VIOLATED, "Check constraint violated")
        )
        assert translated.http_status == 422
        assert translated.code is ErrorCode.VALIDATION_FAILED

    def test_composite_fk_failure_reports_a_cross_organization_reference(self) -> None:
        """§6.3 / AC-03: a composite organization FK rejecting a row means exactly this."""
        translated = translate_database_error(integrity_error(MYSQL_FK_PARENT_MISSING))
        assert translated.code is ErrorCode.INVALID_ORGANIZATION_REFERENCE
        assert translated.http_status == 422

    def test_lock_contention_is_retryable(self) -> None:
        """§24.7: clients retry 409 CONCURRENT_MODIFICATION with jittered backoff."""
        for errno in (MYSQL_LOCK_WAIT_TIMEOUT, MYSQL_DEADLOCK):
            translated = translate_database_error(operational_error(errno))
            assert translated.is_retryable
            assert translated.http_status == 409


class TestUnknownErrorsArePropagated:
    def test_unknown_error_number_is_reraised(self) -> None:
        """Guessing at an unknown database failure would hide a real fault behind a 422."""
        original = operational_error(2013, "Lost connection to MySQL server")
        with pytest.raises(OperationalError) as caught:
            translate_database_error(original)
        assert caught.value is original

    def test_missing_error_number_is_reraised(self) -> None:
        original = OperationalError("SELECT 1", {}, Exception("no errno"))
        with pytest.raises(OperationalError):
            translate_database_error(original)


class TestNoInternalDetailLeaks:
    @pytest.mark.parametrize(
        ("errno", "message"),
        [
            (MYSQL_DUPLICATE_ENTRY, "Duplicate entry 'ABCPE1234F' for key 'uq_employees_org_code'"),
            (
                MYSQL_CHECK_CONSTRAINT_VIOLATED,
                "Check constraint 'ck_employees_emp05_manager_not_self' is violated.",
            ),
            (MYSQL_DATA_TOO_LONG, "Data too long for column 'aadhaar_number' at row 1"),
        ],
    )
    def test_client_message_excludes_constraint_names_and_values(
        self, errno: int, message: str
    ) -> None:
        """§8 of the brief: no database errors, schema internals or data values to clients.

        A duplicate-key message from MySQL quotes the offending **value**, which for an
        identifier column would be a sensitive disclosure in an error body.
        """
        translated: EmsError = translate_database_error(operational_error(errno, message))
        for leak in ("uq_employees", "ck_employees", "ABCPE1234F", "aadhaar", "Duplicate entry"):
            assert leak not in translated.message
