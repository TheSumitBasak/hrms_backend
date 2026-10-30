"""Employee ID rules, sequences and code registry (§24.4.7)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import CurrentOrg, DbSession, IfMatch, get_employee_code_service
from app.db.session import unit_of_work
from app.models.employee_code import EmployeeIdGenerationRule
from app.schemas.common import etag_for
from app.schemas.id_rules import (
    CodeRegistryResponse,
    IdRuleCreateRequest,
    IdRuleResponse,
    IdRuleStatusRequest,
    IdRuleUpdateRequest,
    IdSequenceResponse,
    PreviewRequest,
    PreviewResponse,
)
from app.services.employee_codes import EmployeeCodeService

router = APIRouter(prefix="/orgs/{org_id}", tags=["Employee ID rules"])


def _etag(response: Response, row_version: int) -> None:
    response.headers["ETag"] = etag_for(row_version)


def _rule_response(rule: EmployeeIdGenerationRule, last_issued: int | None) -> IdRuleResponse:
    return IdRuleResponse(
        id=rule.id,
        organization_id=rule.organization_id,
        rule_code=rule.rule_code,
        rule_name=rule.rule_name,
        department_id=rule.department_id,
        prefix=rule.prefix,
        suffix=rule.suffix,
        format_pattern=rule.format_pattern,
        separator=rule.separator,
        year_format=rule.year_format,
        include_organization_code="{ORG}" in rule.format_pattern,
        include_department_code="{DEPT}" in rule.format_pattern,
        include_year="{YEAR}" in rule.format_pattern,
        sequence_reset_policy=rule.sequence_reset_policy,
        sequence_group_code=rule.sequence_group_code,
        sequence_start=rule.sequence_start,
        sequence_increment=rule.sequence_increment,
        sequence_padding_length=rule.sequence_padding_length,
        last_issued_sequence=last_issued,
        effective_from=rule.effective_from,
        effective_to=rule.effective_to,
        priority=rule.priority,
        status=rule.status,
        row_version=rule.row_version,
    )


@router.get(
    "/employee-id-rules",
    response_model=list[IdRuleResponse],
    summary="List employee ID rules",
)
def list_rules(
    context: CurrentOrg,
    service: Annotated[EmployeeCodeService, Depends(get_employee_code_service)],
    department_id: uuid.UUID | None = None,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
    as_of: date | None = None,
) -> list[IdRuleResponse]:
    rules = service.list_rules(context, department_id=department_id, status=status_filter)
    return [_rule_response(rule, service.last_issued_sequence(rule)) for rule in rules]


@router.post(
    "/employee-id-rules",
    response_model=IdRuleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a draft employee ID rule",
)
def create_rule(
    payload: IdRuleCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[EmployeeCodeService, Depends(get_employee_code_service)],
) -> IdRuleResponse:
    with unit_of_work(db):
        rule = service.create_rule(context, values=payload.model_dump(mode="json"))
        last = service.last_issued_sequence(rule)
    return _rule_response(rule, last)


@router.get(
    "/employee-id-rules/{rule_id}",
    response_model=IdRuleResponse,
    summary="Read an employee ID rule",
)
def read_rule(
    rule_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    service: Annotated[EmployeeCodeService, Depends(get_employee_code_service)],
) -> IdRuleResponse:
    rule = service.get_rule(context, rule_id)
    _etag(response, rule.row_version)
    return _rule_response(rule, service.last_issued_sequence(rule))


@router.patch(
    "/employee-id-rules/{rule_id}",
    response_model=IdRuleResponse,
    summary="Edit an employee ID rule",
)
def update_rule(
    rule_id: uuid.UUID,
    payload: IdRuleUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[EmployeeCodeService, Depends(get_employee_code_service)],
) -> IdRuleResponse:
    with unit_of_work(db):
        rule = service.update_rule(
            context,
            rule_id=rule_id,
            changes=payload.model_dump(mode="json", exclude_unset=True),
            if_match=if_match,
        )
        last = service.last_issued_sequence(rule)
    _etag(response, rule.row_version)
    return _rule_response(rule, last)


@router.post(
    "/employee-id-rules/{rule_id}/publish",
    response_model=IdRuleResponse,
    summary="Publish an employee ID rule",
)
def publish_rule(
    rule_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[EmployeeCodeService, Depends(get_employee_code_service)],
) -> IdRuleResponse:
    with unit_of_work(db):
        rule = service.publish_rule(context, rule_id=rule_id)
        last = service.last_issued_sequence(rule)
    _etag(response, rule.row_version)
    return _rule_response(rule, last)


@router.post(
    "/employee-id-rules/{rule_id}/archive",
    response_model=IdRuleResponse,
    summary="Archive an employee ID rule",
)
def archive_rule(
    rule_id: uuid.UUID,
    payload: IdRuleStatusRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[EmployeeCodeService, Depends(get_employee_code_service)],
) -> IdRuleResponse:
    with unit_of_work(db):
        rule = service.archive_rule(context, rule_id=rule_id, reason=payload.reason)
        last = service.last_issued_sequence(rule)
    _etag(response, rule.row_version)
    return _rule_response(rule, last)


@router.post(
    "/employee-id-rules/preview",
    response_model=PreviewResponse,
    summary="Preview the next employee code",
)
def preview_rule(
    payload: PreviewRequest,
    context: CurrentOrg,
    service: Annotated[EmployeeCodeService, Depends(get_employee_code_service)],
) -> PreviewResponse:
    rule, _, code = service.preview(
        context,
        department_id=payload.department_id,
        basis_date=payload.basis_date,
    )
    return PreviewResponse(
        selected_rule=_rule_response(rule, service.last_issued_sequence(rule)),
        indicative_code=code,
    )


@router.get(
    "/employee-id-sequences",
    response_model=list[IdSequenceResponse],
    summary="Read ID sequence counters",
)
def list_sequences(
    context: CurrentOrg,
    service: Annotated[EmployeeCodeService, Depends(get_employee_code_service)],
) -> list[IdSequenceResponse]:
    return [IdSequenceResponse.model_validate(row) for row in service.list_sequences(context)]


@router.get(
    "/employee-code-registry",
    response_model=list[CodeRegistryResponse],
    summary="Search issued and retired employee codes",
)
def search_registry(
    context: CurrentOrg,
    service: Annotated[EmployeeCodeService, Depends(get_employee_code_service)],
    code: str | None = None,
    employee_id: uuid.UUID | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[CodeRegistryResponse]:
    return [
        CodeRegistryResponse.model_validate(row)
        for row in service.search_registry(
            context, code=code, employee_id=employee_id, status=status_filter
        )
    ]


__all__ = ["router"]
