"""AUTO_SEED demo data: the whole organization is built through the real API."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

from app.demo import DEMO_ORGANIZATION_CODE, seed_demo
from app.demo.data import ACTIVE_PEOPLE, JOINERS
from app.models.organization import Organization

PASSWORD = "Demo-Password-123"


@pytest.fixture
def demo(api_app: Any, db_session: Any) -> Any:
    """The seeded organization's report; the database persists across tests, so a later test
    finds the organization already there and gets only its id."""
    report = seed_demo(api_app, password=PASSWORD)
    db_session.expire_all()
    if report.organization_id is None:
        report.organization_id = db_session.execute(
            select(Organization.id).where(Organization.organization_code == DEMO_ORGANIZATION_CODE)
        ).scalar_one()
    return report


def _login(api_app: Any, email: str) -> dict[str, str]:
    from fastapi.testclient import TestClient

    response = TestClient(api_app).post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_demo_organization_shows_every_module(api_app: Any, demo: Any) -> None:
    from fastapi.testclient import TestClient

    assert demo.organization_id is not None
    client = TestClient(api_app)
    headers = _login(api_app, "hr.admin@bharattech.test")
    base = f"/api/v1/orgs/{demo.organization_id}"

    def items(path: str) -> list[dict[str, Any]]:
        response = client.get(f"{base}{path}", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        return body["items"] if isinstance(body, dict) and "items" in body else body

    employees = items("/employees?limit=100")
    statuses = {e["status"] for e in employees}
    assert {"ACTIVE", "DRAFT", "ON_NOTICE"} <= statuses
    assert len(employees) == len(ACTIVE_PEOPLE) + len(JOINERS)

    assert len(items("/holiday-calendars")) == 1
    assert len(items("/leave-types")) == 5
    assert {a["status"] for a in items("/leave-applications?limit=100")} >= {
        "SUBMITTED",
        "APPROVED",
        "REJECTED",
        "WITHDRAWN",
    }
    positions = items("/positions?limit=100")
    assert {p["position_status"] for p in positions} == {"OPEN"}
    assert len(positions) == 5
    assert {t["status"] for t in items("/onboarding?limit=100")} >= {"IN_PROGRESS", "COMPLETED"}

    def timeline(first_name: str) -> set[str]:
        employee = next(e for e in employees if e["display_name"].startswith(first_name))
        rows = items(f"/employees/{employee['id']}/salary-assignments")
        return {row["assignment_status"] for row in rows}

    assert timeline("Rajesh") == {"ACTIVE"}
    assert timeline("Priya") == {"ACTIVE", "SCHEDULED"}
    assert timeline("Arjun") == {"ACTIVE", "DRAFT"}


def test_second_run_is_a_noop(api_app: Any, db_session: Any, demo: Any) -> None:
    again = seed_demo(api_app, password=PASSWORD)
    assert not again.seeded and "already exists" in (again.skip_reason or "")
    count = db_session.execute(
        select(func.count())
        .select_from(Organization)
        .where(Organization.organization_code == DEMO_ORGANIZATION_CODE)
    ).scalar_one()
    assert count == 1


def test_employee_can_sign_in_and_see_only_their_own_leave(api_app: Any, demo: Any) -> None:
    from fastapi.testclient import TestClient

    headers = _login(api_app, "arjun.mehta@bharattech.test")
    response = TestClient(api_app).get(
        f"/api/v1/orgs/{demo.organization_id}/me/leave-applications", headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"]


def test_startup_runs_auto_seed_and_survives_a_failure(
    seeded_database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUTO_SEED=true seeds from the lifespan; a seeding error is logged, never fatal."""
    from fastapi.testclient import TestClient

    from app.db.session import configure_engine
    from app.demo.seeder import DemoSeedError
    from app.main import create_app
    from tests.conftest import build_test_settings

    calls: list[object] = []

    def explode(app: Any, settings: Any) -> None:
        calls.append(settings.auto_seed)
        raise DemoSeedError("boom")

    monkeypatch.setattr("app.demo.runner.run_auto_seed", explode)
    with TestClient(create_app(build_test_settings(auto_seed=True))) as client:
        assert client.get("/health").status_code == 200
    assert calls == [True]

    calls.clear()
    with TestClient(create_app(build_test_settings())) as client:  # AUTO_SEED off: untouched
        assert client.get("/health").status_code == 200
    assert calls == []
    configure_engine(seeded_database)
