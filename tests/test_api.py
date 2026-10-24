"""End-to-end smoke tests across the main API surface."""
from __future__ import annotations


def test_root_and_health(client):
    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["data"]["name"]

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["data"]["status"] == "ok"


def test_requires_authentication(client):
    response = client.get("/api/employees")
    assert response.status_code == 401
    assert response.json()["errors"][0]["code"] == "UNAUTHENTICATED"


def test_login_and_me(client, auth_headers):
    me = client.get("/api/auth/me", headers=auth_headers)
    assert me.status_code == 200
    assert me.json()["data"]["email"] == "admin@test.example.com"
    assert "global_admin" in me.json()["data"]["role_codes"]


def test_organization_crud(client, auth_headers):
    created = client.post(
        "/api/organizations",
        json={
            "name": "Crud Org",
            "country_code": "US",
            "default_currency": "USD",
            "timezone": "America/New_York",
        },
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    org_id = created.json()["data"]["id"]

    listed = client.get("/api/organizations", headers=auth_headers)
    assert listed.status_code == 200
    assert listed.json()["meta"]["total"] >= 1

    fetched = client.get(f"/api/organizations/{org_id}", headers=auth_headers)
    assert fetched.status_code == 200

    patched = client.patch(
        f"/api/organizations/{org_id}",
        json={"legal_name": "Crud Org LLC"},
        headers=auth_headers,
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["legal_name"] == "Crud Org LLC"

    deleted = client.delete(f"/api/organizations/{org_id}", headers=auth_headers)
    assert deleted.status_code == 200
    assert client.get(f"/api/organizations/{org_id}", headers=auth_headers).status_code == 404


def test_employee_flow(client, auth_headers):
    orgs = client.get("/api/organizations", headers=auth_headers).json()["data"]
    org_id = orgs[0]["id"]
    entities = client.get("/api/legal-entities", headers=auth_headers).json()["data"]
    entity_id = entities[0]["id"]

    dept = client.post(
        "/api/departments",
        json={"org_id": org_id, "name": "Quality", "code": "QA"},
        headers=auth_headers,
    )
    assert dept.status_code == 201, dept.text
    dept_id = dept.json()["data"]["id"]

    designation = client.post(
        "/api/designations",
        json={"org_id": org_id, "title": "QA Engineer", "code": "QAE"},
        headers=auth_headers,
    )
    assert designation.status_code == 201
    designation_id = designation.json()["data"]["id"]

    employee = client.post(
        "/api/employees",
        json={
            "org_id": org_id,
            "employee_code": "EMP9001",
            "first_name": "Vikram",
            "last_name": "Singh",
            "email": "vikram.singh@test.local",
            "date_of_joining": "2025-01-15",
            "employment_type": "full_time",
            "legal_entity_id": entity_id,
            "department_id": dept_id,
            "designation_id": designation_id,
        },
        headers=auth_headers,
    )
    assert employee.status_code == 201, employee.text
    employee_id = employee.json()["data"]["id"]

    personal = client.put(
        f"/api/employees/{employee_id}/personal-details",
        json={"city": "Pune", "state": "Maharashtra"},
        headers=auth_headers,
    )
    assert personal.status_code == 200
    assert personal.json()["data"]["city"] == "Pune"

    profile = client.get(f"/api/employees/{employee_id}/profile", headers=auth_headers)
    assert profile.status_code == 200
    assert profile.json()["data"]["personal_details"]["city"] == "Pune"

    directory = client.get("/api/employees/directory/list", headers=auth_headers)
    assert directory.status_code == 200

    audit = client.get(
        "/api/audit-logs",
        params={"filter[entity_type]": "employees"},
        headers=auth_headers,
    )
    assert audit.status_code == 200
    assert audit.json()["meta"]["total"] >= 1


def test_leave_flow(client, auth_headers):
    orgs = client.get("/api/organizations", headers=auth_headers).json()["data"]
    org_id = orgs[0]["id"]
    employees = client.get("/api/employees", headers=auth_headers).json()["data"]
    employee_id = employees[0]["id"]

    leave_type = client.post(
        "/api/leave-types",
        json={"org_id": org_id, "name": "Casual Leave", "code": "CL", "accrual_type": "monthly"},
        headers=auth_headers,
    )
    assert leave_type.status_code == 201, leave_type.text
    leave_type_id = leave_type.json()["data"]["id"]

    balance = client.post(
        "/api/leave-balances",
        json={
            "employee_id": employee_id,
            "leave_type_id": leave_type_id,
            "balance": "12",
            "available": "12",
        },
        headers=auth_headers,
    )
    assert balance.status_code == 201

    request = client.post(
        "/api/leave-requests",
        json={
            "employee_id": employee_id,
            "leave_type_id": leave_type_id,
            "start_date": "2026-10-01",
            "end_date": "2026-10-03",
            "days": "3",
            "reason": "Family",
        },
        headers=auth_headers,
    )
    assert request.status_code == 201, request.text
    request_id = request.json()["data"]["id"]

    approved = client.post(f"/api/leave-requests/{request_id}/approve", headers=auth_headers)
    assert approved.status_code == 200
    assert approved.json()["data"]["status"] == "approved"

    balances = client.get(f"/api/employees/{employee_id}/leave-balances", headers=auth_headers)
    assert balances.status_code == 200
    row = next(b for b in balances.json()["data"] if b["leave_type_id"] == leave_type_id)
    assert float(row["used"]) == 3.0
    assert float(row["available"]) == 9.0


def test_currency_conversion(client, auth_headers):
    orgs = client.get("/api/organizations", headers=auth_headers).json()["data"]
    org_id = orgs[0]["id"]
    client.post(
        "/api/currency-rates",
        json={
            "org_id": org_id,
            "base_currency": "USD",
            "quote_currency": "INR",
            "rate": "83.5",
            "effective_date": "2026-01-01",
        },
        headers=auth_headers,
    )
    converted = client.get(
        "/api/currency/convert",
        params={"amount": "100", "base": "USD", "quote": "INR", "on_date": "2026-06-01"},
        headers=auth_headers,
    )
    assert converted.status_code == 200, converted.text
    assert converted.json()["data"]["converted_amount"] == "8350.00"


def test_payroll_run_lifecycle(client, auth_headers):
    orgs = client.get("/api/organizations", headers=auth_headers).json()["data"]
    org_id = orgs[0]["id"]
    entities = client.get("/api/legal-entities", headers=auth_headers).json()["data"]
    entity_id = entities[0]["id"]
    employees = client.get("/api/employees", headers=auth_headers).json()["data"]
    employee_id = employees[0]["id"]

    pay_group = client.post(
        "/api/pay-groups",
        json={
            "org_id": org_id,
            "legal_entity_id": entity_id,
            "name": "Test Group",
            "pay_cycle": "monthly",
            "currency": "INR",
        },
        headers=auth_headers,
    )
    assert pay_group.status_code == 201, pay_group.text
    pay_group_id = pay_group.json()["data"]["id"]

    structure = client.post(
        "/api/salary-structures",
        json={"org_id": org_id, "name": "Test Structure", "code": "TS-1"},
        headers=auth_headers,
    )
    structure_id = structure.json()["data"]["id"]

    client.patch(
        f"/api/employees/{employee_id}",
        json={"pay_group_id": pay_group_id},
        headers=auth_headers,
    )
    salary = client.post(
        "/api/employee-salaries",
        json={
            "employee_id": employee_id,
            "structure_id": structure_id,
            "ctc": "1200000",
            "effective_from": "2026-01-01",
            "currency": "INR",
        },
        headers=auth_headers,
    )
    assert salary.status_code == 201, salary.text

    run = client.post(
        "/api/payroll-runs",
        json={
            "org_id": org_id,
            "legal_entity_id": entity_id,
            "pay_group_id": pay_group_id,
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
            "pay_date": "2026-10-01",
            "run_type": "regular",
        },
        headers=auth_headers,
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["data"]["id"]

    processed = client.post(f"/api/payroll-runs/{run_id}/process", headers=auth_headers)
    assert processed.status_code == 200, processed.text
    assert processed.json()["data"]["processed"] >= 1

    validated = client.post(f"/api/payroll-runs/{run_id}/validate", headers=auth_headers)
    assert validated.status_code == 200

    approved = client.post(f"/api/payroll-runs/{run_id}/approve", headers=auth_headers)
    assert approved.status_code == 200

    finalized = client.post(f"/api/payroll-runs/{run_id}/finalize", headers=auth_headers)
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["data"]["payslips"] >= 1

    summary = client.get(f"/api/payroll-runs/{run_id}/summary", headers=auth_headers)
    assert summary.status_code == 200
    assert summary.json()["data"]["headcount"] >= 1

    bank = client.get(f"/api/payroll-runs/{run_id}/bank-file", headers=auth_headers)
    assert bank.status_code == 200
    assert "employee_code" in bank.text


def test_dashboard_and_reports(client, auth_headers):
    dashboard = client.get("/api/dashboard", headers=auth_headers)
    assert dashboard.status_code == 200
    assert "headcount" in dashboard.json()["data"]

    inbox = client.get("/api/inbox", headers=auth_headers)
    assert inbox.status_code == 200
    assert isinstance(inbox.json()["data"], list)

    reports = client.get("/api/reports", headers=auth_headers)
    assert reports.status_code == 200
    assert len(reports.json()["data"]) >= 1

    headcount = client.get("/api/reports/headcount", headers=auth_headers)
    assert headcount.status_code == 200
