"""Task definition rules, dependency graphs, due dates and derived fields (addendum 4.3-4.8)."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.onboarding_rules import (
    TaskDefinitionError,
    can_transition,
    due_date,
    find_cycle,
    instance_phase,
    is_overdue,
    normalize_task,
    progress_percent,
)

BASE = {"phase": "PRE_JOINING", "owner_type": "EMPLOYEE"}


def _problem_fields(error: pytest.ExceptionInfo[TaskDefinitionError]) -> set[str]:
    return {field for field, _ in error.value.problems}


def test_checklist_defaults_and_employee_visibility() -> None:
    task = normalize_task({**BASE, "task_type": "CHECKLIST"})
    assert task["is_mandatory"] is True and task["is_waivable"] is False
    assert task["visible_to_employee"] is True  # forced for EMPLOYEE-owned tasks
    assert task["reviewer_type"] is None and task["min_documents"] is None


def test_optional_tasks_default_to_waivable() -> None:
    task = normalize_task({**BASE, "task_type": "CHECKLIST", "is_mandatory": False})
    assert task["is_waivable"] is True


def test_blocking_rules() -> None:
    with pytest.raises(TaskDefinitionError) as error:
        normalize_task(
            {**BASE, "task_type": "CHECKLIST", "is_mandatory": False, "blocks_activation": True}
        )
    assert _problem_fields(error) == {"blocks_activation"}
    with pytest.raises(TaskDefinitionError):
        normalize_task(
            {
                "phase": "POST_JOINING",
                "owner_type": "HR_COORDINATOR",
                "task_type": "CHECKLIST",
                "blocks_activation": True,
            }
        )


def test_data_submission_forces_review_and_needs_a_reviewer_and_section() -> None:
    with pytest.raises(TaskDefinitionError) as error:
        normalize_task({**BASE, "task_type": "DATA_SUBMISSION"})
    assert _problem_fields(error) == {"reviewer_type", "data_section_code"}
    task = normalize_task(
        {
            **BASE,
            "task_type": "DATA_SUBMISSION",
            "data_section_code": "CONTACT",
            "reviewer_type": "HR_COORDINATOR",
        }
    )
    assert task["review_required"] is True


def test_document_upload_forces_no_review_and_defaults_verification_from_the_category() -> None:
    task = normalize_task(
        {
            **BASE,
            "task_type": "DOCUMENT_UPLOAD",
            "required_document_category_id": "c",
            "review_required": True,
        },
        category_requires_verification=True,
    )
    assert task["review_required"] is False
    assert task["requires_document_verification"] is True and task["min_documents"] == 1
    with pytest.raises(TaskDefinitionError) as error:
        normalize_task(
            {
                **BASE,
                "task_type": "DOCUMENT_UPLOAD",
                "required_document_category_id": "c",
                "requires_document_verification": True,
            },
            category_requires_verification=False,
        )
    assert _problem_fields(error) == {"requires_document_verification"}


def test_acknowledgement_requirements() -> None:
    with pytest.raises(TaskDefinitionError) as error:
        normalize_task(
            {**BASE, "task_type": "ACKNOWLEDGEMENT", "acknowledgement_kind": "PRIVACY_NOTICE"}
        )
    assert {
        "policy_version_label",
        "acknowledgement_statement",
        "privacy_notice_basis",
    } <= _problem_fields(error)
    task = normalize_task(
        {
            **BASE,
            "task_type": "ACKNOWLEDGEMENT",
            "acknowledgement_kind": "POLICY",
            "policy_version_label": "2026.1",
            "acknowledgement_statement": "I accept",
            "policy_file_id": "f",
        }
    )
    assert task["acknowledgement_kind"] == "POLICY"


def test_owner_and_evidence_rules() -> None:
    with pytest.raises(TaskDefinitionError) as error:
        normalize_task(
            {
                "phase": "PRE_JOINING",
                "owner_type": "SPECIFIC_USER",
                "task_type": "DATA_SUBMISSION",
                "data_section_code": "BANK",
                "reviewer_type": "DEPARTMENT_QUEUE",
                "requires_evidence": True,
            }
        )
    assert _problem_fields(error) == {
        "owner_user_id",
        "reviewer_department_id",
        "requires_evidence",
    }


def test_cycle_detection() -> None:
    assert find_cycle([("b", "a"), ("c", "b")]) is None
    cycle = find_cycle([("a", "b"), ("b", "c"), ("c", "a")])
    assert cycle is not None and cycle[0] == cycle[-1]
    assert find_cycle([("a", "a")]) is not None


def test_due_dates_follow_the_basis_and_are_null_without_a_joining_date() -> None:
    joining, initiated = date(2026, 12, 1), date(2026, 11, 20)
    assert due_date("JOINING_DATE", -3, joining_date=joining, initiated_on=initiated) == date(
        2026, 11, 28
    )
    assert due_date("INITIATION_DATE", 5, joining_date=None, initiated_on=initiated) == date(
        2026, 11, 25
    )
    assert due_date("JOINING_DATE", 0, joining_date=None, initiated_on=initiated) is None


def test_phase_progress_and_overdue() -> None:
    joining = date(2026, 12, 1)
    assert instance_phase(None, date(2026, 12, 1)) == "PRE_JOINING"
    assert instance_phase(joining, date(2026, 11, 30)) == "PRE_JOINING"
    assert instance_phase(joining, joining) == "JOINING_DAY"
    assert instance_phase(joining, date(2026, 12, 2)) == "POST_JOINING"
    assert progress_percent(0, 0) == 100 and progress_percent(12, 7) == 58
    assert is_overdue("IN_PROGRESS", date(2026, 1, 1), date(2026, 1, 2))
    assert not is_overdue("COMPLETED", date(2026, 1, 1), date(2026, 1, 2))
    assert not is_overdue("NOT_STARTED", None, date(2026, 1, 2))


def test_state_machine_table() -> None:
    assert can_transition("NOT_STARTED", "IN_PROGRESS") and can_transition("SUBMITTED", "REJECTED")
    assert can_transition("COMPLETED", "IN_PROGRESS")  # reopen
    assert not can_transition("COMPLETED", "REJECTED")
    assert not can_transition("CANCELLED", "IN_PROGRESS")
    assert not can_transition("REJECTED", "COMPLETED")
