"""Tests for the auth, search, settings, payroll-tab and cross-cutting additions."""
from __future__ import annotations

import io
import time


def test_refresh_and_permissions(client, auth_headers):
    me = client.get("/api/auth/me", headers=auth_headers).json()["data"]
    assert "permissions" in me and me["is_global_admin"] is True

    perms = client.get("/api/auth/permissions", headers=auth_headers)
    assert perms.status_code == 200
    assert perms.json()["data"]["is_global_admin"] is True

    login = client.post(
        "/api/auth/login",
        json={"email": "admin@test.example.com", "password": "Admin@12345"},
    ).json()["data"]
    assert login.get("refresh_token")
    refreshed = client.post("/api/auth/refresh", json={"refresh_token": login["refresh_token"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["data"]["access_token"]


def test_forgot_and_reset_password(client):
    forgot = client.post("/api/auth/forgot-password", json={"email": "admin@test.example.com"})
    assert forgot.status_code == 200
    token = forgot.json()["data"].get("reset_token")
    assert token
    reset = client.post(
        "/api/auth/reset-password",
        json={"token": token, "new_password": "Admin@12345"},
    )
    assert reset.status_code == 200
    # token is single-use
    again = client.post(
        "/api/auth/reset-password",
        json={"token": token, "new_password": "Admin@12345"},
    )
    assert again.status_code == 400


def test_switch_org_and_patch_me(client, auth_headers):
    orgs = client.get("/api/organizations", headers=auth_headers).json()["data"]
    org_id = orgs[0]["id"]
    switched = client.post("/api/auth/switch-org", json={"org_id": org_id}, headers=auth_headers)
    assert switched.status_code == 200
    assert switched.json()["data"]["active_org_id"] == org_id

    patched = client.patch("/api/auth/me", json={"phone": "+911234567890"}, headers=auth_headers)
    assert patched.status_code == 200


def test_access_admin_actions(client, auth_headers):
    email = f"invite_{int(time.time())}@example.com"
    inv = client.post("/api/users/invite", json={"email": email}, headers=auth_headers)
    assert inv.status_code == 201, inv.text
    user_id = inv.json()["data"]["user_id"]

    history = client.get(f"/api/users/{user_id}/login-history", headers=auth_headers)
    assert history.status_code == 200

    reset = client.post(f"/api/users/{user_id}/reset-password", json={}, headers=auth_headers)
    assert reset.status_code == 200
    assert reset.json()["data"]["temporary_password"]


def test_lookups_and_search(client, auth_headers):
    depts = client.get("/api/lookups/departments", headers=auth_headers)
    assert depts.status_code == 200
    assert isinstance(depts.json()["data"], list)

    emp_search = client.get("/api/employees/search?q=a", headers=auth_headers)
    assert emp_search.status_code == 200

    global_search = client.get("/api/search?q=", headers=auth_headers)
    assert global_search.status_code == 200
    assert "employees" in global_search.json()["data"]


def test_me_preferences_and_dashboard(client, auth_headers):
    assert client.put("/api/me/preferences", json={"theme": "dark"}, headers=auth_headers).status_code == 200
    assert client.get("/api/me/preferences", headers=auth_headers).json()["data"]["theme"] == "dark"
    assert client.put(
        "/api/me/dashboard-layout", json={"widgets": ["headcount"]}, headers=auth_headers
    ).status_code == 200
    assert client.get("/api/me/dashboard-layout", headers=auth_headers).json()["data"]["widgets"] == ["headcount"]

    dash = client.get("/api/dashboard", headers=auth_headers).json()["data"]
    for key in ("attrition_trend", "payroll_by_department", "approvals", "events", "headcount"):
        assert key in dash


def test_settings_and_features(client, auth_headers):
    current = client.get("/api/organizations/current", headers=auth_headers)
    assert current.status_code == 200
    assert current.json()["data"]["organization"]["id"]

    got = client.get("/api/settings/currency", headers=auth_headers)
    assert got.status_code == 200
    assert "org_default_display_currency" in got.json()["data"]

    put = client.put(
        "/api/settings/currency",
        json={"display_mode": "converted_only"},
        headers=auth_headers,
    )
    assert put.status_code == 200
    assert put.json()["data"]["display_mode"] == "converted_only"

    features = client.get("/api/tenant/features", headers=auth_headers)
    assert features.status_code == 200
    assert any(f["code"] == "multi_currency" for f in features.json()["data"])

    csv_data = b"base_currency,quote_currency,rate,effective_date,source\nUSD,INR,83.5,2026-01-01,test\n"
    imported = client.post(
        "/api/currency-rates/import",
        files={"file": ("rates.csv", io.BytesIO(csv_data), "text/csv")},
        headers=auth_headers,
    )
    assert imported.status_code == 200
    assert imported.json()["data"]["imported"] == 1


def test_report_schedules_and_export(client, auth_headers):
    created = client.post(
        "/api/reports/schedules",
        json={"report_code": "headcount", "frequency": "weekly", "recipients": ["x@y.com"]},
        headers=auth_headers,
    )
    assert created.status_code == 201
    schedule_id = created.json()["data"]["id"]
    assert client.get("/api/reports/schedules", headers=auth_headers).status_code == 200
    assert client.delete(f"/api/reports/schedules/{schedule_id}", headers=auth_headers).status_code == 200

    export = client.get("/api/reports/headcount/export?format=csv", headers=auth_headers)
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")


def test_workflow_and_bulk_update(client, auth_headers):
    assert client.post("/api/notifications/read-all", headers=auth_headers).status_code == 200
    assert client.get("/api/notifications/unread-count", headers=auth_headers).status_code == 200
    assert client.get("/api/inbox/me", headers=auth_headers).status_code == 200

    bulk = client.post(
        "/api/departments/bulk-update",
        json={"ids": [], "data": {"is_active": True}},
        headers=auth_headers,
    )
    assert bulk.status_code == 200
    assert bulk.json()["data"]["updated"] == 0


def test_idempotency_key(client, auth_headers):
    orgs = client.get("/api/organizations", headers=auth_headers).json()["data"]
    org_id = orgs[0]["id"]
    headers = {**auth_headers, "Idempotency-Key": f"test-{time.time()}"}
    payload = {"org_id": org_id, "name": "Idem Org", "code": "IDEM-1", "country_code": "IN", "base_currency": "INR"}
    first = client.post("/api/legal-entities", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    second = client.post("/api/legal-entities", json=payload, headers=headers)
    assert second.status_code == first.status_code
    assert second.headers.get("Idempotency-Replayed") == "true"
