"""Ad-hoc smoke test for newly added endpoints (run: python -m scripts.smoke_new)."""
from __future__ import annotations

import io
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.models as _models  # noqa: F401
from app.main import app

fails: list[tuple] = []
checked = 0


def call(method, path, expected, json=None, files=None, data=None, headers=None, label=None):
    global checked
    checked += 1
    kwargs = {}
    if json is not None:
        kwargs["json"] = json
    if files is not None:
        kwargs["files"] = files
    if data is not None:
        kwargs["data"] = data
    if headers:
        kwargs["headers"] = headers
    resp = client.request(method, path, **kwargs)
    ok = resp.status_code in (expected if isinstance(expected, (tuple, list)) else (expected,))
    if not ok:
        fails.append((label or f"{method} {path}", resp.status_code, resp.text[:160]))
    return resp


with TestClient(app) as client:
    # --- auth ---------------------------------------------------------------
    login = call("POST", "/api/auth/login", 200, json={"email": "admin@example.com", "password": "Admin@12345"}, label="login")
    tokens = login.json()["data"]
    H = {"Authorization": f"Bearer {tokens['access_token']}"}
    call("POST", "/api/auth/refresh", 200, json={"refresh_token": tokens["refresh_token"]}, label="refresh")
    call("GET", "/api/auth/me", 200, headers=H, label="me")
    call("GET", "/api/auth/permissions", 200, headers=H, label="permissions")
    call("PATCH", "/api/auth/me", 200, json={"phone": "+919812345678"}, headers=H, label="patch me")
    call("POST", "/api/auth/forgot-password", 200, json={"email": "admin@example.com"}, label="forgot-password")

    orgs = call("GET", "/api/organizations", 200, headers=H, label="orgs").json()["data"]
    org_id = orgs[0]["id"]
    call("POST", "/api/auth/switch-org", 200, json={"org_id": org_id}, headers=H, label="switch-org")

    employees = call("GET", "/api/employees", 200, headers=H, label="employees").json()["data"]
    employee_id = employees[0]["id"] if employees else None

    # --- access -------------------------------------------------------------
    uniq = f"inv_{int(time.time())}@example.com"
    inv = call("POST", "/api/users/invite", 201, json={"email": uniq, "role_codes": ["global_admin"]}, headers=H, label="invite")
    invited_id = inv.json()["data"]["user_id"]
    call("GET", f"/api/users/{invited_id}/login-history", 200, headers=H, label="login-history")
    call("POST", f"/api/users/{invited_id}/reset-password", 200, json={}, headers=H, label="admin reset-password")

    # --- lookups / search ---------------------------------------------------
    call("GET", "/api/lookups/departments?q=", 200, headers=H, label="lookup departments")
    call("GET", "/api/employees/search?q=a", 200, headers=H, label="employee search")
    call("GET", "/api/search?q=a", 200, headers=H, label="global search")

    # --- me / dashboard -----------------------------------------------------
    call("GET", "/api/me/preferences", 200, headers=H, label="get prefs")
    call("PUT", "/api/me/preferences", 200, json={"theme": "dark"}, headers=H, label="put prefs")
    call("GET", "/api/me/dashboard-layout", 200, headers=H, label="get layout")
    call("PUT", "/api/me/dashboard-layout", 200, json={"widgets": ["headcount", "payroll"]}, headers=H, label="put layout")
    dash = call("GET", "/api/dashboard", 200, headers=H, label="dashboard")
    for key in ("attrition_trend", "payroll_by_department", "approvals", "events"):
        if key not in dash.json()["data"]:
            fails.append(("dashboard missing " + key, 200, "missing"))

    rnd = uuid.uuid4()
    # --- payroll ------------------------------------------------------------
    call("GET", f"/api/payroll-runs/{rnd}/variances", 404, headers=H, label="variances 404")
    call("GET", f"/api/payroll-runs/{rnd}/statutory", 404, headers=H, label="statutory 404")
    call("GET", f"/api/payroll-runs/{rnd}/challans?type=pf", 404, headers=H, label="challan 404")
    call("GET", f"/api/payroll-runs/{rnd}/approvals", 404, headers=H, label="run approvals 404")
    call("GET", f"/api/payroll-runs/{rnd}/audit", 404, headers=H, label="run audit 404")
    call("POST", "/api/payslips/bulk-release", 200, json={"ids": [], "action": "release"}, headers=H, label="bulk release empty")

    # --- finance ------------------------------------------------------------
    call("POST", f"/api/expense-claims/{rnd}/remind", 404, headers=H, label="expense remind 404")
    call("POST", f"/api/expense-claims/{rnd}/unpaid", 404, headers=H, label="expense unpaid 404")
    call("POST", f"/api/loans/{rnd}/remind", 404, headers=H, label="loan remind 404")
    call("POST", f"/api/loans/{rnd}/mark-paid-outside-payroll", 404, headers=H, label="loan mark paid 404")
    call("POST", f"/api/tax-declarations/{rnd}/remind", 404, headers=H, label="tax remind 404")

    # --- HR -----------------------------------------------------------------
    if employee_id:
        call("GET", f"/api/leave-requests/team-overlap?employee_id={employee_id}&start=2026-01-01&end=2026-12-31", 200, headers=H, label="team overlap")
        call("GET", f"/api/employees/{employee_id}/onboarding", 200, headers=H, label="employee onboarding")
        avatar = call("POST", f"/api/employees/{employee_id}/avatar", 200, files={"file": ("a.png", io.BytesIO(b"PNGDATA"), "image/png")}, headers=H, label="avatar")
        call("GET", f"/api/employees/{employee_id}/avatar", 200, headers=H, label="get avatar")
    call("GET", "/api/attendance-records/summary?from=2026-01-01&to=2026-12-31", 200, headers=H, label="attendance summary")
    call("GET", f"/api/attendance-records/{rnd}/timeline", 404, headers=H, label="attendance timeline 404")
    call("POST", f"/api/attendance-records/{rnd}/mark-manual", 404, json={"status": "present"}, headers=H, label="mark manual 404")
    call("POST", f"/api/exit-requests/{rnd}/reject", 404, json={"reason": "x"}, headers=H, label="exit reject 404")
    call("POST", f"/api/exit-requests/{rnd}/cancel", 404, headers=H, label="exit cancel 404")
    call("POST", f"/api/exit-requests/{rnd}/full-and-final", 404, headers=H, label="f&f 404")
    call("POST", f"/api/assets/{rnd}/unavailable", 404, json={}, headers=H, label="asset unavailable 404")

    # --- workflows ----------------------------------------------------------
    call("POST", f"/api/approvals/{rnd}/request-info", 404, json={"comments": "x"}, headers=H, label="request-info 404")
    call("POST", "/api/notifications/read-all", 200, headers=H, label="read-all")
    call("GET", "/api/notifications/unread-count", 200, headers=H, label="unread-count")
    call("GET", "/api/inbox/me", 200, headers=H, label="inbox/me")

    # --- settings -----------------------------------------------------------
    call("GET", "/api/organizations/current", 200, headers=H, label="organizations/current")
    call("GET", "/api/settings/currency", 200, headers=H, label="get currency settings")
    call("PUT", "/api/settings/currency", 200, json={"display_mode": "converted_only", "allow_user_override": True}, headers=H, label="put currency settings")
    call("GET", "/api/tenant/features", 200, headers=H, label="tenant features")
    call("POST", f"/api/integrations/{rnd}/connect", 404, json={"config": {}}, headers=H, label="integration connect 404")
    call("POST", f"/api/integrations/{rnd}/callback", 404, json={"code": "abc"}, headers=H, label="integration callback 404")
    csv_data = b"base_currency,quote_currency,rate,effective_date,source\nUSD,INR,83.5,2026-01-01,import\n"
    call("POST", "/api/currency-rates/import", 200, files={"file": ("rates.csv", io.BytesIO(csv_data), "text/csv")}, headers=H, label="import rates")

    # --- documents ----------------------------------------------------------
    call("POST", f"/api/documents/{rnd}/signed-url", 404, headers=H, label="signed-url 404")

    # --- reports ------------------------------------------------------------
    call("GET", "/api/reports/schedules", 200, headers=H, label="list schedules")
    sched = call("POST", "/api/reports/schedules", 201, json={"report_code": "headcount", "frequency": "weekly", "recipients": ["a@b.com"]}, headers=H, label="create schedule")
    sched_id = sched.json()["data"]["id"]
    call("DELETE", f"/api/reports/schedules/{sched_id}", 200, headers=H, label="delete schedule")
    call("GET", "/api/reports/headcount/export?format=csv", 200, headers=H, label="report export csv")

    # --- cross-cutting ------------------------------------------------------
    call("POST", "/api/departments/bulk-update", 200, json={"ids": [], "data": {"is_active": True}}, headers=H, label="bulk-update")
    call("POST", "/api/auth/logout", 200, headers=H, label="logout")

print(f"checked {checked} calls, failures: {len(fails)}")
for f in fails:
    print("  FAIL", f)
