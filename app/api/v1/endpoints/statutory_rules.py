"""Platform statutory rules: browse, draft, publish (payroll addendum §15.2)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import DbSession, get_statutory_rule_service
from app.api.v1.endpoints.organizations import require_platform_permission
from app.core.context import ActorContext
from app.core.permissions import Permission
from app.db.session import unit_of_work
from app.models.statutory_rules import StatutoryRuleVersion
from app.repositories.statutory_rules import VersionContents
from app.schemas.statutory_rules import (
    RuleSetResponse,
    RuleVersionDraftRequest,
    RuleVersionPublishRequest,
    RuleVersionResponse,
)
from app.services.statutory_rules import StatutoryRuleService

router = APIRouter(prefix="/statutory-rules", tags=["Statutory rules"])

Service = Annotated[StatutoryRuleService, Depends(get_statutory_rule_service)]


def _version_response(
    version: StatutoryRuleVersion, contents: VersionContents
) -> RuleVersionResponse:
    own = RuleVersionResponse.model_fields.keys() - {"parameters", "slabs", "list_values"}
    return RuleVersionResponse.model_validate(
        {
            **{name: getattr(version, name) for name in own},
            "parameters": contents.parameters,
            "slabs": contents.slabs,
            "list_values": contents.list_values,
        }
    )


@router.get("", response_model=list[RuleSetResponse], summary="Browse rule sets")
def browse_rules(
    service: Service,
    _: Annotated[ActorContext, Depends(require_platform_permission(Permission.PAYROLL_VIEW))],
    family: str | None = None,
    jurisdiction_code: str | None = None,
    as_of: date | None = None,
) -> list[RuleSetResponse]:
    """Published versions only; with ``as_of``, the single version in force that day."""
    return [
        RuleSetResponse(
            id=rule_set.id,
            rule_family=rule_set.rule_family,
            jurisdiction_code=rule_set.jurisdiction_code,
            rule_set_code=rule_set.rule_set_code,
            description=rule_set.description,
            versions=[_version_response(version, contents) for version, contents in versions],
        )
        for rule_set, versions in service.browse(
            family=family, jurisdiction_code=jurisdiction_code, as_of=as_of
        )
    ]


@router.post(
    "/{set_id}/versions",
    response_model=RuleVersionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Draft a rule version",
)
def draft_version(
    set_id: uuid.UUID,
    payload: RuleVersionDraftRequest,
    db: DbSession,
    service: Service,
    actor: Annotated[
        ActorContext, Depends(require_platform_permission(Permission.STATUTORY_RULE_PREPARE))
    ],
) -> RuleVersionResponse:
    with unit_of_work(db):
        version, contents = service.draft(actor, set_id, payload.model_dump())
    return _version_response(version, contents)


@router.post(
    "/versions/{version_id}/publish",
    response_model=RuleVersionResponse,
    summary="Publish a drafted rule version (maker-checker)",
)
def publish_version(
    version_id: uuid.UUID,
    payload: RuleVersionPublishRequest,
    db: DbSession,
    service: Service,
    actor: Annotated[
        ActorContext, Depends(require_platform_permission(Permission.STATUTORY_RULE_PUBLISH))
    ],
) -> RuleVersionResponse:
    with unit_of_work(db):
        version, contents = service.publish(
            actor,
            version_id,
            verification_status=payload.verification_status,
            legal_review_notes=payload.legal_review_notes,
        )
    return _version_response(version, contents)
