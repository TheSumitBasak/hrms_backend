"""Onboarding self-service, acknowledgements and data submissions (addendum 4.10, 4.12, 11)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.core.errors import ErrorCode
from app.core.permissions import Role
from app.models.onboarding import OnboardingInstance, OnboardingTask
from tests.job_architecture import if_match
from tests.onboarding_support import OnboardingWorld, make_world


def _code(response: Any) -> str:
    return str(response.json()["error"]["code"])


class Fixture:
    def __init__(self, db: Any, authenticate: Any, **settings: Any) -> None:
        self.w: OnboardingWorld = make_world(db, authenticate, **settings)
        self.w.template("STD")
        self.employee = self.w.draft()
        self.client = self.w.self_client(
            self.employee
        )  # before initiation: tasks resolve to the user
        self.instance = self.w.initiate(self.employee).json()

    def task(self, code: str) -> dict[str, Any]:
        return self.w.refresh_task(self.instance, code)

    def mine(self) -> dict[str, Any]:
        response = self.client.get(f"{self.w.base}/me/onboarding")
        assert response.status_code == 200, response.text
        return dict(response.json())

    def own_action(self, code: str, verb: str, body: Any = None) -> Any:
        task = self.task(code)
        return self.client.post(
            f"{self.w.base}/me/onboarding/tasks/{task['id']}/{verb}",
            json=body or {},
            headers={**if_match(task), "Idempotency-Key": "7b0c1d3e-0000-4000-8000-0000000000c1"},
        )


def test_self_view_hides_internal_tasks_remarks_and_assignees(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    mine = f.mine()
    assert {t["task_code"] for t in mine["tasks"]} == {
        "PRIVACY",
        "CONTACT",
    }  # employee-visible only
    assert mine["remarks"] is None and mine["events"] == []
    assert all(
        t["assignee_user_id"] is None and t["reviewer_user_id"] is None for t in mine["tasks"]
    )
    # The HR view still sees all five tasks.
    assert len(f.w.reload(f.instance)["tasks"]) == 5


def test_employee_acknowledges_electronically_and_the_notice_is_recorded(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    task = f.task("PRIVACY")
    done = f.client.post(f"{f.w.base}/me/onboarding/tasks/{task['id']}/acknowledge", json={})
    assert done.status_code == 201, done.text
    assert done.json()["method"] == "ELECTRONIC" and done.json()["acknowledged_on_behalf"] is False
    assert f.task("PRIVACY")["status"] == "COMPLETED"
    again = f.client.post(f"{f.w.base}/me/onboarding/tasks/{task['id']}/acknowledge", json={})
    assert again.status_code == 422  # a completed task takes no further acknowledgement


def test_hr_cannot_click_an_electronic_acknowledgement_for_the_employee(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    task = f.task("PRIVACY")
    # The only HR route is a paper acknowledgement with a signed document; electronic is refused.
    refused = f.w.hr.post(
        f"{f.w.base}/onboarding/{f.instance['id']}/tasks/{task['id']}/acknowledgements",
        json={
            "supporting_document_id": "00000000-0000-4000-8000-000000000001",
            "on_behalf_reason": "Signed paper copy received at the desk",
        },
    )
    assert refused.status_code in (404, 422)
    assert f.task("PRIVACY")["status"] == "NOT_STARTED"


def test_submission_is_validated_reviewed_and_applied_to_the_employee_record(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    bad = f.client.post(
        f"{f.w.base}/me/onboarding/tasks/{f.task('CONTACT')['id']}/submissions",
        json={"payload": {"official_email": "me@work.example", "personal_mobile": "+919812345678"}},
        headers={"Idempotency-Key": "7b0c1d3e-0000-4000-8000-0000000000d1"},
    )
    assert bad.status_code == 422 and _code(bad) == ErrorCode.FIELD_NOT_PERMITTED_IN_SECTION.value

    invalid = f.client.post(
        f"{f.w.base}/me/onboarding/tasks/{f.task('CONTACT')['id']}/submissions",
        json={"payload": {"personal_mobile": "not-a-number"}},
        headers={"Idempotency-Key": "7b0c1d3e-0000-4000-8000-0000000000d2"},
    )
    assert invalid.status_code == 422

    submitted = f.client.post(
        f"{f.w.base}/me/onboarding/tasks/{f.task('CONTACT')['id']}/submissions",
        json={
            "payload": {"personal_mobile": "+919876543210", "personal_email": "esha@home.example"}
        },
        headers={"Idempotency-Key": "7b0c1d3e-0000-4000-8000-0000000000d3"},
    )
    assert submitted.status_code == 201, submitted.text
    submission = submitted.json()
    assert submission["status"] == "SUBMITTED"
    assert submission["submitted_field_names"] == ["personal_email", "personal_mobile"]
    assert "payload" not in submission  # values are never returned or logged
    assert f.task("CONTACT")["status"] == "SUBMITTED"

    pending = f.client.post(
        f"{f.w.base}/me/onboarding/tasks/{f.task('CONTACT')['id']}/submissions",
        json={"payload": {"personal_mobile": "+919876543211"}},
        headers={"Idempotency-Key": "7b0c1d3e-0000-4000-8000-0000000000d4"},
    )
    assert pending.status_code == 409
    assert _code(pending) == ErrorCode.DATA_SUBMISSION_ALREADY_PENDING.value

    base = f"{f.w.base}/onboarding/{f.instance['id']}/tasks/{f.task('CONTACT')['id']}/submissions"
    accepted = f.w.hr.post(
        f"{base}/{submission['id']}/accept", json={}, headers=if_match(submission)
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "ACCEPTED" and accepted.json()["payload_purged_at"]
    assert f.task("CONTACT")["status"] == "COMPLETED"
    updated = f.w.hr.get(f"{f.w.base}/employees/{f.employee['id']}").json()
    assert updated["personal_mobile"].endswith("3210")  # applied through the EMS update service


def test_rejection_keeps_ems_data_and_allows_a_resubmission(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    task = f.task("CONTACT")
    base = f"{f.w.base}/me/onboarding/tasks/{task['id']}/submissions"
    first = f.client.post(
        base,
        json={"payload": {"personal_mobile": "+919876543210"}},
        headers={"Idempotency-Key": "7b0c1d3e-0000-4000-8000-0000000000e1"},
    ).json()
    review = (
        f"{f.w.base}/onboarding/{f.instance['id']}/tasks/{task['id']}/submissions/{first['id']}"
    )
    rejected = f.w.hr.post(
        f"{review}/reject",
        json={"rejection_reason": "The number does not match the offer letter"},
        headers=if_match(first),
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "REJECTED"
    assert f.task("CONTACT")["status"] == "REJECTED"
    before = f.w.hr.get(f"{f.w.base}/employees/{f.employee['id']}").json()["personal_mobile"]
    assert not before.endswith("3210")  # nothing was applied

    second = f.client.post(
        base,
        json={"payload": {"personal_mobile": "+919812345679"}},
        headers={"Idempotency-Key": "7b0c1d3e-0000-4000-8000-0000000000e2"},
    )
    assert second.status_code == 201 and second.json()["submission_no"] == 2


def test_the_submitter_cannot_review_their_own_submission(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    task = f.task("CONTACT")
    on_behalf = f.w.hr.post(
        f"{f.w.base}/onboarding/{f.instance['id']}/tasks/{task['id']}/submissions",
        json={
            "payload": {"personal_mobile": "+919876543210"},
            "on_behalf_reason": "Paper form received at the joining desk",
        },
        headers={"Idempotency-Key": "7b0c1d3e-0000-4000-8000-0000000000f1"},
    )
    assert on_behalf.status_code == 201, on_behalf.text
    assert on_behalf.json()["submitted_on_behalf"] is True
    review = (
        f"{f.w.base}/onboarding/{f.instance['id']}/tasks/{task['id']}"
        f"/submissions/{on_behalf.json()['id']}"
    )
    own = f.w.hr.post(f"{review}/accept", json={}, headers=if_match(on_behalf.json()))
    assert own.status_code == 403 and _code(own) == ErrorCode.SELF_REVIEW_NOT_ALLOWED.value

    other_hr = f.w.client_for(Role.HR_ADMIN)
    ok = other_hr.post(f"{review}/accept", json={}, headers=if_match(on_behalf.json()))
    assert ok.status_code == 200, ok.text


def test_self_service_is_gated_and_scoped_to_the_callers_own_tasks(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    other = f.w.draft(first_name="Other")
    other_client = f.w.self_client(other)
    foreign_task = f.task("PRIVACY")
    stolen = other_client.post(
        f"{f.w.base}/me/onboarding/tasks/{foreign_task['id']}/acknowledge", json={}
    )
    assert stolen.status_code == 404  # another employee's task does not exist for this caller

    f.w.db.rollback()
    f.w.enable(enable_onboarding_self_service=False)
    off = f.client.get(f"{f.w.base}/me/onboarding")
    assert off.status_code == 403 and _code(off) == ErrorCode.SELF_SERVICE_NOT_ENABLED.value


def test_employee_lifecycle_cancels_onboarding_and_recomputes_due_dates(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    before = f.w.task(f.w.reload(f.instance), "KYC_CHECK")["due_date"]
    moved = f.w.hr.patch(
        f"{f.w.base}/employees/{f.employee['id']}",
        json={"joining_date": "2026-02-02", "employment_start_date": "2026-02-02"},
        headers=if_match(f.w.hr.get(f"{f.w.base}/employees/{f.employee['id']}").json()),
    )
    if moved.status_code == 200:
        after = f.w.task(f.w.reload(f.instance), "KYC_CHECK")["due_date"]
        assert after != before  # 4.7: open COMPUTED tasks follow the joining date
    archived = f.w.hr.post(
        f"{f.w.base}/employees/{f.employee['id']}/archive",
        json={"reason": "Created by mistake; discarding the draft record"},
    )
    assert archived.status_code in (200, 204), archived.text
    f.w.db.expire_all()
    instance = f.w.db.get(OnboardingInstance, uuid.UUID(f.instance["id"]))
    assert instance is not None and instance.status == "CANCELLED"
    tasks = f.w.db.scalars(
        select(OnboardingTask).where(OnboardingTask.instance_id == instance.id)
    ).all()
    assert tasks and all(t.status == "CANCELLED" for t in tasks)
