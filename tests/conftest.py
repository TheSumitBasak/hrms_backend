"""Pytest fixtures: isolated database and an authenticated client.

By default the suite runs on a throwaway SQLite file so it works anywhere.
Point it at MySQL by setting ``HRMS_TEST_DATABASE_URL``, e.g.::

    $env:HRMS_TEST_DATABASE_URL="mysql+pymysql://root:admin@127.0.0.1:3306/hrms_test?charset=utf8mb4"
    python -m pytest -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

TEST_DB = BACKEND_ROOT / "test_hrms.db"
DATABASE_URL = os.environ.get("HRMS_TEST_DATABASE_URL") or f"sqlite:///{TEST_DB.as_posix()}"
USING_SQLITE = DATABASE_URL.startswith("sqlite")

os.environ["DATABASE_URL"] = DATABASE_URL
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["SEED_ADMIN_EMAIL"] = "admin@test.example.com"
os.environ["SEED_ADMIN_PASSWORD"] = "Admin@12345"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import (  # noqa: E402
    Base,
    SessionLocal,
    engine,
    ensure_database,
    init_db,
)
from app.core.security import hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import LegalEntity, Organization  # noqa: E402
from app.models.auth import Role, User, UserRole  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database():
    if USING_SQLITE:
        TEST_DB.unlink(missing_ok=True)
    # Make sure the MySQL schema exists, then start from a clean schema.
    ensure_database()
    Base.metadata.drop_all(bind=engine)
    init_db()
    db = SessionLocal()
    try:
        org = Organization(
            name="Test Org",
            country_code="IN",
            default_currency="INR",
            timezone="Asia/Kolkata",
        )
        db.add(org)
        db.flush()
        db.add(
            LegalEntity(
                org_id=org.id,
                name="Test Entity",
                code="TEST-IN",
                country_code="IN",
                base_currency="INR",
            )
        )
        role = Role(org_id=org.id, name="Global Admin", code="global_admin", is_system=True)
        db.add(role)
        db.flush()
        user = User(email="admin@test.example.com", password_hash=hash_password("Admin@12345"))
        db.add(user)
        db.flush()
        db.add(UserRole(user_id=user.id, role_id=role.id))
        db.commit()
    finally:
        db.close()
    yield
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
    if USING_SQLITE:
        try:
            TEST_DB.unlink(missing_ok=True)
        except PermissionError:  # pragma: no cover - Windows file lock
            pass


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def auth_headers(client):
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@test.example.com", "password": "Admin@12345"},
    )
    assert response.status_code == 200, response.text
    token = response.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}
