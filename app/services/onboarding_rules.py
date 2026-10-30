"""Pure onboarding rules (addendum 4.3, 4.6, 4.7, 4.8). No database access, no clock."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from typing import Any

OPEN_STATUSES = frozenset({"NOT_STARTED", "IN_PROGRESS", "SUBMITTED", "REJECTED"})
CLOSED_OK = frozenset({"COMPLETED", "WAIVED"})
DATA_SECTIONS = frozenset(
    {
        "PERSONAL",
        "CONTACT",
        "IDENTIFIERS",
        "EDUCATION",
        "PREVIOUS_EMPLOYMENT",
        "FAMILY",
        "NOMINATIONS",
        "BANK",
    }
)

# 4.8: (from_status -> allowed target statuses) for user-driven actions.
TRANSITIONS: dict[str, frozenset[str]] = {
    "NOT_STARTED": frozenset({"IN_PROGRESS", "COMPLETED", "SUBMITTED", "WAIVED", "CANCELLED"}),
    "IN_PROGRESS": frozenset({"COMPLETED", "SUBMITTED", "WAIVED", "CANCELLED"}),
    "SUBMITTED": frozenset({"COMPLETED", "REJECTED", "WAIVED", "CANCELLED", "IN_PROGRESS"}),
    "REJECTED": frozenset({"SUBMITTED", "WAIVED", "CANCELLED"}),
    "COMPLETED": frozenset({"IN_PROGRESS"}),
    "WAIVED": frozenset({"IN_PROGRESS"}),
    "CANCELLED": frozenset(),
}


def can_transition(source: str, target: str) -> bool:
    return target in TRANSITIONS.get(source, frozenset())


class TaskDefinitionError(ValueError):
    """A task definition breaks a 4.3 rule; ``problems`` is ``[(field, message)]``."""

    def __init__(self, problems: list[tuple[str, str]]) -> None:
        super().__init__("; ".join(f"{f}: {m}" for f, m in problems))
        self.problems = problems


def normalize_task(  # noqa: PLR0912, PLR0915 - one branch per 4.3 rule, in spec order
    values: Mapping[str, Any], *, category_requires_verification: bool | None = None
) -> dict[str, Any]:
    """Apply the 4.3 defaults and forced values, then validate the type-specific requirements."""
    task = dict(values)
    problems: list[tuple[str, str]] = []

    def need(field: str, message: str) -> None:
        if task.get(field) in (None, ""):
            problems.append((field, message))

    task_type = task["task_type"]
    owner = task["owner_type"]
    task.setdefault("is_mandatory", True)
    task.setdefault("blocks_activation", False)
    task["is_waivable"] = task.get("is_waivable", not task["is_mandatory"])
    task.setdefault("requires_evidence", False)
    task.setdefault("due_basis", "JOINING_DATE")
    task.setdefault("due_offset_days", 0)
    task.setdefault("display_order", 0)

    if task["blocks_activation"] and not task["is_mandatory"]:
        problems.append(("blocks_activation", "A blocking task must be mandatory."))
    if task["blocks_activation"] and task["phase"] == "POST_JOINING":
        problems.append(("blocks_activation", "A POST_JOINING task cannot block activation."))
    if owner == "SPECIFIC_USER":
        need("owner_user_id", "Required for SPECIFIC_USER.")
    if owner == "DEPARTMENT_QUEUE":
        need("owner_department_id", "Required for DEPARTMENT_QUEUE.")
    task["visible_to_employee"] = (
        True if owner == "EMPLOYEE" else task.get("visible_to_employee", False)
    )

    # Review: forced on for data submissions, forced off for uploads (verification does it).
    if task_type == "DATA_SUBMISSION":
        task["review_required"] = True
    elif task_type == "DOCUMENT_UPLOAD":
        task["review_required"] = False
    task["review_required"] = task.get("review_required", False)
    if task["review_required"]:
        need("reviewer_type", "Required when review is required.")
        if task.get("reviewer_type") == "SPECIFIC_USER":
            need("reviewer_user_id", "Required for a SPECIFIC_USER reviewer.")
        if task.get("reviewer_type") == "DEPARTMENT_QUEUE":
            need("reviewer_department_id", "Required for a DEPARTMENT_QUEUE reviewer.")
        if task.get("reviewer_type") in ("EMPLOYEE", "REPORTING_MANAGER"):
            problems.append(("reviewer_type", "Not an allowed reviewer type."))
    else:
        for field in ("reviewer_type", "reviewer_user_id", "reviewer_department_id"):
            task[field] = None

    if task_type != "CHECKLIST" and task["requires_evidence"]:
        problems.append(("requires_evidence", "Only a CHECKLIST task can require evidence."))
    if task_type == "DOCUMENT_UPLOAD":
        need("required_document_category_id", "Required for DOCUMENT_UPLOAD.")
        task["min_documents"] = task.get("min_documents") or 1
        if not 1 <= task["min_documents"] <= 20:
            problems.append(("min_documents", "Must be 1 to 20."))
        verify = task.get("requires_document_verification")
        if verify is None:
            verify = bool(category_requires_verification)
        if verify and category_requires_verification is False:
            problems.append(
                ("requires_document_verification", "The category does not require verification.")
            )
        task["requires_document_verification"] = verify
    else:
        task["min_documents"] = None
        task["requires_document_verification"] = None
        task["required_document_category_id"] = None
    if task_type == "DATA_SUBMISSION":
        if task.get("data_section_code") not in DATA_SECTIONS:
            problems.append(("data_section_code", "Required: a valid section."))
    else:
        task["data_section_code"] = None
    if task_type == "ACKNOWLEDGEMENT":
        need("acknowledgement_kind", "Required for ACKNOWLEDGEMENT.")
        need("policy_version_label", "Required for ACKNOWLEDGEMENT.")
        need("acknowledgement_statement", "Required for ACKNOWLEDGEMENT.")
        if task.get("acknowledgement_kind") == "POLICY":
            need("policy_file_id", "Required for a POLICY acknowledgement.")
        if task.get("acknowledgement_kind") == "PRIVACY_NOTICE":
            need("privacy_notice_basis", "Required for a PRIVACY_NOTICE acknowledgement.")
            if task.get("privacy_notice_basis") == "CONSENT":
                need("consent_scope", "Required when the basis is CONSENT.")
    else:
        for field in (
            "acknowledgement_kind",
            "policy_file_id",
            "policy_version_label",
            "privacy_notice_basis",
            "consent_scope",
            "acknowledgement_statement",
        ):
            task[field] = None
    if problems:
        raise TaskDefinitionError(problems)
    return task


def find_cycle(edges: Iterable[tuple[str, str]]) -> list[str] | None:
    """A dependency cycle as a list of node keys, or None. Edges are (task, prerequisite)."""
    graph: dict[str, list[str]] = {}
    for task, prerequisite in edges:
        graph.setdefault(task, []).append(prerequisite)
        graph.setdefault(prerequisite, [])
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in graph[node]:
            if state.get(nxt, 0) == 1:
                return [*stack[stack.index(nxt) :], nxt]
            if state.get(nxt, 0) == 0 and (found := visit(nxt)) is not None:
                return found
        state[node] = 2
        stack.pop()
        return None

    for node in graph:
        if state.get(node, 0) == 0 and (found := visit(node)) is not None:
            return found
    return None


def due_date(
    basis: str, offset_days: int, *, joining_date: date | None, initiated_on: date
) -> date | None:
    """4.7: ``basis + offset``; null when the basis (joining date) is not known yet."""
    anchor = joining_date if basis == "JOINING_DATE" else initiated_on
    return None if anchor is None else anchor + timedelta(days=offset_days)


def instance_phase(joining_date: date | None, today: date) -> str:
    """ONB-06: informational phase derived from the joining date."""
    if joining_date is None or today < joining_date:
        return "PRE_JOINING"
    return "JOINING_DAY" if today == joining_date else "POST_JOINING"


def progress_percent(mandatory_total: int, mandatory_closed: int) -> int:
    return 100 if mandatory_total == 0 else (100 * mandatory_closed) // mandatory_total


def is_overdue(status: str, due: date | None, today: date) -> bool:
    return due is not None and due < today and status in OPEN_STATUSES
