"""Employee onboarding (addendum 4): templates, instances, tasks, documents, submissions.

MySQL notes (addendum 8.7 gives PostgreSQL definitions): partial unique indexes are UNIQUE
indexes over stored generated columns that are NULL outside the predicate (``draft_key``,
``open_key``, ``pending_key``); exclusion constraints on version intervals cannot exist, so
non-overlap and conflict (ONBOARDING_TEMPLATE_CONFLICT) are enforced by the service under a row
lock on the parent template.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import (
    AppendOnlyColumns,
    StandardColumns,
    archive_reason_constraint,
    tenant_index,
)
from app.db.types import UtcDateTime, Uuid7
from app.models.attendance import CODE_PATTERN_SQL

PHASES = "('PRE_JOINING','JOINING_DAY','POST_JOINING')"
TASK_TYPES = "('CHECKLIST','DOCUMENT_UPLOAD','DATA_SUBMISSION','ACKNOWLEDGEMENT')"
OWNER_TYPES = "('HR_COORDINATOR','REPORTING_MANAGER','EMPLOYEE','SPECIFIC_USER','DEPARTMENT_QUEUE')"
SECTIONS = (
    "('PERSONAL','CONTACT','IDENTIFIERS','EDUCATION','PREVIOUS_EMPLOYMENT',"
    "'FAMILY','NOMINATIONS','BANK')"
)
TASK_STATUSES = (
    "('NOT_STARTED','IN_PROGRESS','SUBMITTED','REJECTED','COMPLETED','WAIVED','CANCELLED')"
)
OPEN_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "SUBMITTED", "REJECTED")
APPLIES = (
    ("branch", "branches"),
    ("department", "departments"),
    ("designation", "designations"),
    ("grade", "grades"),
    ("employment_type", "employment_types"),
    ("employee_category", "employee_categories"),
)
_SPECIFICITY = " + ".join(f"(applies_{name}_id IS NOT NULL)" for name, _ in APPLIES)


def _org_fk(table: str, column: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", column],
        [f"{table}.organization_id", f"{table}.id"],
        name=name,
        ondelete="RESTRICT",
    )


def _organization_column() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )


def _user(nullable: bool = True) -> Mapped[Any]:
    return mapped_column(Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=nullable)


class TaskDefinition:
    """The task definition columns shared by template tasks and instance tasks (4.3, 4.5)."""

    task_code: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    instructions: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    phase: Mapped[str] = mapped_column(String(12), nullable=False)
    task_type: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(18), nullable=False)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    blocks_activation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_waivable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reviewer_type: Mapped[str | None] = mapped_column(String(18), nullable=True)
    due_basis: Mapped[str] = mapped_column(String(16), nullable=False, default="JOINING_DATE")
    due_offset_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    requires_document_verification: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    min_documents: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    data_section_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    acknowledgement_kind: Mapped[str | None] = mapped_column(String(14), nullable=True)
    policy_version_label: Mapped[str | None] = mapped_column(String(20), nullable=True)
    privacy_notice_basis: Mapped[str | None] = mapped_column(String(12), nullable=True)
    consent_scope: Mapped[str | None] = mapped_column(String(200), nullable=True)
    acknowledgement_statement: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    requires_evidence: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    visible_to_employee: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


def _definition_checks(prefix: str) -> tuple[CheckConstraint, ...]:
    return (
        CheckConstraint(f"task_code REGEXP {CODE_PATTERN_SQL}", name=f"ck_{prefix}_code"),
        CheckConstraint(f"phase IN {PHASES}", name=f"ck_{prefix}_phase"),
        CheckConstraint(f"task_type IN {TASK_TYPES}", name=f"ck_{prefix}_type"),
        CheckConstraint(f"owner_type IN {OWNER_TYPES}", name=f"ck_{prefix}_owner"),
        CheckConstraint(
            "blocks_activation = 0 OR (is_mandatory = 1 AND phase <> 'POST_JOINING')",
            name=f"ck_{prefix}_blocking",
        ),
        CheckConstraint(
            "due_offset_days BETWEEN -365 AND 365 AND due_basis IN "
            "('JOINING_DATE','INITIATION_DATE')",
            name=f"ck_{prefix}_due",
        ),
        CheckConstraint(
            f"data_section_code IS NULL OR data_section_code IN {SECTIONS}",
            name=f"ck_{prefix}_section",
        ),
        CheckConstraint(
            "(task_type = 'DATA_SUBMISSION') = (data_section_code IS NOT NULL)",
            name=f"ck_{prefix}_section_type",
        ),
        CheckConstraint(
            "(task_type = 'ACKNOWLEDGEMENT') = (acknowledgement_kind IS NOT NULL)",
            name=f"ck_{prefix}_ack_type",
        ),
    )


class OnboardingTemplate(Base, StandardColumns):
    __tablename__ = "onboarding_templates"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_onboarding_templates_org_id"),
        UniqueConstraint(
            "organization_id", "template_code_normalized", name="uq_onboarding_templates_code"
        ),
        CheckConstraint(
            f"template_code REGEXP {CODE_PATTERN_SQL}", name="ck_onboarding_templates_code"
        ),
        CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="ck_onboarding_templates_status"),
        archive_reason_constraint("onboarding_templates"),
        tenant_index("onboarding_templates", "status", name="ix_onboarding_templates_status"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    template_code: Mapped[str] = mapped_column(String(30), nullable=False)
    template_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(template_code)", persisted=True), nullable=False
    )
    template_name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")


class OnboardingTemplateVersion(Base, StandardColumns):
    __tablename__ = "onboarding_template_versions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_onb_template_versions_org_id"),
        UniqueConstraint("template_id", "version_number", name="uq_onb_template_versions_number"),
        UniqueConstraint("template_id", "draft_key", name="uq_onb_template_versions_one_draft"),
        _org_fk("onboarding_templates", "template_id", "fk_onb_template_versions_template"),
        *(
            _org_fk(table, f"applies_{name}_id", f"fk_onb_template_versions_{name}")
            for name, table in APPLIES
        ),
        CheckConstraint(
            "status IN ('DRAFT','ACTIVE','ARCHIVED')", name="ck_onb_template_versions_status"
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_onb_template_versions_range",
        ),
        CheckConstraint(
            "status = 'DRAFT' OR effective_from IS NOT NULL",
            name="ck_onb_template_versions_published",
        ),
        CheckConstraint("priority BETWEEN 1 AND 999", name="ck_onb_template_versions_priority"),
        tenant_index(
            "onboarding_template_versions",
            "status",
            "effective_from",
            name="ix_onb_template_versions_selection",
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="DRAFT")
    draft_key: Mapped[int | None] = mapped_column(
        SmallInteger,
        Computed("CASE WHEN status = 'DRAFT' THEN 1 END", persisted=True),
        nullable=True,
    )
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=100)
    applies_branch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applies_department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applies_designation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applies_grade_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applies_employment_type_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applies_employee_category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applicability_specificity: Mapped[int] = mapped_column(
        SmallInteger, Computed(_SPECIFICITY, persisted=True), nullable=False
    )
    change_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    published_by: Mapped[uuid.UUID | None] = _user()
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class OnboardingTemplateTask(Base, StandardColumns, TaskDefinition):
    __tablename__ = "onboarding_template_tasks"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_onb_template_tasks_org_id"),
        UniqueConstraint("template_version_id", "task_code", name="uq_onb_template_tasks_code"),
        _org_fk(
            "onboarding_template_versions", "template_version_id", "fk_onb_template_tasks_version"
        ),
        _org_fk("departments", "owner_department_id", "fk_onb_template_tasks_owner_dept"),
        _org_fk("departments", "reviewer_department_id", "fk_onb_template_tasks_reviewer_dept"),
        _org_fk(
            "document_categories", "required_document_category_id", "fk_onb_template_tasks_category"
        ),
        *_definition_checks("onb_template_tasks"),
        Index("ix_onb_template_tasks_version", "template_version_id", "display_order"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    template_version_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = _user()
    owner_department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    reviewer_user_id: Mapped[uuid.UUID | None] = _user()
    reviewer_department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    required_document_category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    policy_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("organization_files.id", ondelete="RESTRICT"), nullable=True
    )


class OnboardingTemplateTaskDependency(Base, AppendOnlyColumns):
    __tablename__ = "onboarding_template_task_dependencies"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_onb_template_deps_org_id"),
        UniqueConstraint("task_id", "prerequisite_task_id", name="uq_onb_template_deps_pair"),
        _org_fk(
            "onboarding_template_versions", "template_version_id", "fk_onb_template_deps_version"
        ),
        _org_fk("onboarding_template_tasks", "task_id", "fk_onb_template_deps_task"),
        _org_fk("onboarding_template_tasks", "prerequisite_task_id", "fk_onb_template_deps_prereq"),
        CheckConstraint("task_id <> prerequisite_task_id", name="ck_onb_template_deps_distinct"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    template_version_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    prerequisite_task_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)


class OnboardingInstance(Base, StandardColumns):
    __tablename__ = "onboarding_instances"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_onboarding_instances_org_id"),
        # ONB-02: at most one non-cancelled instance per employee record.
        UniqueConstraint("employee_id", "open_key", name="uq_onboarding_instances_one_open"),
        _org_fk("employees", "employee_id", "fk_onboarding_instances_employee"),
        _org_fk("onboarding_templates", "template_id", "fk_onboarding_instances_template"),
        _org_fk(
            "onboarding_template_versions", "template_version_id", "fk_onboarding_instances_version"
        ),
        CheckConstraint(
            "status IN ('IN_PROGRESS','COMPLETED','CANCELLED')",
            name="ck_onboarding_instances_status",
        ),
        CheckConstraint(
            "selection_mode IN ('AUTOMATIC','EXPLICIT')", name="ck_onboarding_instances_mode"
        ),
        CheckConstraint(
            "completion_mode IS NULL OR completion_mode IN ('MANUAL','AUTOMATIC')",
            name="ck_onboarding_instances_completion",
        ),
        CheckConstraint(
            "cancellation_source IS NULL OR cancellation_source IN "
            "('USER','EMPLOYEE_ARCHIVED','EMPLOYEE_SEPARATED','TRANSFER')",
            name="ck_onboarding_instances_source",
        ),
        CheckConstraint(
            "status <> 'CANCELLED' OR (cancellation_reason IS NOT NULL AND cancelled_at IS NOT "
            "NULL)",
            name="ck_onboarding_instances_cancelled",
        ),
        tenant_index("onboarding_instances", "status", name="ix_onboarding_instances_status"),
        tenant_index(
            "onboarding_instances", "employee_id", name="ix_onboarding_instances_employee"
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    template_version_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    template_code_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    template_name_snapshot: Mapped[str] = mapped_column(String(150), nullable=False)
    template_version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    selection_mode: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="IN_PROGRESS")
    open_key: Mapped[int | None] = mapped_column(
        SmallInteger,
        Computed("CASE WHEN status <> 'CANCELLED' THEN 1 END", persisted=True),
        nullable=True,
    )
    initiated_on: Mapped[date] = mapped_column(Date, nullable=False)
    target_completion_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    hr_coordinator_user_id: Mapped[uuid.UUID] = _user(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    completed_by: Mapped[uuid.UUID | None] = _user()
    completion_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    cancelled_by: Mapped[uuid.UUID | None] = _user()
    cancellation_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    cancellation_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reopened_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    remarks: Mapped[str | None] = mapped_column(String(2000), nullable=True)


class OnboardingInstanceEvent(Base, AppendOnlyColumns):
    __tablename__ = "onboarding_instance_events"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_onb_instance_events_org_id"),
        _org_fk("employees", "employee_id", "fk_onb_instance_events_employee"),
        _org_fk("onboarding_instances", "instance_id", "fk_onb_instance_events_instance"),
        CheckConstraint(
            "event_type IN ('INITIATED','COMPLETED','AUTO_COMPLETED','CANCELLED','REOPENED',"
            "'COORDINATOR_CHANGED','TARGET_DATE_CHANGED','TASK_ADDED','DUE_DATES_RECALCULATED')",
            name="ck_onb_instance_events_type",
        ),
        Index("ix_onb_instance_events_instance", "instance_id", "occurred_at"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    details: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    actor_user_id: Mapped[uuid.UUID] = _user(nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class OnboardingTask(Base, StandardColumns, TaskDefinition):
    __tablename__ = "onboarding_tasks"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_onboarding_tasks_org_id"),
        UniqueConstraint("instance_id", "task_code", name="uq_onboarding_tasks_code"),
        _org_fk("employees", "employee_id", "fk_onboarding_tasks_employee"),
        _org_fk("onboarding_instances", "instance_id", "fk_onboarding_tasks_instance"),
        _org_fk("departments", "assignee_department_id", "fk_onboarding_tasks_assignee_dept"),
        _org_fk("departments", "reviewer_department_id", "fk_onboarding_tasks_reviewer_dept"),
        _org_fk(
            "document_categories", "required_document_category_id", "fk_onboarding_tasks_category"
        ),
        *_definition_checks("onboarding_tasks"),
        CheckConstraint(f"status IN {TASK_STATUSES}", name="ck_onboarding_tasks_status"),
        CheckConstraint(
            "assignee_resolution IN "
            "('RESOLVED','FALLBACK_TO_COORDINATOR','AWAITING_EMPLOYEE_ACCESS')",
            name="ck_onboarding_tasks_resolution",
        ),
        CheckConstraint(
            "due_date_source IN ('COMPUTED','MANUAL')", name="ck_onboarding_tasks_due_source"
        ),
        CheckConstraint(
            "status <> 'REJECTED' OR rejection_reason IS NOT NULL",
            name="ck_onboarding_tasks_rejected",
        ),
        CheckConstraint(
            "status <> 'WAIVED' OR (waiver_reason IS NOT NULL AND CHAR_LENGTH(waiver_reason) >= "
            "20)",
            name="ck_onboarding_tasks_waived",
        ),
        tenant_index(
            "onboarding_tasks", "instance_id", "status", name="ix_onboarding_tasks_instance"
        ),
        tenant_index(
            "onboarding_tasks", "assignee_user_id", "status", name="ix_onboarding_tasks_assignee"
        ),
        tenant_index("onboarding_tasks", "due_date", name="ix_onboarding_tasks_due"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    template_task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("onboarding_template_tasks.id", ondelete="RESTRICT"), nullable=True
    )
    policy_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("organization_files.id", ondelete="RESTRICT"), nullable=True
    )
    required_document_category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    required_document_category_code_snapshot: Mapped[str | None] = mapped_column(
        String(40), nullable=True
    )
    assignee_user_id: Mapped[uuid.UUID | None] = _user()
    assignee_department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    assignee_resolution: Mapped[str] = mapped_column(String(26), nullable=False, default="RESOLVED")
    reviewer_user_id: Mapped[uuid.UUID | None] = _user()
    reviewer_department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date_source: Mapped[str] = mapped_column(String(10), nullable=False, default="COMPUTED")
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="NOT_STARTED")
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    started_by: Mapped[uuid.UUID | None] = _user()
    submitted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    submitted_by: Mapped[uuid.UUID | None] = _user()
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    completed_by: Mapped[uuid.UUID | None] = _user()
    reviewed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = _user()
    rejection_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    waived_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    waived_by: Mapped[uuid.UUID | None] = _user()
    waiver_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    completion_remarks: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    acted_on_behalf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reopen_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


class OnboardingTaskDependency(Base, AppendOnlyColumns):
    __tablename__ = "onboarding_task_dependencies"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_onb_task_deps_org_id"),
        UniqueConstraint("task_id", "prerequisite_task_id", name="uq_onb_task_deps_pair"),
        _org_fk("onboarding_instances", "instance_id", "fk_onb_task_deps_instance"),
        _org_fk("onboarding_tasks", "task_id", "fk_onb_task_deps_task"),
        _org_fk("onboarding_tasks", "prerequisite_task_id", "fk_onb_task_deps_prereq"),
        CheckConstraint("task_id <> prerequisite_task_id", name="ck_onb_task_deps_distinct"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    prerequisite_task_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)


class OnboardingTaskEvent(Base, AppendOnlyColumns):
    __tablename__ = "onboarding_task_events"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_onb_task_events_org_id"),
        _org_fk("onboarding_instances", "instance_id", "fk_onb_task_events_instance"),
        _org_fk("onboarding_tasks", "task_id", "fk_onb_task_events_task"),
        CheckConstraint(
            "event_type IN "
            "('CREATED','STARTED','SUBMITTED','COMPLETED','REJECTED','REOPENED','WAIVED',"
            "'CANCELLED','REASSIGNED','DUE_DATE_CHANGED','DOCUMENT_LINKED','DOCUMENT_UNLINKED')",
            name="ck_onb_task_events_type",
        ),
        CheckConstraint(
            "source IN ('USER','DOCUMENT_VERIFICATION','DATA_SUBMISSION','ACKNOWLEDGEMENT',"
            "'MANAGER_CHANGE','JOINING_DATE_CHANGE','INSTANCE_TRANSITION','USER_ACCESS_LINKED')",
            name="ck_onb_task_events_source",
        ),
        Index("ix_onb_task_events_task", "task_id", "occurred_at"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    from_assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    to_assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    from_assignee_department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    to_assignee_department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    due_date_before: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date_after: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="USER")
    related_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    related_submission_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    acted_on_behalf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    actor_user_id: Mapped[uuid.UUID] = _user(nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class OnboardingTaskDocument(Base, StandardColumns):
    __tablename__ = "onboarding_task_documents"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_onb_task_documents_org_id"),
        UniqueConstraint("task_id", "document_id", name="uq_onb_task_documents_pair"),
        _org_fk("onboarding_instances", "instance_id", "fk_onb_task_documents_instance"),
        _org_fk("onboarding_tasks", "task_id", "fk_onb_task_documents_task"),
        _org_fk("employee_documents", "document_id", "fk_onb_task_documents_document"),
        CheckConstraint(
            "link_status IN ('ACTIVE','SUPERSEDED','UNLINKED')", name="ck_onb_task_documents_status"
        ),
        CheckConstraint(
            "link_status <> 'UNLINKED' OR (unlink_reason IS NOT NULL AND "
            "CHAR_LENGTH(unlink_reason) >= 10)",
            name="ck_onb_task_documents_unlink",
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    link_status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")
    linked_by: Mapped[uuid.UUID] = _user(nullable=False)
    linked_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    unlinked_by: Mapped[uuid.UUID | None] = _user()
    unlinked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    unlink_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class OnboardingAcknowledgement(Base, AppendOnlyColumns):
    __tablename__ = "onboarding_acknowledgements"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_onb_acknowledgements_org_id"),
        _org_fk("employees", "employee_id", "fk_onb_acknowledgements_employee"),
        _org_fk("onboarding_instances", "instance_id", "fk_onb_acknowledgements_instance"),
        _org_fk("onboarding_tasks", "task_id", "fk_onb_acknowledgements_task"),
        CheckConstraint(
            "acknowledgement_kind IN ('PRIVACY_NOTICE','POLICY')",
            name="ck_onb_acknowledgements_kind",
        ),
        CheckConstraint(
            "method IN ('ELECTRONIC','PAPER_UPLOADED')", name="ck_onb_acknowledgements_method"
        ),
        CheckConstraint(
            "method <> 'PAPER_UPLOADED' OR supporting_document_id IS NOT NULL",
            name="ck_onb_acknowledgements_paper",
        ),
        CheckConstraint(
            "acknowledged_on_behalf = 0 OR method = 'PAPER_UPLOADED'",
            name="ck_onb_acknowledgements_behalf",
        ),
        Index("ix_onb_acknowledgements_task", "task_id", "acknowledged_at"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    acknowledgement_kind: Mapped[str] = mapped_column(String(14), nullable=False)
    policy_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("organization_files.id", ondelete="RESTRICT"), nullable=True
    )
    policy_version_label: Mapped[str] = mapped_column(String(20), nullable=False)
    statement_snapshot: Mapped[str] = mapped_column(String(1000), nullable=False)
    method: Mapped[str] = mapped_column(String(14), nullable=False)
    supporting_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    delivered_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    acknowledged_by_user_id: Mapped[uuid.UUID] = _user(nullable=False)
    acknowledged_on_behalf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    on_behalf_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    privacy_notice_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    acknowledged_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class OnboardingDataSubmission(Base, StandardColumns):
    """An employee's proposed values for one EMS section. The payload is encrypted (ONB-11)."""

    __tablename__ = "onboarding_data_submissions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_onb_data_submissions_org_id"),
        UniqueConstraint("task_id", "submission_no", name="uq_onb_data_submissions_number"),
        # ONB-10: at most one SUBMITTED submission per task.
        UniqueConstraint("task_id", "pending_key", name="uq_onb_data_submissions_pending"),
        _org_fk("employees", "employee_id", "fk_onb_data_submissions_employee"),
        _org_fk("onboarding_instances", "instance_id", "fk_onb_data_submissions_instance"),
        _org_fk("onboarding_tasks", "task_id", "fk_onb_data_submissions_task"),
        CheckConstraint(f"section_code IN {SECTIONS}", name="ck_onb_data_submissions_section"),
        CheckConstraint(
            "status IN ('SUBMITTED','ACCEPTED','REJECTED','WITHDRAWN')",
            name="ck_onb_data_submissions_status",
        ),
        CheckConstraint(
            "status <> 'REJECTED' OR rejection_reason IS NOT NULL",
            name="ck_onb_data_submissions_rejected",
        ),
        tenant_index(
            "onboarding_data_submissions", "task_id", "status", name="ix_onb_data_submissions_task"
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    section_code: Mapped[str] = mapped_column(String(20), nullable=False)
    submission_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="SUBMITTED")
    pending_key: Mapped[int | None] = mapped_column(
        SmallInteger,
        Computed("CASE WHEN status = 'SUBMITTED' THEN 1 END", persisted=True),
        nullable=True,
    )
    supersedes_submission_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    payload_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary(65535), nullable=True)
    payload_schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    submitted_field_names: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    submitted_by: Mapped[uuid.UUID] = _user(nullable=False)
    submitted_on_behalf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    on_behalf_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    decided_by: Mapped[uuid.UUID | None] = _user()
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    field_comments: Mapped[list[dict[str, str]] | None] = mapped_column(JSON, nullable=True)
    acknowledged_warnings: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    applied_entity_refs: Mapped[list[dict[str, str]] | None] = mapped_column(JSON, nullable=True)
    payload_purged_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


__all__ = [
    "OnboardingAcknowledgement",
    "OnboardingDataSubmission",
    "OnboardingInstance",
    "OnboardingInstanceEvent",
    "OnboardingTask",
    "OnboardingTaskDependency",
    "OnboardingTaskDocument",
    "OnboardingTaskEvent",
    "OnboardingTemplate",
    "OnboardingTemplateTask",
    "OnboardingTemplateTaskDependency",
    "OnboardingTemplateVersion",
]
