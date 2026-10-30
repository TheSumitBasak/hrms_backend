"""Scheduled-job behaviours (§24.7)."""

from __future__ import annotations

import uuid
from contextlib import suppress
from datetime import timedelta
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import today_in, utc_now
from app.core.errors import JobAlreadyRunning
from app.models.audit import IntegrityFinding
from app.models.document import EmployeeDocument, UploadStatus
from app.models.employee_code import EmployeeIdGenerationRule, IdRuleStatus
from app.models.organization import Organization, OrganizationSettings
from app.models.salary import (
    CONFIRMED_STATUSES,
    AssignmentStatus,
    EmployeeSalaryAssignment,
    SalaryTemplateVersion,
    TemplateVersionStatus,
)
from app.services.attendance_jobs import AttendanceJobs
from app.services.job_runs import system_user_id
from app.services.leave_jobs import LeaveJobs
from app.storage.local import LocalStorage

CONFIRMED = {status.value for status in CONFIRMED_STATUSES}


def refresh_salary_statuses(session: Session, organization_id: uuid.UUID) -> int:
    """Re-derive confirmed assignment statuses from dates (§18.2)."""
    organization = session.get(Organization, organization_id)
    today = today_in(organization.timezone if organization else None)
    changed = 0
    rows = list(
        session.execute(
            select(EmployeeSalaryAssignment).where(
                EmployeeSalaryAssignment.organization_id == organization_id,
                EmployeeSalaryAssignment.assignment_status.in_(list(CONFIRMED)),
            )
        ).scalars()
    )
    for row in rows:
        if row.effective_from > today:
            target = AssignmentStatus.SCHEDULED.value
        elif row.effective_to is not None and row.effective_to <= today:
            target = AssignmentStatus.EXPIRED.value
        else:
            target = AssignmentStatus.ACTIVE.value
        if row.assignment_status != target:
            row.assignment_status = target
            changed += 1
    return changed


def refresh_id_rule_statuses(session: Session, organization_id: uuid.UUID) -> int:
    organization = session.get(Organization, organization_id)
    today = today_in(organization.timezone if organization else None)
    changed = 0
    rows = list(
        session.execute(
            select(EmployeeIdGenerationRule).where(
                EmployeeIdGenerationRule.organization_id == organization_id,
                EmployeeIdGenerationRule.status.in_(
                    [
                        IdRuleStatus.SCHEDULED.value,
                        IdRuleStatus.ACTIVE.value,
                        IdRuleStatus.EXPIRED.value,
                    ]
                ),
            )
        ).scalars()
    )
    for row in rows:
        if row.effective_from > today:
            target = IdRuleStatus.SCHEDULED.value
        elif row.effective_to is not None and row.effective_to <= today:
            target = IdRuleStatus.EXPIRED.value
        else:
            target = IdRuleStatus.ACTIVE.value
        if row.status != target:
            row.status = target
            changed += 1
    return changed


def cleanup_pending_uploads(
    session: Session, storage: LocalStorage, *, organization_id: uuid.UUID | None = None
) -> int:
    """Mark uploads older than 24 hours FAILED and remove their objects."""
    cutoff = utc_now() - timedelta(hours=24)
    statement = select(EmployeeDocument).where(
        EmployeeDocument.upload_status == UploadStatus.PENDING_UPLOAD.value,
        EmployeeDocument.created_at < cutoff,
    )
    if organization_id is not None:
        statement = statement.where(EmployeeDocument.organization_id == organization_id)
    rows = list(session.execute(statement).scalars())
    for row in rows:
        row.upload_status = UploadStatus.FAILED.value
        row.scan_result = "upload never completed"
        with suppress(OSError):
            storage.delete(row.storage_key)
    return len(rows)


def purge_quarantined(
    session: Session, storage: LocalStorage, *, older_than_hours: int = 24
) -> int:
    cutoff = utc_now() - timedelta(hours=older_than_hours)
    rows = list(
        session.execute(
            select(EmployeeDocument).where(
                EmployeeDocument.upload_status == UploadStatus.QUARANTINED.value,
                EmployeeDocument.created_at < cutoff,
            )
        ).scalars()
    )
    for row in rows:
        storage.delete(row.storage_key)
        row.archived_at = utc_now()
        row.archive_reason = "Quarantined object purged by scheduled job"
    return len(rows)


def check_integrity(session: Session, organization_id: uuid.UUID | None = None) -> int:
    """Detect overlapping confirmed salary intervals and open a P1 finding for each."""
    statement = select(EmployeeSalaryAssignment).where(
        EmployeeSalaryAssignment.assignment_status.in_(list(CONFIRMED))
    )
    if organization_id is not None:
        statement = statement.where(EmployeeSalaryAssignment.organization_id == organization_id)
    rows = list(session.execute(statement).scalars())
    by_employee: dict[uuid.UUID, list[EmployeeSalaryAssignment]] = {}
    for row in rows:
        by_employee.setdefault(row.employee_id, []).append(row)
    findings = 0
    for _employee_id, assignments in by_employee.items():
        assignments.sort(key=lambda row: row.effective_from)
        for earlier, later in pairwise(assignments):
            if earlier.effective_to is None or earlier.effective_to > later.effective_from:
                existing = session.execute(
                    select(IntegrityFinding.id).where(
                        IntegrityFinding.check_name == "salary_assignment_overlap",
                        IntegrityFinding.entity_type == "employee_salary_assignment",
                        IntegrityFinding.resolved_at.is_(None),
                        IntegrityFinding.detail.contains(earlier.employee_id.hex),
                    )
                ).first()
                if existing is not None:
                    continue
                session.add(
                    IntegrityFinding(
                        id=uuid.uuid4(),
                        check_name="salary_assignment_overlap",
                        severity="P1",
                        organization_id=earlier.organization_id,
                        entity_type="employee_salary_assignment",
                        entity_ids=[earlier.id.hex, later.id.hex],
                        detail=(
                            "Confirmed salary intervals overlap for one employee "
                            f"{earlier.employee_id.hex}."
                        ),
                    )
                )
                findings += 1
    return findings


def refresh_template_version_statuses(session: Session, organization_id: uuid.UUID) -> int:
    """Mark ACTIVE versions EXPIRED once their effective_to has passed."""
    organization = session.get(Organization, organization_id)
    today = today_in(organization.timezone if organization else None)
    rows = list(
        session.execute(
            select(SalaryTemplateVersion).where(
                SalaryTemplateVersion.organization_id == organization_id,
                SalaryTemplateVersion.status == TemplateVersionStatus.ACTIVE.value,
                SalaryTemplateVersion.effective_to.is_not(None),
                SalaryTemplateVersion.effective_to <= today,
            )
        ).scalars()
    )
    for row in rows:
        row.status = TemplateVersionStatus.ARCHIVED.value
    return len(rows)


def run_for_organization(
    session: Session, storage: LocalStorage, organization_id: uuid.UUID
) -> dict[str, int]:
    return {
        "salary_statuses": refresh_salary_statuses(session, organization_id),
        "id_rule_statuses": refresh_id_rule_statuses(session, organization_id),
        "template_versions": refresh_template_version_statuses(session, organization_id),
        "pending_uploads": cleanup_pending_uploads(
            session, storage, organization_id=organization_id
        ),
        "quarantined": purge_quarantined(session, storage),
        "integrity_findings": check_integrity(session, organization_id),
    }


def run_leave_jobs(session: Session, organization_id: uuid.UUID) -> dict[str, int]:
    """LEAVE_YEAR_END then LEAVE_ACCRUAL for one organization (§14.1 order). Commits per item."""
    settings = session.get(OrganizationSettings, organization_id)
    if settings is None or not settings.enable_leave_module:
        return {}
    jobs = LeaveJobs(session, organization_id)
    today = jobs.today()
    actor = system_user_id(session)
    session.commit()
    ended = jobs.run_year_end(today, run_key=str(today), trigger="SCHEDULED", requested_by=actor)
    accrued = jobs.run_accrual(today, run_key=str(today), trigger="SCHEDULED", requested_by=actor)
    return {
        "leave_year_end_failed": ended.failed_count,
        "leave_accrual_failed": accrued.failed_count,
        "leave_accrual_posted": accrued.succeeded_count,
    }


def run_attendance_jobs(session: Session, organization_id: uuid.UUID) -> dict[str, int]:
    """ATTENDANCE_DAY_CLOSE then ATTENDANCE_AUTO_FINALIZE (14.1). A concurrent runner is skipped."""
    settings = session.get(OrganizationSettings, organization_id)
    if settings is None or not settings.enable_attendance_module:
        return {}
    jobs = AttendanceJobs(session, organization_id)
    today = jobs.today()
    actor = system_user_id(session)
    session.commit()
    try:
        closed = jobs.run_day_close(
            jobs.window(today), run_key=str(today), trigger="SCHEDULED", requested_by=actor
        )
        finalized = jobs.run_auto_finalize(
            run_key=str(today), trigger="SCHEDULED", requested_by=actor
        )
    except JobAlreadyRunning:
        return {}
    return {
        "attendance_day_close_failed": closed.failed_count,
        "attendance_auto_finalized": finalized.succeeded_count,
    }


__all__ = [
    "check_integrity",
    "cleanup_pending_uploads",
    "purge_quarantined",
    "refresh_id_rule_statuses",
    "refresh_salary_statuses",
    "refresh_template_version_statuses",
    "run_attendance_jobs",
    "run_for_organization",
    "run_leave_jobs",
]
