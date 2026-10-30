"""Onboarding document tasks: link, verify-driven completion, rejection (addendum 4.11)."""

from __future__ import annotations

import uuid
from typing import Any

from app.core.errors import ErrorCode
from app.core.permissions import Role
from tests.integration.test_documents import PDF, _category, _initiate
from tests.onboarding_support import OnboardingWorld, make_world

UPLOAD_TASK = {
    "phase": "PRE_JOINING",
    "task_type": "DOCUMENT_UPLOAD",
    "owner_type": "HR_COORDINATOR",
    "task_code": "ID_PROOF",
    "title": "Collect the identity proof",
    "requires_document_verification": True,
}


def _world(
    db: Any, authenticate: Any
) -> tuple[OnboardingWorld, dict[str, Any], dict[str, Any], str]:
    w = make_world(db, authenticate)
    category = _category(w.hr, w.org_id)
    task = {**UPLOAD_TASK, "required_document_category_id": category["id"]}
    w.template("DOCS", tasks=[task], dependencies=[])
    employee = w.draft()
    instance = w.initiate(employee).json()
    return w, employee, instance, category["id"]


def _upload(w: OnboardingWorld, employee: dict[str, Any], category_id: str) -> dict[str, Any]:
    initiated = _initiate(w.hr, w.org_id, employee["id"], category_id)
    assert initiated.status_code == 201, initiated.text
    body = initiated.json()
    assert w.hr.put(body["upload_url"], content=PDF).status_code == 204
    done = w.hr.post(f"{w.base}/employees/{employee['id']}/documents/{body['id']}/complete")
    assert done.status_code == 200, done.text
    return dict(done.json())


def _verify(w: OnboardingWorld, employee: dict[str, Any], document_id: str, **body: Any) -> Any:
    verifier = w.client_for(Role.HR_ADMIN)
    return verifier.post(
        f"{w.base}/employees/{employee['id']}/documents/{document_id}/verify", json=body
    )


def _link(w: OnboardingWorld, instance: dict[str, Any], document_id: str) -> Any:
    task = w.refresh_task(instance, "ID_PROOF")
    return w.hr.post(
        f"{w.base}/onboarding/{instance['id']}/tasks/{task['id']}/documents",
        json={"document_id": document_id},
    )


def test_verification_completes_the_linked_document_task(
    db_session: Any, authenticate: Any
) -> None:
    w, employee, instance, category_id = _world(db_session, authenticate)
    document = _upload(w, employee, category_id)

    linked = _link(w, instance, document["id"])
    assert linked.status_code == 201, linked.text
    assert _link(w, instance, document["id"]).json()["id"] == linked.json()["id"]  # idempotent

    assert w.task(w.reload(instance), "ID_PROOF")["status"] == "SUBMITTED"  # linking submits

    assert _verify(w, employee, document["id"], decision="VERIFIED").status_code == 200
    assert w.task(w.reload(instance), "ID_PROOF")["status"] == "COMPLETED"


def test_rejected_document_sends_the_task_back(db_session: Any, authenticate: Any) -> None:
    w, employee, instance, category_id = _world(db_session, authenticate)
    document = _upload(w, employee, category_id)
    assert _link(w, instance, document["id"]).status_code == 201

    rejected = _verify(
        w, employee, document["id"], decision="REJECTED", rejection_reason="The scan is unreadable"
    )
    assert rejected.status_code == 200, rejected.text
    assert w.task(w.reload(instance), "ID_PROOF")["status"] == "REJECTED"


def test_document_of_another_category_or_employee_cannot_be_linked(
    db_session: Any, authenticate: Any
) -> None:
    w, employee, instance, _ = _world(db_session, authenticate)
    other_category = _category(
        w.hr, w.org_id, category_code="SIDE_PROOF", category_name="Other proof"
    )
    wrong = _upload(w, employee, other_category["id"])
    refused = _link(w, instance, wrong["id"])
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == ErrorCode.DOCUMENT_TASK_MISMATCH.value

    stranger = w.draft(first_name="Stranger")
    foreign = _upload(w, stranger, other_category["id"])
    assert _link(w, instance, foreign["id"]).status_code == 404

    missing = _link(w, instance, str(uuid.uuid4()))
    assert missing.status_code == 404
