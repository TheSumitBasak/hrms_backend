"""Data access for job families, jobs, competencies, positions and headcount.

Every query here is built through :meth:`OrgScopedRepository.select`, so the tenant predicate
is applied before a service sees the statement — the mandatory repository filter §23.6
requires of a MySQL deployment.

Two things are deliberately centralised in this module rather than left to services:

**Occupancy is aggregated in SQL.** :meth:`PositionAssignmentRepository.occupancy_for` and
:meth:`occupancy_map` return ``SUM(fte)`` and ``COUNT(*)`` from one grouped statement, and
:meth:`PositionRepository.headcount_summary` computes a whole grouped report in a single
statement joined against those aggregates. Loading positions and counting their assignments
in Python would be the N+1 pattern JA §20.1 forbids, and would also let a report drift from
the capacity check.

**The counting predicates exist once.** ``_occupied_predicates`` and
``_reserved_predicates`` encode JA §8.3 — ``ACTIVE`` only, ``effective_from <= d``,
``effective_to`` exclusive — and every caller reuses them. JA §17.6 requires that no report
use a different definition from the capacity check, and sharing the predicate is how that is
guaranteed rather than hoped for.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, case, func, literal, or_, select

from app.core.errors import ResourceNotFound
from app.models.employee import SEPARATED_STATUSES, Employee
from app.models.master import Department, Grade
from app.models.position import (
    OCCUPYING_ASSIGNMENT_STATUSES,
    AssignmentStatus,
    AssignmentType,
    Competency,
    EmployeePositionAssignment,
    HeadcountChangeRequest,
    HeadcountPlan,
    HeadcountPlanStatus,
    HeadcountRequestStatus,
    HeadcountReservation,
    HeadcountScopeType,
    Job,
    JobCompetency,
    JobFamily,
    Position,
    PositionCompetency,
    PositionStatus,
    PositionStatusHistory,
    ReservationStatus,
)
from app.repositories.base import OrgScopedRepository
from app.validation.normalizers import escape_like_pattern

ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class Occupancy:
    """A counted occupancy or reservation: FTE and people, never interchangeable (JA §8.2)."""

    fte: Decimal = ZERO
    headcount: int = 0

    def __add__(self, other: Occupancy) -> Occupancy:
        return Occupancy(self.fte + other.fte, self.headcount + other.headcount)


@dataclass(frozen=True, slots=True)
class SummaryRow:
    """One grouped row of the JA §8.10 headcount summary, straight from SQL."""

    group_key: str | None
    group_id: uuid.UUID | None
    group_label: str | None
    authorized_fte: Decimal
    authorized_headcount: int
    occupied_fte: Decimal
    occupied_headcount: int
    reserved_fte: Decimal
    reserved_headcount: int
    position_count: int
    open_position_count: int


def _as_decimal(value: Any) -> Decimal:
    if value is None:
        return ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


class JobFamilyRepository(OrgScopedRepository[JobFamily]):
    """Families and subfamilies (JA §5)."""

    model = JobFamily

    def get_by_code(self, code: str) -> JobFamily | None:
        statement = self.select(include_archived=True).where(
            JobFamily.family_code_normalized == code.strip().upper()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def search_page(
        self,
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
        conditions: list[ColumnElement[bool]] = []
        if query:
            escaped = escape_like_pattern(query)
            conditions.append(
                or_(
                    JobFamily.family_code_normalized.like(f"{escaped.upper()}%"),
                    JobFamily.name.like(f"%{escaped}%"),
                )
            )
        if node_level is not None:
            conditions.append(JobFamily.node_level == node_level)
        if parent_id is not None:
            conditions.append(JobFamily.parent_job_family_id == parent_id)
        if status:
            conditions.append(JobFamily.status.in_(status))

        columns = {
            "family_code": JobFamily.family_code_normalized,
            "name": JobFamily.name,
            "display_order": JobFamily.display_order,
            "created_at": JobFamily.created_at,
        }
        return _paginate(
            self,
            JobFamily,
            conditions=conditions,
            columns=columns,
            sort_field=sort_field,
            default_sort="display_order",
            direction=direction,
            after_value=after_value,
            after_id=after_id,
            limit=limit,
            with_total=with_total,
            include_archived=include_archived,
            secondary_order=(JobFamily.name,),
        )

    def children(
        self, family_id: uuid.UUID, *, include_archived: bool = True
    ) -> Sequence[JobFamily]:
        statement = self.select(include_archived=include_archived).where(
            JobFamily.parent_job_family_id == family_id
        )
        return list(self._session.execute(statement).scalars())

    def has_active_children(self, family_id: uuid.UUID) -> bool:
        statement = (
            self.select()
            .where(
                JobFamily.parent_job_family_id == family_id,
                JobFamily.status == "ACTIVE",
            )
            .limit(1)
        )
        return self._session.execute(statement).first() is not None

    def family_and_descendant_ids(self, family_id: uuid.UUID) -> list[uuid.UUID]:
        """The node plus its children. Two levels, so one extra query — not a recursive CTE.

        JA §5.1 caps the hierarchy at two levels, which is also why "filter by family and
        include its subfamilies" needs no recursion.
        """
        statement = select(JobFamily.id).where(
            JobFamily.organization_id == self.organization_id,
            JobFamily.parent_job_family_id == family_id,
        )
        return [family_id, *self._session.execute(statement).scalars()]

    def lock_many(self, family_ids: Sequence[uuid.UUID]) -> Sequence[JobFamily]:
        """Lock several family rows in ascending id order (JA §5.3).

        Two concurrent reparent operations must not interleave their cycle checks. A fixed
        lock order is what stops that becoming a deadlock instead.
        """
        if not family_ids:
            return []
        statement = (
            self.select(include_archived=True)
            .where(JobFamily.id.in_(sorted(set(family_ids), key=lambda value: value.bytes)))
            .order_by(JobFamily.id)
            .with_for_update()
        )
        return list(self._session.execute(statement).scalars())


class JobRepository(OrgScopedRepository[Job]):
    """The job catalogue (JA §4)."""

    model = Job

    def get_by_code(self, code: str) -> Job | None:
        statement = self.select(include_archived=True).where(
            Job.job_code_normalized == code.strip().upper()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def title_exists_in_family(
        self, *, title: str, job_family_id: uuid.UUID, exclude_id: uuid.UUID | None = None
    ) -> bool:
        """JA §4.5: a duplicate *active* title inside one family raises a warning."""
        statement = (
            self.select()
            .where(
                Job.job_family_id == job_family_id,
                Job.title == title.strip(),
                Job.status == "ACTIVE",
            )
            .limit(1)
        )
        if exclude_id is not None:
            statement = statement.where(Job.id != exclude_id)
        return self._session.execute(statement).first() is not None

    def search_page(
        self,
        *,
        query: str | None = None,
        job_family_ids: Sequence[uuid.UUID] | None = None,
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
        conditions: list[ColumnElement[bool]] = []
        if query:
            escaped = escape_like_pattern(query)
            conditions.append(
                or_(
                    Job.job_code_normalized.like(f"{escaped.upper()}%"),
                    Job.title.like(f"%{escaped}%"),
                )
            )
        if job_family_ids:
            conditions.append(Job.job_family_id.in_(job_family_ids))
        if grade_id is not None:
            # JA §17.2: "jobs whose default or range includes the grade".
            conditions.append(
                or_(
                    Job.default_grade_id == grade_id,
                    Job.min_grade_id == grade_id,
                    Job.max_grade_id == grade_id,
                )
            )
        if employment_type_id is not None:
            conditions.append(Job.default_employment_type_id == employment_type_id)
        if status:
            conditions.append(Job.status.in_(status))
        if effective_on is not None:
            conditions.append(Job.effective_from <= effective_on)
            conditions.append(or_(Job.effective_to.is_(None), Job.effective_to > effective_on))

        columns = {
            "job_code": Job.job_code_normalized,
            "title": Job.title,
            "created_at": Job.created_at,
            "effective_from": Job.effective_from,
        }
        return _paginate(
            self,
            Job,
            conditions=conditions,
            columns=columns,
            sort_field=sort_field,
            default_sort="title",
            direction=direction,
            after_value=after_value,
            after_id=after_id,
            limit=limit,
            with_total=with_total,
            include_archived=include_archived,
        )

    def active_jobs_in_families(self, family_ids: Sequence[uuid.UUID]) -> int:
        if not family_ids:
            return 0
        statement = (
            select(func.count())
            .select_from(Job)
            .where(
                Job.organization_id == self.organization_id,
                Job.job_family_id.in_(family_ids),
                Job.archived_at.is_(None),
                Job.status == "ACTIVE",
            )
        )
        return int(self._session.execute(statement).scalar() or 0)

    def grades_in_use(self, grade_id: uuid.UUID) -> int:
        statement = (
            select(func.count())
            .select_from(Job)
            .where(
                Job.organization_id == self.organization_id,
                Job.archived_at.is_(None),
                or_(
                    Job.default_grade_id == grade_id,
                    Job.min_grade_id == grade_id,
                    Job.max_grade_id == grade_id,
                ),
            )
        )
        return int(self._session.execute(statement).scalar() or 0)

    def load_many(self, job_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Job]:
        if not job_ids:
            return {}
        statement = self.select(include_archived=True).where(Job.id.in_(set(job_ids)))
        return {row.id: row for row in self._session.execute(statement).scalars()}


class CompetencyRepository(OrgScopedRepository[Competency]):
    """The competency catalogue (JA §9.1). Requirements only — never an employee rating."""

    model = Competency

    def get_by_code(self, code: str) -> Competency | None:
        statement = self.select(include_archived=True).where(
            Competency.competency_code_normalized == code.strip().upper()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def search_page(
        self,
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
        conditions: list[ColumnElement[bool]] = []
        if query:
            escaped = escape_like_pattern(query)
            conditions.append(
                or_(
                    Competency.competency_code_normalized.like(f"{escaped.upper()}%"),
                    Competency.name.like(f"%{escaped}%"),
                )
            )
        if category_code is not None:
            conditions.append(Competency.category_code == category_code)
        if status:
            conditions.append(Competency.status.in_(status))

        columns = {
            "competency_code": Competency.competency_code_normalized,
            "name": Competency.name,
            "category_code": Competency.category_code,
            "created_at": Competency.created_at,
        }
        return _paginate(
            self,
            Competency,
            conditions=conditions,
            columns=columns,
            sort_field=sort_field,
            default_sort="name",
            direction=direction,
            after_value=after_value,
            after_id=after_id,
            limit=limit,
            with_total=with_total,
            include_archived=include_archived,
        )

    def load_many(self, competency_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Competency]:
        if not competency_ids:
            return {}
        statement = self.select(include_archived=True).where(Competency.id.in_(set(competency_ids)))
        return {row.id: row for row in self._session.execute(statement).scalars()}


class _CompetencyMappingRepository[ModelT: (JobCompetency, PositionCompetency)](
    OrgScopedRepository[ModelT]
):
    """Shared effective-dated mapping queries for job and position competencies."""

    owner_column: str

    def _owner(self) -> Any:
        return getattr(self.model, self.owner_column)

    def active_for_owner(self, owner_id: uuid.UUID, *, as_of: date) -> Sequence[ModelT]:
        """Mappings in force on ``as_of`` — a half-open interval (JA §10.15)."""
        statement = (
            self.select()
            .where(
                self._owner() == owner_id,
                self.model.effective_from <= as_of,
                or_(self.model.effective_to.is_(None), self.model.effective_to > as_of),
            )
            .order_by(self.model.display_order, self.model.id)
        )
        return list(self._session.execute(statement).scalars())

    def all_for_owner(self, owner_id: uuid.UUID) -> Sequence[ModelT]:
        statement = (
            self.select(include_archived=True)
            .where(self._owner() == owner_id)
            .order_by(self.model.effective_from, self.model.id)
        )
        return list(self._session.execute(statement).scalars())

    def overlapping(
        self,
        *,
        owner_id: uuid.UUID,
        competency_id: uuid.UUID,
        effective_from: date,
        effective_to: date | None,
        exclude_id: uuid.UUID | None = None,
    ) -> Sequence[ModelT]:
        """Any mapping for the same pair whose interval intersects the proposed one.

        MySQL has no exclusion constraint, so this in-transaction query plus the same-start
        unique key is what prevents two overlapping active requirements (JA §9.3).
        """
        end = self.model.effective_to
        conditions: list[ColumnElement[bool]] = [
            self._owner() == owner_id,
            self.model.competency_id == competency_id,
            or_(end.is_(None), end > effective_from),
        ]
        if effective_to is not None:
            conditions.append(self.model.effective_from < effective_to)
        statement = self.select(include_archived=True).where(*conditions)
        if exclude_id is not None:
            statement = statement.where(self.model.id != exclude_id)
        return list(self._session.execute(statement).scalars())

    def active_mappings_for_competency(self, competency_id: uuid.UUID, *, as_of: date) -> int:
        statement = (
            select(func.count())
            .select_from(self.model)
            .where(
                self.model.organization_id == self.organization_id,
                self.model.competency_id == competency_id,
                self.model.archived_at.is_(None),
                self.model.effective_from <= as_of,
                or_(self.model.effective_to.is_(None), self.model.effective_to > as_of),
            )
        )
        return int(self._session.execute(statement).scalar() or 0)


class JobCompetencyRepository(_CompetencyMappingRepository[JobCompetency]):
    model = JobCompetency
    owner_column = "job_id"

    def active_for_jobs(
        self, job_ids: Sequence[uuid.UUID], *, as_of: date
    ) -> dict[uuid.UUID, list[JobCompetency]]:
        """Batched variant, so listing N jobs with their requirements is one query."""
        if not job_ids:
            return {}
        statement = (
            self.select()
            .where(
                JobCompetency.job_id.in_(set(job_ids)),
                JobCompetency.effective_from <= as_of,
                or_(JobCompetency.effective_to.is_(None), JobCompetency.effective_to > as_of),
            )
            .order_by(JobCompetency.display_order, JobCompetency.id)
        )
        grouped: dict[uuid.UUID, list[JobCompetency]] = {}
        for row in self._session.execute(statement).scalars():
            grouped.setdefault(row.job_id, []).append(row)
        return grouped


class PositionCompetencyRepository(_CompetencyMappingRepository[PositionCompetency]):
    model = PositionCompetency
    owner_column = "position_id"


class PositionStatusHistoryRepository(OrgScopedRepository[PositionStatusHistory]):
    """Append-only position lifecycle history (JA §10.7)."""

    model = PositionStatusHistory

    def list_for_position(
        self,
        position_id: uuid.UUID,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Sequence[PositionStatusHistory]:
        statement = self.select(include_archived=True).where(
            PositionStatusHistory.position_id == position_id
        )
        if since is not None:
            statement = statement.where(PositionStatusHistory.effective_at >= since)
        if until is not None:
            statement = statement.where(PositionStatusHistory.effective_at <= until)
        statement = statement.order_by(PositionStatusHistory.effective_at, PositionStatusHistory.id)
        return list(self._session.execute(statement).scalars())


class PositionRepository(OrgScopedRepository[Position]):
    """Positions, and the grouped headcount reports derived from them (JA §7, §8.10)."""

    model = Position

    def get_by_code(self, code: str) -> Position | None:
        statement = self.select(include_archived=True).where(
            Position.position_code_normalized == code.strip().upper()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def lock_many(self, position_ids: Sequence[uuid.UUID]) -> list[Position]:
        """Lock position rows in ascending id order (JA §8.9 lock order step 2)."""
        if not position_ids:
            return []
        ordered = sorted(set(position_ids), key=lambda value: value.bytes)
        statement = (
            self.select(include_archived=True)
            .where(Position.id.in_(ordered))
            .order_by(Position.id)
            .with_for_update()
        )
        rows = list(self._session.execute(statement).scalars())
        if len(rows) != len(ordered):
            raise ResourceNotFound("position")
        return rows

    def search_page(
        self,
        *,
        query: str | None = None,
        position_status: Sequence[str] | None = None,
        job_id: uuid.UUID | None = None,
        department_id: uuid.UUID | None = None,
        sub_department_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        grade_id: uuid.UUID | None = None,
        reports_to_position_ids: Sequence[uuid.UUID] | None = None,
        include_archived: bool = False,
        sort_field: str = "position_code",
        direction: str = "asc",
        after_value: Any | None = None,
        after_id: uuid.UUID | None = None,
        limit: int = 25,
        with_total: bool = False,
    ) -> tuple[list[Position], bool, int | None]:
        conditions: list[ColumnElement[bool]] = []
        if query:
            escaped = escape_like_pattern(query)
            conditions.append(
                or_(
                    Position.position_code_normalized.like(f"{escaped.upper()}%"),
                    Position.position_title.like(f"%{escaped}%"),
                )
            )
        if position_status:
            conditions.append(Position.position_status.in_(position_status))
        if job_id is not None:
            conditions.append(Position.job_id == job_id)
        if department_id is not None:
            conditions.append(Position.department_id == department_id)
        if sub_department_id is not None:
            conditions.append(Position.sub_department_id == sub_department_id)
        if branch_id is not None:
            conditions.append(Position.branch_id == branch_id)
        if grade_id is not None:
            conditions.append(Position.grade_id == grade_id)
        if reports_to_position_ids is not None:
            conditions.append(Position.reports_to_position_id.in_(reports_to_position_ids))

        columns = {
            "position_code": Position.position_code_normalized,
            "position_title": Position.position_title,
            "position_status": Position.position_status,
            "authorized_fte": Position.authorized_fte,
            "created_at": Position.created_at,
        }
        return _paginate(
            self,
            Position,
            conditions=conditions,
            columns=columns,
            sort_field=sort_field,
            default_sort="position_code",
            direction=direction,
            after_value=after_value,
            after_id=after_id,
            limit=limit,
            with_total=with_total,
            include_archived=include_archived,
        )

    def direct_children(self, position_id: uuid.UUID) -> list[uuid.UUID]:
        statement = select(Position.id).where(
            Position.organization_id == self.organization_id,
            Position.reports_to_position_id == position_id,
        )
        return list(self._session.execute(statement).scalars())

    def subtree_ids(self, position_id: uuid.UUID, *, max_depth: int = 20) -> list[uuid.UUID]:
        """Breadth-first descent, bounded by ``max_depth`` (JA §17.4 ``include_indirect``)."""
        frontier = {position_id}
        found: set[uuid.UUID] = {position_id}
        for _ in range(max_depth):
            if not frontier:
                break
            statement = select(Position.id).where(
                Position.organization_id == self.organization_id,
                Position.reports_to_position_id.in_(frontier),
            )
            frontier = set(self._session.execute(statement).scalars()) - found
            found |= frontier
        return list(found)

    def reporting_chain(self, start_position_id: uuid.UUID, *, max_depth: int) -> list[uuid.UUID]:
        """Walk up ``reports_to_position_id``, returning the ids visited.

        Stops at ``max_depth`` so a pre-existing cycle cannot spin forever; the caller reads
        a chain of exactly ``max_depth`` as "too deep" and rejects the edit (JA §7.5).
        """
        chain: list[uuid.UUID] = []
        current: uuid.UUID | None = start_position_id
        seen: set[uuid.UUID] = set()
        for _ in range(max_depth):
            if current is None or current in seen:
                break
            seen.add(current)
            chain.append(current)
            statement = select(Position.reports_to_position_id).where(
                Position.organization_id == self.organization_id, Position.id == current
            )
            current = self._session.execute(statement).scalar_one_or_none()
        return chain

    def matching_plan_scope(
        self, plan: HeadcountPlan, *, statuses: Sequence[str] | None = None
    ) -> Select[tuple[Position]]:
        """A tenant-filtered SELECT of the positions a plan covers (JA §8.6).

        ``DEPARTMENT`` needs no sub-department clause: a position in a sub-department also
        carries its parent ``department_id``, so equality already includes them.
        """
        statement = self.select().where(*_plan_scope_conditions(plan))
        if statuses:
            statement = statement.where(Position.position_status.in_(statuses))
        return statement

    def establishment_for_plan(
        self,
        plan: HeadcountPlan,
        *,
        statuses: Sequence[str],
        exclude_position_id: uuid.UUID | None = None,
    ) -> Occupancy:
        """``SUM(authorized_fte)`` and ``SUM(max_incumbents)`` of the positions a plan covers.

        ``exclude_position_id`` lets a capacity check leave out the position being changed,
        so raising one position's FTE is measured against the *others* plus its new value
        rather than against its own old value twice (JA §8.4).
        """
        conditions = list(_plan_scope_conditions(plan))
        conditions.append(Position.position_status.in_(statuses))
        if exclude_position_id is not None:
            conditions.append(Position.id != exclude_position_id)
        statement = select(
            func.coalesce(func.sum(Position.authorized_fte), 0),
            func.coalesce(func.sum(Position.max_incumbents), 0),
        ).where(
            Position.organization_id == self.organization_id,
            Position.archived_at.is_(None),
            *conditions,
        )
        row = self._session.execute(statement).one()
        return Occupancy(_as_decimal(row[0]), int(row[1] or 0))

    def headcount_summary(  # noqa: PLR0912 - one branch per JA §17.5 group_by dimension
        self,
        *,
        group_by: str,
        as_of: date,
        department_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        grade_id: uuid.UUID | None = None,
        position_id: uuid.UUID | None = None,
        include_draft: bool = False,
    ) -> list[SummaryRow]:
        """The whole JA §8.10 summary in one statement.

        Positions are left-joined against two pre-aggregated subqueries — occupancy and
        reservations — and then grouped. One query, no per-position round trip, and the same
        §8.3 predicates the capacity check uses.
        """
        occupied = (
            select(
                EmployeePositionAssignment.position_id.label("position_id"),
                func.coalesce(func.sum(EmployeePositionAssignment.fte), 0).label("fte"),
                func.count().label("headcount"),
            )
            .where(
                EmployeePositionAssignment.organization_id == self.organization_id,
                *_occupied_predicates(as_of),
            )
            .group_by(EmployeePositionAssignment.position_id)
            .subquery()
        )
        reserved = (
            select(
                HeadcountReservation.position_id.label("position_id"),
                func.coalesce(func.sum(HeadcountReservation.reserved_fte), 0).label("fte"),
                func.coalesce(func.sum(HeadcountReservation.reserved_headcount), 0).label(
                    "headcount"
                ),
            )
            .where(
                HeadcountReservation.organization_id == self.organization_id,
                *_reserved_predicates(as_of),
            )
            .group_by(HeadcountReservation.position_id)
            .subquery()
        )

        # The grouping key and its human label, per JA §17.5 `group_by`. Typed loosely
        # because the two expressions are a column in some branches and a literal NULL in
        # others, which is exactly what an ungrouped organization-wide total needs.
        key: Any
        label: Any
        if group_by == "DEPARTMENT":
            key, label = Position.department_id, Department.name
        elif group_by == "BRANCH":
            key, label = Position.branch_id, literal(None)
        elif group_by == "JOB":
            key, label = Position.job_id, Job.title
        elif group_by == "GRADE":
            key, label = Position.grade_id, Grade.name
        elif group_by == "POSITION":
            key, label = Position.id, Position.position_title
        else:
            key, label = literal(None), literal(None)

        statuses = [PositionStatus.OPEN.value, PositionStatus.FROZEN.value]
        if include_draft:
            statuses.append(PositionStatus.DRAFT.value)

        statement = (
            select(
                key.label("group_id"),
                label.label("group_label"),
                func.coalesce(func.sum(Position.authorized_fte), 0),
                func.coalesce(func.sum(Position.max_incumbents), 0),
                func.coalesce(func.sum(occupied.c.fte), 0),
                func.coalesce(func.sum(occupied.c.headcount), 0),
                func.coalesce(func.sum(reserved.c.fte), 0),
                func.coalesce(func.sum(reserved.c.headcount), 0),
                func.count(Position.id),
                func.coalesce(
                    func.sum(
                        case((Position.position_status == PositionStatus.OPEN.value, 1), else_=0)
                    ),
                    0,
                ),
            )
            .select_from(Position)
            .outerjoin(occupied, occupied.c.position_id == Position.id)
            .outerjoin(reserved, reserved.c.position_id == Position.id)
            .where(
                Position.organization_id == self.organization_id,
                Position.archived_at.is_(None),
                Position.position_status.in_(statuses),
            )
        )
        if group_by == "DEPARTMENT":
            statement = statement.outerjoin(
                Department,
                and_(
                    Department.id == Position.department_id,
                    Department.organization_id == self.organization_id,
                ),
            )
        elif group_by == "JOB":
            statement = statement.outerjoin(
                Job,
                and_(Job.id == Position.job_id, Job.organization_id == self.organization_id),
            )
        elif group_by == "GRADE":
            statement = statement.outerjoin(
                Grade,
                and_(Grade.id == Position.grade_id, Grade.organization_id == self.organization_id),
            )

        if department_id is not None:
            statement = statement.where(Position.department_id == department_id)
        if branch_id is not None:
            statement = statement.where(Position.branch_id == branch_id)
        if job_id is not None:
            statement = statement.where(Position.job_id == job_id)
        if grade_id is not None:
            statement = statement.where(Position.grade_id == grade_id)
        if position_id is not None:
            statement = statement.where(Position.id == position_id)

        if group_by != "ORGANIZATION":
            statement = statement.group_by(key, label).order_by(label, key)

        rows = self._session.execute(statement).all()
        return [
            SummaryRow(
                group_key=group_by,
                group_id=row[0],
                group_label=row[1],
                authorized_fte=_as_decimal(row[2]),
                authorized_headcount=int(row[3] or 0),
                occupied_fte=_as_decimal(row[4]),
                occupied_headcount=int(row[5] or 0),
                reserved_fte=_as_decimal(row[6]),
                reserved_headcount=int(row[7] or 0),
                position_count=int(row[8] or 0),
                open_position_count=int(row[9] or 0),
            )
            for row in rows
        ]

    def over_capacity_positions(self, *, as_of: date) -> list[tuple[Position, Occupancy]]:
        """Positions whose occupancy exceeds their authorization (JA §17.5 reconciliation).

        A non-empty result is a defect, not a business state: the capacity check is supposed
        to make this impossible. The nightly checker reports it as P1 and does not repair it.
        """
        occupied = (
            select(
                EmployeePositionAssignment.position_id.label("position_id"),
                func.coalesce(func.sum(EmployeePositionAssignment.fte), 0).label("fte"),
                func.count().label("headcount"),
            )
            .where(
                EmployeePositionAssignment.organization_id == self.organization_id,
                *_occupied_predicates(as_of),
            )
            .group_by(EmployeePositionAssignment.position_id)
            .subquery()
        )
        statement = (
            select(Position, occupied.c.fte, occupied.c.headcount)
            .join(occupied, occupied.c.position_id == Position.id)
            .where(
                Position.organization_id == self.organization_id,
                Position.archived_at.is_(None),
                or_(
                    occupied.c.fte > Position.authorized_fte,
                    occupied.c.headcount > Position.max_incumbents,
                ),
            )
        )
        return [
            (row[0], Occupancy(_as_decimal(row[1]), int(row[2] or 0)))
            for row in self._session.execute(statement).all()
        ]

    def self_reporting_positions(self) -> Sequence[Position]:
        statement = self.select(include_archived=True).where(
            Position.reports_to_position_id == Position.id
        )
        return list(self._session.execute(statement).scalars())

    def count_for_job(self, job_id: uuid.UUID, *, statuses: Sequence[str] | None = None) -> int:
        statement = (
            select(func.count())
            .select_from(Position)
            .where(
                Position.organization_id == self.organization_id,
                Position.job_id == job_id,
                Position.archived_at.is_(None),
            )
        )
        if statuses:
            statement = statement.where(Position.position_status.in_(statuses))
        return int(self._session.execute(statement).scalar() or 0)

    def count_for_grade(self, grade_id: uuid.UUID) -> int:
        statement = (
            select(func.count())
            .select_from(Position)
            .where(
                Position.organization_id == self.organization_id,
                Position.grade_id == grade_id,
                Position.archived_at.is_(None),
            )
        )
        return int(self._session.execute(statement).scalar() or 0)

    def load_many(self, position_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Position]:
        if not position_ids:
            return {}
        statement = self.select(include_archived=True).where(Position.id.in_(set(position_ids)))
        return {row.id: row for row in self._session.execute(statement).scalars()}


class PositionAssignmentRepository(OrgScopedRepository[EmployeePositionAssignment]):
    """Occupancy — the authoritative record of who fills which position (JA §10.8)."""

    model = EmployeePositionAssignment

    def occupancy_for(
        self,
        position_id: uuid.UUID,
        *,
        as_of: date,
        exclude_assignment_id: uuid.UUID | None = None,
    ) -> Occupancy:
        """JA §8.4 ``OccupiedFTE``/``OccupiedHeadcount`` for one position.

        ``exclude_assignment_id`` implements the §8.4 rule that an assignment being *changed*
        is removed from occupancy before the new FTE is applied — otherwise reducing 1.00 FTE
        to 0.50 would be rejected against its own occupancy.
        """
        statement = select(
            func.coalesce(func.sum(EmployeePositionAssignment.fte), 0), func.count()
        ).where(
            EmployeePositionAssignment.organization_id == self.organization_id,
            EmployeePositionAssignment.position_id == position_id,
            *_occupied_predicates(as_of),
        )
        if exclude_assignment_id is not None:
            statement = statement.where(EmployeePositionAssignment.id != exclude_assignment_id)
        row = self._session.execute(statement).one()
        return Occupancy(_as_decimal(row[0]), int(row[1] or 0))

    def occupancy_map(
        self, position_ids: Sequence[uuid.UUID], *, as_of: date
    ) -> dict[uuid.UUID, Occupancy]:
        """Occupancy for many positions in one grouped query (no N+1 in list endpoints)."""
        if not position_ids:
            return {}
        statement = (
            select(
                EmployeePositionAssignment.position_id,
                func.coalesce(func.sum(EmployeePositionAssignment.fte), 0),
                func.count(),
            )
            .where(
                EmployeePositionAssignment.organization_id == self.organization_id,
                EmployeePositionAssignment.position_id.in_(set(position_ids)),
                *_occupied_predicates(as_of),
            )
            .group_by(EmployeePositionAssignment.position_id)
        )
        return {
            row[0]: Occupancy(_as_decimal(row[1]), int(row[2] or 0))
            for row in self._session.execute(statement).all()
        }

    def lock_for_position(self, position_id: uuid.UUID) -> Sequence[EmployeePositionAssignment]:
        """Lock the assignment rows of one position (JA §8.9 lock order step 4)."""
        statement = (
            self.select(include_archived=True)
            .where(
                EmployeePositionAssignment.position_id == position_id,
                EmployeePositionAssignment.assignment_status.in_(OCCUPYING_ASSIGNMENT_STATUSES),
            )
            .order_by(EmployeePositionAssignment.id)
            .with_for_update()
        )
        return list(self._session.execute(statement).scalars())

    def active_for_position(
        self, position_id: uuid.UUID, *, as_of: date
    ) -> Sequence[EmployeePositionAssignment]:
        """The incumbents of a position on ``as_of``, in a deterministic order.

        Used to default a reporting manager from the parent position's incumbent (JA §7.5);
        ordering by type then start date keeps that default from depending on row order.
        """
        statement = (
            self.select(include_archived=True)
            .where(
                EmployeePositionAssignment.position_id == position_id,
                *_occupied_predicates(as_of),
            )
            .order_by(
                EmployeePositionAssignment.assignment_type,
                EmployeePositionAssignment.effective_from,
                EmployeePositionAssignment.id,
            )
        )
        return list(self._session.execute(statement).scalars())

    def all_for_position(self, position_id: uuid.UUID) -> Sequence[EmployeePositionAssignment]:
        """Every assignment row a position has ever had, in any status.

        Archiving looks at this rather than at open assignments only: JA §7.8 blocks the
        archive of a position that carries assignment history, because that history is what
        makes the seat auditable.
        """
        statement = (
            self.select(include_archived=True)
            .where(EmployeePositionAssignment.position_id == position_id)
            .order_by(EmployeePositionAssignment.effective_from, EmployeePositionAssignment.id)
        )
        return list(self._session.execute(statement).scalars())

    def occupying_for_position(
        self, position_id: uuid.UUID
    ) -> Sequence[EmployeePositionAssignment]:
        """``ACTIVE`` or ``SCHEDULED`` rows — what blocks a close or archive (JA §7.8)."""
        statement = self.select(include_archived=True).where(
            EmployeePositionAssignment.position_id == position_id,
            EmployeePositionAssignment.assignment_status.in_(OCCUPYING_ASSIGNMENT_STATUSES),
        )
        return list(self._session.execute(statement).scalars())

    def list_for_employee(
        self,
        employee_id: uuid.UUID,
        *,
        status: Sequence[str] | None = None,
        as_of: date | None = None,
    ) -> Sequence[EmployeePositionAssignment]:
        statement = self.select(include_archived=True).where(
            EmployeePositionAssignment.employee_id == employee_id
        )
        if status:
            statement = statement.where(EmployeePositionAssignment.assignment_status.in_(status))
        if as_of is not None:
            statement = statement.where(
                EmployeePositionAssignment.effective_from <= as_of,
                or_(
                    EmployeePositionAssignment.effective_to.is_(None),
                    EmployeePositionAssignment.effective_to > as_of,
                ),
            )
        statement = statement.order_by(
            EmployeePositionAssignment.effective_from.desc(), EmployeePositionAssignment.id
        )
        return list(self._session.execute(statement).scalars())

    def active_primary_for_employee(
        self, employee_id: uuid.UUID
    ) -> EmployeePositionAssignment | None:
        statement = self.select(include_archived=True).where(
            EmployeePositionAssignment.employee_id == employee_id,
            EmployeePositionAssignment.assignment_status == AssignmentStatus.ACTIVE.value,
            EmployeePositionAssignment.assignment_type == AssignmentType.PRIMARY.value,
        )
        return self._session.execute(statement).scalar_one_or_none()

    def open_for_employee(self, employee_id: uuid.UUID) -> Sequence[EmployeePositionAssignment]:
        """Every ``ACTIVE`` or ``SCHEDULED`` assignment the employee holds."""
        statement = (
            self.select(include_archived=True)
            .where(
                EmployeePositionAssignment.employee_id == employee_id,
                EmployeePositionAssignment.assignment_status.in_(OCCUPYING_ASSIGNMENT_STATUSES),
            )
            .order_by(EmployeePositionAssignment.effective_from, EmployeePositionAssignment.id)
        )
        return list(self._session.execute(statement).scalars())

    def overlapping_for_employee(
        self,
        *,
        employee_id: uuid.UUID,
        position_id: uuid.UUID,
        effective_from: date,
        effective_to: date | None,
        exclude_id: uuid.UUID | None = None,
    ) -> Sequence[EmployeePositionAssignment]:
        """Open assignments to the *same position* whose interval intersects the new one.

        Holding two positions at once is legitimate (``ACTING``/``SECONDARY``, JA §10.8), so
        the overlap rule is per position, plus the separate one-active-``PRIMARY`` rule.
        """
        end = EmployeePositionAssignment.effective_to
        conditions: list[ColumnElement[bool]] = [
            EmployeePositionAssignment.employee_id == employee_id,
            EmployeePositionAssignment.position_id == position_id,
            EmployeePositionAssignment.assignment_status.in_(OCCUPYING_ASSIGNMENT_STATUSES),
            or_(end.is_(None), end > effective_from),
        ]
        if effective_to is not None:
            conditions.append(EmployeePositionAssignment.effective_from < effective_to)
        statement = self.select(include_archived=True).where(*conditions)
        if exclude_id is not None:
            statement = statement.where(EmployeePositionAssignment.id != exclude_id)
        return list(self._session.execute(statement).scalars())

    def lock_for_employee(self, employee_id: uuid.UUID) -> Sequence[EmployeePositionAssignment]:
        statement = (
            self.select(include_archived=True)
            .where(
                EmployeePositionAssignment.employee_id == employee_id,
                EmployeePositionAssignment.assignment_status.in_(OCCUPYING_ASSIGNMENT_STATUSES),
            )
            .order_by(EmployeePositionAssignment.id)
            .with_for_update()
        )
        return list(self._session.execute(statement).scalars())

    def due_to_activate(self, *, as_of: date) -> Sequence[EmployeePositionAssignment]:
        """``SCHEDULED`` rows whose effective date has arrived (JA §15.3)."""
        statement = self.select(include_archived=True).where(
            EmployeePositionAssignment.assignment_status == AssignmentStatus.SCHEDULED.value,
            EmployeePositionAssignment.effective_from <= as_of,
            or_(
                EmployeePositionAssignment.effective_to.is_(None),
                EmployeePositionAssignment.effective_to > as_of,
            ),
        )
        return list(self._session.execute(statement).scalars())

    def active_for_separated_employees(self) -> Sequence[EmployeePositionAssignment]:
        """Assignments still active for an employee whose employment has ended (JA §19.11)."""
        statement = (
            select(EmployeePositionAssignment)
            .join(
                Employee,
                and_(
                    Employee.id == EmployeePositionAssignment.employee_id,
                    Employee.organization_id == EmployeePositionAssignment.organization_id,
                ),
            )
            .where(
                EmployeePositionAssignment.organization_id == self.organization_id,
                EmployeePositionAssignment.assignment_status.in_(OCCUPYING_ASSIGNMENT_STATUSES),
                Employee.status.in_([status.value for status in SEPARATED_STATUSES]),
            )
        )
        return list(self._session.execute(statement).scalars())

    def overlapping_pairs(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """Pairs of open assignments for the same employee and position that overlap.

        The nightly backstop for the invariant MySQL cannot declare (JA §8.9).
        """
        other = EmployeePositionAssignment.__table__.alias("other")
        statement = (
            select(EmployeePositionAssignment.id, other.c.id)
            .select_from(EmployeePositionAssignment)
            .join(
                other,
                and_(
                    other.c.organization_id == EmployeePositionAssignment.organization_id,
                    other.c.employee_id == EmployeePositionAssignment.employee_id,
                    other.c.position_id == EmployeePositionAssignment.position_id,
                    other.c.id > EmployeePositionAssignment.id,
                ),
            )
            .where(
                EmployeePositionAssignment.organization_id == self.organization_id,
                EmployeePositionAssignment.assignment_status.in_(OCCUPYING_ASSIGNMENT_STATUSES),
                other.c.assignment_status.in_(OCCUPYING_ASSIGNMENT_STATUSES),
                or_(
                    other.c.effective_to.is_(None),
                    other.c.effective_to > EmployeePositionAssignment.effective_from,
                ),
                or_(
                    EmployeePositionAssignment.effective_to.is_(None),
                    EmployeePositionAssignment.effective_to > other.c.effective_from,
                ),
            )
        )
        return [(row[0], row[1]) for row in self._session.execute(statement).all()]


class HeadcountPlanRepository(OrgScopedRepository[HeadcountPlan]):
    """Scoped establishment caps (JA §8.6)."""

    model = HeadcountPlan

    def get_by_code(self, code: str) -> HeadcountPlan | None:
        statement = self.select(include_archived=True).where(
            HeadcountPlan.plan_code_normalized == code.strip().upper()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def list_plans(
        self,
        *,
        scope_type: str | None = None,
        scope_id: uuid.UUID | None = None,
        plan_status: Sequence[str] | None = None,
        effective_on: date | None = None,
        include_archived: bool = False,
        sort_field: str = "effective_from",
        direction: str = "desc",
        after_value: Any | None = None,
        after_id: uuid.UUID | None = None,
        limit: int = 25,
        with_total: bool = False,
    ) -> tuple[list[HeadcountPlan], bool, int | None]:
        conditions: list[ColumnElement[bool]] = []
        if scope_type is not None:
            conditions.append(HeadcountPlan.scope_type == scope_type)
        if scope_id is not None:
            conditions.append(
                or_(
                    HeadcountPlan.department_id == scope_id,
                    HeadcountPlan.branch_id == scope_id,
                    HeadcountPlan.work_location_id == scope_id,
                    HeadcountPlan.job_id == scope_id,
                    HeadcountPlan.grade_id == scope_id,
                )
            )
        if plan_status:
            conditions.append(HeadcountPlan.plan_status.in_(plan_status))
        elif not include_archived:
            conditions.append(HeadcountPlan.plan_status != HeadcountPlanStatus.ARCHIVED.value)
        if effective_on is not None:
            conditions.append(HeadcountPlan.effective_from <= effective_on)
            conditions.append(
                or_(
                    HeadcountPlan.effective_to.is_(None),
                    HeadcountPlan.effective_to > effective_on,
                )
            )
        columns = {
            "plan_code": HeadcountPlan.plan_code_normalized,
            "name": HeadcountPlan.name,
            "effective_from": HeadcountPlan.effective_from,
            "created_at": HeadcountPlan.created_at,
        }
        return _paginate(
            self,
            HeadcountPlan,
            conditions=conditions,
            columns=columns,
            sort_field=sort_field,
            default_sort="effective_from",
            direction=direction,
            after_value=after_value,
            after_id=after_id,
            limit=limit,
            with_total=with_total,
            include_archived=True,
        )

    def overlapping_active(
        self,
        *,
        scope_key: str,
        effective_from: date,
        effective_to: date | None,
        exclude_id: uuid.UUID | None = None,
    ) -> Sequence[HeadcountPlan]:
        end = HeadcountPlan.effective_to
        conditions: list[ColumnElement[bool]] = [
            HeadcountPlan.scope_key == scope_key,
            HeadcountPlan.plan_status == HeadcountPlanStatus.ACTIVE.value,
            or_(end.is_(None), end > effective_from),
        ]
        if effective_to is not None:
            conditions.append(HeadcountPlan.effective_from < effective_to)
        statement = self.select(include_archived=True).where(*conditions)
        if exclude_id is not None:
            statement = statement.where(HeadcountPlan.id != exclude_id)
        return list(self._session.execute(statement).scalars())

    def active_plans_for_position(self, position: Position, *, as_of: date) -> list[HeadcountPlan]:
        """Every ACTIVE plan whose scope covers this position on ``as_of`` (JA §8.6).

        Matching is independent per plan and every match must be satisfied, so an
        organization plan and a department plan both apply and the tightest one decides.
        """
        scope_matches = [HeadcountPlan.scope_type == HeadcountScopeType.ORGANIZATION.value]
        scope_matches.append(
            and_(
                HeadcountPlan.scope_type == HeadcountScopeType.DEPARTMENT.value,
                HeadcountPlan.department_id == position.department_id,
            )
        )
        if position.branch_id is not None:
            scope_matches.append(
                and_(
                    HeadcountPlan.scope_type == HeadcountScopeType.BRANCH.value,
                    HeadcountPlan.branch_id == position.branch_id,
                )
            )
        if position.work_location_id is not None:
            scope_matches.append(
                and_(
                    HeadcountPlan.scope_type == HeadcountScopeType.WORK_LOCATION.value,
                    HeadcountPlan.work_location_id == position.work_location_id,
                )
            )
        scope_matches.append(
            and_(
                HeadcountPlan.scope_type == HeadcountScopeType.JOB.value,
                HeadcountPlan.job_id == position.job_id,
            )
        )
        if position.grade_id is not None:
            scope_matches.append(
                and_(
                    HeadcountPlan.scope_type == HeadcountScopeType.GRADE.value,
                    HeadcountPlan.grade_id == position.grade_id,
                )
            )
        statement = (
            self.select(include_archived=True)
            .where(
                HeadcountPlan.plan_status == HeadcountPlanStatus.ACTIVE.value,
                HeadcountPlan.effective_from <= as_of,
                or_(HeadcountPlan.effective_to.is_(None), HeadcountPlan.effective_to > as_of),
                or_(*scope_matches),
            )
            .order_by(HeadcountPlan.id)
        )
        return list(self._session.execute(statement).scalars())

    def lock(self, plan_id: uuid.UUID) -> HeadcountPlan:
        statement = (
            self.select(include_archived=True).where(HeadcountPlan.id == plan_id).with_for_update()
        )
        row = self._session.execute(statement).scalar_one_or_none()
        if row is None:
            raise ResourceNotFound("headcount plan")
        return row

    def lock_many(self, plan_ids: Sequence[uuid.UUID]) -> Sequence[HeadcountPlan]:
        """JA §8.9 lock order step 3: plans in ascending id order."""
        if not plan_ids:
            return []
        statement = (
            self.select(include_archived=True)
            .where(HeadcountPlan.id.in_(sorted(set(plan_ids), key=lambda value: value.bytes)))
            .order_by(HeadcountPlan.id)
            .with_for_update()
        )
        return list(self._session.execute(statement).scalars())

    def active_plans(self, *, as_of: date) -> Sequence[HeadcountPlan]:
        statement = self.select(include_archived=True).where(
            HeadcountPlan.plan_status == HeadcountPlanStatus.ACTIVE.value,
            HeadcountPlan.effective_from <= as_of,
            or_(HeadcountPlan.effective_to.is_(None), HeadcountPlan.effective_to > as_of),
        )
        return list(self._session.execute(statement).scalars())

    def overlapping_active_pairs(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
        other = HeadcountPlan.__table__.alias("other_plan")
        statement = (
            select(HeadcountPlan.id, other.c.id)
            .select_from(HeadcountPlan)
            .join(
                other,
                and_(
                    other.c.organization_id == HeadcountPlan.organization_id,
                    other.c.scope_key == HeadcountPlan.scope_key,
                    other.c.id > HeadcountPlan.id,
                ),
            )
            .where(
                HeadcountPlan.organization_id == self.organization_id,
                HeadcountPlan.plan_status == HeadcountPlanStatus.ACTIVE.value,
                other.c.plan_status == HeadcountPlanStatus.ACTIVE.value,
                or_(
                    other.c.effective_to.is_(None),
                    other.c.effective_to > HeadcountPlan.effective_from,
                ),
                or_(
                    HeadcountPlan.effective_to.is_(None),
                    HeadcountPlan.effective_to > other.c.effective_from,
                ),
            )
        )
        return [(row[0], row[1]) for row in self._session.execute(statement).all()]


class HeadcountChangeRequestRepository(OrgScopedRepository[HeadcountChangeRequest]):
    """The maker-checker request aggregate (JA §8.7)."""

    model = HeadcountChangeRequest

    def list_requests(
        self,
        *,
        request_status: Sequence[str] | None = None,
        target_type: str | None = None,
        position_id: uuid.UUID | None = None,
        headcount_plan_id: uuid.UUID | None = None,
        sort_field: str = "created_at",
        direction: str = "desc",
        after_value: Any | None = None,
        after_id: uuid.UUID | None = None,
        limit: int = 25,
        with_total: bool = False,
    ) -> tuple[list[HeadcountChangeRequest], bool, int | None]:
        conditions: list[ColumnElement[bool]] = []
        if request_status:
            conditions.append(HeadcountChangeRequest.request_status.in_(request_status))
        if target_type == "POSITION":
            conditions.append(HeadcountChangeRequest.position_id.is_not(None))
        elif target_type == "HEADCOUNT_PLAN":
            conditions.append(HeadcountChangeRequest.headcount_plan_id.is_not(None))
        if position_id is not None:
            conditions.append(HeadcountChangeRequest.position_id == position_id)
        if headcount_plan_id is not None:
            conditions.append(HeadcountChangeRequest.headcount_plan_id == headcount_plan_id)
        columns = {
            "created_at": HeadcountChangeRequest.created_at,
            "request_status": HeadcountChangeRequest.request_status,
        }
        return _paginate(
            self,
            HeadcountChangeRequest,
            conditions=conditions,
            columns=columns,
            sort_field=sort_field,
            default_sort="created_at",
            direction=direction,
            after_value=after_value,
            after_id=after_id,
            limit=limit,
            with_total=with_total,
            include_archived=True,
        )

    def open_for_position(self, position_id: uuid.UUID) -> Sequence[HeadcountChangeRequest]:
        statement = self.select(include_archived=True).where(
            HeadcountChangeRequest.position_id == position_id,
            HeadcountChangeRequest.request_status.in_(
                [HeadcountRequestStatus.DRAFT.value, HeadcountRequestStatus.PENDING.value]
            ),
        )
        return list(self._session.execute(statement).scalars())

    def open_for_plan(self, plan_id: uuid.UUID) -> Sequence[HeadcountChangeRequest]:
        statement = self.select(include_archived=True).where(
            HeadcountChangeRequest.headcount_plan_id == plan_id,
            HeadcountChangeRequest.request_status.in_(
                [HeadcountRequestStatus.DRAFT.value, HeadcountRequestStatus.PENDING.value]
            ),
        )
        return list(self._session.execute(statement).scalars())

    def approved_unapplied_for_position(
        self, position_id: uuid.UUID
    ) -> Sequence[HeadcountChangeRequest]:
        """Approved requests for a position (JA §8.7 approval-to-open reference)."""
        statement = (
            self.select(include_archived=True)
            .where(
                HeadcountChangeRequest.position_id == position_id,
                HeadcountChangeRequest.request_status == HeadcountRequestStatus.APPROVED.value,
            )
            .order_by(HeadcountChangeRequest.decided_at.desc())
        )
        return list(self._session.execute(statement).scalars())

    def lock(self, request_id: uuid.UUID) -> HeadcountChangeRequest:
        statement = (
            self.select(include_archived=True)
            .where(HeadcountChangeRequest.id == request_id)
            .with_for_update()
        )
        row = self._session.execute(statement).scalar_one_or_none()
        if row is None:
            raise ResourceNotFound("headcount change request")
        return row

    def list_for_position_history(self, position_id: uuid.UUID) -> Sequence[HeadcountChangeRequest]:
        statement = (
            self.select(include_archived=True)
            .where(HeadcountChangeRequest.position_id == position_id)
            .order_by(HeadcountChangeRequest.created_at, HeadcountChangeRequest.id)
        )
        return list(self._session.execute(statement).scalars())


class HeadcountReservationRepository(OrgScopedRepository[HeadcountReservation]):
    """Optional non-employee capacity commitments (JA §8.8), off by default."""

    model = HeadcountReservation

    def reserved_for(self, position_id: uuid.UUID, *, as_of: date) -> Occupancy:
        statement = select(
            func.coalesce(func.sum(HeadcountReservation.reserved_fte), 0),
            func.coalesce(func.sum(HeadcountReservation.reserved_headcount), 0),
        ).where(
            HeadcountReservation.organization_id == self.organization_id,
            HeadcountReservation.position_id == position_id,
            *_reserved_predicates(as_of),
        )
        row = self._session.execute(statement).one()
        return Occupancy(_as_decimal(row[0]), int(row[1] or 0))

    def reserved_map(
        self, position_ids: Sequence[uuid.UUID], *, as_of: date
    ) -> dict[uuid.UUID, Occupancy]:
        if not position_ids:
            return {}
        statement = (
            select(
                HeadcountReservation.position_id,
                func.coalesce(func.sum(HeadcountReservation.reserved_fte), 0),
                func.coalesce(func.sum(HeadcountReservation.reserved_headcount), 0),
            )
            .where(
                HeadcountReservation.organization_id == self.organization_id,
                HeadcountReservation.position_id.in_(set(position_ids)),
                *_reserved_predicates(as_of),
            )
            .group_by(HeadcountReservation.position_id)
        )
        return {
            row[0]: Occupancy(_as_decimal(row[1]), int(row[2] or 0))
            for row in self._session.execute(statement).all()
        }

    def list_for_position(
        self, position_id: uuid.UUID, *, reservation_status: Sequence[str] | None = None
    ) -> Sequence[HeadcountReservation]:
        statement = self.select(include_archived=True).where(
            HeadcountReservation.position_id == position_id
        )
        if reservation_status:
            statement = statement.where(
                HeadcountReservation.reservation_status.in_(reservation_status)
            )
        statement = statement.order_by(
            HeadcountReservation.effective_from.desc(), HeadcountReservation.id
        )
        return list(self._session.execute(statement).scalars())

    def active_for_position(self, position_id: uuid.UUID) -> Sequence[HeadcountReservation]:
        statement = self.select(include_archived=True).where(
            HeadcountReservation.position_id == position_id,
            HeadcountReservation.reservation_status == ReservationStatus.ACTIVE.value,
        )
        return list(self._session.execute(statement).scalars())

    def lock(self, reservation_id: uuid.UUID) -> HeadcountReservation:
        statement = (
            self.select(include_archived=True)
            .where(HeadcountReservation.id == reservation_id)
            .with_for_update()
        )
        row = self._session.execute(statement).scalar_one_or_none()
        if row is None:
            raise ResourceNotFound("headcount reservation")
        return row

    def due_to_expire(self, *, moment: datetime) -> Sequence[HeadcountReservation]:
        statement = self.select(include_archived=True).where(
            HeadcountReservation.reservation_status == ReservationStatus.ACTIVE.value,
            HeadcountReservation.expires_at.is_not(None),
            HeadcountReservation.expires_at <= moment,
        )
        return list(self._session.execute(statement).scalars())


# -------------------------------------------------------------------------------------
# Shared predicates and pagination
# -------------------------------------------------------------------------------------


def _occupied_predicates(as_of: date) -> tuple[ColumnElement[bool], ...]:
    """JA §8.3 inclusion rule for an assignment. Defined once; every caller reuses it."""
    return (
        EmployeePositionAssignment.assignment_status == AssignmentStatus.ACTIVE.value,
        EmployeePositionAssignment.effective_from <= as_of,
        or_(
            EmployeePositionAssignment.effective_to.is_(None),
            EmployeePositionAssignment.effective_to > as_of,
        ),
    )


def _reserved_predicates(as_of: date) -> tuple[ColumnElement[bool], ...]:
    """JA §8.3 inclusion rule for a reservation."""
    return (
        HeadcountReservation.reservation_status == ReservationStatus.ACTIVE.value,
        HeadcountReservation.effective_from <= as_of,
        or_(
            HeadcountReservation.expires_at.is_(None),
            func.date(HeadcountReservation.expires_at) > as_of,
        ),
    )


def _plan_scope_conditions(plan: HeadcountPlan) -> tuple[ColumnElement[bool], ...]:
    """The positions one plan covers, as predicates on ``positions`` (JA §8.6)."""
    if plan.scope_type == HeadcountScopeType.ORGANIZATION.value:
        return ()
    if plan.scope_type == HeadcountScopeType.DEPARTMENT.value:
        return (Position.department_id == plan.department_id,)
    if plan.scope_type == HeadcountScopeType.BRANCH.value:
        return (Position.branch_id == plan.branch_id,)
    if plan.scope_type == HeadcountScopeType.WORK_LOCATION.value:
        return (Position.work_location_id == plan.work_location_id,)
    if plan.scope_type == HeadcountScopeType.JOB.value:
        return (Position.job_id == plan.job_id,)
    if plan.scope_type == HeadcountScopeType.GRADE.value:
        return (Position.grade_id == plan.grade_id,)
    raise ValueError(f"unknown headcount plan scope: {plan.scope_type!r}")


def _paginate[ModelT: Any](
    repository: OrgScopedRepository[Any],
    model: type[ModelT],
    *,
    conditions: list[ColumnElement[bool]],
    columns: dict[str, Any],
    sort_field: str,
    default_sort: str,
    direction: str,
    after_value: Any | None,
    after_id: uuid.UUID | None,
    limit: int,
    with_total: bool,
    include_archived: bool,
    secondary_order: tuple[Any, ...] = (),
    total_limit: int = 10000,
) -> tuple[list[ModelT], bool, int | None]:
    """Keyset pagination shared by every list in this module (§26.5).

    One page is fetched with ``limit + 1`` rows so "is there a next page" needs no count, and
    ``id`` is always the final tie-breaker so a page boundary is stable even when the sort
    column repeats.
    """
    statement = repository.select(include_archived=include_archived).where(*conditions)
    sort_column = columns.get(sort_field, columns[default_sort])
    if after_value is not None and after_id is not None:
        comparison = sort_column > after_value if direction == "asc" else sort_column < after_value
        statement = statement.where(
            or_(comparison, and_(sort_column == after_value, model.id > after_id))
        )
    ordered = sort_column.asc() if direction == "asc" else sort_column.desc()
    statement = statement.order_by(ordered, *secondary_order, model.id.asc()).limit(limit + 1)
    rows = list(repository.session.execute(statement).scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]

    total: int | None = None
    if with_total:
        count_statement = (
            select(func.count())
            .select_from(model)
            .where(model.organization_id == repository.organization_id, *conditions)
        )
        if not include_archived:
            count_statement = count_statement.where(model.archived_at.is_(None))
        total = int(repository.session.execute(count_statement).scalar() or 0)
        if total > total_limit:
            total = None
    return rows, has_more, total


__all__ = [
    "CompetencyRepository",
    "HeadcountChangeRequestRepository",
    "HeadcountPlanRepository",
    "HeadcountReservationRepository",
    "JobCompetencyRepository",
    "JobFamilyRepository",
    "JobRepository",
    "Occupancy",
    "PositionAssignmentRepository",
    "PositionCompetencyRepository",
    "PositionRepository",
    "PositionStatusHistoryRepository",
    "SummaryRow",
]
