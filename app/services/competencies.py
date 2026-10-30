"""The competency catalogue and its job and position requirement mappings (JA §9).

A competency here is a **requirement**, never an assessment. JA §9.4 and base §4.2 exclude
employee competency scores, appraisals, gap analysis, learning plans and training
recommendations outright, and nothing in this module stores a per-employee value.

Two rules carry the design:

**Mappings are effective-dated, and a change is a new row.** Replacing a job's requirement set
end-dates the rows that left and inserts rows for the ones that arrived (JA §9.4), so "what
did this job require last March" stays answerable. Deleting the old row would destroy exactly
that.

**A position may add, never soften.** The effective requirement for a position is the union of
its job's mappings and its own, where the higher ``min_proficiency`` wins and ``REQUIRED``
beats ``PREFERRED`` (JA §9.3). A position payload that tries to lower or drop a job
requirement is rejected with ``COMPETENCY_REQUIREMENT_CONFLICT`` rather than quietly ignored,
because a silently ignored softening would read to the author as if it had worked.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in, utc_now
from app.core.context import OrgContext
from app.core.errors import (
    CompetencyInUse,
    CompetencyRequirementConflict,
    DuplicateResource,
    ErrorDetail,
    InvalidOrganizationReference,
    MasterInactive,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.core.warnings import WarningCollector
from app.db.types import uuid7
from app.models.master import LookupList
from app.models.position import (
    CatalogueStatus,
    Competency,
    CompetencyRequirementType,
    JobCompetency,
    PositionCompetency,
)
from app.repositories.master import LookupRepository
from app.repositories.organization import OrganizationRepository
from app.repositories.position import (
    CompetencyRepository,
    JobCompetencyRepository,
    JobRepository,
    PositionCompetencyRepository,
    PositionRepository,
)
from app.services.organizations import assert_etag

READ_PERMISSIONS = (Permission.EMPLOYEE_LIST, Permission.POSITION_VIEW)
COMPETENCY_SORT_FIELDS = frozenset({"competency_code", "name", "category_code", "created_at"})

_REQUIREMENT_RANK = {
    CompetencyRequirementType.PREFERRED.value: 0,
    CompetencyRequirementType.REQUIRED.value: 1,
}


@dataclass(frozen=True, slots=True)
class EffectiveRequirement:
    """One line of the JA §9.3 union of job and position requirements."""

    competency_id: uuid.UUID
    requirement_type: str
    min_proficiency: int
    source: str
    mapping_id: uuid.UUID
    display_order: int | None
    effective_from: date
    effective_to: date | None
    row_version: int


class CompetencyService:
    """The competency catalogue and the two mapping aggregates (JA §9)."""

    def __init__(
        self,
        *,
        competencies: CompetencyRepository,
        job_competencies: JobCompetencyRepository,
        position_competencies: PositionCompetencyRepository,
        jobs: JobRepository,
        positions: PositionRepository,
        lookups: LookupRepository,
        organizations: OrganizationRepository,
        audit: AuditWriter,
    ) -> None:
        self._competencies = competencies
        self._job_competencies = job_competencies
        self._position_competencies = position_competencies
        self._jobs = jobs
        self._positions = positions
        self._lookups = lookups
        self._organizations = organizations
        self._audit = audit

    # ================================================================= catalogue

    def list_competencies(
        self,
        context: OrgContext,
        *,
        query: str | None = None,
        category_code: str | None = None,
        status: Sequence[str] | None = None,
        include_archived: bool = False,
        sort_field: str = "name",
        direction: str = "asc",
        after_value: Any | None = None,
        after_id: uuid.UUID | None = None,
        limit: int = 25,
        with_total: bool = False,
    ) -> tuple[list[Competency], bool, int | None]:
        context.require_any(*READ_PERMISSIONS)
        if include_archived:
            context.require(Permission.MASTER_MANAGE)
        return self._competencies.search_page(
            query=query,
            category_code=category_code,
            status=status,
            include_archived=include_archived,
            sort_field=sort_field,
            direction=direction,
            after_value=after_value,
            after_id=after_id,
            limit=limit,
            with_total=with_total,
        )

    def get_competency(self, context: OrgContext, competency_id: uuid.UUID) -> Competency:
        context.require_any(*READ_PERMISSIONS)
        return self._competencies.require(
            competency_id, include_archived=True, resource="competency"
        )

    def create_competency(
        self,
        context: OrgContext,
        *,
        values: dict[str, Any],
        acknowledged_warnings: list[str],
    ) -> tuple[Competency, list[str]]:
        context.require(Permission.MASTER_MANAGE)
        code = str(values["competency_code"])
        if self._competencies.get_by_code(code) is not None:
            raise DuplicateResource("A competency with that code already exists.").with_detail(
                field="competency_code",
                code="DUPLICATE",
                message="Competency codes are unique per organization, archived rows included.",
            )
        category = str(values["category_code"])
        self._require_active_category(category)
        acknowledged = WarningCollector(acknowledged_warnings).enforce()

        competency = Competency(
            id=uuid7(),
            organization_id=context.organization_id,
            competency_code=code,
            name=str(values["name"]),
            description=_text_or_none(values.get("description")),
            category_code=category,
            status=str(values.get("status") or CatalogueStatus.ACTIVE.value),
            created_by=context.user_id,
        )
        self._competencies.add(competency)
        self._competencies.flush()
        self._audit.record_change(
            context,
            action=AuditAction.COMPETENCY_CREATED,
            entity_type="competency",
            entity_id=competency.id,
            after={
                "competency_code": competency.competency_code,
                "name": competency.name,
                "category_code": competency.category_code,
                "status": competency.status,
            },
            fields=["competency_code", "name", "category_code", "status"],
            acknowledged_warnings=acknowledged,
        )
        return competency, acknowledged

    def update_competency(
        self,
        context: OrgContext,
        *,
        competency_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
        acknowledged_warnings: list[str],
    ) -> tuple[Competency, list[str]]:
        context.require(Permission.MASTER_MANAGE)
        competency = self._competencies.require(competency_id, resource="competency")
        assert_etag(competency.row_version, if_match)

        applied = {
            key: value
            for key, value in changes.items()
            if key in {"name", "description", "category_code"}
        }
        if "category_code" in applied:
            self._require_active_category(str(applied["category_code"]))
        acknowledged = WarningCollector(acknowledged_warnings).enforce()

        before = {field: getattr(competency, field) for field in applied}
        for field, value in applied.items():
            setattr(competency, field, value)
        competency.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.COMPETENCY_UPDATED,
            entity_type="competency",
            entity_id=competency.id,
            before=before,
            after={field: getattr(competency, field) for field in applied},
            fields=list(applied),
            acknowledged_warnings=acknowledged,
        )
        return competency, acknowledged

    def set_competency_status(
        self,
        context: OrgContext,
        *,
        competency_id: uuid.UUID,
        active: bool,
        reason: str,
        if_match: int | None,
    ) -> Competency:
        context.require(Permission.MASTER_MANAGE)
        competency = self._competencies.require(competency_id, resource="competency")
        assert_etag(competency.row_version, if_match)
        target = CatalogueStatus.ACTIVE.value if active else CatalogueStatus.INACTIVE.value
        if competency.status == target:
            raise ValidationFailed(f"The competency is already {target.lower()}.")
        if active:
            self._require_active_category(competency.category_code)

        before = {"status": competency.status}
        competency.status = target
        competency.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.COMPETENCY_STATUS_CHANGED,
            entity_type="competency",
            entity_id=competency.id,
            before=before,
            after={"status": target},
            fields=["status"],
            reason=reason,
        )
        return competency

    def archive_competency(
        self,
        context: OrgContext,
        *,
        competency_id: uuid.UUID,
        reason: str,
        if_match: int | None,
    ) -> Competency:
        """JA §9.4: blocked while an active job or position mapping exists."""
        context.require(Permission.MASTER_MANAGE)
        competency = self._competencies.require(competency_id, resource="competency")
        assert_etag(competency.row_version, if_match)
        today = self._today()
        in_use = self._job_competencies.active_mappings_for_competency(
            competency.id, as_of=today
        ) + self._position_competencies.active_mappings_for_competency(competency.id, as_of=today)
        if in_use:
            raise CompetencyInUse(
                f"{in_use} active job or position mapping(s) require this competency."
            ).with_detail(
                field="competency_id",
                code="COMPETENCY_IN_USE",
                message="End-date those requirements before archiving.",
            )

        competency.archived_at = utc_now()
        competency.archived_by = context.user_id
        competency.archive_reason = reason
        competency.status = CatalogueStatus.INACTIVE.value
        competency.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.COMPETENCY_ARCHIVED,
            entity_type="competency",
            entity_id=competency.id,
            after={"archived_at": competency.archived_at, "status": competency.status},
            fields=["archived_at", "status"],
            reason=reason,
        )
        return competency

    def restore_competency(
        self,
        context: OrgContext,
        *,
        competency_id: uuid.UUID,
        reason: str,
        if_match: int | None,
    ) -> Competency:
        context.require(Permission.MASTER_MANAGE)
        competency = self._competencies.require(
            competency_id, include_archived=True, resource="competency"
        )
        assert_etag(competency.row_version, if_match)
        if competency.archived_at is None:
            raise ValidationFailed("The competency is not archived.")
        competency.archived_at = None
        competency.archived_by = None
        competency.archive_reason = None
        competency.status = CatalogueStatus.INACTIVE.value
        competency.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.COMPETENCY_RESTORED,
            entity_type="competency",
            entity_id=competency.id,
            after={"archived_at": None, "status": competency.status},
            fields=["archived_at", "status"],
            reason=reason,
        )
        return competency

    def competency_labels(self, competency_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Competency]:
        """Resolve code and name for a set of competencies in one query.

        Serialisers call this once per response rather than fetching per requirement, which
        is what keeps a job or position read off the N+1 path (JA §20.1).
        """
        return self._competencies.load_many(list(competency_ids))

    # ============================================================= job mappings

    def job_requirements(
        self, context: OrgContext, *, job_id: uuid.UUID, as_of: date | None = None
    ) -> list[EffectiveRequirement]:
        context.require_any(*READ_PERMISSIONS)
        self._jobs.require(job_id, include_archived=True, resource="job")
        moment = as_of or self._today()
        return [
            _requirement(row, "JOB")
            for row in self._job_competencies.active_for_owner(job_id, as_of=moment)
        ]

    def replace_job_requirements(
        self,
        context: OrgContext,
        *,
        job_id: uuid.UUID,
        mappings: Sequence[dict[str, Any]],
        reason: str | None,
        if_match: int | None,
        acknowledged_warnings: list[str],
    ) -> list[EffectiveRequirement]:
        """Replace the active requirement set of a job (JA §9.4 ``PUT``)."""
        context.require(Permission.MASTER_MANAGE)
        job = self._jobs.require(job_id, resource="job")
        assert_etag(job.row_version, if_match)
        acknowledged = WarningCollector(acknowledged_warnings).enforce()

        today = self._today()
        changed = self._apply_replacement(
            context,
            repository=self._job_competencies,
            owner_field="job_id",
            owner_id=job.id,
            mappings=mappings,
            today=today,
        )
        # Touch the job so its ETag moves: a client holding the old version must re-read
        # before changing the requirement set again.
        job.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.JOB_COMPETENCY_MAPPING_CHANGED,
            entity_type="job",
            entity_id=job.id,
            after={"competency_count": len(mappings), "changed": changed},
            fields=["competencies"],
            reason=reason,
            acknowledged_warnings=acknowledged,
        )
        return [
            _requirement(row, "JOB")
            for row in self._job_competencies.active_for_owner(job.id, as_of=today)
        ]

    # ======================================================== position mappings

    def position_requirements(
        self, context: OrgContext, *, position_id: uuid.UUID, as_of: date | None = None
    ) -> list[EffectiveRequirement]:
        """The JA §9.3 union: job baseline plus position additions, strictest wins."""
        context.require(Permission.POSITION_VIEW)
        position = self._positions.require(position_id, include_archived=True, resource="position")
        moment = as_of or self._today()
        job_rows = [
            _requirement(row, "JOB")
            for row in self._job_competencies.active_for_owner(position.job_id, as_of=moment)
        ]
        position_rows = [
            _requirement(row, "POSITION")
            for row in self._position_competencies.active_for_owner(position.id, as_of=moment)
        ]
        return merge_requirements(job_rows, position_rows)

    def replace_position_requirements(
        self,
        context: OrgContext,
        *,
        position_id: uuid.UUID,
        mappings: Sequence[dict[str, Any]],
        reason: str | None,
        if_match: int | None,
        acknowledged_warnings: list[str],
    ) -> list[EffectiveRequirement]:
        """Replace a position's *additional* requirements (JA §9.4).

        The payload is checked against the job baseline first: a line that lowers a job
        requirement, or that would remove one, is a conflict rather than an override.
        """
        context.require(Permission.POSITION_MANAGE)
        position = self._positions.require(position_id, resource="position")
        assert_etag(position.row_version, if_match)
        if position.archived_at is not None:
            raise ValidationFailed("An archived position cannot be edited.")
        acknowledged = WarningCollector(acknowledged_warnings).enforce()

        today = self._today()
        baseline = {
            row.competency_id: row
            for row in self._job_competencies.active_for_owner(position.job_id, as_of=today)
        }
        for mapping in mappings:
            job_requirement = baseline.get(mapping["competency_id"])
            if job_requirement is None:
                continue
            proposed_type = str(
                mapping.get("requirement_type") or CompetencyRequirementType.REQUIRED.value
            )
            proposed_level = int(mapping.get("min_proficiency") or 1)
            softer_level = proposed_level < job_requirement.min_proficiency
            softer_type = (
                _REQUIREMENT_RANK[proposed_type]
                < _REQUIREMENT_RANK[job_requirement.requirement_type]
            )
            if softer_level or softer_type:
                raise CompetencyRequirementConflict(
                    "A position may raise a job requirement but not lower it."
                ).with_detail(
                    field="mappings",
                    code="COMPETENCY_REQUIREMENT_CONFLICT",
                    message=(
                        f"The job requires {job_requirement.requirement_type} at "
                        f"proficiency {job_requirement.min_proficiency}."
                    ),
                )

        changed = self._apply_replacement(
            context,
            repository=self._position_competencies,
            owner_field="position_id",
            owner_id=position.id,
            mappings=mappings,
            today=today,
        )
        position.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.POSITION_COMPETENCY_MAPPING_CHANGED,
            entity_type="position",
            entity_id=position.id,
            after={"competency_count": len(mappings), "changed": changed},
            fields=["competencies"],
            reason=reason,
            acknowledged_warnings=acknowledged,
        )
        return self.position_requirements(context, position_id=position.id, as_of=today)

    # ------------------------------------------------------------------ helpers

    def seed_initial_mappings(
        self,
        context: OrgContext,
        *,
        owner_field: str,
        owner_id: uuid.UUID,
        mappings: Sequence[dict[str, Any]],
    ) -> None:
        """Attach the ``competencies[]`` supplied on a job or position create.

        Separate from the replacement path because a create has no previous set to end-date
        and has already validated its own ETag.
        """
        if not mappings:
            return
        repository: Any = (
            self._job_competencies if owner_field == "job_id" else self._position_competencies
        )
        self._apply_replacement(
            context,
            repository=repository,
            owner_field=owner_field,
            owner_id=owner_id,
            mappings=mappings,
            today=self._today(),
        )

    def _apply_replacement(
        self,
        context: OrgContext,
        *,
        repository: Any,
        owner_field: str,
        owner_id: uuid.UUID,
        mappings: Sequence[dict[str, Any]],
        today: date,
    ) -> int:
        """Reconcile the stored mapping set with the requested one.

        A mapping row is keyed by ``(owner, competency, effective_from)``, which is what makes
        the three cases below distinct rather than arbitrary: a payload naming a start date
        addresses the row with that start date.

        Unchanged rows are left strictly alone. Re-inserting an identical requirement on every
        save would fill the history with noise and collide with the same-start unique key.
        """
        active = {
            row.competency_id: row for row in repository.active_for_owner(owner_id, as_of=today)
        }
        history = repository.all_for_owner(owner_id)
        desired: dict[uuid.UUID, dict[str, Any]] = {}
        for mapping in mappings:
            competency = self._require_active_competency(mapping["competency_id"])
            desired[competency.id] = mapping

        changed = 0

        # --- removals ---------------------------------------------------------------
        for competency_id, row in active.items():
            if competency_id in desired:
                continue
            self._retire(row, today=today, context=context)
            changed += 1

        # --- additions and changes ---------------------------------------------------
        for competency_id, mapping in desired.items():
            effective_from = mapping.get("effective_from") or today
            effective_to = mapping.get("effective_to")
            if effective_to is not None and effective_to <= effective_from:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "effective_to",
                            "INVALID_RANGE",
                            "effective_to must be after effective_from.",
                        )
                    ]
                )

            current = active.get(competency_id)
            if (
                current is not None
                and _matches(current, mapping)
                and current.effective_from == effective_from
                and current.effective_to == effective_to
            ):
                continue

            addressed = next(
                (
                    row
                    for row in history
                    if row.competency_id == competency_id and row.effective_from == effective_from
                ),
                None,
            )
            if addressed is not None:
                # The payload names this row's start date, so it is this row that is being
                # written — including the case where an earlier call in the same day archived
                # it, which is why the archival fields are cleared here.
                self._write_mapping(addressed, mapping, effective_to=effective_to, context=context)
                changed += 1
                continue

            if current is not None:
                # A row that started earlier keeps its history and is closed at the new start.
                self._end_at(current, boundary=effective_from, context=context)

            overlapping = [
                row
                for row in repository.overlapping(
                    owner_id=owner_id,
                    competency_id=competency_id,
                    effective_from=effective_from,
                    effective_to=effective_to,
                )
                if row is not current and row.archived_at is None
            ]
            if overlapping:
                raise DuplicateResource(
                    "An overlapping requirement already exists for that competency."
                ).with_detail(
                    field="mappings",
                    code="DUPLICATE_RESOURCE",
                    message="End-date the existing requirement before adding a new one.",
                )

            model = JobCompetency if owner_field == "job_id" else PositionCompetency
            inserted = model(
                id=uuid7(),
                organization_id=context.organization_id,
                competency_id=competency_id,
                effective_from=effective_from,
                effective_to=effective_to,
                created_by=context.user_id,
                **{owner_field: owner_id},
            )
            self._write_mapping(inserted, mapping, effective_to=effective_to, context=context)
            repository.add(inserted)
            changed += 1
        repository.flush()
        return changed

    @staticmethod
    def _write_mapping(
        row: Any, mapping: dict[str, Any], *, effective_to: date | None, context: OrgContext
    ) -> None:
        """Apply a payload line to a mapping row, clearing any archival it carried."""
        row.requirement_type = str(
            mapping.get("requirement_type") or CompetencyRequirementType.REQUIRED.value
        )
        row.min_proficiency = int(mapping.get("min_proficiency") or 1)
        row.display_order = mapping.get("display_order")
        row.effective_to = effective_to
        row.archived_at = None
        row.archived_by = None
        row.archive_reason = None
        row.updated_by = context.user_id

    def _retire(self, row: Any, *, today: date, context: OrgContext) -> None:
        """Take a requirement out of the active set.

        A row that has already applied is end-dated at ``today``, which preserves the period
        during which it was in force. A row that starts today or later never applied, and
        cannot be end-dated at all — ``effective_to > effective_from`` forbids a zero-length
        interval — so it is archived instead. §10.14 rules out a hard delete either way.
        """
        if row.effective_from < today:
            self._end_at(row, boundary=today, context=context)
            return
        row.archived_at = utc_now()
        row.archived_by = context.user_id
        row.archive_reason = "Requirement removed before it took effect"
        row.updated_by = context.user_id

    def _end_at(self, row: Any, *, boundary: date, context: OrgContext) -> None:
        """Close a mapping's interval at ``boundary`` (exclusive, §10.15)."""
        if boundary <= row.effective_from:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "effective_from",
                        "INVALID_RANGE",
                        "A requirement cannot be end-dated before it starts.",
                    )
                ]
            )
        row.effective_to = boundary
        row.updated_by = context.user_id

    def _require_active_competency(self, competency_id: uuid.UUID) -> Competency:
        competency = self._competencies.get(competency_id)
        if competency is None:
            raise InvalidOrganizationReference(
                "That competency does not belong to this organization."
            ).with_detail(
                field="competency_id",
                code="INVALID_ORGANIZATION_REFERENCE",
                message="Competencies are organization-scoped.",
            )
        if competency.status != CatalogueStatus.ACTIVE.value:
            raise MasterInactive(
                "That competency is inactive and cannot be newly required."
            ).with_detail(
                field="competency_id", code="MASTER_INACTIVE", message="Reactivate it first."
            )
        return competency

    def _require_active_category(self, category_code: str) -> None:
        """JA §9.5: the category is an active ``COMPETENCY_CATEGORY`` lookup value."""
        value = self._lookups.get_value(LookupList.COMPETENCY_CATEGORY.value, category_code)
        if value is None:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "category_code",
                        "UNKNOWN_VALUE",
                        "Not a configured COMPETENCY_CATEGORY value.",
                    )
                ]
            )
        if value.status != "ACTIVE":
            raise MasterInactive("That competency category is inactive.").with_detail(
                field="category_code", code="MASTER_INACTIVE", message="Reactivate it first."
            )

    def _today(self) -> date:
        organization = self._organizations.get(self._competencies.organization_id)
        return today_in(organization.timezone if organization else None)


def merge_requirements(
    job_rows: Sequence[EffectiveRequirement], position_rows: Sequence[EffectiveRequirement]
) -> list[EffectiveRequirement]:
    """JA §9.3: union by competency; higher proficiency wins, ``REQUIRED`` beats ``PREFERRED``."""
    merged: dict[uuid.UUID, EffectiveRequirement] = {row.competency_id: row for row in job_rows}
    for row in position_rows:
        current = merged.get(row.competency_id)
        if current is None:
            merged[row.competency_id] = row
            continue
        merged[row.competency_id] = EffectiveRequirement(
            competency_id=row.competency_id,
            requirement_type=(
                current.requirement_type
                if _REQUIREMENT_RANK[current.requirement_type]
                >= _REQUIREMENT_RANK[row.requirement_type]
                else row.requirement_type
            ),
            min_proficiency=max(current.min_proficiency, row.min_proficiency),
            source="POSITION",
            mapping_id=row.mapping_id,
            display_order=row.display_order
            if row.display_order is not None
            else current.display_order,
            effective_from=row.effective_from,
            effective_to=row.effective_to,
            row_version=row.row_version,
        )
    return sorted(
        merged.values(),
        key=lambda row: (
            row.display_order if row.display_order is not None else 1_000_000,
            row.competency_id.bytes,
        ),
    )


def _requirement(row: Any, source: str) -> EffectiveRequirement:
    return EffectiveRequirement(
        competency_id=row.competency_id,
        requirement_type=row.requirement_type,
        min_proficiency=row.min_proficiency,
        source=source,
        mapping_id=row.id,
        display_order=row.display_order,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        row_version=row.row_version,
    )


def _matches(row: Any, mapping: dict[str, Any]) -> bool:
    return bool(
        row.requirement_type
        == str(mapping.get("requirement_type") or CompetencyRequirementType.REQUIRED.value)
        and row.min_proficiency == int(mapping.get("min_proficiency") or 1)
        and row.display_order == mapping.get("display_order")
    )


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = ["CompetencyService", "EffectiveRequirement", "merge_requirements"]
