"""Cursor pagination, sorting and ETag parsing (§24.1, §26.4, §26.5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.errors import EmsError, ErrorCode
from app.schemas.common import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    CursorCodec,
    PageParams,
    SortDirection,
    SortSpec,
    etag_for,
    parse_if_match,
)

pytestmark = pytest.mark.unit

SECRET = "test-secret-at-least-thirty-two-characters-long"
ALLOWED_SORTS = frozenset(
    {
        "employee_code",
        "display_name",
        "joining_date",
        "department_name",
        "designation_name",
        "status",
        "created_at",
    }
)
"""§26.4: exactly these seven values are permitted."""


@pytest.fixture
def codec() -> CursorCodec:
    return CursorCodec(secret=SECRET, ttl_seconds=86400)


class TestCursorRoundTrip:
    def test_round_trip_preserves_position(self, codec: CursorCodec) -> None:
        last_id = uuid.uuid4()
        cursor = codec.encode(
            sort_field="display_name",
            sort_value="Rajesh Patel",
            last_id=last_id,
            direction=SortDirection.ASC,
        )
        decoded = codec.decode(cursor)
        assert decoded.sort_field == "display_name"
        assert decoded.sort_value == "Rajesh Patel"
        assert decoded.last_id == last_id
        assert decoded.direction is SortDirection.ASC

    def test_cursor_is_opaque(self, codec: CursorCodec) -> None:
        """§26.5 calls the cursor opaque; a reader should not see the sort value directly."""
        cursor = codec.encode(
            sort_field="display_name",
            sort_value="Rajesh Patel",
            last_id=uuid.uuid4(),
            direction=SortDirection.ASC,
        )
        assert "Rajesh" not in cursor
        assert "display_name" not in cursor

    def test_dates_survive_the_round_trip(self, codec: CursorCodec) -> None:
        cursor = codec.encode(
            sort_field="created_at",
            sort_value=datetime(2026, 10, 28, 6, 44, tzinfo=UTC),
            last_id=uuid.uuid4(),
            direction=SortDirection.DESC,
        )
        assert codec.decode(cursor).sort_value == "2026-10-28T06:44:00+00:00"

    def test_null_sort_value_survives(self, codec: CursorCodec) -> None:
        cursor = codec.encode(
            sort_field="official_email",
            sort_value=None,
            last_id=uuid.uuid4(),
            direction=SortDirection.ASC,
        )
        assert codec.decode(cursor).sort_value is None

    def test_cursor_is_url_safe(self, codec: CursorCodec) -> None:
        cursor = codec.encode(
            sort_field="display_name",
            sort_value="राजेश कुमार / O'Brien",
            last_id=uuid.uuid4(),
            direction=SortDirection.ASC,
        )
        assert all(character.isalnum() or character in "-_." for character in cursor)


class TestCursorTampering:
    """A cursor goes into a WHERE clause, so it must be authenticated input."""

    def test_modified_payload_is_rejected(self, codec: CursorCodec) -> None:
        cursor = codec.encode(
            sort_field="display_name",
            sort_value="A",
            last_id=uuid.uuid4(),
            direction=SortDirection.ASC,
        )
        body, signature = cursor.split(".", 1)
        tampered = f"{body[:-2]}XY.{signature}"
        with pytest.raises(EmsError) as caught:
            codec.decode(tampered)
        assert caught.value.code is ErrorCode.VALIDATION_FAILED

    def test_stripped_signature_is_rejected(self, codec: CursorCodec) -> None:
        cursor = codec.encode(
            sort_field="display_name",
            sort_value="A",
            last_id=uuid.uuid4(),
            direction=SortDirection.ASC,
        )
        with pytest.raises(EmsError):
            codec.decode(cursor.split(".", 1)[0])

    def test_cursor_from_another_deployment_is_rejected(self, codec: CursorCodec) -> None:
        """Signing with the app secret stops a cursor crossing deployments."""
        other = CursorCodec(secret="a-completely-different-secret-value-here", ttl_seconds=86400)
        cursor = other.encode(
            sort_field="display_name",
            sort_value="A",
            last_id=uuid.uuid4(),
            direction=SortDirection.ASC,
        )
        with pytest.raises(EmsError):
            codec.decode(cursor)

    @pytest.mark.parametrize(
        "garbage",
        ["", ".", "not-a-cursor", "a.b", "!!!.!!!", "x" * 400],
    )
    def test_garbage_is_rejected_uniformly(self, codec: CursorCodec, garbage: str) -> None:
        with pytest.raises(EmsError) as caught:
            codec.decode(garbage)
        # The same message for every failure mode, so probing reveals nothing.
        assert "invalid or has expired" in caught.value.message

    def test_expired_cursor_is_rejected(self) -> None:
        """§26.5: cursors are valid 24 hours."""
        short_lived = CursorCodec(secret=SECRET, ttl_seconds=0)
        cursor = short_lived.encode(
            sort_field="display_name",
            sort_value="A",
            last_id=uuid.uuid4(),
            direction=SortDirection.ASC,
        )
        with pytest.raises(EmsError):
            short_lived.decode(cursor)


class TestPageParams:
    def test_defaults_match_spec(self) -> None:
        params = PageParams()
        assert params.limit == DEFAULT_PAGE_SIZE == 25
        assert params.cursor is None
        assert params.with_total is False

    def test_limit_is_capped(self) -> None:
        """§26.5: maximum 100. An unbounded limit is an availability risk (§18 of the brief)."""
        assert MAX_PAGE_SIZE == 100
        with pytest.raises(ValueError, match="less than or equal to 100"):
            PageParams(limit=1000)

    def test_limit_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="greater than or equal to 1"):
            PageParams(limit=0)

    def test_unknown_parameters_are_rejected(self) -> None:
        """An ignored `offset=` would silently not paginate; §26.5 forbids offsets."""
        with pytest.raises(ValueError, match="offset"):
            PageParams(offset=100)  # type: ignore[call-arg]


class TestSortSpec:
    def test_default_when_absent(self) -> None:
        spec = SortSpec.parse(None, allowed=ALLOWED_SORTS, default_field="display_name")
        assert spec.field == "display_name"
        assert spec.direction is SortDirection.ASC

    @pytest.mark.parametrize("field_name", sorted(ALLOWED_SORTS))
    def test_every_allowed_field_parses(self, field_name: str) -> None:
        spec = SortSpec.parse(field_name, allowed=ALLOWED_SORTS, default_field="display_name")
        assert spec.field == field_name

    def test_direction_is_parsed(self) -> None:
        spec = SortSpec.parse(
            "joining_date:desc", allowed=ALLOWED_SORTS, default_field="display_name"
        )
        assert spec.field == "joining_date"
        assert spec.direction is SortDirection.DESC

    def test_unknown_field_is_rejected(self) -> None:
        """§26.4: unknown sort fields -> 422 VALIDATION_FAILED."""
        with pytest.raises(EmsError) as caught:
            SortSpec.parse("salary", allowed=ALLOWED_SORTS, default_field="display_name")
        assert caught.value.code is ErrorCode.VALIDATION_FAILED

    def test_sort_allowlist_prevents_ordering_by_a_hidden_column(self) -> None:
        """Sorting by a column the caller cannot read would leak its ordering."""
        for hidden in ("date_of_birth", "remarks", "pan", "aadhaar_number", "value_blind_index"):
            with pytest.raises(EmsError):
                SortSpec.parse(hidden, allowed=ALLOWED_SORTS, default_field="display_name")

    def test_unknown_direction_is_rejected(self) -> None:
        with pytest.raises(EmsError):
            SortSpec.parse(
                "display_name:sideways", allowed=ALLOWED_SORTS, default_field="display_name"
            )

    def test_sql_injection_attempt_in_sort_is_rejected(self) -> None:
        with pytest.raises(EmsError):
            SortSpec.parse(
                "display_name; DROP TABLE employees",
                allowed=ALLOWED_SORTS,
                default_field="display_name",
            )


class TestEtags:
    def test_etag_is_the_quoted_row_version(self) -> None:
        """§24.1: "GET returns ETag (= row_version)"."""
        assert etag_for(3) == '"3"'

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ('"3"', 3),
            ("3", 3),
            ('W/"3"', 3),
            ('  "7"  ', 7),
            (None, None),
            ("*", None),
        ],
    )
    def test_if_match_parsing(self, header: str | None, expected: int | None) -> None:
        assert parse_if_match(header) == expected

    def test_round_trip(self) -> None:
        assert parse_if_match(etag_for(42)) == 42

    @pytest.mark.parametrize("header", ['"abc"', "not-a-number", '"1.5"', '""'])
    def test_malformed_if_match_is_rejected(self, header: str) -> None:
        with pytest.raises(EmsError) as caught:
            parse_if_match(header)
        assert caught.value.code is ErrorCode.VALIDATION_FAILED
