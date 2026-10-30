"""Concurrency: salary overlap and primary-bank invariants."""

from __future__ import annotations

import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.audit.writer import AuditWriter
from app.core.context import OrgContext, RequestMetadata
from app.core.crypto import BlindIndexer, FieldCipher
from app.core.errors import SalaryAssignmentOverlap
from app.core.permissions import ROLE_PERMISSIONS, PermissionSet, Role
from app.integrations.keys import EnvKeyProvider
from app.repositories.bank import EmployeeBankAccountRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.organization import (
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.repositories.salary import (
    AssignmentComponentRepository,
    SalaryAssignmentRepository,
    SalaryComponentRepository,
    SalaryRevisionHistoryRepository,
    SalaryTemplateComponentRepository,
    SalaryTemplateRepository,
    SalaryTemplateVersionRepository,
)
from app.services.bank import BankAccountService
from app.services.salary import SalaryService
from tests.integration.test_employees import _setup
from tests.integration.test_salary import _component, _employee, _published_template


def _context(organization_id: uuid.UUID, user: Any) -> OrgContext:
    return OrgContext(
        user_id=user.id,
        email=user.email,
        display_name="Load",
        metadata=RequestMetadata(),
        organization_id=organization_id,
        permissions=PermissionSet(
            roles=frozenset({Role.ORGANIZATION_ADMIN, Role.HR_ADMIN}),
            permissions=ROLE_PERMISSIONS[Role.ORGANIZATION_ADMIN] | ROLE_PERMISSIONS[Role.HR_ADMIN],
        ),
    )


def _salary_service(session: Session, organization_id: uuid.UUID) -> SalaryService:
    return SalaryService(
        components=SalaryComponentRepository(session, organization_id),
        templates=SalaryTemplateRepository(session, organization_id),
        versions=SalaryTemplateVersionRepository(session, organization_id),
        mappings=SalaryTemplateComponentRepository(session, organization_id),
        assignments=SalaryAssignmentRepository(session, organization_id),
        assignment_components=AssignmentComponentRepository(session),
        history=SalaryRevisionHistoryRepository(session, organization_id),
        employees=EmployeeRepository(session, organization_id),
        organizations=OrganizationRepository(session),
        settings=OrganizationSettingsRepository(session),
        audit=AuditWriter(session),
    )


def test_overlapping_confirmation_race(
    api_client: Any, db_session: Any, authenticate: Any, engine: Any
) -> None:
    organization, client, masters = _setup(db_session, authenticate)
    component = _component(client, organization.id)
    version = _published_template(client, organization.id, component["id"])
    employee = _employee(client, organization.id, masters)
    base = f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/salary-assignments"
    first = client.post(
        base,
        json={
            "template_id": version["template_id"],
            "effective_from": "2027-01-01",
            "effective_to": "2027-06-01",
            "revision_reason": "ANNUAL_REVISION",
        },
    ).json()
    second = client.post(
        base,
        json={
            "template_id": version["template_id"],
            "effective_from": "2027-01-01",
            "effective_to": "2027-07-01",
            "revision_reason": "PROMOTION",
        },
    ).json()
    from app.models.access import UserOrganizationAccess
    from app.models.platform import User

    grant = (
        db_session.query(UserOrganizationAccess)
        .filter(UserOrganizationAccess.organization_id == organization.id)
        .first()
    )
    user = db_session.get(User, grant.user_id)

    def confirm(assignment_id: str) -> str:
        for attempt in range(5):
            session = Session(engine)
            try:
                service = _salary_service(session, organization.id)
                context = _context(organization.id, user)
                service.confirm(
                    context,
                    uuid.UUID(employee["id"]),
                    uuid.UUID(assignment_id),
                    backdate_reason=None,
                )
                session.commit()
                return "confirmed"
            except SalaryAssignmentOverlap:
                session.rollback()
                return "overlap"
            except Exception:
                session.rollback()
                if attempt == 4:
                    raise
                time.sleep(random.uniform(0.01, 0.05))
            finally:
                session.close()
        return "failed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, [first["id"], second["id"]]))
    assert sorted(results) == ["confirmed", "overlap"], results


def test_primary_bank_race(
    api_client: Any, db_session: Any, authenticate: Any, engine: Any, test_settings: Any
) -> None:
    organization, client, masters = _setup(db_session, authenticate)
    employee = _employee(client, organization.id, masters)
    keys = EnvKeyProvider(
        encryption_master_key=test_settings.decoded_encryption_key(),
        blind_index_master_key=test_settings.decoded_blind_index_key(),
        key_version=test_settings.encryption_key_version,
    )
    from app.models.access import UserOrganizationAccess
    from app.models.platform import User

    grant = (
        db_session.query(UserOrganizationAccess)
        .filter(UserOrganizationAccess.organization_id == organization.id)
        .first()
    )
    user = db_session.get(User, grant.user_id)

    def add(account_number: str) -> None:
        session = Session(engine)
        try:
            service = BankAccountService(
                accounts=EmployeeBankAccountRepository(session, organization.id),
                employees=EmployeeRepository(session, organization.id),
                organizations=OrganizationRepository(session),
                cipher=FieldCipher(keys),
                blind_indexer=BlindIndexer(keys),
                audit=AuditWriter(session),
            )
            service.add_account(
                _context(organization.id, user),
                employee["id"],
                {
                    "account_holder_name": "Asha Rao",
                    "bank_name": "State Bank of India",
                    "account_number": account_number,
                    "ifsc_code": "SBIN0001234",
                    "account_type": "SAVINGS",
                    "effective_from": date(2026, 10, 1),
                    "is_primary": True,
                },
                [],
            )
            session.commit()
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(add, ["30123456789", "40123456780"]))

    db_session.expire_all()
    rows = client.get(
        f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/bank-accounts"
    ).json()
    assert len([row for row in rows if row["is_primary"]]) == 1
