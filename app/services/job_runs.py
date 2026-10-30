"""Shared run bookkeeping for scheduled jobs (addendum 14.2): one runner per organization.

``start`` does the check-and-insert under a lock on the organization's settings row, so a second
runner gets ``JOB_ALREADY_RUNNING``; a RUNNING row with a stale heartbeat is failed and replaced.
Correctness never depends on this: item-level idempotency (ledger keys, unique rows, state checks)
makes any rerun safe.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.actions import AuditAction
from app.audit.events import AuditEvent, ChangeSet
from app.audit.writer import AuditWriter
from app.core.clock import today_in, utc_now
from app.core.context import SYSTEM_ACTOR_NAME
from app.core.errors import JobAlreadyRunning
from app.db.types import uuid7
from app.models.leave import BackgroundJobRun, JobCode
from app.models.organization import Organization, OrganizationSettings
from app.models.platform import AuthProvider, User, UserStatus
from app.repositories.leave import BackgroundJobRunRepository
from app.repositories.organization import OrganizationSettingsRepository

SYSTEM_EMAIL = "system@ems.invalid"
STALE_HEARTBEAT = timedelta(minutes=15)
MAX_FAILURE_DETAILS = 1000


def system_user_id(session: Session) -> uuid.UUID:
    """The non-interactive account scheduled jobs act as (§14.1 "use the system user").

    OIDC with no password and status INACTIVE, so it can never authenticate; it exists only so
    ``created_by`` / ``requested_by`` foreign keys and audit rows have an actor.
    """
    found = session.execute(select(User.id).where(User.email == SYSTEM_EMAIL)).scalar_one_or_none()
    if found is not None:
        return found
    user = User(
        id=uuid7(),
        email=SYSTEM_EMAIL,
        display_name=SYSTEM_ACTOR_NAME,
        auth_provider=AuthProvider.OIDC,
        status=UserStatus.INACTIVE,
    )
    try:
        with session.begin_nested():
            session.add(user)
    except IntegrityError:  # a concurrent runner created it first
        return session.execute(select(User.id).where(User.email == SYSTEM_EMAIL)).scalar_one()
    return user.id


@dataclass(slots=True)
class RunTally:
    processed: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)


class JobBase:
    def __init__(self, session: Session, organization_id: uuid.UUID) -> None:
        self._session = session
        self._org = organization_id
        self._runs = BackgroundJobRunRepository(session, organization_id)
        self._audit = AuditWriter(session)
        self.settings = OrganizationSettingsRepository(session).require(organization_id)
        organization = session.get(Organization, organization_id)
        self._timezone = organization.timezone if organization else None

    def today(self) -> date:
        return today_in(self._timezone)

    def start_run(
        self,
        job_code: JobCode,
        *,
        run_key: str,
        trigger: str,
        requested_by: uuid.UUID,
        parameters: dict[str, Any] | None,
    ) -> BackgroundJobRun:
        """§14.2: check-and-insert under a lock on the organization's settings row."""
        self._session.execute(
            select(OrganizationSettings.organization_id)
            .where(OrganizationSettings.organization_id == self._org)
            .with_for_update()
        )
        running = self._runs.running(job_code.value)
        if running is not None:
            heartbeat = running.heartbeat_at or running.started_at
            if utc_now() - heartbeat <= STALE_HEARTBEAT:
                self._session.rollback()
                raise JobAlreadyRunning()
            running.status = "FAILED"
            running.finished_at = utc_now()
            self._session.flush()
        now = utc_now()
        run = BackgroundJobRun(
            id=uuid7(),
            organization_id=self._org,
            job_code=job_code.value,
            run_key=run_key,
            attempt_no=self._runs.next_attempt(job_code.value, run_key),
            trigger=trigger,
            parameters=parameters,
            status="RUNNING",
            started_at=now,
            heartbeat_at=now,
            requested_by=requested_by,
            created_by=requested_by,
        )
        self._runs.add(run)
        self._session.commit()
        return run

    def _finish(self, run: BackgroundJobRun, tally: RunTally, audit_action: AuditAction) -> None:
        run.processed_count = tally.processed
        run.succeeded_count = tally.succeeded
        run.skipped_count = tally.skipped
        run.failed_count = tally.failed
        run.failure_details = tally.failures[:MAX_FAILURE_DETAILS] or None
        run.finished_at = utc_now()
        run.status = (
            "SUCCEEDED"
            if tally.failed == 0
            else ("FAILED" if tally.succeeded == 0 and tally.skipped == 0 else "PARTIALLY_FAILED")
        )
        self._audit.record(
            AuditEvent(
                action=audit_action,
                entity_type="background_job_run",
                entity_id=run.id,
                actor_user_id=run.requested_by,
                actor_role_codes="SYSTEM",
                organization_id=self._org,
                changes=ChangeSet(
                    changed_fields=["status"],
                    before_values={},
                    after_values={
                        "job_code": run.job_code,
                        "status": run.status,
                        "succeeded": tally.succeeded,
                        "failed": tally.failed,
                    },
                ),
            )
        )
        self._session.commit()

    def _heartbeat(self, run: BackgroundJobRun) -> None:
        run.heartbeat_at = utc_now()
