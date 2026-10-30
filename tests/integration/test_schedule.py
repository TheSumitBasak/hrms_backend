"""Work-schedule configuration (addendum §5.2, §5.16): patterns, calendars, holidays."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import update

from app.core.errors import ErrorCode
from app.core.permissions import Role
from app.models.organization import OrganizationSettings
from tests.factories import create_organization, create_user
from tests.job_architecture import BRANCH, if_match

SUN_2SAT = [
    {"day_of_week": 7, "occurrence": "ALL"},
    {"day_of_week": 6, "occurrence": "SECOND"},
    {"day_of_week": 6, "occurrence": "FOURTH"},
]


def _today() -> date:
    return datetime.now(UTC).date()


class Setup:
    def __init__(self, client: Any, organization: Any) -> None:
        self.client = client
        self.org = organization
        self.base = f"/api/v1/orgs/{organization.id}"

    def pattern(self, code: str = "SUN-2SAT", days: Any = None) -> dict[str, Any]:
        response = self.client.post(
            f"{self.base}/weekly-off-patterns",
            json={
                "pattern_code": code,
                "pattern_name": "Sundays and 2nd/4th Saturdays",
                "days": SUN_2SAT if days is None else days,
            },
        )
        assert response.status_code == 201, response.text
        return dict(response.json())

    def calendar(self, code: str = "GJ", default: bool = False) -> dict[str, Any]:
        response = self.client.post(
            f"{self.base}/holiday-calendars",
            json={
                "calendar_code": code,
                "calendar_name": "Gujarat offices",
                "is_organization_default": default,
            },
        )
        assert response.status_code == 201, response.text
        return dict(response.json())


def make(
    db_session: Any,
    authenticate: Any,
    *,
    roles: tuple[Role, ...] = (Role.HR_ADMIN,),
    module: bool = True,
) -> Setup:
    organization = create_organization(db_session)
    if module:
        db_session.execute(
            update(OrganizationSettings)
            .where(OrganizationSettings.organization_id == organization.id)
            .values(enable_leave_module=True, leave_tracking_start_date=date(2026, 1, 1))
        )
    user = create_user(db_session, grants=[(organization.id, role) for role in roles])
    db_session.commit()
    return Setup(authenticate(user), organization)


class TestWeeklyOffPatterns:
    def test_create_read_and_duplicate_code(self, db_session: Any, authenticate: Any) -> None:
        s = make(db_session, authenticate)
        created = s.pattern()
        assert len(created["days"]) == 3
        read = s.client.get(f"{s.base}/weekly-off-patterns/{created['id']}")
        assert read.status_code == 200 and read.headers["ETag"]
        duplicate = s.client.post(
            f"{s.base}/weekly-off-patterns",
            json={"pattern_code": "SUN-2SAT", "pattern_name": "Again", "days": []},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == ErrorCode.DUPLICATE_RESOURCE

    def test_all_excludes_other_occurrences(self, db_session: Any, authenticate: Any) -> None:
        s = make(db_session, authenticate)
        response = s.client.post(
            f"{s.base}/weekly-off-patterns",
            json={
                "pattern_code": "BAD",
                "pattern_name": "Bad",
                "days": [
                    {"day_of_week": 7, "occurrence": "ALL"},
                    {"day_of_week": 7, "occurrence": "FIRST"},
                ],
            },
        )
        assert response.status_code == 422

    def test_days_cannot_change_while_the_pattern_is_the_org_default(
        self, db_session: Any, authenticate: Any
    ) -> None:
        s = make(db_session, authenticate)
        pattern = s.pattern()
        db_session.execute(
            update(OrganizationSettings)
            .where(OrganizationSettings.organization_id == s.org.id)
            .values(default_weekly_off_pattern_id=pattern["id"])
        )
        db_session.commit()
        response = s.client.patch(
            f"{s.base}/weekly-off-patterns/{pattern['id']}",
            json={"days": []},
            headers=if_match(pattern),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == ErrorCode.WEEKLY_OFF_PATTERN_IN_USE
        renamed = s.client.patch(
            f"{s.base}/weekly-off-patterns/{pattern['id']}",
            json={"pattern_name": "Renamed"},
            headers=if_match(pattern),
        )
        assert renamed.status_code == 200, renamed.text


class TestModuleGateAndPermissions:
    def test_mutation_on_a_disabled_module_is_refused(
        self, db_session: Any, authenticate: Any
    ) -> None:
        s = make(db_session, authenticate, module=False)
        response = s.client.post(
            f"{s.base}/holiday-calendars",
            json={"calendar_code": "X", "calendar_name": "Calendar X"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == ErrorCode.MODULE_NOT_ENABLED

    def test_hr_executive_cannot_manage_holidays_by_default(
        self, db_session: Any, authenticate: Any
    ) -> None:
        s = make(db_session, authenticate, roles=(Role.HR_EXECUTIVE,))
        response = s.client.post(
            f"{s.base}/holiday-calendars", json={"calendar_code": "X", "calendar_name": "Cal X"}
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED

    def test_a_past_holiday_also_needs_attendance_correct(
        self, db_session: Any, authenticate: Any
    ) -> None:
        # OA holds holiday.manage but not attendance.correct (§10.3).
        s = make(db_session, authenticate, roles=(Role.ORGANIZATION_ADMIN,))
        calendar = s.calendar()
        past = (_today() - timedelta(days=3)).isoformat()
        url = f"{s.base}/holiday-calendars/{calendar['id']}/holidays"
        denied = s.client.post(
            url, json={"holiday_date": past, "holiday_name": "Late", "holiday_type": "MANDATORY"}
        )
        assert denied.status_code == 403
        future = (_today() + timedelta(days=30)).isoformat()
        allowed = s.client.post(
            url, json={"holiday_date": future, "holiday_name": "Soon", "holiday_type": "OPTIONAL"}
        )
        assert allowed.status_code == 201, allowed.text


class TestCalendarsAndHolidays:
    def test_only_one_active_default_calendar(self, db_session: Any, authenticate: Any) -> None:
        s = make(db_session, authenticate)
        s.calendar("A", default=True)
        second = s.client.post(
            f"{s.base}/holiday-calendars",
            json={"calendar_code": "B", "calendar_name": "Cal B", "is_organization_default": True},
        )
        assert second.status_code == 409

    def test_cancelled_date_can_be_added_again_but_active_duplicates_cannot(
        self, db_session: Any, authenticate: Any
    ) -> None:
        s = make(db_session, authenticate)
        calendar = s.calendar()
        day = (_today() + timedelta(days=40)).isoformat()
        url = f"{s.base}/holiday-calendars/{calendar['id']}/holidays"
        body = {"holiday_date": day, "holiday_name": "Diwali", "holiday_type": "MANDATORY"}
        first = s.client.post(url, json=body)
        assert first.status_code == 201, first.text
        assert s.client.post(url, json=body).status_code == 409

        cancelled = s.client.post(
            f"{url}/{first.json()['id']}/cancel",
            json={"cancellation_reason": "Declared in error"},
            headers=if_match(first.json()),
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "CANCELLED"
        assert s.client.post(url, json=body).status_code == 201

    def test_branch_assignments_do_not_overlap(self, db_session: Any, authenticate: Any) -> None:
        s = make(db_session, authenticate, roles=(Role.ORGANIZATION_ADMIN, Role.HR_ADMIN))
        branch = s.client.post(f"{s.base}/branches", json=BRANCH)
        assert branch.status_code == 201, branch.text
        calendar = s.calendar()
        url = f"{s.base}/branches/{branch.json()['id']}/holiday-calendar-assignments"
        first = s.client.post(
            url,
            json={
                "calendar_id": calendar["id"],
                "effective_from": "2026-01-01",
                "effective_to": "2027-01-01",
            },
        )
        assert first.status_code == 201, first.text
        overlap = s.client.post(
            url, json={"calendar_id": calendar["id"], "effective_from": "2026-06-01"}
        )
        assert overlap.status_code == 422
        adjacent = s.client.post(
            url, json={"calendar_id": calendar["id"], "effective_from": "2027-01-01"}
        )
        assert adjacent.status_code == 201, adjacent.text


class TestTenantIsolation:
    def test_foreign_organization_path_and_foreign_ids(
        self, db_session: Any, authenticate: Any
    ) -> None:
        a = make(db_session, authenticate)
        b = make(db_session, authenticate)
        calendar = a.calendar()
        assert b.client.get(f"{a.base}/holiday-calendars").status_code == 403
        response = b.client.get(f"{b.base}/holiday-calendars/{calendar['id']}")
        assert response.status_code == 404
        assert b.client.get(f"{b.base}/holiday-calendars").json()["items"] == []
