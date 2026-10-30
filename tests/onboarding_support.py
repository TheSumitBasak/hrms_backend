"""Shared setup for the onboarding suites (addendum 4)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import update

from app.core.permissions import Role
from app.models.organization import OrganizationSettings
from tests.factories import create_user, enable_configurable_permission
from tests.job_architecture import Architecture, if_match, setup_architecture

CHECKLIST = {"phase": "PRE_JOINING", "task_type": "CHECKLIST", "owner_type": "HR_COORDINATOR"}

STANDARD_TASKS: list[dict[str, Any]] = [
    {
        **CHECKLIST,
        "task_code": "KYC_CHECK",
        "title": "Verify identity documents",
        "blocks_activation": True,
        "is_waivable": True,
        "due_offset_days": -3,
    },
    {**CHECKLIST, "task_code": "SETUP", "title": "Prepare the workstation", "due_offset_days": 1},
    {
        **CHECKLIST,
        "task_code": "WELCOME_KIT",
        "title": "Hand over the welcome kit",
        "is_mandatory": False,
        "is_waivable": True,
        "due_offset_days": 2,
    },
    {
        "phase": "PRE_JOINING",
        "task_type": "ACKNOWLEDGEMENT",
        "owner_type": "EMPLOYEE",
        "task_code": "PRIVACY",
        "title": "Acknowledge the privacy notice",
        "acknowledgement_kind": "PRIVACY_NOTICE",
        "policy_version_label": "2026.1",
        "privacy_notice_basis": "NOTICE_ONLY",
        "acknowledgement_statement": "I have read the privacy notice",
        "due_offset_days": -5,
    },
    {
        "phase": "PRE_JOINING",
        "task_type": "DATA_SUBMISSION",
        "owner_type": "EMPLOYEE",
        "task_code": "CONTACT",
        "title": "Provide your contact details",
        "data_section_code": "CONTACT",
        "reviewer_type": "HR_COORDINATOR",
        "due_offset_days": -7,
    },
]
STANDARD_DEPENDENCIES = [{"task_code": "SETUP", "prerequisite_task_code": "KYC_CHECK"}]


class OnboardingWorld:
    def __init__(self, arch: Architecture, db: Any, authenticate: Any) -> None:
        self.arch = arch
        self.db = db
        self.authenticate = authenticate
        self.hr = arch.client
        self.org_id = arch.organization.id
        self.base = arch.base
        self._granted = False

    def enable(self, **settings: Any) -> None:
        """Turn the module on (first call) or change organisation flags (later calls)."""
        values = {
            "enable_onboarding_module": True,
            "enable_onboarding_self_service": True,
            **settings,
        }
        self.db.execute(
            update(OrganizationSettings)
            .where(OrganizationSettings.organization_id == self.org_id)
            .values(**values)
        )
        if not self._granted:
            enable_configurable_permission(
                self.db,
                organization_id=self.org_id,
                role=Role.EMPLOYEE_SELF,
                permission_code="self.onboarding",
            )
            self._granted = True
        self.db.commit()

    def draft(self, **overrides: Any) -> dict[str, Any]:
        return self.arch.create_employee(activate=False, **overrides)

    def active(self, **overrides: Any) -> dict[str, Any]:
        return self.arch.create_employee(**overrides)

    def self_client(self, employee: dict[str, Any]) -> Any:
        user = create_user(
            self.db,
            grants=[(self.org_id, Role.EMPLOYEE_SELF)],
            employee_id=uuid.UUID(employee["id"]),
        )
        self.db.commit()
        return self.authenticate(user)

    def client_for(self, role: Role, employee_id: str | None = None) -> Any:
        user = create_user(
            self.db,
            grants=[(self.org_id, role)],
            employee_id=uuid.UUID(employee_id) if employee_id else None,
        )
        self.db.commit()
        return self.authenticate(user)

    def template(
        self,
        code: str = "STD",
        *,
        tasks: list[dict[str, Any]] | None = None,
        dependencies: list[dict[str, str]] | None = None,
        publish: bool = True,
        **version: Any,
    ) -> dict[str, Any]:
        response = self.hr.post(
            f"{self.base}/onboarding-templates",
            json={
                "template_code": code,
                "template_name": f"{code} onboarding",
                "version": {"effective_from": "2026-01-01", **version},
                "tasks": STANDARD_TASKS if tasks is None else tasks,
                "dependencies": STANDARD_DEPENDENCIES if dependencies is None else dependencies,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert response.status_code == 201, response.text
        template = dict(response.json())
        if publish:
            draft = template["versions"][0]
            published = self.hr.post(
                f"{self.base}/onboarding-templates/{template['id']}/versions/{draft['id']}/publish",
                headers=if_match(draft),
            )
            assert published.status_code == 200, published.text
        return template

    def initiate(self, employee: dict[str, Any], **body: Any) -> Any:
        return self.hr.post(
            f"{self.base}/employees/{employee['id']}/onboarding",
            json=body,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )

    def task(self, instance: dict[str, Any], code: str) -> dict[str, Any]:
        found = next(t for t in instance["tasks"] if t["task_code"] == code)
        return dict(found)

    def act(
        self, client: Any, instance: dict[str, Any], code: str, verb: str, body: Any = None
    ) -> Any:
        task = self.refresh_task(instance, code)
        return client.post(
            f"{self.base}/onboarding/{instance['id']}/tasks/{task['id']}/{verb}",
            json=body or {},
            headers=if_match(task),
        )

    def refresh_task(self, instance: dict[str, Any], code: str) -> dict[str, Any]:
        fresh = self.hr.get(f"{self.base}/onboarding/{instance['id']}").json()
        return self.task(fresh, code)

    def reload(self, instance: dict[str, Any]) -> dict[str, Any]:
        response = self.hr.get(f"{self.base}/onboarding/{instance['id']}")
        assert response.status_code == 200, response.text
        return dict(response.json())


def make_world(db_session: Any, authenticate: Any, **settings: Any) -> OnboardingWorld:
    arch = setup_architecture(db_session, authenticate, with_job=False)
    world = OnboardingWorld(arch, db_session, authenticate)
    world.enable(**settings)
    return world
