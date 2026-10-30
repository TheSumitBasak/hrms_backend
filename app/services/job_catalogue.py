"""Job families and the job catalogue (JA §4, §5, §6.2).

The two aggregates live in one service because they are one editorial workflow — a job always
belongs to a family, and the family hierarchy only exists to classify jobs.

Three rules in here are worth knowing before changing anything:

**The family hierarchy is exactly two levels.** JA §5.1 makes ``FAMILY -> SUBFAMILY`` the
whole model. The database contributes the self-parent CHECK and the level/parent consistency
CHECK; this service adds the "parent must be a FAMILY", "a parent with children cannot become
a child" and depth checks, and it takes the affected rows under a lock in ascending id order
so two concurrent reparents cannot interleave into a cycle (JA §5.3).

**Grade ranges are validated by ``rank``, not by id.** A rank comparison spans rows, so no
CHECK can express it (JA §6.2). :func:`validate_grade_range` is the single implementation and
:class:`app.services.positions.PositionService` calls the same function, so a job range and a
position grade can never be judged by two different rules.

**A code is never reused.** Uniqueness is case-insensitive and includes inactive *and*
archived rows (JA §4.5), which is why every duplicate check goes through a repository lookup
with ``include_archived=True``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any, cast

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in, utc_now
from app.core.context import OrgContext
from app.core.errors import (
    DuplicateResource,
    ErrorDetail,
    FieldReadOnly,
    InvalidGradeRange,
    InvalidOrganizationReference,
    JobFamilyCycle,
    JobFamilyInUse,
    JobInUse,
    MasterInactive,
    MasterInUseByJobArchitecture,
    ResourceNotFound,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.core.warnings import WarningCode, WarningCollector
from app.db.types import uuid7
from app.models.master import Grade
from app.models.position import (
    ESTABLISHMENT_POSITION_STATUSES,
    JOB_FAMILY_MAX_DEPTH,
    CatalogueStatus,
    Job,
    JobFamily,
    JobNodeLevel,
)
from app.repositories.master import GradeRepository, MasterReferenceChecker
from app.repositories.organization import OrganizationRepository
from app.repositories.position import (
    JobCompetencyRepository,
    JobFamilyRepository,
    JobRepository,
    PositionAssignmentRepository,
    PositionRepository,
)
from app.schemas.position import JobSummary
from app.services.organizations import assert_etag

READ_PERMISSIONS = (Permission.EMPLOYEE_LIST, Permission.POSITION_VIEW)
"""JA §12.3: catalogue reads need either. They expose no headcount and no incumbent."""

JOB_SORT_FIELDS = frozenset({"job_code", "title", "created_at", "effective_from"})
FAMILY_SORT_FIELDS = frozenset({"family_code", "name", "display_order", "created_at"})

JOB_EDITABLE_FIELDS = frozenset(
    {
        "title",
        "description",
        "responsibilities",
        "job_family_id",
        "default_grade_id",
        "min_grade_id",
        "max_grade_id",
        "default_designation_id",
        "default_employment_type_id",
        "min_experience_years",
        "required_qualifications",
        "effective_from",
        "effective_to",
    }
)

FAMILY_EDITABLE_FIELDS = frozenset({"name", "description", "parent_job_family_id", "display_order"})


def validate_grade_range(
    *,
    default_grade: Grade | None,
    min_grade: Grade | None,
    max_grade: Grade | None,
) -> None:
    """JA §6.2: the range is inclusive by ``rank`` and must contain the default grade.

    Lower rank is more junior (base A-10), so ``min.rank <= max.rank``. Raising rather than
    returning a boolean keeps every call site from having to remember to check.
    """
    if min_grade is not None and max_grade is not None and min_grade.rank > max_grade.rank:
        raise InvalidGradeRange("The minimum grade outranks the maximum grade.").with_detail(
            field="min_grade_id",
            code="INVALID_GRADE_RANGE",
            message=(f"min rank {min_grade.rank} must not exceed max rank {max_grade.rank}."),
        )
    if default_grade is None:
        return
    if min_grade is not None and default_grade.rank < min_grade.rank:
        raise InvalidGradeRange("The default grade is below the minimum of the range.").with_detail(
            field="default_grade_id",
            code="INVALID_GRADE_RANGE",
            message=f"rank {default_grade.rank} is below min rank {min_grade.rank}.",
        )
    if max_grade is not None and default_grade.rank > max_grade.rank:
        raise InvalidGradeRange("The default grade is above the maximum of the range.").with_detail(
            field="default_grade_id",
            code="INVALID_GRADE_RANGE",
            message=f"rank {default_grade.rank} is above max rank {max_grade.rank}.",
        )


class JobCatalogueService:
    """Job families and jobs (JA §4, §5)."""

    def __init__(
        self,
        *,
        families: JobFamilyRepository,
        jobs: JobRepository,
        job_competencies: JobCompetencyRepository,
        positions: PositionRepository,
        assignments: PositionAssignmentRepository,
        grades: GradeRepository,
        masters: MasterReferenceChecker,
        organizations: OrganizationRepository,
        audit: AuditWriter,
    ) -> None:
        self._families = families
        self._jobs = jobs
        self._job_competencies = job_competencies
        self._positions = positions
        self._assignments = assignments
        self._grades = grades
        self._masters = masters
        self._organizations = organizations
        self._audit = audit

    # ================================================================= families

    def list_families(
        self,
        context: OrgContext,
        *,
        query: str | None = None,
        node_level: str | None = None,
        parent_id: uuid.UUID | None = None,
        status: Sequence[str] | None = None,
        include_archived: bool = False,
        sort_field: str = "display_order",
        direction: str = "asc",
        after_value: Any | None = None,
        after_id: uuid.UUID | None = None,
        limit: int = 25,
        with_total: bool = False,
    ) -> tuple[list[JobFamily], bool, int | None]:
        context.require_any(*READ_PERMISSIONS)
        if include_archived:
            context.require(Permission.MASTER_MANAGE)
        return self._families.search_page(
            query=query,
            node_level=node_level,
            parent_id=parent_id,
            status=status,
            include_archived=include_archived,
            sort_field=sort_field,
            direction=direction,
            after_value=after_value,
            after_id=after_id,
            limit=limit,
            with_total=with_total,
        )

    def get_family(self, context: OrgContext, family_id: uuid.UUID) -> JobFamily:
        context.require_any(*READ_PERMISSIONS)
        return self._families.require(family_id, include_archived=True, resource="job family")

    def create_family(
        self,
        context: OrgContext,
        *,
        values: dict[str, Any],
        acknowledged_warnings: list[str],
    ) -> tuple[JobFamily, list[str]]:
        context.require(Permission.MASTER_MANAGE)
        code = str(values["family_code"])
        self._assert_family_code_free(code)

        node_level = str(values["node_level"])
        parent_id = values.get("parent_job_family_id")
        parent = self._resolve_parent(node_level, parent_id)

        acknowledged = WarningCollector(acknowledged_warnings).enforce()
        family = JobFamily(
            id=uuid7(),
            organization_id=context.organization_id,
            family_code=code,
            name=str(values["name"]),
            description=_text_or_none(values.get("description")),
            node_level=node_level,
            parent_job_family_id=parent.id if parent is not None else None,
            display_order=values.get("display_order"),
            status=CatalogueStatus.ACTIVE.value,
            created_by=context.user_id,
        )
        self._families.add(family)
        self._families.flush()
        self._audit.record_change(
            context,
            action=AuditAction.JOB_FAMILY_CREATED,
            entity_type="job_family",
            entity_id=family.id,
            after={
                "family_code": family.family_code,
                "name": family.name,
                "node_level": family.node_level,
                "parent_job_family_id": family.parent_job_family_id,
                "status": family.status,
            },
            fields=["family_code", "name", "node_level", "parent_job_family_id", "status"],
            acknowledged_warnings=acknowledged,
        )
        return family, acknowledged

    def update_family(
        self,
        context: OrgContext,
        *,
        family_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
        acknowledged_warnings: list[str],
    ) -> tuple[JobFamily, list[str]]:
        context.require(Permission.MASTER_MANAGE)
        family = self._families.require(family_id, resource="job family")
        assert_etag(family.row_version, if_match)

        applied = {key: value for key, value in changes.items() if key in FAMILY_EDITABLE_FIELDS}
        if "parent_job_family_id" in applied:
            self._validate_reparent(family, applied["parent_job_family_id"])

        acknowledged = WarningCollector(acknowledged_warnings).enforce()
        before = {field: getattr(family, field) for field in applied}
        for field, value in applied.items():
            setattr(family, field, value)
        family.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.JOB_FAMILY_UPDATED,
            entity_type="job_family",
            entity_id=family.id,
            before=before,
            after={field: getattr(family, field) for field in applied},
            fields=list(applied),
            acknowledged_warnings=acknowledged,
        )
        return family, acknowledged

    def set_family_status(
        self,
        context: OrgContext,
        *,
        family_id: uuid.UUID,
        active: bool,
        reason: str,
        if_match: int | None,
    ) -> JobFamily:
        context.require(Permission.MASTER_MANAGE)
        family = self._families.require(family_id, resource="job family")
        assert_etag(family.row_version, if_match)
        target = CatalogueStatus.ACTIVE.value if active else CatalogueStatus.INACTIVE.value
        if family.status == target:
            raise ValidationFailed(f"The job family is already {target.lower()}.")
        if active and family.parent_job_family_id is not None:
            parent = self._families.get(family.parent_job_family_id, include_archived=True)
            if parent is None or parent.status != CatalogueStatus.ACTIVE.value:
                raise MasterInactive("The parent family is inactive; reactivate it first.")

        before = {"status": family.status}
        family.status = target
        family.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.JOB_FAMILY_STATUS_CHANGED,
            entity_type="job_family",
            entity_id=family.id,
            before=before,
            after={"status": target},
            fields=["status"],
            reason=reason,
        )
        return family

    def archive_family(
        self, context: OrgContext, *, family_id: uuid.UUID, reason: str, if_match: int | None
    ) -> JobFamily:
        """JA §5.3: blocked while an active child or an active job references the node."""
        context.require(Permission.MASTER_MANAGE)
        family = self._families.require(family_id, resource="job family")
        assert_etag(family.row_version, if_match)
        if self._families.has_active_children(family.id):
            raise JobFamilyInUse(
                "This family still has an active subfamily. Deactivate or archive it first."
            )
        if self._jobs.active_jobs_in_families([family.id]) > 0:
            raise JobFamilyInUse(
                "Active jobs are classified under this node. Reclassify or archive them first."
            )

        family.archived_at = utc_now()
        family.archived_by = context.user_id
        family.archive_reason = reason
        family.status = CatalogueStatus.INACTIVE.value
        family.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.JOB_FAMILY_ARCHIVED,
            entity_type="job_family",
            entity_id=family.id,
            after={"archived_at": family.archived_at, "status": family.status},
            fields=["archived_at", "status"],
            reason=reason,
        )
        return family

    def restore_family(
        self, context: OrgContext, *, family_id: uuid.UUID, reason: str, if_match: int | None
    ) -> JobFamily:
        """JA §15.1: an archived catalogue row is restored to ``INACTIVE``, never to active."""
        context.require(Permission.MASTER_MANAGE)
        family = self._families.require(family_id, include_archived=True, resource="job family")
        assert_etag(family.row_version, if_match)
        if family.archived_at is None:
            raise ValidationFailed("The job family is not archived.")
        family.archived_at = None
        family.archived_by = None
        family.archive_reason = None
        family.status = CatalogueStatus.INACTIVE.value
        family.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.JOB_FAMILY_RESTORED,
            entity_type="job_family",
            entity_id=family.id,
            after={"archived_at": None, "status": family.status},
            fields=["archived_at", "status"],
            reason=reason,
        )
        return family

    def family_summaries(
        self, family_ids: Sequence[uuid.UUID | None]
    ) -> dict[uuid.UUID, JobFamily]:
        """Batched family lookup so a job list does not query per row.

        Accepts ``None`` entries and drops them, so callers can pass a nullable parent id
        straight through without a branch at every call site.
        """
        ids = {family_id for family_id in family_ids if family_id is not None}
        if not ids:
            return {}
        rows = self._families.session.execute(
            self._families.select(include_archived=True).where(JobFamily.id.in_(ids))
        ).scalars()
        return {row.id: row for row in rows}

    # ===================================================================== jobs

    def list_jobs(
        self,
        context: OrgContext,
        *,
        query: str | None = None,
        job_family_id: uuid.UUID | None = None,
        include_subfamilies: bool = True,
        grade_id: uuid.UUID | None = None,
        employment_type_id: uuid.UUID | None = None,
        status: Sequence[str] | None = None,
        effective_on: date | None = None,
        include_archived: bool = False,
        sort_field: str = "title",
        direction: str = "asc",
        after_value: Any | None = None,
        after_id: uuid.UUID | None = None,
        limit: int = 25,
        with_total: bool = False,
    ) -> tuple[list[Job], bool, int | None]:
        context.require_any(*READ_PERMISSIONS)
        if include_archived:
            context.require(Permission.MASTER_MANAGE)

        family_ids: list[uuid.UUID] | None = None
        if job_family_id is not None:
            node = self._families.require(
                job_family_id, include_archived=True, resource="job family"
            )
            # JA §5.3: filtering by a family includes its subfamilies; filtering by a
            # subfamily cannot, because it has no children.
            if include_subfamilies and node.node_level == JobNodeLevel.FAMILY.value:
                family_ids = self._families.family_and_descendant_ids(node.id)
            else:
                family_ids = [node.id]

        return self._jobs.search_page(
            query=query,
            job_family_ids=family_ids,
            grade_id=grade_id,
            employment_type_id=employment_type_id,
            status=status,
            effective_on=effective_on,
            include_archived=include_archived,
            sort_field=sort_field,
            direction=direction,
            after_value=after_value,
            after_id=after_id,
            limit=limit,
            with_total=with_total,
        )

    def get_job(self, context: OrgContext, job_id: uuid.UUID) -> Job:
        context.require_any(*READ_PERMISSIONS)
        return self._jobs.require(job_id, include_archived=True, resource="job")

    def create_job(
        self,
        context: OrgContext,
        *,
        values: dict[str, Any],
        acknowledged_warnings: list[str],
    ) -> tuple[Job, list[str]]:
        context.require(Permission.MASTER_MANAGE)
        organization = self._organizations.get(context.organization_id)
        if organization is None or organization.archived_at is not None:
            raise ResourceNotFound("organization")
        if organization.status != "ACTIVE":
            raise ValidationFailed("The organization is inactive; jobs cannot be created.")

        code = str(values["job_code"])
        if self._jobs.get_by_code(code) is not None:
            raise DuplicateResource("A job with that code already exists.").with_detail(
                field="job_code",
                code="DUPLICATE",
                message="Job codes are unique per organization, including archived rows.",
            )

        family = self._require_active_family(values["job_family_id"])
        grades = self._resolve_grades(values)
        validate_grade_range(
            default_grade=grades.get("default_grade_id"),
            min_grade=grades.get("min_grade_id"),
            max_grade=grades.get("max_grade_id"),
        )
        self._masters.require_active("designations", values.get("default_designation_id"))
        self._masters.require_active("employment-types", values.get("default_employment_type_id"))

        effective_from = values.get("effective_from") or today_in(organization.timezone)
        effective_to = values.get("effective_to")
        _assert_interval("effective_from", effective_from, effective_to)

        warnings = WarningCollector(acknowledged_warnings)
        warnings.add_if(
            self._jobs.title_exists_in_family(title=str(values["title"]), job_family_id=family.id),
            WarningCode.JOB_TITLE_DUPLICATE,
        )
        acknowledged = warnings.enforce()

        job = Job(
            id=uuid7(),
            organization_id=context.organization_id,
            job_code=code,
            title=str(values["title"]),
            description=_text_or_none(values.get("description")),
            responsibilities=_text_or_none(values.get("responsibilities")),
            job_family_id=family.id,
            default_grade_id=values.get("default_grade_id"),
            min_grade_id=values.get("min_grade_id"),
            max_grade_id=values.get("max_grade_id"),
            default_designation_id=values.get("default_designation_id"),
            default_employment_type_id=values.get("default_employment_type_id"),
            min_experience_years=values.get("min_experience_years"),
            required_qualifications=_text_or_none(values.get("required_qualifications")),
            status=str(values.get("status") or CatalogueStatus.ACTIVE.value),
            effective_from=effective_from,
            effective_to=effective_to,
            created_by=context.user_id,
        )
        self._jobs.add(job)
        self._jobs.flush()
        self._audit.record_change(
            context,
            action=AuditAction.JOB_CREATED,
            entity_type="job",
            entity_id=job.id,
            after={
                "job_code": job.job_code,
                "title": job.title,
                "job_family_id": job.job_family_id,
                "status": job.status,
                "effective_from": job.effective_from,
            },
            fields=["job_code", "title", "job_family_id", "status", "effective_from"],
            acknowledged_warnings=acknowledged,
        )
        return job, acknowledged

    def update_job(
        self,
        context: OrgContext,
        *,
        job_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
        acknowledged_warnings: list[str],
    ) -> tuple[Job, list[str]]:
        context.require(Permission.MASTER_MANAGE)
        job = self._jobs.require(job_id, resource="job")
        assert_etag(job.row_version, if_match)

        if "job_code" in changes and str(changes["job_code"]) != job.job_code:
            self._assert_code_mutable(job)
            if self._jobs.get_by_code(str(changes["job_code"])) is not None:
                raise DuplicateResource("A job with that code already exists.")

        applied: dict[str, Any] = {
            key: value for key, value in changes.items() if key in JOB_EDITABLE_FIELDS
        }
        if "job_code" in changes:
            applied["job_code"] = str(changes["job_code"])

        if "job_family_id" in applied:
            applied["job_family_id"] = self._require_active_family(applied["job_family_id"]).id
        if "default_designation_id" in applied:
            self._masters.require_active("designations", applied["default_designation_id"])
        if "default_employment_type_id" in applied:
            self._masters.require_active("employment-types", applied["default_employment_type_id"])

        merged = {
            "default_grade_id": applied.get("default_grade_id", job.default_grade_id),
            "min_grade_id": applied.get("min_grade_id", job.min_grade_id),
            "max_grade_id": applied.get("max_grade_id", job.max_grade_id),
        }
        grades = self._resolve_grades(merged, require_active=_touches_grades(applied))
        validate_grade_range(
            default_grade=grades.get("default_grade_id"),
            min_grade=grades.get("min_grade_id"),
            max_grade=grades.get("max_grade_id"),
        )
        _assert_interval(
            "effective_from",
            applied.get("effective_from", job.effective_from),
            applied.get("effective_to", job.effective_to),
        )

        warnings = WarningCollector(acknowledged_warnings)
        if "title" in applied:
            warnings.add_if(
                self._jobs.title_exists_in_family(
                    title=str(applied["title"]),
                    job_family_id=applied.get("job_family_id", job.job_family_id),
                    exclude_id=job.id,
                ),
                WarningCode.JOB_TITLE_DUPLICATE,
            )
        acknowledged = warnings.enforce()

        before = {field: getattr(job, field) for field in applied}
        for field, value in applied.items():
            setattr(job, field, value)
        job.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.JOB_UPDATED,
            entity_type="job",
            entity_id=job.id,
            before=before,
            after={field: getattr(job, field) for field in applied},
            fields=list(applied),
            acknowledged_warnings=acknowledged,
        )
        return job, acknowledged

    def set_job_status(
        self,
        context: OrgContext,
        *,
        job_id: uuid.UUID,
        active: bool,
        reason: str,
        if_match: int | None,
    ) -> Job:
        context.require(Permission.MASTER_MANAGE)
        job = self._jobs.require(job_id, resource="job")
        assert_etag(job.row_version, if_match)
        target = CatalogueStatus.ACTIVE.value if active else CatalogueStatus.INACTIVE.value
        if job.status == target:
            raise ValidationFailed(f"The job is already {target.lower()}.")
        if active:
            # JA §15.1: reactivating is a new use of the family and grades it references.
            self._require_active_family(job.job_family_id)
            self._resolve_grades(
                {
                    "default_grade_id": job.default_grade_id,
                    "min_grade_id": job.min_grade_id,
                    "max_grade_id": job.max_grade_id,
                },
                require_active=True,
            )

        before = {"status": job.status}
        job.status = target
        job.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.JOB_STATUS_CHANGED,
            entity_type="job",
            entity_id=job.id,
            before=before,
            after={"status": target},
            fields=["status"],
            reason=reason,
        )
        return job

    def archive_job(
        self, context: OrgContext, *, job_id: uuid.UUID, reason: str, if_match: int | None
    ) -> Job:
        """JA §4.5: blocked by an ``OPEN``/``FROZEN`` position, not by a closed one."""
        context.require(Permission.MASTER_MANAGE)
        job = self._jobs.require(job_id, resource="job")
        assert_etag(job.row_version, if_match)
        open_positions = self._positions.count_for_job(
            job.id, statuses=sorted(ESTABLISHMENT_POSITION_STATUSES)
        )
        if open_positions:
            raise JobInUse(
                f"{open_positions} open or frozen position(s) reference this job."
            ).with_detail(
                field="job_id",
                code="JOB_IN_USE",
                message="Close those positions before archiving the job.",
            )

        job.archived_at = utc_now()
        job.archived_by = context.user_id
        job.archive_reason = reason
        job.status = CatalogueStatus.INACTIVE.value
        job.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.JOB_ARCHIVED,
            entity_type="job",
            entity_id=job.id,
            after={"archived_at": job.archived_at, "status": job.status},
            fields=["archived_at", "status"],
            reason=reason,
        )
        return job

    def restore_job(
        self, context: OrgContext, *, job_id: uuid.UUID, reason: str, if_match: int | None
    ) -> Job:
        context.require(Permission.MASTER_MANAGE)
        job = self._jobs.require(job_id, include_archived=True, resource="job")
        assert_etag(job.row_version, if_match)
        if job.archived_at is None:
            raise ValidationFailed("The job is not archived.")
        job.archived_at = None
        job.archived_by = None
        job.archive_reason = None
        job.status = CatalogueStatus.INACTIVE.value
        job.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.JOB_RESTORED,
            entity_type="job",
            entity_id=job.id,
            after={"archived_at": None, "status": job.status},
            fields=["archived_at", "status"],
            reason=reason,
        )
        return job

    def jobs_for(self, job_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Job]:
        """Batched job lookup, so a position list resolves its job titles in one query."""
        return self._jobs.load_many(list(job_ids))

    def job_summary(self, job: Job) -> JobSummary:
        """The compact job reference a position response embeds."""
        return JobSummary(
            id=job.id,
            job_code=job.job_code,
            title=job.title,
            job_family_id=job.job_family_id,
            default_grade_id=job.default_grade_id,
            status=job.status,
            effective_from=job.effective_from,
            effective_to=job.effective_to,
            row_version=job.row_version,
        )

    # ------------------------------------------------------------- grade reuse

    def grades_for(self, grade_ids: Sequence[uuid.UUID | None]) -> dict[uuid.UUID, Grade]:
        ids = {grade_id for grade_id in grade_ids if grade_id is not None}
        if not ids:
            return {}
        rows = [
            cast(Grade, row)
            for row in self._grades.session.execute(
                self._grades.select(include_archived=True).where(Grade.id.in_(ids))
            ).scalars()
        ]
        return {row.id: row for row in rows}

    def assert_grade_not_in_use(self, context: OrgContext, grade_id: uuid.UUID) -> None:
        """JA §3.3 JA-GRD-005: a grade referenced by a job or position stays usable.

        Exposed for a future ``/masters/grades/{id}/archive`` route, which the specification
        says must return ``MASTER_IN_USE_BY_JOB_ARCHITECTURE`` while any reference exists.
        This module adds no such route.
        """
        context.require(Permission.MASTER_MANAGE)
        references = self._jobs.grades_in_use(grade_id) + self._positions.count_for_grade(grade_id)
        if references:
            raise MasterInUseByJobArchitecture(
                f"{references} job(s) or position(s) reference this grade."
            )

    # ------------------------------------------------------------------ helpers

    def _assert_family_code_free(self, code: str) -> None:
        if self._families.get_by_code(code) is not None:
            raise DuplicateResource("A job family with that code already exists.").with_detail(
                field="family_code",
                code="DUPLICATE",
                message="Family codes are unique per organization, including archived rows.",
            )

    def _resolve_parent(self, node_level: str, parent_id: uuid.UUID | None) -> JobFamily | None:
        if node_level == JobNodeLevel.FAMILY.value:
            if parent_id is not None:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "parent_job_family_id",
                            "NOT_APPLICABLE",
                            "A FAMILY node must not have a parent.",
                        )
                    ]
                )
            return None
        if parent_id is None:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "parent_job_family_id",
                        "REQUIRED",
                        "A SUBFAMILY requires a FAMILY parent.",
                    )
                ]
            )
        parent = self._families.require(parent_id, resource="job family")
        if parent.node_level != JobNodeLevel.FAMILY.value:
            raise JobFamilyCycle(
                "A subfamily may only be attached to a top-level family."
            ).with_detail(
                field="parent_job_family_id",
                code="JOB_FAMILY_CYCLE",
                message=f"The hierarchy is at most {JOB_FAMILY_MAX_DEPTH} levels deep.",
            )
        if parent.status != CatalogueStatus.ACTIVE.value:
            raise MasterInactive("The parent family is inactive.")
        return parent

    def _validate_reparent(self, family: JobFamily, parent_id: uuid.UUID | None) -> None:
        """JA §5.3: only a subfamily moves, and only under a different top-level family.

        The affected rows are locked in ascending id order first, so two concurrent moves
        serialise instead of both passing a check that the other then invalidates.
        """
        if parent_id is not None and parent_id == family.id:
            raise JobFamilyCycle("A job family cannot be its own parent.")

        candidates = [family.id] + ([parent_id] if parent_id is not None else [])
        self._families.lock_many(candidates)

        has_children = bool(self._families.children(family.id))
        if parent_id is None:
            if family.node_level == JobNodeLevel.SUBFAMILY.value:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "parent_job_family_id",
                            "REQUIRED",
                            "A SUBFAMILY must keep a FAMILY parent.",
                        )
                    ]
                )
            return
        if family.node_level == JobNodeLevel.FAMILY.value:
            raise JobFamilyCycle("A top-level family cannot become a subfamily.").with_detail(
                field="parent_job_family_id",
                code="JOB_FAMILY_CYCLE",
                message="Create a new subfamily instead of demoting a family.",
            )
        if has_children:
            raise JobFamilyCycle("A node with children cannot be reparented.")

        parent = self._families.require(parent_id, resource="job family")
        if parent.node_level != JobNodeLevel.FAMILY.value:
            raise JobFamilyCycle(f"The hierarchy is at most {JOB_FAMILY_MAX_DEPTH} levels deep.")
        if parent.status != CatalogueStatus.ACTIVE.value:
            raise MasterInactive("The target family is inactive.")

    def _require_active_family(self, family_id: uuid.UUID) -> JobFamily:
        family = self._families.require(family_id, resource="job family")
        if family.status != CatalogueStatus.ACTIVE.value:
            raise MasterInactive("That job family is inactive and cannot classify a new job.")
        return family

    def _resolve_grades(
        self, values: dict[str, Any], *, require_active: bool = True
    ) -> dict[str, Grade]:
        """Load the referenced grades, rejecting a cross-organization or inactive one.

        The repository is organization-scoped, so a grade from another tenant simply is not
        found — which is reported as ``INVALID_ORGANIZATION_REFERENCE`` rather than a 404,
        because the caller supplied it in a body field (JA §14.1).
        """
        resolved: dict[str, Grade] = {}
        for field in ("default_grade_id", "min_grade_id", "max_grade_id"):
            grade_id = values.get(field)
            if grade_id is None:
                continue
            grade = cast(Grade | None, self._grades.get(grade_id))
            if grade is None:
                raise InvalidOrganizationReference(
                    "That grade does not belong to this organization."
                ).with_detail(
                    field=field,
                    code="INVALID_ORGANIZATION_REFERENCE",
                    message="Grades are organization-scoped.",
                )
            if require_active and grade.status != "ACTIVE":
                raise MasterInactive("That grade is inactive.").with_detail(
                    field=field, code="MASTER_INACTIVE", message="Reactivate it first."
                )
            resolved[field] = grade
        return resolved

    def _assert_code_mutable(self, job: Job) -> None:
        """JA §4.5: the code freezes as soon as anything references the job."""
        if self._positions.count_for_job(job.id) > 0:
            raise FieldReadOnly(
                "The job code cannot change once a position references the job."
            ).with_detail(
                field="job_code", code="FIELD_READ_ONLY", message="Create a new job instead."
            )


def _assert_interval(field: str, start: date, end: date | None) -> None:
    if end is not None and end <= start:
        raise ValidationFailed(
            details=[
                ErrorDetail(
                    "effective_to",
                    "INVALID_RANGE",
                    f"effective_to must be after {field}.",
                )
            ]
        )


def _touches_grades(applied: dict[str, Any]) -> bool:
    return any(field in applied for field in ("default_grade_id", "min_grade_id", "max_grade_id"))


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = ["JobCatalogueService", "validate_grade_range"]
