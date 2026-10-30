"""§24.4.7 employee ID rules, preview and atomic issuance."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from app.core.errors import ErrorCode
from app.core.permissions import ROLE_PERMISSIONS, PermissionSet, Role
from app.db.types import uuid7
from app.models.employee import Employee
from tests.factories import create_organization, create_user

RULE = {
    "rule_name": "Organization-wide codes",
    "rule_code": "ORG-SERIES",
    "format_pattern": "{ORG}{SEP}{SEQ}",
    "separator": "-",
    "sequence_reset_policy": "NEVER",
    "sequence_start": 1,
    "sequence_increment": 1,
    "sequence_padding_length": 4,
    "effective_from": "2020-01-01",
    "priority": 100,
}


def _client(db_session: Any, authenticate: Any):
    organization = create_organization(db_session)
    user = create_user(db_session, grants=[(organization.id, Role.ORGANIZATION_ADMIN)])
    db_session.commit()
    return organization, authenticate(user), user


def _create_published_rule(client: Any, organization_code: str) -> dict[str, Any]:
    created = client.post(f"/api/v1/orgs/{organization_code}/employee-id-rules", json=RULE)
    assert created.status_code == 201, created.text
    rule = created.json()
    published = client.post(
        f"/api/v1/orgs/{organization_code}/employee-id-rules/{rule['id']}/publish"
    )
    assert published.status_code == 200, published.text
    return published.json()


class TestIdRules:
    def test_create_and_publish_derives_active(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, _ = _client(db_session, authenticate)
        rule = _create_published_rule(client, str(organization.id))
        assert rule["status"] == "ACTIVE"
        assert rule["include_organization_code"] is True
        assert rule["include_year"] is False

    def test_preview_does_not_consume_a_sequence(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, _ = _client(db_session, authenticate)
        _create_published_rule(client, str(organization.id))
        url = f"/api/v1/orgs/{organization.id}/employee-id-rules/preview"

        first = client.post(url, json={"basis_date": "2026-10-28"})
        assert first.status_code == 200, first.text
        assert first.json()["indicative"] is True
        assert first.json()["indicative_code"] == f"{organization.code}-0001"

        second = client.post(url, json={"basis_date": "2026-10-28"})
        assert second.json()["indicative_code"] == f"{organization.code}-0001"

        sequences = client.get(f"/api/v1/orgs/{organization.id}/employee-id-sequences")
        assert sequences.json() == []

    def test_publish_conflict_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, _ = _client(db_session, authenticate)
        _create_published_rule(client, str(organization.id))
        second = client.post(
            f"/api/v1/orgs/{organization.id}/employee-id-rules",
            json={**RULE, "rule_code": "ORG-SERIES-2"},
        )
        assert second.status_code == 201, second.text
        response = client.post(
            f"/api/v1/orgs/{organization.id}/employee-id-rules/{second.json()['id']}/publish"
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == ErrorCode.ID_RULE_CONFLICT.value

    def test_pattern_is_validated(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, _ = _client(db_session, authenticate)
        response = client.post(
            f"/api/v1/orgs/{organization.id}/employee-id-rules",
            json={**RULE, "rule_code": "BAD", "format_pattern": "{ORG}"},
        )
        assert response.status_code == 422

    def test_draft_is_editable_but_issued_rule_is_not(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, user = _client(db_session, authenticate)
        created = client.post(f"/api/v1/orgs/{organization.id}/employee-id-rules", json=RULE).json()
        current = client.get(f"/api/v1/orgs/{organization.id}/employee-id-rules/{created['id']}")
        patched = client.patch(
            f"/api/v1/orgs/{organization.id}/employee-id-rules/{created['id']}",
            json={"rule_name": "Renamed draft"},
            headers={"If-Match": current.headers["etag"]},
        )
        assert patched.status_code == 200, patched.text

        published = client.post(
            f"/api/v1/orgs/{organization.id}/employee-id-rules/{created['id']}/publish"
        )
        assert published.status_code == 200, published.text
        rule = published.json()

        employee = Employee(
            id=uuid7(),
            organization_id=organization.id,
            first_name="Asha",
            last_name="Rao",
            status="DRAFT",
        )
        db_session.add(employee)
        db_session.commit()

        from app.audit.writer import AuditWriter
        from app.core.context import RequestMetadata
        from app.repositories.employee import EmployeeRepository
        from app.repositories.employee_code import (
            EmployeeCodeRegistryRepository,
            EmployeeIdSequenceRepository,
            IdRuleRepository,
        )
        from app.repositories.master import DepartmentRepository
        from app.repositories.organization import (
            OrganizationRepository,
            OrganizationSettingsRepository,
        )
        from app.services.employee_codes import EmployeeCodeService

        service = EmployeeCodeService(
            rules=IdRuleRepository(db_session, organization.id),
            sequences=EmployeeIdSequenceRepository(db_session, organization.id),
            registry=EmployeeCodeRegistryRepository(db_session, organization.id),
            organizations=OrganizationRepository(db_session),
            settings=OrganizationSettingsRepository(db_session),
            departments=DepartmentRepository(db_session, organization.id),
            employees=EmployeeRepository(db_session, organization.id),
            audit=AuditWriter(db_session),
        )
        from app.core.context import OrgContext
        from app.core.permissions import Role as RoleEnum

        context = OrgContext(
            user_id=user.id,
            email=user.email,
            display_name="Admin",
            metadata=RequestMetadata(),
            organization_id=organization.id,
            permissions=PermissionSet(
                roles=frozenset({RoleEnum.ORGANIZATION_ADMIN}),
                permissions=ROLE_PERMISSIONS[RoleEnum.ORGANIZATION_ADMIN],
            ),
        )
        code, issued_rule = service.issue_code(
            context, employee=employee, basis_date=date(2026, 10, 28)
        )
        assert code == f"{organization.code}-0001"
        assert issued_rule.id == uuid.UUID(rule["id"])
        employee.employee_code = code
        db_session.commit()

        import pytest

        from app.core.errors import IdRuleImmutable

        with pytest.raises(IdRuleImmutable):
            service.update_rule(
                context,
                rule_id=uuid.UUID(rule["id"]),
                changes={"format_pattern": "{ORG}{SEQ}"},
                if_match=rule["row_version"],
            )

    def test_issuance_increments_the_shared_counter(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, user = _client(db_session, authenticate)
        _create_published_rule(client, str(organization.id))

        from app.audit.writer import AuditWriter
        from app.core.context import OrgContext, RequestMetadata
        from app.core.permissions import Role as RoleEnum
        from app.repositories.employee import EmployeeRepository
        from app.repositories.employee_code import (
            EmployeeCodeRegistryRepository,
            EmployeeIdSequenceRepository,
            IdRuleRepository,
        )
        from app.repositories.master import DepartmentRepository
        from app.repositories.organization import (
            OrganizationRepository,
            OrganizationSettingsRepository,
        )
        from app.services.employee_codes import EmployeeCodeService

        service = EmployeeCodeService(
            rules=IdRuleRepository(db_session, organization.id),
            sequences=EmployeeIdSequenceRepository(db_session, organization.id),
            registry=EmployeeCodeRegistryRepository(db_session, organization.id),
            organizations=OrganizationRepository(db_session),
            settings=OrganizationSettingsRepository(db_session),
            departments=DepartmentRepository(db_session, organization.id),
            employees=EmployeeRepository(db_session, organization.id),
            audit=AuditWriter(db_session),
        )
        context = OrgContext(
            user_id=user.id,
            email=user.email,
            display_name="Admin",
            metadata=RequestMetadata(),
            organization_id=organization.id,
            permissions=PermissionSet(
                roles=frozenset({RoleEnum.ORGANIZATION_ADMIN}),
                permissions=ROLE_PERMISSIONS[RoleEnum.ORGANIZATION_ADMIN],
            ),
        )
        codes = []
        for first in ("Asha", "Bala"):
            employee = Employee(
                id=uuid7(),
                organization_id=organization.id,
                first_name=first,
                last_name="Rao",
                status="DRAFT",
            )
            db_session.add(employee)
            db_session.flush()
            code, _ = service.issue_code(context, employee=employee, basis_date=date(2026, 10, 28))
            codes.append(code)
        db_session.commit()
        assert codes == [f"{organization.code}-0001", f"{organization.code}-0002"]
