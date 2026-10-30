"""§24.7 job behaviours."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.core.clock import utc_now
from app.db.types import uuid7
from app.jobs.runner import (
    check_integrity,
    cleanup_pending_uploads,
    refresh_id_rule_statuses,
    refresh_salary_statuses,
)
from app.models.audit import IntegrityFinding
from app.models.document import EmployeeDocument
from app.models.employee_code import EmployeeIdGenerationRule
from app.models.salary import EmployeeSalaryAssignment
from app.storage.local import LocalStorage
from tests.integration.test_documents import _category, _initiate
from tests.integration.test_employees import _employee_body, _setup
from tests.integration.test_salary import (
    TODAY,
    _component,
    _employee,
    _published_template,
)


class TestJobs:
    def test_rule_status_refresh(self, api_client: Any, db_session: Any, authenticate: Any) -> None:
        organization, client, _ = _setup(db_session, authenticate)
        created = client.post(
            f"/api/v1/orgs/{organization.id}/employee-id-rules",
            json={
                "rule_name": "Future rule",
                "rule_code": "FUTURE",
                "format_pattern": "{ORG}{SEP}{SEQ}",
                "separator": "-",
                "effective_from": "2027-01-01",
                "priority": 999,
            },
        ).json()
        client.post(f"/api/v1/orgs/{organization.id}/employee-id-rules/{created['id']}/publish")
        row = db_session.get(EmployeeIdGenerationRule, created["id"])
        row.effective_from = date(2020, 1, 1)
        db_session.commit()
        assert refresh_id_rule_statuses(db_session, organization.id) == 1
        db_session.commit()
        db_session.refresh(row)
        assert row.status == "ACTIVE"

    def test_salary_status_refresh(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        component = _component(client, organization.id)
        version = _published_template(client, organization.id, component["id"])
        employee = _employee(client, organization.id, masters)
        created = client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/salary-assignments",
            json={
                "template_id": version["template_id"],
                "effective_from": TODAY,
                "revision_reason": "NEW_JOINER",
                "confirm": True,
            },
        ).json()
        row = db_session.get(EmployeeSalaryAssignment, created["id"])
        row.effective_from = date(2020, 1, 1)
        row.effective_to = date(2020, 2, 1)
        db_session.commit()
        assert refresh_salary_statuses(db_session, organization.id) == 1
        db_session.commit()
        db_session.refresh(row)
        assert row.assignment_status == "EXPIRED"

    def test_pending_upload_cleanup(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = client.post(
            f"/api/v1/orgs/{organization.id}/employees", json=_employee_body(masters)
        ).json()
        category = _category(client, organization.id)
        started = _initiate(client, organization.id, employee["id"], category["id"]).json()
        row = db_session.get(EmployeeDocument, started["id"])
        row.created_at = utc_now() - timedelta(hours=25)
        db_session.commit()
        storage = LocalStorage(__import__("pathlib").Path(__import__("tempfile").mkdtemp()))
        assert cleanup_pending_uploads(db_session, storage) == 1
        db_session.commit()
        db_session.refresh(row)
        assert row.upload_status == "FAILED"

    def test_integrity_detects_overlap(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters)
        for start, end in (
            (date(2020, 1, 1), date(2021, 1, 1)),
            (date(2020, 6, 1), date(2022, 1, 1)),
        ):
            db_session.add(
                EmployeeSalaryAssignment(
                    id=uuid7(),
                    organization_id=organization.id,
                    employee_id=employee["id"],
                    currency="INR",
                    assignment_status="ACTIVE",
                    approval_state="NOT_REQUIRED",
                    effective_from=start,
                    effective_to=end,
                    effective_to_source="USER",
                    revision_reason="TEST",
                )
            )
        db_session.commit()
        assert check_integrity(db_session, organization.id) == 1
        db_session.commit()
        finding = db_session.query(IntegrityFinding).one()
        assert finding.severity == "P1"
