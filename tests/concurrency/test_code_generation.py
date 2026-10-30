"""AC-16: 50 concurrent code issuances are unique and contiguous."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.audit.writer import AuditWriter
from app.core.context import OrgContext, RequestMetadata
from app.core.permissions import ROLE_PERMISSIONS, PermissionSet, Role
from app.models.employee import Employee
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
from tests.integration.test_employees import _employee_body, _setup


def _service(
    session: Session, organization_id: Any, user: Any
) -> tuple[EmployeeCodeService, OrgContext]:
    service = EmployeeCodeService(
        rules=IdRuleRepository(session, organization_id),
        sequences=EmployeeIdSequenceRepository(session, organization_id),
        registry=EmployeeCodeRegistryRepository(session, organization_id),
        organizations=OrganizationRepository(session),
        settings=OrganizationSettingsRepository(session),
        departments=DepartmentRepository(session, organization_id),
        employees=EmployeeRepository(session, organization_id),
        audit=AuditWriter(session),
    )
    context = OrgContext(
        user_id=user.id,
        email=user.email,
        display_name="Load",
        metadata=RequestMetadata(),
        organization_id=organization_id,
        permissions=PermissionSet(
            roles=frozenset({Role.ORGANIZATION_ADMIN}),
            permissions=ROLE_PERMISSIONS[Role.ORGANIZATION_ADMIN],
        ),
    )
    return service, context


def test_ac16_fifty_concurrent_codes(
    api_client: Any, db_session: Any, authenticate: Any, engine: Any
) -> None:
    organization, client, masters = _setup(db_session, authenticate)
    from app.models.access import UserOrganizationAccess
    from app.models.platform import User

    grant = (
        db_session.query(UserOrganizationAccess)
        .filter(UserOrganizationAccess.organization_id == organization.id)
        .first()
    )
    user = db_session.get(User, grant.user_id)
    employees = []
    for index in range(50):
        employee = client.post(
            f"/api/v1/orgs/{organization.id}/employees",
            json=_employee_body(masters, first_name=f"Load{index}", employee_code=None),
        ).json()
        employees.append(employee["id"])

    # ON_CREATE issued codes above; reset the employees to simulate 50 simultaneous first
    # issuances against the same sequence. The registry rows are removed, not reused.
    from app.models.employee_code import EmployeeCodeRegistry

    db_session.query(Employee).filter(Employee.id.in_(employees)).update(
        {"employee_code": None},
        synchronize_session=False,
    )
    db_session.query(EmployeeCodeRegistry).filter(
        EmployeeCodeRegistry.employee_id.in_(employees)
    ).delete(synchronize_session=False)
    from app.models.employee_code import EmployeeIdSequence

    db_session.query(EmployeeIdSequence).filter(
        EmployeeIdSequence.organization_id == organization.id
    ).delete(synchronize_session=False)
    db_session.commit()

    def issue(employee_id: Any) -> str:
        session = Session(engine)
        try:
            service, context = _service(session, organization.id, user)
            employee = session.get(Employee, employee_id)
            assert employee is not None
            code, _ = service.issue_code(context, employee=employee, basis_date=date(2026, 10, 28))
            employee.employee_code = code
            session.commit()
            return code
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(issue, employees))

    assert len(set(codes)) == 50
    numbers = sorted(int(code.rsplit("-", 1)[1]) for code in codes)
    assert numbers == list(range(1, 51))
